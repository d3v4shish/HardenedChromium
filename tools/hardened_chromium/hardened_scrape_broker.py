#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Local broker for multi-app scraping through one visible Chromium profile.

Start Hardened Chromium first:

  tools/hardened_chromium/run_for_automation.sh

Then start this broker. Local applications submit URLs to the broker; the broker
opens one normal tab per job in that already-running browser, captures normalized
feed records plus rendered snapshots, and exposes job outputs over a loopback
HTTP API.
"""

from __future__ import annotations

import argparse
import base64
from collections import defaultdict, deque
import contextlib
import csv
import dataclasses
import hashlib
import html
import io
import json
import os
from pathlib import Path
import queue
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse
import urllib.error
import urllib.request


DEFAULT_PORT = 8877
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
SCHEMA_VERSION = 1
PRIVACY_SETTINGS_SCHEMA_VERSION = 2
TERMINAL_STATUSES = {"completed", "failed", "stopped", "interrupted"}
MEDIA_MODES = {"loop", "obs", "synthetic", "system", "none"}
AUDIO_CAPTURE_MODES = {"fake", "system"}
PRIVACY_SOURCES = {"fake", "real"}
FAKE_CAMERA_BACKENDS = {"loop", "obs", "synthetic"}
DEFAULT_FAKE_LOCATION = {
    "enabled": True,
    "latitude": 28.6139,
    "longitude": 77.2090,
    "accuracy": 100.0,
}
DEFAULT_LOOP_VIDEO_CANDIDATES = (
    Path.home() / "Workspace/Temp/Y4MConverter/asd.y4m",
)
MAX_LOOP_VIDEO_UPLOAD_BYTES = 512 * 1024 * 1024
Y4M_MAGIC = b"YUV4MPEG2"
DERIVED_CHECKPOINT_MIN_SECONDS = 10.0
EXPORT_PROCESS_LOCK = threading.Lock()
STREAM_TICKET_TTL_SECONDS = 30.0
STREAM_MAX_OUTSTANDING_TICKETS = 4096
STREAM_HEARTBEAT_SECONDS = 15.0
STREAM_REPLAY_EVENTS_PER_JOB = 4096
STREAM_QUEUE_MAX_MESSAGES = STREAM_REPLAY_EVENTS_PER_JOB + 16
STREAM_QUEUE_MAX_BYTES = 4 * 1024 * 1024
EVENT_PERSIST_BATCH_SIZE = 64
EVENT_PERSIST_BATCH_SECONDS = 0.010
BLOCKED_TEXT_MARKERS = (
    "captcha",
    "verify you are human",
    "are you human",
    "human verification",
    "unusual traffic",
    "sign in to continue",
    "log in to continue",
    "login to continue",
    "enable cookies",
    "accept cookies",
    "cookie consent",
    "permission required",
)


def default_loop_video_file() -> str:
  env_path = os.environ.get("HARDENED_DEFAULT_LOOP_VIDEO_FILE", "").strip()
  candidates: list[Path] = []
  if env_path:
    candidates.append(Path(env_path).expanduser())
  candidates.extend(DEFAULT_LOOP_VIDEO_CANDIDATES)
  for candidate in candidates:
    with contextlib.suppress(OSError):
      resolved = candidate.resolve()
      if resolved.is_file():
        return str(resolved)
  return ""


SCHEMA_FIELD_MODES = {
    "text",
    "inner_text",
    "text_content",
    "html",
    "inner_html",
    "outer_html",
    "href",
    "src",
    "datetime",
}


def loopback_host_header_allowed(value: str, port: int) -> bool:
  """Return whether an HTTP Host header addresses this loopback broker."""
  if not value:
    return False
  try:
    parsed = urlparse(f"http://{value}")
    parsed_port = parsed.port if parsed.port is not None else 80
  except ValueError:
    return False
  return parsed.hostname in LOOPBACK_HOSTS and parsed_port == port


def loopback_origin_allowed(value: str, port: int) -> bool:
  """Allow only the broker's own loopback web origin in no-auth mode."""
  if not value:
    return True
  try:
    parsed = urlparse(value)
    parsed_port = parsed.port if parsed.port is not None else 80
  except ValueError:
    return False
  return (
      parsed.scheme == "http"
      and parsed.hostname in LOOPBACK_HOSTS
      and parsed_port == port
      and not parsed.username
      and not parsed.password
      and parsed.path in ("", "/")
      and not parsed.query
      and not parsed.fragment
  )


COLLECT_ITEMS_JS = r"""
(() => {
  const absoluteUrl = value => {
    try {
      return value ? new URL(value, location.href).href : '';
    } catch {
      return '';
    }
  };
  const cleanText = value => String(value || '').replace(/\s+/g, ' ').trim();
  const textOf = node => cleanText(node ? node.innerText || node.textContent : '');
  const firstText = (root, selector) => {
    try {
      return textOf(root.querySelector(selector));
    } catch {
      return '';
    }
  };
  const allUrls = (root, selector, attr, limit = 80) => {
    const urls = [];
    const seen = new Set();
    try {
      for (const node of root.querySelectorAll(selector)) {
        const url = absoluteUrl(node.getAttribute(attr) || node[attr] || '');
        if (url && !seen.has(url)) {
          seen.add(url);
          urls.push(url);
        }
        if (urls.length >= limit) break;
      }
    } catch {
    }
    return urls;
  };
  const itemKey = item => [
    item.adapter,
    item.permalink,
    item.author,
    item.timeText,
    item.text.slice(0, 600)
  ].filter(Boolean).join('|');
  const visibleEnough = element => {
    const rect = element.getBoundingClientRect();
    return rect.width >= 120 && rect.height >= 40 &&
        rect.bottom >= 0 && rect.top <= innerHeight * 2.5;
  };
  const baseItem = (element, adapter) => {
    const links = allUrls(element, 'a[href]', 'href');
    const media = [
      ...allUrls(element, 'img[src]', 'src'),
      ...allUrls(element, 'video[src]', 'src'),
      ...allUrls(element, 'video source[src]', 'src'),
    ];
    return {
      adapter,
      author: '',
      timeText: '',
      text: textOf(element),
      permalink: links[0] || '',
      links,
      media,
      capturedAt: Date.now(),
    };
  };
  const collectTweet = article => {
    const item = baseItem(article, 'x-twitter');
    item.text = firstText(article, '[data-testid="tweetText"]') || item.text;
    item.author = firstText(article, '[data-testid="User-Name"]') ||
        firstText(article, 'div[dir="ltr"] span');
    item.timeText = firstText(article, 'time') || '';
    const statusLink = [...article.querySelectorAll('a[href*="/status/"]')]
        .map(a => absoluteUrl(a.getAttribute('href'))).find(Boolean);
    if (statusLink) item.permalink = statusLink;
    item.key = item.permalink || itemKey(item);
    return item;
  };
  const collectLinkedIn = element => {
    const item = baseItem(element, 'linkedin');
    item.author = firstText(element,
        '.update-components-actor__title, .feed-shared-actor__title, [class*="actor__title"]');
    item.text = firstText(element,
        '.feed-shared-update-v2__description, .update-components-text, [class*="update-components-text"]') ||
        item.text;
    item.timeText = firstText(element, 'time, [class*="actor__sub-description"]');
    const activityLink = [...element.querySelectorAll('a[href*="activity"], a[href*="urn"]')]
        .map(a => absoluteUrl(a.getAttribute('href'))).find(Boolean);
    if (activityLink) item.permalink = activityLink;
    item.key = item.permalink || element.getAttribute('data-urn') || itemKey(item);
    return item;
  };
  const collectGeneric = element => {
    const item = baseItem(element, 'generic');
    item.author = firstText(element,
        '[rel="author"], [itemprop="author"], .author, [class*="author"], [class*="user"], h2, h3');
    item.timeText = firstText(element, 'time, [datetime], .time, [class*="time"], [class*="date"]');
    item.key = item.permalink || itemKey(item);
    return item;
  };

  const tweetSelector = 'article[data-testid="tweet"]';
  const linkedInSelector = [
    '.feed-shared-update-v2',
    '[data-urn*="activity"]',
    '[class*="feed-shared-update"]'
  ].join(',');
  const genericSelector = [
    'main article',
    '[role="feed"] > *',
    '[role="list"] > [role="listitem"]',
    '[role="article"]',
    '.post',
    '.feed-item',
    '.timeline-item',
    '.search-result'
  ].join(',');
  const rootSelector = [tweetSelector, linkedInSelector, genericSelector].join(',');
  const stateKey = '__hardenedIncrementalCollectorV2';
  let state = window[stateKey];

  const enqueue = (collectorState, element) => {
    if (!element || element.nodeType !== Node.ELEMENT_NODE) return;
    if (collectorState.pending.size >= 5000) {
      collectorState.overflow = true;
      return;
    }
    collectorState.pending.add(element);
  };
  const register = (collectorState, element) => {
    if (!element || element.nodeType !== Node.ELEMENT_NODE ||
        collectorState.observed.has(element)) return;
    collectorState.observed.add(element);
    collectorState.intersection.observe(element);
    enqueue(collectorState, element);
  };
  const scanTree = (collectorState, node) => {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
    try {
      if (node.matches(rootSelector)) register(collectorState, node);
      for (const element of node.querySelectorAll(rootSelector)) {
        register(collectorState, element);
      }
    } catch {
      collectorState.overflow = true;
    }
  };
  const unregisterTree = (collectorState, node) => {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
    const unregister = element => {
      if (!collectorState.observed.delete(element)) return;
      collectorState.pending.delete(element);
      collectorState.intersection.unobserve(element);
    };
    try {
      if (node.matches(rootSelector)) unregister(node);
      for (const element of node.querySelectorAll(rootSelector)) unregister(element);
    } catch {
    }
  };
  const fullScan = collectorState => {
    collectorState.overflow = false;
    scanTree(collectorState, document.documentElement);
  };

  if (!state || state.selector !== rootSelector) {
    if (state) {
      state.mutation.disconnect();
      state.intersection.disconnect();
    }
    state = {
      selector: rootSelector,
      cycles: 0,
      overflow: false,
      pending: new Set(),
      observed: new Set(),
    };
    state.intersection = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (entry.isIntersecting) enqueue(state, entry.target);
      }
    }, {rootMargin: '250% 0px'});
    state.mutation = new MutationObserver(records => {
      for (const record of records) {
        for (const node of record.removedNodes || []) unregisterTree(state, node);
        for (const node of record.addedNodes || []) scanTree(state, node);
        const target = record.target.nodeType === Node.ELEMENT_NODE ?
            record.target : record.target.parentElement;
        if (target) {
          try {
            const root = target.closest(rootSelector);
            if (root) enqueue(state, root);
          } catch {
          }
        }
      }
    });
    state.mutation.observe(document.documentElement, {
      attributes: true,
      characterData: true,
      childList: true,
      subtree: true,
    });
    window[stateKey] = state;
    fullScan(state);
  }
  state.cycles++;
  if (state.overflow || state.cycles % 10 === 0) fullScan(state);

  const roots = [];
  for (const element of state.pending) {
    state.pending.delete(element);
    if (!element.isConnected) continue;
    roots.push(element);
    if (roots.length >= 240) break;
  }

  const seenElements = new Set();
  const items = [];
  const add = item => {
    if (!item || !item.text || item.text.length < 8) return;
    item.text = item.text.slice(0, 20000);
    item.author = String(item.author || '').slice(0, 300);
    item.timeText = String(item.timeText || '').slice(0, 300);
    item.key = String(item.key || itemKey(item)).slice(0, 1200);
    items.push(item);
  };

  for (const element of roots) {
    if (items.length >= 160) break;
    if (!visibleEnough(element)) continue;
    if (element.matches(tweetSelector)) {
      add(collectTweet(element));
    } else if (element.matches(linkedInSelector)) {
      add(collectLinkedIn(element));
    } else {
      add(collectGeneric(element));
    }
    seenElements.add(element);
  }

  return {
    href: location.href,
    title: document.title || '',
    readyState: document.readyState,
    scrollY,
    scrollHeight: document.scrollingElement ? document.scrollingElement.scrollHeight : document.body.scrollHeight,
    viewportHeight: innerHeight,
    bodyText: items.length ? '' :
        cleanText(document.body ? document.body.innerText : '').slice(0, 5000),
    items,
  };
})()
"""


CUSTOM_COLLECT_ITEMS_JS_TEMPLATE = r"""
(() => {
  const schema = __SCHEMA_JSON__;
  const absoluteUrl = value => {
    try {
      return value ? new URL(String(value), location.href).href : '';
    } catch {
      return '';
    }
  };
  const cleanText = value => String(value || '').replace(/\s+/g, ' ').trim();
  const textOf = node => cleanText(node ? node.innerText || node.textContent : '');
  const nodeValue = (node, field) => {
    if (!node) return '';
    const mode = String(field.mode || 'text').toLowerCase();
    if (mode.startsWith('attr:')) {
      return cleanText(node.getAttribute(mode.slice(5)) || '');
    }
    if (mode === 'href') {
      const link = node.matches && node.matches('a[href]') ? node :
          (node.closest ? node.closest('a[href]') : null);
      return absoluteUrl((link && (link.getAttribute('href') || link.href)) ||
          node.getAttribute('href') || node.href || '');
    }
    if (mode === 'src') {
      const media = node.matches && node.matches('[src]') ? node :
          (node.querySelector ? node.querySelector('[src]') : null);
      return absoluteUrl((media && (media.getAttribute('src') || media.src)) ||
          node.getAttribute('src') || node.src || '');
    }
    if (mode === 'datetime') return cleanText(node.getAttribute('datetime') || node.dateTime || textOf(node));
    if (mode === 'html' || mode === 'inner_html') return String(node.innerHTML || '').slice(0, 100000);
    if (mode === 'outer_html') return String(node.outerHTML || '').slice(0, 100000);
    if (mode === 'text_content') return cleanText(node.textContent || '');
    return textOf(node);
  };
  const nodesFor = (root, selector, multiple) => {
    try {
      if (!selector) return [root];
      return multiple ? [...root.querySelectorAll(selector)] : [root.querySelector(selector)];
    } catch {
      return [];
    }
  };
  const allUrls = (root, selector, attr, limit = 80) => {
    const urls = [];
    const seen = new Set();
    try {
      for (const node of root.querySelectorAll(selector)) {
        const url = absoluteUrl(node.getAttribute(attr) || node[attr] || '');
        if (url && !seen.has(url)) {
          seen.add(url);
          urls.push(url);
        }
        if (urls.length >= limit) break;
      }
    } catch {
    }
    return urls;
  };
  const visibleEnough = element => {
    if (schema.includeHidden) return true;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width >= 1 && rect.height >= 1 &&
        style.visibility !== 'hidden' && style.display !== 'none' &&
        rect.bottom >= -innerHeight && rect.top <= innerHeight * 3;
  };
  const firstField = (fields, ...names) => {
    const lower = new Map(Object.entries(fields).map(([key, value]) => [key.toLowerCase(), value]));
    for (const name of names) {
      if (!lower.has(name)) continue;
      const value = lower.get(name);
      if (Array.isArray(value)) return value.find(Boolean) || '';
      return value || '';
    }
    return '';
  };
  const itemKey = item => [
    item.schemaId,
    item.permalink,
    item.author,
    item.timeText,
    item.text.slice(0, 600)
  ].filter(Boolean).join('|');

  const fields = Array.isArray(schema.fields) ? schema.fields : [];
  const rootSelector = schema.itemRoot || 'body';
  const signature = JSON.stringify({rootSelector, fields, includeHidden: schema.includeHidden});
  const stateKey = '__hardenedIncrementalSchemaCollectorV2';
  let state = window[stateKey];

  const enqueue = (collectorState, element) => {
    if (!element || element.nodeType !== Node.ELEMENT_NODE) return;
    if (collectorState.pending.size >= 5000) {
      collectorState.overflow = true;
      return;
    }
    collectorState.pending.add(element);
  };
  const register = (collectorState, element) => {
    if (!element || element.nodeType !== Node.ELEMENT_NODE ||
        collectorState.observed.has(element)) return;
    collectorState.observed.add(element);
    collectorState.intersection.observe(element);
    enqueue(collectorState, element);
  };
  const scanTree = (collectorState, node) => {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
    try {
      if (node.matches(rootSelector)) register(collectorState, node);
      for (const element of node.querySelectorAll(rootSelector)) {
        register(collectorState, element);
      }
    } catch {
      collectorState.overflow = true;
    }
  };
  const unregisterTree = (collectorState, node) => {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
    const unregister = element => {
      if (!collectorState.observed.delete(element)) return;
      collectorState.pending.delete(element);
      collectorState.intersection.unobserve(element);
    };
    try {
      if (node.matches(rootSelector)) unregister(node);
      for (const element of node.querySelectorAll(rootSelector)) unregister(element);
    } catch {
    }
  };
  const fullScan = collectorState => {
    collectorState.overflow = false;
    scanTree(collectorState, document.documentElement);
  };

  if (!state || state.signature !== signature) {
    if (state) {
      state.mutation.disconnect();
      state.intersection.disconnect();
    }
    state = {
      signature,
      cycles: 0,
      overflow: false,
      pending: new Set(),
      observed: new Set(),
    };
    state.intersection = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (entry.isIntersecting) enqueue(state, entry.target);
      }
    }, {rootMargin: '300% 0px'});
    state.mutation = new MutationObserver(records => {
      for (const record of records) {
        for (const node of record.removedNodes || []) unregisterTree(state, node);
        for (const node of record.addedNodes || []) scanTree(state, node);
        const target = record.target.nodeType === Node.ELEMENT_NODE ?
            record.target : record.target.parentElement;
        if (target) {
          try {
            const root = target.closest(rootSelector);
            if (root) enqueue(state, root);
          } catch {
          }
        }
      }
    });
    state.mutation.observe(document.documentElement, {
      attributes: true,
      characterData: true,
      childList: true,
      subtree: true,
    });
    window[stateKey] = state;
    fullScan(state);
  }
  state.cycles++;
  if (state.overflow || state.cycles % 10 === 0) fullScan(state);

  const roots = [];
  const rootLimit = schema.maxPreviewItems || 200;
  for (const element of state.pending) {
    state.pending.delete(element);
    if (!element.isConnected) continue;
    roots.push(element);
    if (roots.length >= rootLimit) break;
  }

  const items = [];
  for (const element of roots) {
    if (!visibleEnough(element)) continue;
    const values = {};
    const htmlFields = {};
    let missingRequired = false;
    for (const field of fields) {
      const name = String(field.name || '').trim();
      if (!name) continue;
      const multiple = Boolean(field.multiple);
      const rawValues = [];
      for (const node of nodesFor(element, String(field.selector || ''), multiple)) {
        const value = nodeValue(node, field);
        if (value) rawValues.push(value);
        if (!multiple && rawValues.length) break;
      }
      if (field.required && !rawValues.length) {
        missingRequired = true;
        break;
      }
      if (String(field.mode || '').toLowerCase().includes('html')) {
        htmlFields[name] = multiple ? rawValues : (rawValues[0] || '');
      } else {
        values[name] = multiple ? rawValues : (rawValues[0] || '');
      }
    }
    if (missingRequired) continue;
    const links = allUrls(element, 'a[href]', 'href');
    const media = [
      ...allUrls(element, 'img[src]', 'src'),
      ...allUrls(element, 'video[src]', 'src'),
      ...allUrls(element, 'video source[src]', 'src'),
      ...allUrls(element, 'audio[src]', 'src'),
      ...allUrls(element, 'audio source[src]', 'src'),
    ];
    const item = {
      adapter: 'custom-schema',
      schemaId: schema.id || '',
      schemaName: schema.name || '',
      fields: values,
      htmlFields,
      author: firstField(values, 'author', 'user', 'username', 'name'),
      timeText: firstField(values, 'time', 'time_text', 'date', 'datetime', 'published'),
      text: firstField(values, 'text', 'content', 'body', 'description', 'title') || textOf(element),
      permalink: absoluteUrl(firstField(values, 'permalink', 'url', 'href', 'link') || links[0] || ''),
      links,
      media,
      capturedAt: Date.now(),
    };
    item.key = firstField(values, 'key', 'id') || item.permalink || itemKey(item);
    items.push(item);
  }

  return {
    href: location.href,
    title: document.title || '',
    readyState: document.readyState,
    scrollY,
    scrollHeight: document.scrollingElement ? document.scrollingElement.scrollHeight : document.body.scrollHeight,
    viewportHeight: innerHeight,
    bodyText: items.length ? '' :
        cleanText(document.body ? document.body.innerText : '').slice(0, 5000),
    schema: {id: schema.id || '', name: schema.name || ''},
    items,
  };
})()
"""


SELECTOR_PICKER_JS = r"""
(() => new Promise(resolve => {
  const previous = window.__hardenedSelectorPickerCleanup;
  if (typeof previous === 'function') previous();

  const cssEscape = value => {
    if (window.CSS && typeof CSS.escape === 'function') return CSS.escape(value);
    return String(value).replace(/[^a-zA-Z0-9_-]/g, ch =>
      '\\' + ch.charCodeAt(0).toString(16) + ' ');
  };
  const quoteAttr = value => String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const unique = selector => {
    try { return document.querySelectorAll(selector).length === 1; } catch { return false; }
  };
  const nthOfType = element => {
    const tag = element.localName;
    let index = 1;
    for (let sibling = element.previousElementSibling; sibling; sibling = sibling.previousElementSibling) {
      if (sibling.localName === tag) index++;
    }
    return `${tag}:nth-of-type(${index})`;
  };
  const reusableSelectorFor = element => {
    if (!element || element.nodeType !== Node.ELEMENT_NODE) return '';
    if (element.id) return `#${cssEscape(element.id)}`;
    for (const attr of ['data-testid', 'data-test', 'data-cy', 'data-qa', 'aria-label', 'name']) {
      const value = element.getAttribute(attr);
      if (value) return `${element.localName}[${attr}="${quoteAttr(value)}"]`;
    }
    const classes = [...element.classList]
        .filter(value => value && !/[0-9]{4,}/.test(value))
        .slice(0, 3)
        .map(cssEscape);
    if (classes.length) return `${element.localName}.${classes.join('.')}`;
    return element.localName || '';
  };
  const selectorFor = element => {
    if (!element || element.nodeType !== Node.ELEMENT_NODE) return '';
    if (element.id) {
      const selector = `#${cssEscape(element.id)}`;
      if (unique(selector)) return selector;
    }
    for (const attr of ['data-testid', 'data-test', 'data-cy', 'data-qa', 'aria-label', 'name']) {
      const value = element.getAttribute(attr);
      if (!value) continue;
      const selector = `${element.localName}[${attr}="${quoteAttr(value)}"]`;
      if (unique(selector)) return selector;
    }
    const classes = [...element.classList]
        .filter(value => value && !/[0-9]{4,}/.test(value))
        .slice(0, 3)
        .map(cssEscape);
    if (classes.length) {
      const selector = `${element.localName}.${classes.join('.')}`;
      if (unique(selector)) return selector;
    }
    const parts = [];
    for (let node = element; node && node.nodeType === Node.ELEMENT_NODE; node = node.parentElement) {
      if (node.id) {
        parts.unshift(`#${cssEscape(node.id)}`);
        break;
      }
      parts.unshift(nthOfType(node));
      const selector = parts.join(' > ');
      if (unique(selector)) break;
      if (parts.length >= 7) break;
    }
    return parts.join(' > ');
  };

  const box = document.createElement('div');
  box.style.cssText = [
    'position:fixed',
    'z-index:2147483647',
    'pointer-events:none',
    'border:2px solid #ff2a00',
    'background:rgba(255,42,0,0.08)',
    'box-sizing:border-box',
    'display:none',
  ].join(';');
  const label = document.createElement('div');
  label.textContent = 'Hardened selector picker: click an element, Esc cancels';
  label.style.cssText = [
    'position:fixed',
    'z-index:2147483647',
    'left:12px',
    'top:12px',
    'padding:8px 10px',
    'border-radius:8px',
    'background:#b71c1c',
    'color:white',
    'font:13px system-ui,sans-serif',
    'pointer-events:none',
  ].join(';');
  document.documentElement.append(box, label);

  let target = null;
  const cleanup = result => {
    document.removeEventListener('mousemove', onMove, true);
    document.removeEventListener('click', onClick, true);
    document.removeEventListener('keydown', onKeyDown, true);
    clearTimeout(timer);
    box.remove();
    label.remove();
    delete window.__hardenedSelectorPickerCleanup;
    resolve(result);
  };
  window.__hardenedSelectorPickerCleanup = () => cleanup({ok: false, cancelled: true});
  const onMove = event => {
    target = event.target;
    if (!target || target === box || target === label) return;
    const rect = target.getBoundingClientRect();
    box.style.display = 'block';
    box.style.left = `${Math.max(0, rect.left)}px`;
    box.style.top = `${Math.max(0, rect.top)}px`;
    box.style.width = `${Math.max(1, rect.width)}px`;
    box.style.height = `${Math.max(1, rect.height)}px`;
  };
  const onClick = event => {
    event.preventDefault();
    event.stopPropagation();
    const element = target || event.target;
    const selector = reusableSelectorFor(element);
    const uniqueSelector = selectorFor(element);
    let matchingCount = 0;
    let uniqueMatchingCount = 0;
    try { matchingCount = selector ? document.querySelectorAll(selector).length : 0; } catch {}
    try { uniqueMatchingCount = uniqueSelector ? document.querySelectorAll(uniqueSelector).length : 0; } catch {}
    cleanup({
      ok: Boolean(selector),
      selector,
      uniqueSelector,
      matchingCount,
      uniqueMatchingCount,
      tagName: element && element.tagName ? element.tagName.toLowerCase() : '',
      text: element ? String(element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 500) : '',
      href: element && element.closest ? (element.closest('a[href]') || {}).href || '' : '',
    });
  };
  const onKeyDown = event => {
    if (event.key === 'Escape') cleanup({ok: false, cancelled: true});
  };
  const timer = setTimeout(() => cleanup({ok: false, error: 'selector picker timed out'}), 120000);
  document.addEventListener('mousemove', onMove, true);
  document.addEventListener('click', onClick, true);
  document.addEventListener('keydown', onKeyDown, true);
}))()
"""


SCROLL_JS = r"""
(() => {
  const scroller = document.scrollingElement || document.documentElement || document.body;
  const before = scroller.scrollTop;
  scroller.scrollBy({top: Math.max(600, innerHeight * 0.85), behavior: 'instant'});
  return {
    before,
    after: scroller.scrollTop,
    scrollHeight: scroller.scrollHeight,
    viewportHeight: innerHeight,
  };
})()
"""


def raw_html_js(max_chars: int) -> str:
  return rf"""
(() => {{
  let html = '<!doctype html>\n' + document.documentElement.outerHTML;
  if (!document.querySelector('base[href]')) {{
    const escapedBase = location.href
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;');
    html = html.replace(/<head([^>]*)>/i, '<head$1><base href="' + escapedBase + '">');
  }}
  return {{
    href: location.href,
    title: document.title || '',
    html: html.length > {max_chars} ? html.slice(0, {max_chars}) +
        '\n<!-- Hardened scrape broker: raw HTML truncated -->' : html,
    truncated: html.length > {max_chars},
    length: html.length,
  }};
}})()
"""


def visible_text_js(max_chars: int) -> str:
  return rf"""
(() => {{
  const text = String(document.body ? document.body.innerText || document.body.textContent || '' : '')
      .replace(/\n{{3,}}/g, '\n\n')
      .trim();
  return {{
    href: location.href,
    title: document.title || '',
    text: text.length > {max_chars} ? text.slice(0, {max_chars}) +
        '\n\n[Hardened scrape broker: visible text truncated]' : text,
    truncated: text.length > {max_chars},
    length: text.length,
  }};
}})()
"""


def schema_collect_items_js(schema: dict[str, Any]) -> str:
  return CUSTOM_COLLECT_ITEMS_JS_TEMPLATE.replace(
      "__SCHEMA_JSON__",
      json.dumps(schema, ensure_ascii=False, separators=(",", ":")))


def wait_for_selector_js(selector: str) -> str:
  return r"""
(() => new Promise(resolve => {
  const selector = __SELECTOR__;
  const done = () => {
    try {
      const node = document.querySelector(selector);
      if (node) {
        resolve({ok: true, selector, text: String(node.innerText || node.textContent || '').slice(0, 1000)});
        return true;
      }
    } catch (error) {
      resolve({ok: false, error: String(error)});
      return true;
    }
    return false;
  };
  if (done()) return;
  const observer = new MutationObserver(() => {
    if (done()) observer.disconnect();
  });
  observer.observe(document.documentElement, {childList: true, subtree: true});
  setTimeout(() => {
    observer.disconnect();
    resolve({ok: false, error: 'selector wait timed out', selector});
  }, 30000);
}))()
""".replace("__SELECTOR__", json.dumps(selector))


def page_summary_js(max_chars: int = 200000) -> str:
  return rf"""
(() => {{
  const absoluteUrl = value => {{
    try {{ return value ? new URL(String(value), location.href).href : ''; }} catch {{ return ''; }}
  }};
  const cleanText = value => String(value || '').replace(/\s+/g, ' ').trim();
  const links = [...document.querySelectorAll('a[href]')].slice(0, 2000).map(node => ({{
    text: cleanText(node.innerText || node.textContent).slice(0, 500),
    href: absoluteUrl(node.getAttribute('href') || node.href || ''),
  }})).filter(item => item.href);
  const media = [...document.querySelectorAll('img[src], video[src], video source[src], audio[src], audio source[src]')]
      .slice(0, 2000).map(node => absoluteUrl(node.getAttribute('src') || node.src || '')).filter(Boolean);
  const forms = [...document.querySelectorAll('form')].slice(0, 200).map(form => ({{
    action: absoluteUrl(form.getAttribute('action') || ''),
    method: String(form.getAttribute('method') || 'get').toLowerCase(),
    inputs: [...form.querySelectorAll('input, textarea, select')].slice(0, 200).map(input => ({{
      tag: input.tagName.toLowerCase(),
      name: input.getAttribute('name') || '',
      type: input.getAttribute('type') || '',
      placeholder: input.getAttribute('placeholder') || '',
    }})),
  }}));
  const text = String(document.body ? document.body.innerText || document.body.textContent || '' : '').slice(0, {max_chars});
  return {{
    href: location.href,
    title: document.title || '',
    readyState: document.readyState,
    scrollY,
    scrollHeight: document.scrollingElement ? document.scrollingElement.scrollHeight : document.body.scrollHeight,
    viewportHeight: innerHeight,
    links,
    media,
    forms,
    visibleText: text,
  }};
}})()
"""


@dataclasses.dataclass
class BrokerConfig:
  cdp_endpoint: str
  output_root: Path
  token: str
  no_auth: bool = False
  max_active_jobs: int = 8
  max_active_jobs_per_app: int = 2
  start_scheduler: bool = True


@dataclasses.dataclass
class AppRegistration:
  id: str
  name: str
  secret: str
  description: str = ""
  created_at: float = dataclasses.field(default_factory=time.time)
  updated_at: float = dataclasses.field(default_factory=time.time)

  def summary(self, include_secret: bool = False) -> dict[str, Any]:
    value = {
        "id": self.id,
        "name": self.name,
        "description": self.description,
        "createdAt": iso_time(self.created_at),
        "updatedAt": iso_time(self.updated_at),
    }
    if include_secret:
      value["secret"] = self.secret
    return value


@dataclasses.dataclass
class ExtractorSchema:
  id: str
  name: str
  item_root: str
  fields: list[dict[str, Any]]
  app_id: str = "admin"
  description: str = ""
  source_url: str = ""
  host: str = ""
  include_hidden: bool = False
  created_at: float = dataclasses.field(default_factory=time.time)
  updated_at: float = dataclasses.field(default_factory=time.time)

  def to_dict(self) -> dict[str, Any]:
    return {
        "id": self.id,
        "appId": self.app_id,
        "name": self.name,
        "description": self.description,
        "sourceUrl": self.source_url,
        "host": self.host,
        "itemRoot": self.item_root,
        "includeHidden": self.include_hidden,
        "fields": list(self.fields),
        "createdAt": iso_time(self.created_at),
        "updatedAt": iso_time(self.updated_at),
        "createdAtEpoch": self.created_at,
        "updatedAtEpoch": self.updated_at,
    }

  def summary(self) -> dict[str, Any]:
    return {
        "id": self.id,
        "appId": self.app_id,
        "name": self.name,
        "description": self.description,
        "sourceUrl": self.source_url,
        "host": self.host,
        "itemRoot": self.item_root,
        "includeHidden": self.include_hidden,
        "fieldCount": len(self.fields),
        "fields": list(self.fields),
        "createdAt": iso_time(self.created_at),
        "updatedAt": iso_time(self.updated_at),
    }


@dataclasses.dataclass(frozen=True)
class BrokerAuth:
  kind: str
  app_id: str = ""

  @property
  def is_admin(self) -> bool:
    return self.kind in ("admin", "no-auth")


@dataclasses.dataclass(frozen=True)
class StreamTicket:
  token: str
  auth: BrokerAuth
  expires_at: float


class StreamSubscription:
  """Bounded event queue for one app-wide WebSocket connection."""

  def __init__(self, auth: BrokerAuth):
    self.auth = auth
    self.job_ids: set[str] = set()
    self.messages: deque[dict[str, Any]] = deque()
    self.message_bytes = 0
    self.condition = threading.Condition(threading.Lock())
    self.closed = False
    self.close_reason = ""

  def enqueue(self, event: dict[str, Any]) -> bool:
    encoded_size = len(json.dumps(event, separators=(",", ":")))
    with self.condition:
      if self.closed or event.get("jobId") not in self.job_ids:
        return False
      if (len(self.messages) >= STREAM_QUEUE_MAX_MESSAGES or
          self.message_bytes + encoded_size > STREAM_QUEUE_MAX_BYTES):
        self.closed = True
        self.close_reason = "slow_consumer"
        self.condition.notify_all()
        return False
      self.messages.append(event)
      self.message_bytes += encoded_size
      self.condition.notify()
      return True

  def enqueue_control(self, message: dict[str, Any]) -> bool:
    encoded_size = len(json.dumps(message, separators=(",", ":")))
    with self.condition:
      if self.closed:
        return False
      if (len(self.messages) >= STREAM_QUEUE_MAX_MESSAGES or
          self.message_bytes + encoded_size > STREAM_QUEUE_MAX_BYTES):
        self.closed = True
        self.close_reason = "slow_consumer"
        self.condition.notify_all()
        return False
      self.messages.append(message)
      self.message_bytes += encoded_size
      self.condition.notify()
      return True

  def pop(self, timeout: float) -> dict[str, Any] | None:
    with self.condition:
      if not self.messages and not self.closed:
        self.condition.wait(timeout=timeout)
      if not self.messages:
        return None
      message = self.messages.popleft()
      self.message_bytes = max(
          0, self.message_bytes -
          len(json.dumps(message, separators=(",", ":"))))
      return message

  def close(self, reason: str = "closed") -> None:
    with self.condition:
      self.closed = True
      self.close_reason = reason
      self.condition.notify_all()


class BrokerEventHub:
  """Low-latency in-memory fanout with bounded per-job replay."""

  def __init__(self):
    self.lock = threading.RLock()
    self.condition = threading.Condition(self.lock)
    self.next_sequences: dict[str, int] = defaultdict(int)
    self.rings: dict[str, deque[dict[str, Any]]] = defaultdict(
        lambda: deque(maxlen=STREAM_REPLAY_EVENTS_PER_JOB))
    self.subscriptions: set[StreamSubscription] = set()

  def restore_sequence(self, job_id: str, sequence: int) -> None:
    with self.lock:
      self.next_sequences[job_id] = max(
          self.next_sequences[job_id], sequence)

  def publish(
      self,
      job: "Job",
      event_type: str,
      data: dict[str, Any] | None = None,
  ) -> dict[str, Any]:
    now = time.time()
    with self.condition:
      sequence = self.next_sequences[job.id] + 1
      self.next_sequences[job.id] = sequence
      event = {
          "schemaVersion": SCHEMA_VERSION,
          "type": event_type,
          "time": iso_time(now),
          "timeEpoch": now,
          "jobId": job.id,
          "appId": job.app_id,
          "sequence": sequence,
          "data": data or {},
      }
      self.rings[job.id].append(event)
      subscriptions = list(self.subscriptions)
      for subscription in subscriptions:
        if (subscription.auth.is_admin or
            subscription.auth.app_id == job.app_id):
          subscription.enqueue(event)
      self.condition.notify_all()
    return event

  def latest_sequence(self, job_id: str) -> int:
    with self.lock:
      return self.next_sequences.get(job_id, 0)

  def events_after(
      self, job_id: str, sequence: int,
  ) -> tuple[list[dict[str, Any]], bool]:
    with self.lock:
      ring = self.rings.get(job_id)
      if not ring:
        return [], sequence < self.next_sequences.get(job_id, 0)
      oldest = int(ring[0]["sequence"])
      expired = sequence < oldest - 1
      latest = int(ring[-1]["sequence"])
      if sequence >= latest:
        return [], expired
      count = latest - max(sequence, oldest - 1)
      if count <= 64:
        # deque supports efficient indexing at either end, so the common
        # one/few-event case does not scan the entire replay ring.
        return [ring[-offset] for offset in range(count, 0, -1)], expired
      return [event for event in ring
              if int(event["sequence"]) > sequence], expired

  def wait_after(
      self, job_id: str, sequence: int, timeout: float,
  ) -> tuple[list[dict[str, Any]], bool]:
    deadline = time.monotonic() + timeout
    with self.condition:
      while self.next_sequences.get(job_id, 0) <= sequence:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
          break
        self.condition.wait(timeout=remaining)
    return self.events_after(job_id, sequence)

  def subscribe(self, auth: BrokerAuth) -> StreamSubscription:
    subscription = StreamSubscription(auth)
    with self.lock:
      self.subscriptions.add(subscription)
    return subscription

  def unsubscribe(self, subscription: StreamSubscription) -> None:
    with self.lock:
      self.subscriptions.discard(subscription)
    subscription.close()

  def add_job(
      self,
      subscription: StreamSubscription,
      job: "Job",
      after_sequence: int,
  ) -> None:
    # Adding the job and replaying its cursor are atomic with publish(). This
    # prevents both a missed event and a duplicate at the live/replay boundary.
    with self.lock:
      with subscription.condition:
        subscription.job_ids.add(job.id)
      ring = list(self.rings.get(job.id, ()))
      latest_sequence = self.next_sequences.get(job.id, 0)
      expired = (
          after_sequence < latest_sequence if not ring else
          after_sequence < int(ring[0]["sequence"]) - 1)
      if expired:
        subscription.enqueue_control({
            "type": "resync_required",
            "jobId": job.id,
            "afterSequence": after_sequence,
            "latestSequence": latest_sequence,
        })
        return
      replay = [event for event in ring
                if int(event["sequence"]) > after_sequence]
      replay_bytes = sum(len(json.dumps(event, separators=(",", ":")))
                         for event in replay)
      with subscription.condition:
        replay_fits = (
            len(subscription.messages) + len(replay) <=
            STREAM_QUEUE_MAX_MESSAGES and
            subscription.message_bytes + replay_bytes <=
            STREAM_QUEUE_MAX_BYTES)
      if not replay_fits:
        subscription.enqueue_control({
            "type": "resync_required",
            "jobId": job.id,
            "afterSequence": after_sequence,
            "latestSequence": latest_sequence,
            "reason": "replay_exceeds_bounded_queue",
        })
        return
      for event in replay:
        subscription.enqueue(event)

  def remove_job(self, subscription: StreamSubscription, job_id: str) -> None:
    with subscription.condition:
      subscription.job_ids.discard(job_id)


class EventPersistenceWriter:
  """Batches JSONL persistence so stream delivery never waits on disk."""

  def __init__(self, broker: "Broker"):
    self.broker = broker
    # Persistence is deliberately outside the latency-critical publish path.
    # The stream queues themselves are bounded; this internal writer queue is
    # lossless so a disk stall cannot make the broker silently drop audit data.
    self.queue: queue.Queue[tuple["Job", dict[str, Any]] | None] = queue.Queue()
    self.close_lock = threading.Lock()
    self.closed = False
    self.thread = threading.Thread(
        target=self._run, name="hardened-event-persistence", daemon=True)
    self.thread.start()

  def enqueue(self, job: "Job", event: dict[str, Any]) -> None:
    with self.close_lock:
      if self.closed:
        raise RuntimeError("event persistence writer is closed")
      self.queue.put_nowait((job, event))

  def flush(self) -> None:
    self.queue.join()

  def close(self) -> None:
    with self.close_lock:
      if self.closed:
        return
      self.closed = True
    self.flush()
    self.queue.put(None)
    self.thread.join(timeout=5)

  def _run(self) -> None:
    while True:
      entry = self.queue.get()
      if entry is None:
        self.queue.task_done()
        return
      batch = [entry]
      stop_after_batch = False
      deadline = time.monotonic() + EVENT_PERSIST_BATCH_SECONDS
      while len(batch) < EVENT_PERSIST_BATCH_SIZE:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
          break
        try:
          next_entry = self.queue.get(timeout=remaining)
        except queue.Empty:
          break
        if next_entry is None:
          self.queue.task_done()
          stop_after_batch = True
          break
        batch.append(next_entry)
      try:
        self._write_batch(batch)
      finally:
        for _ in batch:
          self.queue.task_done()
      if stop_after_batch:
        return

  def _write_batch(self, batch: list[tuple["Job", dict[str, Any]]]) -> None:
    grouped: dict[Path, list[str]] = defaultdict(list)
    for job, event in batch:
      line = json.dumps(event, ensure_ascii=False) + "\n"
      grouped[job.output_dir / "events.jsonl"].append(line)
      grouped[self.broker.events_file].append(line)
      grouped[self.broker.events_dir /
              f"{sanitize_path_part(job.id)}.jsonl"].append(line)
    with self.broker.state_lock:
      for path, lines in grouped.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
          file.writelines(lines)


class CdpError(RuntimeError):
  pass


class CdpWebSocket:
  def __init__(self, websocket_url: str):
    self.websocket_url = websocket_url
    self.next_id = 1
    self.sock: socket.socket | None = None

  def connect(self) -> None:
    parsed = urlparse(self.websocket_url)
    if parsed.scheme != "ws":
      raise CdpError(f"unsupported websocket scheme: {parsed.scheme}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
      path = f"{path}?{parsed.query}"

    sock = socket.create_connection((host, port), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    response = b""
    while b"\r\n\r\n" not in response:
      chunk = sock.recv(4096)
      if not chunk:
        raise CdpError("websocket handshake closed")
      response += chunk
      if len(response) > 65536:
        raise CdpError("websocket handshake too large")
    if b" 101 " not in response.split(b"\r\n", 1)[0]:
      raise CdpError(response.decode("utf-8", "replace").splitlines()[0])
    self.sock = sock

  def close(self) -> None:
    if self.sock:
      try:
        self._send_frame(b"", opcode=0x8)
      except OSError:
        pass
      try:
        self.sock.close()
      except OSError:
        pass
    self.sock = None

  def command(
      self,
      method: str,
      params: dict[str, Any] | None = None,
      timeout: float = 30,
  ) -> dict[str, Any]:
    if not self.sock:
      self.connect()
    command_id = self.next_id
    self.next_id += 1
    self._send_json({"id": command_id, "method": method, "params": params or {}})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
      remaining = max(0.1, deadline - time.monotonic())
      message = self._recv_json(remaining)
      if not message:
        continue
      if message.get("id") != command_id:
        continue
      if "error" in message:
        raise CdpError(f"{method}: {message['error']}")
      return message.get("result", {})
    raise CdpError(f"timeout waiting for {method}")

  def _send_json(self, value: dict[str, Any]) -> None:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    self._send_frame(payload, opcode=0x1)

  def _send_frame(self, payload: bytes, opcode: int) -> None:
    if not self.sock:
      raise CdpError("websocket is not connected")
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
      header.append(0x80 | length)
    elif length <= 0xFFFF:
      header.append(0x80 | 126)
      header.extend(struct.pack("!H", length))
    else:
      header.append(0x80 | 127)
      header.extend(struct.pack("!Q", length))
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    self.sock.sendall(bytes(header) + mask + masked)

  def _recv_json(self, timeout: float) -> dict[str, Any] | None:
    frame = self._recv_frame(timeout)
    if frame is None:
      return None
    fin, opcode, payload = frame
    if opcode == 0x9:
      self._send_frame(payload, opcode=0xA)
      return None
    if opcode == 0x8:
      raise CdpError("websocket closed")
    if opcode != 0x1:
      return None
    payloads = [payload]
    while not fin:
      continuation = self._recv_frame(timeout)
      if continuation is None:
        return None
      fin, opcode, payload = continuation
      if opcode == 0x9:
        self._send_frame(payload, opcode=0xA)
        continue
      if opcode == 0x8:
        raise CdpError("websocket closed")
      if opcode != 0x0:
        raise CdpError("unexpected websocket frame during fragmented message")
      payloads.append(payload)
    payload = b"".join(payloads)
    return json.loads(payload.decode("utf-8"))

  def _recv_frame(self, timeout: float) -> tuple[bool, int, bytes] | None:
    if not self.sock:
      raise CdpError("websocket is not connected")
    self.sock.settimeout(timeout)
    try:
      first = self._recv_exact(2)
    except socket.timeout:
      return None
    fin = bool(first[0] & 0x80)
    opcode = first[0] & 0x0F
    masked = bool(first[1] & 0x80)
    length = first[1] & 0x7F
    if length == 126:
      length = struct.unpack("!H", self._recv_exact(2))[0]
    elif length == 127:
      length = struct.unpack("!Q", self._recv_exact(8))[0]
    mask = self._recv_exact(4) if masked else b""
    payload = self._recv_exact(length) if length else b""
    if masked:
      payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return fin, opcode, payload

  def _recv_exact(self, length: int) -> bytes:
    if not self.sock:
      raise CdpError("websocket is not connected")
    chunks: list[bytes] = []
    remaining = length
    while remaining:
      chunk = self.sock.recv(remaining)
      if not chunk:
        raise CdpError("websocket closed")
      chunks.append(chunk)
      remaining -= len(chunk)
    return b"".join(chunks)


def websocket_accept_value(key: str) -> str:
  digest = hashlib.sha1(  # nosec: WebSocket protocol requires SHA-1.
      (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode(
          "ascii")).digest()
  return base64.b64encode(digest).decode("ascii")


def send_websocket_frame(
    sock: socket.socket,
    payload: bytes,
    opcode: int = 0x1,
) -> None:
  header = bytearray([0x80 | opcode])
  length = len(payload)
  if length < 126:
    header.append(length)
  elif length <= 0xFFFF:
    header.append(126)
    header.extend(struct.pack("!H", length))
  else:
    header.append(127)
    header.extend(struct.pack("!Q", length))
  sock.sendall(bytes(header) + payload)


def receive_websocket_frame(
    sock: socket.socket,
    require_mask: bool = True,
) -> tuple[bool, int, bytes]:
  first = receive_socket_exact(sock, 2)
  fin = bool(first[0] & 0x80)
  opcode = first[0] & 0x0F
  masked = bool(first[1] & 0x80)
  if require_mask and not masked:
    raise ValueError("client WebSocket frames must be masked")
  length = first[1] & 0x7F
  if length == 126:
    length = struct.unpack("!H", receive_socket_exact(sock, 2))[0]
  elif length == 127:
    length = struct.unpack("!Q", receive_socket_exact(sock, 8))[0]
  if length > STREAM_QUEUE_MAX_BYTES:
    raise ValueError("WebSocket frame exceeds the 4MiB limit")
  mask = receive_socket_exact(sock, 4) if masked else b""
  payload = receive_socket_exact(sock, length) if length else b""
  if masked:
    payload = bytes(
        byte ^ mask[index % 4] for index, byte in enumerate(payload))
  return fin, opcode, payload


def receive_socket_exact(sock: socket.socket, length: int) -> bytes:
  chunks: list[bytes] = []
  remaining = length
  while remaining:
    chunk = sock.recv(remaining)
    if not chunk:
      raise ConnectionResetError("WebSocket closed")
    chunks.append(chunk)
    remaining -= len(chunk)
  return b"".join(chunks)


def websocket_close_payload(code: int, reason: str) -> bytes:
  return struct.pack("!H", code) + reason.encode("utf-8")[:123]


@dataclasses.dataclass
class Job:
  id: str
  app_id: str
  url: str
  output_dir: Path
  config: dict[str, Any]
  status: str = "created"
  reason: str = ""
  created_at: float = dataclasses.field(default_factory=time.time)
  started_at: float | None = None
  updated_at: float = dataclasses.field(default_factory=time.time)
  finished_at: float | None = None
  target_id: str = ""
  websocket_url: str = ""
  current_url: str = ""
  title: str = ""
  items: list[dict[str, Any]] = dataclasses.field(default_factory=list)
  exports: dict[str, str] = dataclasses.field(default_factory=dict)
  raw_html_truncated: bool = False
  raw_html_length: int = 0
  error: str = ""
  event_sequence: int = 0
  last_output_item_count: int = 0
  last_output_flush_monotonic: float = dataclasses.field(
      default_factory=time.monotonic)
  stop_event: threading.Event = dataclasses.field(default_factory=threading.Event)
  resume_event: threading.Event = dataclasses.field(default_factory=threading.Event)
  event_lock: threading.RLock = dataclasses.field(default_factory=threading.RLock)
  lock: threading.RLock = dataclasses.field(default_factory=threading.RLock)
  output_lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)

  def summary(self, include_items: bool = False) -> dict[str, Any]:
    with self.lock:
      value = {
          "id": self.id,
          "appId": self.app_id,
          "url": self.url,
          "currentUrl": self.current_url,
          "title": self.title,
          "status": self.status,
          "reason": self.reason,
          "error": self.error,
          "createdAt": iso_time(self.created_at),
          "startedAt": iso_time(self.started_at),
          "updatedAt": iso_time(self.updated_at),
          "finishedAt": iso_time(self.finished_at),
          "targetId": self.target_id,
          "outputDir": str(self.output_dir),
          "itemCount": len(self.items),
          "eventSequence": self.event_sequence,
          "exports": dict(self.exports),
          "rawHtmlTruncated": self.raw_html_truncated,
          "rawHtmlLength": self.raw_html_length,
          "config": dict(self.config),
      }
      if include_items:
        value["items"] = list(self.items)
      return value

  def set_status(self, status: str, reason: str = "") -> None:
    with self.lock:
      self.status = status
      self.reason = reason
      self.updated_at = time.time()


class Broker:
  def __init__(self, config: BrokerConfig):
    self.config = config
    self.jobs: dict[str, Job] = {}
    self.jobs_lock = threading.RLock()
    self.apps: dict[str, AppRegistration] = {}
    self.apps_lock = threading.RLock()
    self.schemas: dict[str, ExtractorSchema] = {}
    self.schemas_lock = threading.RLock()
    self.state_lock = threading.RLock()
    self.state_dir = self.config.output_root / "_broker_state"
    self.apps_file = self.state_dir / "apps.json"
    self.jobs_file = self.state_dir / "jobs.json"
    self.schemas_file = self.state_dir / "schemas.json"
    self.media_settings_file = self.state_dir / "browser_media_settings.json"
    self.location_settings_file = (
        self.state_dir / "browser_location_settings.json")
    self.privacy_rules_file = self.state_dir / "browser_privacy_rules.json"
    self.loop_video_dir = self.state_dir / "loop_videos"
    self.events_file = self.state_dir / "events.jsonl"
    self.events_dir = self.state_dir / "events"
    self.event_hub = BrokerEventHub()
    self.stream_tickets: dict[str, StreamTicket] = {}
    self.stream_tickets_lock = threading.Lock()
    self.scheduler_condition = threading.Condition(threading.RLock())
    self.queued_by_app: dict[str, deque[str]] = defaultdict(deque)
    self.scheduler_apps: deque[str] = deque()
    self.active_by_app: dict[str, int] = defaultdict(int)
    self.active_jobs = 0
    self.load_state()
    self.event_writer = EventPersistenceWriter(self)
    self.scheduler_thread: threading.Thread | None = None
    if self.config.start_scheduler:
      self.scheduler_thread = threading.Thread(
          target=self.scheduler_loop,
          name="hardened-job-scheduler",
          daemon=True)
      self.scheduler_thread.start()

  def load_state(self) -> None:
    self.state_dir.mkdir(parents=True, exist_ok=True)
    self.events_dir.mkdir(parents=True, exist_ok=True)
    self.loop_video_dir.mkdir(parents=True, exist_ok=True)
    self.load_apps()
    self.load_schemas()
    self.load_jobs()

  def get_media_settings(self) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    if self.media_settings_file.exists():
      try:
        value = json.loads(self.media_settings_file.read_text(encoding="utf-8"))
        if isinstance(value, dict):
          settings = value
      except Exception as error:  # pylint: disable=broad-except
        print(f"Warning: failed to load browser media settings: {error}",
              file=sys.stderr)
    return normalize_media_settings(settings, self.media_settings_file)

  def save_media_settings(self, request: dict[str, Any]) -> dict[str, Any]:
    current = self.get_media_settings()
    camera_source = str(
        request.get("cameraSource") or request.get("camera_source") or
        current.get("cameraSource") or "fake").strip().lower()
    if camera_source not in PRIVACY_SOURCES:
      raise ValueError("cameraSource must be real or fake")
    fake_camera_backend = str(
        request.get("fakeCameraBackend") or
        request.get("fake_camera_backend") or
        current.get("fakeCameraBackend") or "loop").strip().lower()
    if fake_camera_backend not in FAKE_CAMERA_BACKENDS:
      raise ValueError(
          "fakeCameraBackend must be one of: " +
          ", ".join(sorted(FAKE_CAMERA_BACKENDS)))
    requested_media_mode = request.get("mediaMode") or request.get("media_mode")
    if requested_media_mode:
      media_mode = str(requested_media_mode).strip().lower()
      camera_source = "real" if media_mode in ("system", "none") else "fake"
      if media_mode in FAKE_CAMERA_BACKENDS:
        fake_camera_backend = media_mode
    else:
      media_mode = "system" if camera_source == "real" else fake_camera_backend
    if media_mode not in MEDIA_MODES:
      raise ValueError(
          "mediaMode must be one of: " + ", ".join(sorted(MEDIA_MODES)))
    microphone_source = str(
        request.get("microphoneSource") or request.get("microphone_source") or
        current.get("microphoneSource") or "fake").strip().lower()
    requested_audio_mode = (
        request.get("audioCaptureMode") or request.get("audio_capture_mode"))
    if requested_audio_mode:
      audio_capture_mode = str(requested_audio_mode).strip().lower()
      microphone_source = (
          "real" if audio_capture_mode == "system" else "fake")
    else:
      if microphone_source not in PRIVACY_SOURCES:
        raise ValueError("microphoneSource must be real or fake")
      audio_capture_mode = (
          "system" if microphone_source == "real" else "fake")
    if audio_capture_mode not in AUDIO_CAPTURE_MODES:
      raise ValueError(
          "audioCaptureMode must be one of: " +
          ", ".join(sorted(AUDIO_CAPTURE_MODES)))

    loop_video_file = str(
        request.get("loopVideoFile") if "loopVideoFile" in request else
        request.get("loop_video_file") if "loop_video_file" in request else
        current.get("loopVideoFile", "")).strip()
    loop_video_name = str(request.get("loopVideoName") or current.get("loopVideoName") or "")
    loop_video_bytes = int(current.get("loopVideoBytes") or 0)

    if loop_video_file:
      loop_video_path = Path(loop_video_file).expanduser().resolve()
      validate_y4m_file(loop_video_path)
      loop_video_file = str(loop_video_path)
      loop_video_name = loop_video_name or loop_video_path.name
      loop_video_bytes = loop_video_path.stat().st_size
    else:
      loop_video_name = ""
      loop_video_bytes = 0

    now = time.time()
    settings = {
        "schemaVersion": PRIVACY_SETTINGS_SCHEMA_VERSION,
        "cameraSource": camera_source,
        "fakeCameraBackend": fake_camera_backend,
        "microphoneSource": microphone_source,
        "mediaMode": media_mode,
        "audioCaptureMode": audio_capture_mode,
        "loopVideoFile": loop_video_file,
        "loopVideoName": loop_video_name,
        "loopVideoBytes": loop_video_bytes,
        "updatedAt": iso_time(now),
        "updatedAtEpoch": now,
    }
    atomic_write_json(self.media_settings_file, settings)
    return normalize_media_settings(settings, self.media_settings_file)

  def imported_loop_video_path(self, filename: str) -> Path:
    basename = sanitize_path_part(Path(filename or "loop").stem)
    return self.loop_video_dir / (
        f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-"
        f"{secrets.token_hex(3)}-{basename}.y4m")

  def use_imported_loop_video(
      self,
      filename: str,
      path: Path,
  ) -> dict[str, Any]:
    validate_y4m_file(path)
    return self.save_media_settings({
        "mediaMode": "loop",
        "loopVideoFile": str(path),
        "loopVideoName": filename or path.name,
    })

  def get_location_settings(self) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    if self.location_settings_file.exists():
      try:
        value = json.loads(
            self.location_settings_file.read_text(encoding="utf-8"))
        if isinstance(value, dict):
          settings = value
      except Exception as error:  # pylint: disable=broad-except
        print(f"Warning: failed to load browser location settings: {error}",
              file=sys.stderr)
    return normalize_location_settings(settings, self.location_settings_file)

  def get_privacy_rules(self) -> list[dict[str, Any]]:
    with self.state_lock:
      if not self.privacy_rules_file.exists():
        return []
      try:
        value = json.loads(
            self.privacy_rules_file.read_text(encoding="utf-8"))
      except Exception:
        return []
      raw_rules = value.get("rules", []) if isinstance(value, dict) else []
      return [rule for rule in raw_rules if isinstance(rule, dict)]

  def save_privacy_rule(self, request: dict[str, Any]) -> dict[str, Any]:
    origin = normalize_site_origin(
        request.get("origin") or request.get("site") or "")
    if not origin:
      raise ValueError("origin must be an http(s) website origin")
    rule_id = base64.urlsafe_b64encode(origin.encode("utf-8")).decode(
        "ascii").rstrip("=")
    rule = {
        "id": rule_id,
        "origin": origin,
        "cameraSource": privacy_source_value(
            request.get("cameraSource"), "fake"),
        "microphoneSource": privacy_source_value(
            request.get("microphoneSource"), "fake"),
        "locationSource": privacy_source_value(
            request.get("locationSource"), "fake"),
        "fakeCameraBackend": fake_camera_backend_value(
            request.get("fakeCameraBackend"), "loop"),
        "updatedAt": iso_time(time.time()),
    }
    with self.state_lock:
      rules = [entry for entry in self.get_privacy_rules()
               if entry.get("id") != rule_id]
      rules.append(rule)
      rules.sort(key=lambda entry: str(entry.get("origin") or ""))
      atomic_write_json(self.privacy_rules_file, {
          "schemaVersion": PRIVACY_SETTINGS_SCHEMA_VERSION,
          "rules": rules,
      })
    return rule

  def delete_privacy_rule(self, rule_id: str) -> bool:
    with self.state_lock:
      rules = self.get_privacy_rules()
      remaining = [rule for rule in rules if rule.get("id") != rule_id]
      if len(remaining) == len(rules):
        return False
      atomic_write_json(self.privacy_rules_file, {
          "schemaVersion": PRIVACY_SETTINGS_SCHEMA_VERSION,
          "rules": remaining,
      })
      return True

  def save_location_settings(self, request: dict[str, Any]) -> dict[str, Any]:
    current = self.get_location_settings()
    source = str(
        request.get("source") or request.get("locationSource") or
        request.get("location_source") or current.get("source") or
        ("fake" if current.get("enabled", True) else "real")).strip().lower()
    if "enabled" in request or "locationEnabled" in request:
      enabled_value = request.get("enabled", request.get("locationEnabled"))
      source = "fake" if bool_value(enabled_value, True) else "real"
    if source not in PRIVACY_SOURCES:
      raise ValueError("location source must be real or fake")
    enabled = source == "fake"
    latitude = clamp_float(
        request.get("latitude", current.get("latitude")),
        -90.0, 90.0, DEFAULT_FAKE_LOCATION["latitude"])
    longitude = clamp_float(
        request.get("longitude", current.get("longitude")),
        -180.0, 180.0, DEFAULT_FAKE_LOCATION["longitude"])
    accuracy = clamp_float(
        request.get("accuracy", current.get("accuracy")),
        1.0, 100000.0, DEFAULT_FAKE_LOCATION["accuracy"])
    now = time.time()
    settings = {
        "schemaVersion": PRIVACY_SETTINGS_SCHEMA_VERSION,
        "source": source,
        "enabled": enabled,
        "latitude": latitude,
        "longitude": longitude,
        "accuracy": accuracy,
        "updatedAt": iso_time(now),
        "updatedAtEpoch": now,
    }
    atomic_write_json(self.location_settings_file, settings)
    normalized = normalize_location_settings(
        settings, self.location_settings_file)
    return normalized

  def load_apps(self) -> None:
    if not self.apps_file.exists():
      return
    try:
      data = json.loads(self.apps_file.read_text(encoding="utf-8"))
      apps = data.get("apps", [])
      if not isinstance(apps, list):
        return
      with self.apps_lock:
        for raw in apps:
          if not isinstance(raw, dict):
            continue
          raw_app_id = str(raw.get("id") or "").strip()
          secret = str(raw.get("secret", ""))
          if not raw_app_id or not secret:
            continue
          app_id = sanitize_path_part(raw_app_id)
          self.apps[app_id] = AppRegistration(
              id=app_id,
              name=str(raw.get("name") or app_id),
              secret=secret,
              description=str(raw.get("description") or ""),
              created_at=float(raw.get("createdAtEpoch") or time.time()),
              updated_at=float(raw.get("updatedAtEpoch") or time.time()),
          )
    except Exception as error:  # pylint: disable=broad-except
      print(f"Warning: failed to load broker apps state: {error}", file=sys.stderr)

  def load_schemas(self) -> None:
    if not self.schemas_file.exists():
      return
    try:
      data = json.loads(self.schemas_file.read_text(encoding="utf-8"))
      schemas = data.get("schemas", [])
      if not isinstance(schemas, list):
        return
      with self.schemas_lock:
        for raw in schemas:
          if not isinstance(raw, dict):
            continue
          try:
            schema = schema_from_record(raw)
          except ValueError:
            continue
          self.schemas[schema.id] = schema
    except Exception as error:  # pylint: disable=broad-except
      print(f"Warning: failed to load broker schemas state: {error}", file=sys.stderr)

  def load_jobs(self) -> None:
    if not self.jobs_file.exists():
      return
    try:
      data = json.loads(self.jobs_file.read_text(encoding="utf-8"))
      jobs = data.get("jobs", [])
      if not isinstance(jobs, list):
        return
      with self.jobs_lock:
        for raw in jobs:
          if not isinstance(raw, dict):
            continue
          job_id = str(raw.get("id", "")).strip()
          url = str(raw.get("url", "")).strip()
          output_dir = Path(str(raw.get("outputDir", ""))).expanduser()
          config = raw.get("config", {})
          if not job_id or not url or not isinstance(config, dict):
            continue
          status = str(raw.get("status") or "interrupted")
          reason = str(raw.get("reason") or "")
          finished_at = float(raw["finishedAtEpoch"]) if raw.get("finishedAtEpoch") else None
          if status != "queued" and status not in TERMINAL_STATUSES:
            status = "interrupted"
            reason = "broker_restarted"
            finished_at = time.time()
          job = Job(
              id=job_id,
              app_id=sanitize_path_part(str(raw.get("appId") or raw.get("app_id") or "app")),
              url=url,
              output_dir=output_dir,
              config=dict(config),
              status=status,
              reason=reason,
              created_at=float(raw.get("createdAtEpoch") or time.time()),
              started_at=(float(raw["startedAtEpoch"]) if raw.get("startedAtEpoch") else None),
              updated_at=float(raw.get("updatedAtEpoch") or time.time()),
              finished_at=finished_at,
              target_id=str(raw.get("targetId") or ""),
              websocket_url=str(raw.get("webSocketDebuggerUrl") or ""),
              current_url=str(raw.get("currentUrl") or ""),
              title=str(raw.get("title") or ""),
              exports=dict(raw.get("exports") or {}),
              raw_html_truncated=bool(raw.get("rawHtmlTruncated")),
              raw_html_length=int(raw.get("rawHtmlLength") or 0),
              error=str(raw.get("error") or ""),
              event_sequence=int(raw.get("eventSequence") or 0),
          )
          self.event_hub.restore_sequence(job.id, job.event_sequence)
          job.items = load_items_file(job.output_dir / "items.jsonl")
          if (job.output_dir / "events.jsonl").exists():
            job.exports["eventsJsonl"] = str(job.output_dir / "events.jsonl")
          self.jobs[job.id] = job
          if job.status == "queued":
            self.enqueue_job(job, persist=False)
      self.persist_jobs()
    except Exception as error:  # pylint: disable=broad-except
      print(f"Warning: failed to load broker jobs state: {error}", file=sys.stderr)

  def register_app(self, request: dict[str, Any]) -> AppRegistration:
    name = limit_text(request.get("name", ""), 120)
    if not name:
      raise ValueError("name is required")
    description = limit_text(request.get("description", ""), 500)
    requested_id = str(request.get("app_id") or request.get("appId") or "").strip()
    base = sanitize_path_part(requested_id or name).lower()
    if base in ("admin", "root", "health", "jobs", "apps", "ui"):
      base = f"{base}_app"
    with self.apps_lock:
      app_id = base
      while app_id in self.apps:
        app_id = f"{base}-{secrets.token_hex(3)}"
      now = time.time()
      app = AppRegistration(
          id=app_id,
          name=name,
          secret=secrets.token_urlsafe(32),
          description=description,
          created_at=now,
          updated_at=now,
      )
      self.apps[app.id] = app
    self.persist_apps()
    return app

  def list_apps(self) -> list[AppRegistration]:
    with self.apps_lock:
      return sorted(self.apps.values(), key=lambda app: app.created_at, reverse=True)

  def get_app(self, app_id: str) -> AppRegistration | None:
    with self.apps_lock:
      return self.apps.get(app_id)

  def authenticate_app(self, app_id: str, secret: str) -> bool:
    if not app_id or not secret:
      return False
    with self.apps_lock:
      app = self.apps.get(app_id)
    return bool(app and secrets.compare_digest(app.secret, secret))

  def issue_stream_ticket(self, auth: BrokerAuth) -> StreamTicket:
    now = time.time()
    ticket = StreamTicket(
        token=secrets.token_urlsafe(32),
        auth=auth,
        expires_at=now + STREAM_TICKET_TTL_SECONDS,
    )
    with self.stream_tickets_lock:
      self._purge_stream_tickets_locked(now)
      while len(self.stream_tickets) >= STREAM_MAX_OUTSTANDING_TICKETS:
        self.stream_tickets.pop(next(iter(self.stream_tickets)))
      self.stream_tickets[ticket.token] = ticket
    return ticket

  def consume_stream_ticket(self, token: str) -> StreamTicket | None:
    now = time.time()
    with self.stream_tickets_lock:
      self._purge_stream_tickets_locked(now)
      ticket = self.stream_tickets.pop(token, None)
    if not ticket or ticket.expires_at < now:
      return None
    return ticket

  def _purge_stream_tickets_locked(self, now: float) -> None:
    expired = [token for token, ticket in self.stream_tickets.items()
               if ticket.expires_at < now]
    for token in expired:
      self.stream_tickets.pop(token, None)

  def close(self) -> None:
    self.event_writer.close()

  def persist_apps(self) -> None:
    with self.state_lock:
      with self.apps_lock:
        apps = [{
            "id": app.id,
            "name": app.name,
            "secret": app.secret,
            "description": app.description,
            "createdAtEpoch": app.created_at,
            "updatedAtEpoch": app.updated_at,
        } for app in sorted(self.apps.values(), key=lambda value: value.id)]
      atomic_write_json(self.apps_file, {
          "schemaVersion": SCHEMA_VERSION,
          "apps": apps,
      })

  def persist_schemas(self) -> None:
    with self.state_lock:
      with self.schemas_lock:
        schemas = [
            schema.to_dict()
            for schema in sorted(self.schemas.values(), key=lambda value: value.id)
        ]
      atomic_write_json(self.schemas_file, {
          "schemaVersion": SCHEMA_VERSION,
          "schemas": schemas,
      })

  def list_schemas(self, app_id: str = "") -> list[ExtractorSchema]:
    with self.schemas_lock:
      schemas = list(self.schemas.values())
      if app_id:
        schemas = [schema for schema in schemas if schema.app_id == app_id]
      return sorted(
          schemas,
          key=lambda schema: schema.updated_at,
          reverse=True)

  def get_schema(self, schema_id: str, app_id: str = "") -> ExtractorSchema | None:
    with self.schemas_lock:
      schema = self.schemas.get(sanitize_path_part(schema_id))
      if schema and app_id and schema.app_id != app_id:
        return None
      return schema

  def save_schema(self, request: dict[str, Any], app_id: str = "admin") -> ExtractorSchema:
    schema = validate_schema_request(request)
    schema.app_id = sanitize_path_part(app_id or "admin")
    now = time.time()
    with self.schemas_lock:
      previous = self.schemas.get(schema.id)
      if previous:
        if previous.app_id != schema.app_id:
          raise ValueError("schema id is owned by another app")
        schema.created_at = previous.created_at
      else:
        schema.created_at = now
      schema.updated_at = now
      self.schemas[schema.id] = schema
    self.persist_schemas()
    return schema

  def delete_schema(self, schema_id: str, app_id: str = "") -> bool:
    schema_id = sanitize_path_part(schema_id)
    with self.schemas_lock:
      schema = self.schemas.get(schema_id)
      existed = bool(schema and (not app_id or schema.app_id == app_id))
      if not existed:
        return False
      self.schemas.pop(schema_id, None)
    if existed:
      self.persist_schemas()
    return existed

  def list_feeds(self, app_id: str = "") -> list[dict[str, Any]]:
    return [self.feed_summary(schema) for schema in self.list_schemas(app_id)]

  def get_feed(self, feed_id: str, app_id: str = "") -> dict[str, Any] | None:
    schema = self.get_schema(feed_id, app_id)
    if not schema:
      return None
    return self.feed_summary(schema, include_schema=True)

  def save_feed(self, request: dict[str, Any], app_id: str = "admin") -> ExtractorSchema:
    schema_request = dict(request)
    if "sourceUrl" not in schema_request and "url" in schema_request:
      schema_request["sourceUrl"] = schema_request.get("url")
    if "itemRoot" not in schema_request and "item_root" in schema_request:
      schema_request["itemRoot"] = schema_request.get("item_root")
    return self.save_schema(schema_request, app_id)

  def delete_feed(self, feed_id: str, app_id: str = "") -> bool:
    return self.delete_schema(feed_id, app_id)

  def run_feed(
      self,
      feed_id: str,
      request: dict[str, Any],
      app_id_override: str = "",
  ) -> Job:
    schema = self.get_schema(feed_id, app_id_override)
    if not schema:
      raise ValueError("feed not found")
    url = str(request.get("url") or request.get("sourceUrl") or schema.source_url).strip()
    if not url:
      raise ValueError("feed has no URL")
    body = dict(request)
    body["url"] = url
    body["schema_id"] = schema.id
    return self.create_job(
        body, app_id_override=app_id_override or schema.app_id)

  def latest_job_for_schema(self, schema_id: str, app_id: str = "") -> Job | None:
    schema_id = sanitize_path_part(schema_id)
    with self.jobs_lock:
      jobs = [
          job for job in self.jobs.values()
          if str(job.config.get("schema_id") or "") == schema_id and
          (not app_id or job.app_id == app_id)
      ]
    if not jobs:
      return None
    return sorted(jobs, key=lambda job: job.created_at, reverse=True)[0]

  def feed_summary(
      self,
      schema: ExtractorSchema,
      include_schema: bool = False,
  ) -> dict[str, Any]:
    latest = self.latest_job_for_schema(schema.id, schema.app_id)
    value = {
        "id": schema.id,
        "appId": schema.app_id,
        "name": schema.name,
        "description": schema.description,
        "url": schema.source_url,
        "sourceUrl": schema.source_url,
        "host": schema.host,
        "itemRoot": schema.item_root,
        "fieldCount": len(schema.fields),
        "fields": list(schema.fields),
        "createdAt": iso_time(schema.created_at),
        "updatedAt": iso_time(schema.updated_at),
        "latestJob": latest.summary(False) if latest else None,
        "latest": latest_output_links(latest) if latest else {},
    }
    if include_schema:
      value["schema"] = schema.summary()
    return value

  def persist_jobs(self) -> None:
    with self.state_lock:
      with self.jobs_lock:
        jobs = [
            job_record(job)
            for job in sorted(self.jobs.values(), key=lambda value: value.id)
        ]
      atomic_write_json(self.jobs_file, {
          "schemaVersion": SCHEMA_VERSION,
          "jobs": jobs,
      })

  def add_event(
      self,
      job: Job,
      event_type: str,
      data: dict[str, Any] | None = None,
  ) -> None:
    # Sequence assignment, the public job cursor, and persistence enqueue must
    # move together. Control requests can emit events concurrently with the
    # collection worker for the same job.
    with job.event_lock:
      event = self.event_hub.publish(job, event_type, data)
      with job.lock:
        job.output_dir.mkdir(parents=True, exist_ok=True)
        job_events = job.output_dir / "events.jsonl"
        job.exports["eventsJsonl"] = str(job_events)
        job.updated_at = float(event["timeEpoch"])
        job.event_sequence = int(event["sequence"])
      self.event_writer.enqueue(job, event)

  def set_job_status(self, job: Job, status: str, reason: str = "") -> None:
    with job.event_lock:
      with job.lock:
        previous_status = job.status
        previous_reason = job.reason
        job.set_status(status, reason)
      if previous_status != status or previous_reason != reason:
        self.add_event(job, "status", {
            "previousStatus": previous_status,
            "status": status,
            "reason": reason,
        })
        if status in TERMINAL_STATUSES:
          self.event_writer.flush()
    self.persist_jobs()

  def create_job(self, request: dict[str, Any], app_id_override: str = "") -> Job:
    url = str(request.get("url", "")).strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
      raise ValueError("url must be http or https")

    app_id = sanitize_path_part(
        app_id_override or str(request.get("app_id") or request.get("appId") or "app"))
    job_id = make_job_id()
    host = sanitize_path_part(parsed.hostname or "site")
    output_dir = self.config.output_root / host / job_id
    schema = self.resolve_job_schema(request, app_id)
    config = {
        "max_items": clamp_int(request.get("max_items", request.get("maxItems", 500)), 0, 5000),
        "timeout_seconds": clamp_int(
            request.get("timeout_seconds", request.get("timeoutSeconds", 900)), 10, 14400),
        "scroll_delay_ms": clamp_int(
            request.get("scroll_delay_ms", request.get("scrollDelayMs", 1500)), 250, 30000),
        "no_progress_limit": clamp_int(
            request.get("no_progress_limit", request.get("noProgressLimit", 8)), 1, 100),
        "checkpoint_items": clamp_int(
            request.get("checkpoint_items", request.get("checkpointItems", 25)), 1, 1000),
        "raw_snapshots": bool(request.get("raw_snapshots", request.get("rawSnapshots", True))),
        "max_raw_html_chars": clamp_int(
            request.get("max_raw_html_chars", request.get("maxRawHtmlChars", 5_000_000)),
            100_000, 50_000_000),
        "schema_id": str(schema.get("id") or "") if schema else "",
        "schema": schema,
      }
    job = Job(
        id=job_id,
        app_id=app_id,
        url=url,
        output_dir=output_dir,
        config=config,
        status="queued")
    with self.jobs_lock:
      self.jobs[job.id] = job
    self.add_event(job, "created", {"url": url, "config": config})
    # This is persisted by the asynchronous event writer, so accepting an app
    # request remains independent of slow disks or websocket consumers.
    self.add_event(job, "job_accepted", {
        "host": parsed.hostname or "",
    })
    self.enqueue_job(job)
    return job

  def resolve_job_schema(
      self,
      request: dict[str, Any],
      app_id: str,
  ) -> dict[str, Any] | None:
    inline_schema = request.get("schema")
    if isinstance(inline_schema, dict):
      return validate_schema_request(inline_schema).summary()
    schema_id = str(request.get("schema_id") or request.get("schemaId") or "").strip()
    if not schema_id:
      return None
    schema = self.get_schema(schema_id, app_id)
    if not schema:
      raise ValueError(f"schema not found: {schema_id}")
    return schema.summary()

  def enqueue_job(self, job: Job, *, persist: bool = True) -> None:
    with self.scheduler_condition:
      queue = self.queued_by_app[job.app_id]
      if job.id not in queue:
        queue.append(job.id)
      if job.app_id not in self.scheduler_apps:
        self.scheduler_apps.append(job.app_id)
      self.scheduler_condition.notify_all()
    if persist:
      self.persist_jobs()

  def scheduler_loop(self) -> None:
    while True:
      with self.scheduler_condition:
        while True:
          job = self.next_schedulable_job_locked()
          while job is None:
            self.scheduler_condition.wait()
            job = self.next_schedulable_job_locked()
          # Claim the queued job before releasing the scheduler lock. A stop
          # request racing this handoff either wins here (and no worker starts)
          # or sees `scheduled` and is handled by the worker before CDP use.
          with job.lock:
            if job.status != "queued" or job.stop_event.is_set():
              continue
            job.status = "scheduled"
            job.updated_at = time.time()
          break
        self.active_jobs += 1
        self.active_by_app[job.app_id] += 1
      threading.Thread(
          target=self.run_scheduled_job,
          args=(job,),
          name=f"hardened-job-{job.id}",
          daemon=True).start()

  def next_schedulable_job_locked(self) -> Job | None:
    if self.active_jobs >= self.config.max_active_jobs:
      return None
    app_count = len(self.scheduler_apps)
    for _ in range(app_count):
      app_id = self.scheduler_apps.popleft()
      queue = self.queued_by_app[app_id]
      while queue:
        candidate = self.get_job(queue[0])
        if candidate and candidate.status == "queued":
          break
        queue.popleft()
      if not queue:
        self.queued_by_app.pop(app_id, None)
        continue
      self.scheduler_apps.append(app_id)
      if self.active_by_app[app_id] >= self.config.max_active_jobs_per_app:
        continue
      job_id = queue.popleft()
      if not queue:
        self.scheduler_apps.pop()
        self.queued_by_app.pop(app_id, None)
      return self.get_job(job_id)
    return None

  def run_scheduled_job(self, job: Job) -> None:
    try:
      self.run_job(job)
    finally:
      with self.scheduler_condition:
        self.active_jobs = max(0, self.active_jobs - 1)
        self.active_by_app[job.app_id] = max(
            0, self.active_by_app[job.app_id] - 1)
        self.scheduler_condition.notify_all()

  def list_jobs(self, app_id: str = "") -> list[Job]:
    with self.jobs_lock:
      jobs = list(self.jobs.values())
    if app_id:
      jobs = [job for job in jobs if job.app_id == app_id]
    return sorted(jobs, key=lambda job: job.created_at, reverse=True)

  def get_job(self, job_id: str) -> Job | None:
    with self.jobs_lock:
      return self.jobs.get(job_id)

  def run_job(self, job: Job) -> None:
    cdp: CdpWebSocket | None = None
    seen_keys: set[str] = set()
    checkpoint_count = 0
    tab_opened = False
    try:
      if job.stop_event.is_set():
        job.finished_at = time.time()
        self.set_job_status(job, "stopped", "stopped_before_start")
        return
      job.started_at = time.time()
      self.set_job_status(job, "starting")
      if job.stop_event.is_set():
        job.finished_at = time.time()
        self.set_job_status(job, "stopped", "stopped_before_browser_use")
        return
      self.add_event(job, "tab_opening", {})
      target = cdp_create_tab(self.config.cdp_endpoint, "about:blank")
      job.target_id = str(target.get("id", ""))
      job.websocket_url = str(target.get("webSocketDebuggerUrl", ""))
      if not job.websocket_url:
        raise CdpError("CDP did not return webSocketDebuggerUrl")
      self.add_event(job, "tab_opened", {"targetId": job.target_id})
      tab_opened = True

      cdp = CdpWebSocket(job.websocket_url)
      cdp.connect()
      cdp.command("Page.enable")
      cdp.command("Runtime.enable")
      cdp.command("Page.navigate", {"url": job.url}, timeout=10)
      self.add_event(job, "navigated", {"url": job.url})
      self.set_job_status(job, "running")
      wait_for_document_ready(cdp, min(45, job.config["timeout_seconds"]))

      schema = job.config.get("schema")
      collect_expression = (
          schema_collect_items_js(schema)
          if isinstance(schema, dict) else COLLECT_ITEMS_JS)
      if isinstance(schema, dict):
        self.add_event(job, "schema_selected", {
            "schemaId": schema.get("id", ""),
            "name": schema.get("name", ""),
            "itemRoot": schema.get("itemRoot", ""),
        })

      deadline = time.monotonic() + job.config["timeout_seconds"]
      no_progress = 0
      while not job.stop_event.is_set() and time.monotonic() < deadline:
        page_state = evaluate(cdp, collect_expression, timeout=20)
        job.current_url = str(page_state.get("href") or job.current_url or job.url)
        job.title = str(page_state.get("title") or job.title)
        added = add_items(job, page_state.get("items", []), seen_keys)

        if added:
          with job.lock:
            streamed_items = list(job.items[-added:])
          for item in streamed_items:
            self.add_event(job, "item", {
                "index": int(item.get("index", 0)),
                "item": item,
            })
          self.add_event(job, "items_added", {
              "added": added,
              "itemCount": len(job.items),
              "currentUrl": job.current_url,
          })
          no_progress = 0
          checkpoint_count += added
        else:
          no_progress += 1

        if added and checkpoint_count >= job.config["checkpoint_items"]:
          if write_outputs(job):
            checkpoint_count = 0

        if should_pause_for_user(job, page_state, no_progress):
          write_outputs(job, force=True)
          self.persist_jobs()
          self.set_job_status(job, "needs_user", "manual_interaction_required")
          job.resume_event.clear()
          while not job.stop_event.is_set():
            if job.resume_event.wait(timeout=1):
              job.resume_event.clear()
              no_progress = 0
              self.set_job_status(job, "running", "resumed_by_user")
              break
          continue

        max_items = job.config["max_items"]
        if max_items and len(job.items) >= max_items:
          self.set_job_status(job, "completed", "item_limit_reached")
          break

        if no_progress >= job.config["no_progress_limit"]:
          self.set_job_status(job, "completed", "no_more_items_detected")
          break

        evaluate(cdp, SCROLL_JS, timeout=10)
        sleep_interruptibly(job, job.config["scroll_delay_ms"] / 1000)

      if job.stop_event.is_set():
        self.set_job_status(job, "stopped", "stopped_by_request")
      elif job.status == "running":
        self.set_job_status(job, "completed", "timeout_reached")

      if job.status in ("completed", "failed", "stopped"):
        job.finished_at = time.time()
      if job.config["raw_snapshots"] and cdp:
        capture_raw_outputs(job, cdp)
      write_outputs(job, force=True)
      self.add_event(job, "outputs_written", {"exports": dict(job.exports)})
      self.persist_jobs()
    except Exception as error:  # pylint: disable=broad-except
      job.error = str(error)
      if not tab_opened:
        self.add_event(job, "tab_open_failed", {
            "error": redact_token(str(error)),
        })
      self.set_job_status(job, "failed", "exception")
      job.finished_at = time.time()
      try:
        write_outputs(job, force=True)
        self.add_event(job, "outputs_written", {"exports": dict(job.exports)})
      except Exception:
        pass
    finally:
      job.updated_at = time.time()
      self.persist_jobs()
      if cdp:
        cdp.close()

  def stop_job(self, job_id: str) -> Job:
    job = require_job(self, job_id)
    if job.status in TERMINAL_STATUSES:
      return job
    if job.status == "queued":
      job.stop_event.set()
      job.finished_at = time.time()
      self.set_job_status(job, "stopped", "stopped_while_queued")
      with self.scheduler_condition:
        self.scheduler_condition.notify_all()
      return job
    job.stop_event.set()
    job.resume_event.set()
    self.set_job_status(job, "stopping", "stop_requested")
    return job

  def resume_job(self, job_id: str) -> Job:
    job = require_job(self, job_id)
    if job.status != "needs_user":
      raise ValueError("job is not waiting for user interaction")
    job.resume_event.set()
    job.updated_at = time.time()
    self.add_event(job, "resume_requested", {})
    self.persist_jobs()
    return job

  def retry_job(self, job_id: str) -> Job:
    previous = require_job(self, job_id)
    if previous.status not in TERMINAL_STATUSES:
      raise ValueError("only terminal jobs can be retried")
    request = dict(previous.config)
    request["url"] = previous.url
    request["app_id"] = previous.app_id
    request["schema"] = previous.config.get("schema")
    return self.create_job(request, app_id_override=previous.app_id)

  def close_completed_tabs(self, app_id: str = "") -> dict[str, Any]:
    closed: list[str] = []
    failed: dict[str, str] = {}
    for job in self.list_jobs(app_id):
      if job.status not in TERMINAL_STATUSES or not job.target_id:
        continue
      try:
        cdp_close_tab(self.config.cdp_endpoint, job.target_id)
        closed.append(job.id)
        self.add_event(job, "tab_close_requested", {
            "targetId": job.target_id,
            "bulk": True,
        })
      except Exception as error:  # pylint: disable=broad-except
        failed[job.id] = str(error)
    self.persist_jobs()
    return {"ok": not failed, "closed": closed, "failed": failed}

  def close_job_tab(self, job_id: str) -> Job:
    job = require_job(self, job_id)
    if job.target_id:
      cdp_close_tab(self.config.cdp_endpoint, job.target_id)
    job.updated_at = time.time()
    self.add_event(job, "tab_close_requested", {"targetId": job.target_id})
    self.persist_jobs()
    return job

  def preview_schema_for_job(
      self,
      job: Job,
      request: dict[str, Any],
  ) -> dict[str, Any]:
    schema = self.schema_from_request_or_id(request, job.app_id)
    cdp = connect_job_cdp(job)
    try:
      state = evaluate(cdp, schema_collect_items_js(schema), timeout=30)
    finally:
      cdp.close()
    items = state.get("items", []) if isinstance(state, dict) else []
    return {
        "ok": True,
        "jobId": job.id,
        "schema": {"id": schema.get("id", ""), "name": schema.get("name", "")},
        "count": len(items) if isinstance(items, list) else 0,
        "page": state,
    }

  def schema_from_request_or_id(
      self,
      request: dict[str, Any],
      app_id: str,
  ) -> dict[str, Any]:
    inline_schema = request.get("schema")
    if isinstance(inline_schema, dict):
      return validate_schema_request(inline_schema).summary()
    schema_id = str(request.get("schema_id") or request.get("schemaId") or request.get("id") or "")
    schema = self.get_schema(schema_id, app_id)
    if not schema:
      raise ValueError("schema or valid schema_id is required")
    return schema.summary()

  def control_job(self, job: Job, request: dict[str, Any]) -> dict[str, Any]:
    action = str(request.get("action") or "").strip().lower().replace("-", "_")
    if not action:
      raise ValueError("action is required")
    cdp = connect_job_cdp(job)
    try:
      result = run_job_control(job, cdp, action, request)
    finally:
      cdp.close()
    self.add_event(job, "control", {
        "action": action,
        "ok": bool(result.get("ok", True)),
    })
    self.persist_jobs()
    return {"ok": True, "jobId": job.id, "action": action, "result": result}


class BrokerRequestHandler(BaseHTTPRequestHandler):
  server_version = "HardenedScrapeBroker/1.0"

  @property
  def broker(self) -> Broker:
    return self.server.broker  # type: ignore[attr-defined]

  def log_message(self, fmt: str, *args: Any) -> None:
    redacted_args = tuple(redact_token(str(arg)) for arg in args)
    sys.stderr.write("%s - - [%s] %s\n" % (
        self.address_string(),
        self.log_date_time_string(),
        fmt % redacted_args,
    ))

  def end_headers(self) -> None:
    origin = self.headers.get("Origin", "")
    if not self.broker.config.no_auth:
      self.send_header("Access-Control-Allow-Origin", "*")
    elif origin and self.request_metadata_allowed():
      self.send_header("Access-Control-Allow-Origin", origin)
      self.send_header("Vary", "Origin")
    self.send_header(
        "Access-Control-Allow-Headers",
        "Authorization, Content-Type, X-Hardened-App-Id, "
        "X-Hardened-App-Secret, X-Hardened-Filename")
    self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
    self.send_header("X-Content-Type-Options", "nosniff")
    super().end_headers()

  def do_OPTIONS(self) -> None:
    if not self.request_metadata_allowed():
      self.send_request_metadata_error()
      return
    self.send_response(HTTPStatus.NO_CONTENT)
    self.end_headers()

  def do_GET(self) -> None:
    if not self.request_metadata_allowed():
      self.send_request_metadata_error()
      return
    parsed = urlparse(self.path)
    query = parse_qs(parsed.query)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if parts == ["ui"]:
      self.send_broker_ui()
      return
    if parts == ["ws"]:
      ticket = self.broker.consume_stream_ticket(
          query.get("ticket", [""])[0])
      if not ticket:
        self.send_json({
            "ok": False,
            "error": "missing, expired, or already-used stream ticket",
        }, HTTPStatus.UNAUTHORIZED)
        return
      self.handle_websocket(ticket.auth)
      return

    auth = self.authenticate(query)
    if not auth:
      self.send_auth_error()
      return

    if not parts:
      self.send_json(self.index_document(auth))
      return
    if parts == ["health"]:
      app_scope = "" if auth.is_admin else auth.app_id
      self.send_json({
          "ok": True,
          "service": "hardened-scrape-broker",
          "auth": "none" if self.broker.config.no_auth else "token",
          "outputRoot": str(self.broker.config.output_root),
          "stateDir": str(self.broker.state_dir),
          "jobCount": len(self.broker.list_jobs(
              app_scope)),
          "appCount": len(self.broker.list_apps()) if auth.is_admin else None,
          "feedCount": len(self.broker.list_schemas(app_scope)),
          "capacity": {
              "active": self.broker.active_jobs,
              "maximum": self.broker.config.max_active_jobs,
              "perAppMaximum": self.broker.config.max_active_jobs_per_app,
          },
      })
      return
    if parts == ["service", "media-settings"]:
      if not self.require_admin(auth):
        return
      self.send_json({"ok": True, "settings": self.broker.get_media_settings()})
      return
    if parts == ["service", "privacy-settings"]:
      if not self.require_admin(auth):
        return
      self.send_json({
          "ok": True,
          "schemaVersion": PRIVACY_SETTINGS_SCHEMA_VERSION,
          "media": self.broker.get_media_settings(),
          "location": self.broker.get_location_settings(),
          "rules": self.broker.get_privacy_rules(),
      })
      return
    if parts == ["service", "location-settings"]:
      if not self.require_admin(auth):
        return
      self.send_json({
          "ok": True,
          "settings": self.broker.get_location_settings(),
      })
      return
    if parts == ["service", "privacy-rules"]:
      if not self.require_admin(auth):
        return
      self.send_json({
          "ok": True,
          "rules": self.broker.get_privacy_rules(),
          "settingsFile": str(self.broker.privacy_rules_file),
      })
      return
    if parts == ["events.jsonl"]:
      if not self.require_admin(auth):
        return
      self.send_file(self.broker.events_file)
      return
    if parts and parts[0] == "feeds":
      self.handle_feeds_get(parts, auth)
      return
    if parts and parts[0] == "schemas":
      self.handle_schemas_get(parts, auth)
      return
    if parts and parts[0] == "apps":
      if not self.require_admin(auth):
        return
      self.handle_apps_get(parts)
      return
    if parts == ["jobs"]:
      self.send_json({
          "ok": True,
          "jobs": [
              job.summary(False)
              for job in self.broker.list_jobs("" if auth.is_admin else auth.app_id)
          ],
      })
      return
    if len(parts) >= 2 and parts[0] == "jobs":
      self.handle_job_get(parts[1], parts[2:], query, auth)
      return
    self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

  def do_POST(self) -> None:
    if not self.request_metadata_allowed():
      self.send_request_metadata_error()
      return
    parsed = urlparse(self.path)
    query = parse_qs(parsed.query)
    auth = self.authenticate(query)
    if not auth:
      self.send_auth_error()
      return

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    try:
      if parts == ["stream-tickets"]:
        self.read_json_body()
        ticket = self.broker.issue_stream_ticket(auth)
        host = str(self.server.server_address[0])
        if host in ("", "0.0.0.0", "::"):
          host = "127.0.0.1"
        if ":" in host and not host.startswith("["):
          host = f"[{host}]"
        port = int(self.server.server_address[1])
        self.send_json({
            "ok": True,
            "ticket": ticket.token,
            "expiresAt": iso_time(ticket.expires_at),
            "expiresAtEpoch": ticket.expires_at,
            "webSocketUrl": (
                f"ws://{host}:{port}/ws?ticket={quote(ticket.token)}"),
        }, HTTPStatus.CREATED)
        return
      if parts == ["apps"]:
        if not self.require_admin(auth):
          return
        app = self.broker.register_app(self.read_json_body())
        self.send_json({"ok": True, "app": app.summary(True)}, HTTPStatus.CREATED)
        return
      if parts == ["schemas"]:
        schema = self.broker.save_schema(
            self.read_json_body(), "admin" if auth.is_admin else auth.app_id)
        self.send_json({"ok": True, "schema": schema.summary()}, HTTPStatus.CREATED)
        return
      if parts == ["feeds"]:
        schema = self.broker.save_feed(
            self.read_json_body(), "admin" if auth.is_admin else auth.app_id)
        self.send_json({
            "ok": True,
            "feed": self.broker.feed_summary(schema, include_schema=True),
        }, HTTPStatus.CREATED)
        return
      if parts == ["service", "media-settings"]:
        if not self.require_admin(auth):
          return
        settings = self.broker.save_media_settings(self.read_json_body())
        self.send_json({"ok": True, "settings": settings})
        return
      if parts == ["service", "privacy-settings"]:
        if not self.require_admin(auth):
          return
        body = self.read_json_body()
        media = self.broker.save_media_settings(
            body.get("media", body)) if ("media" in body or any(
                key in body for key in (
                    "cameraSource", "microphoneSource",
                    "fakeCameraBackend", "mediaMode"))) else (
                        self.broker.get_media_settings())
        location = self.broker.save_location_settings(
            body.get("location", body)) if ("location" in body or any(
                key in body for key in (
                    "locationSource", "latitude", "longitude",
                    "accuracy"))) else self.broker.get_location_settings()
        self.send_json({
            "ok": True,
            "schemaVersion": PRIVACY_SETTINGS_SCHEMA_VERSION,
            "media": media,
            "location": location,
            "rules": self.broker.get_privacy_rules(),
        })
        return
      if parts == ["service", "location-settings"]:
        if not self.require_admin(auth):
          return
        settings = self.broker.save_location_settings(self.read_json_body())
        self.send_json({"ok": True, "settings": settings})
        return
      if parts == ["service", "privacy-rules"]:
        if not self.require_admin(auth):
          return
        rule = self.broker.save_privacy_rule(self.read_json_body())
        self.send_json({"ok": True, "rule": rule})
        return
      if parts == ["service", "loop-video"]:
        if not self.require_admin(auth):
          return
        settings = self.read_loop_video_upload()
        self.send_json({"ok": True, "settings": settings}, HTTPStatus.CREATED)
        return
      if parts == ["service", "restart"]:
        if not self.require_admin(auth):
          return
        self.read_json_body()
        self.send_json(self.schedule_service_restart(), HTTPStatus.ACCEPTED)
        return
      if parts == ["jobs"]:
        body = self.read_json_body()
        if auth.is_admin:
          body.setdefault("app_id", "admin")
        job = self.broker.create_job(
            body, app_id_override="" if auth.is_admin else auth.app_id)
        self.send_json({"ok": True, "job": job.summary(False)}, HTTPStatus.ACCEPTED)
        return
      if len(parts) == 3 and parts[0] == "feeds" and parts[2] == "run":
        job = self.broker.run_feed(
            parts[1],
            self.read_json_body(),
            app_id_override="" if auth.is_admin else auth.app_id)
        self.send_json({
            "ok": True,
            "feed": self.broker.get_feed(
                parts[1], "" if auth.is_admin else auth.app_id),
            "job": job.summary(False),
        }, HTTPStatus.ACCEPTED)
        return
      if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "preview-schema":
        job = self.require_authorized_job(parts[1], auth)
        if not job:
          return
        self.send_json(self.broker.preview_schema_for_job(job, self.read_json_body()))
        return
      if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "control":
        job = self.require_authorized_job(parts[1], auth)
        if not job:
          return
        self.send_json(self.broker.control_job(job, self.read_json_body()))
        return
      if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "resume":
        job = self.require_authorized_job(parts[1], auth)
        if not job:
          return
        job = self.broker.resume_job(job.id)
        self.send_json({"ok": True, "job": job.summary(False)})
        return
      if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "stop":
        job = self.require_authorized_job(parts[1], auth)
        if not job:
          return
        job = self.broker.stop_job(job.id)
        self.send_json({"ok": True, "job": job.summary(False)})
        return
      if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "retry":
        job = self.require_authorized_job(parts[1], auth)
        if not job:
          return
        retried = self.broker.retry_job(job.id)
        self.send_json(
            {"ok": True, "job": retried.summary(False)}, HTTPStatus.ACCEPTED)
        return
      if parts == ["jobs", "close-completed-tabs"]:
        self.read_json_body()
        self.send_json(self.broker.close_completed_tabs(
            "" if auth.is_admin else auth.app_id))
        return
      if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "close-tab":
        job = self.require_authorized_job(parts[1], auth)
        if not job:
          return
        job = self.broker.close_job_tab(job.id)
        self.send_json({"ok": True, "job": job.summary(False)})
        return
      self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
    except ValueError as error:
      self.send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
    except Exception as error:  # pylint: disable=broad-except
      self.send_json({"ok": False, "error": str(error)},
                     HTTPStatus.INTERNAL_SERVER_ERROR)

  def do_DELETE(self) -> None:
    if not self.request_metadata_allowed():
      self.send_request_metadata_error()
      return
    parsed = urlparse(self.path)
    query = parse_qs(parsed.query)
    auth = self.authenticate(query)
    if not auth:
      self.send_auth_error()
      return
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    try:
      if len(parts) == 2 and parts[0] == "schemas":
        if not self.broker.delete_schema(
            parts[1], "" if auth.is_admin else auth.app_id):
          self.send_json({"ok": False, "error": "schema not found"}, HTTPStatus.NOT_FOUND)
          return
        self.send_json({"ok": True, "deleted": parts[1]})
        return
      if len(parts) == 2 and parts[0] == "feeds":
        if not self.broker.delete_feed(
            parts[1], "" if auth.is_admin else auth.app_id):
          self.send_json({"ok": False, "error": "feed not found"}, HTTPStatus.NOT_FOUND)
          return
        self.send_json({"ok": True, "deleted": parts[1]})
        return
      if len(parts) == 3 and parts[:2] == ["service", "privacy-rules"]:
        if not self.require_admin(auth):
          return
        if not self.broker.delete_privacy_rule(parts[2]):
          self.send_json({"ok": False, "error": "privacy rule not found"},
                         HTTPStatus.NOT_FOUND)
          return
        self.send_json({"ok": True, "deleted": parts[2]})
        return
      self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
    except ValueError as error:
      self.send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)

  def handle_job_get(
      self,
      job_id: str,
      tail: list[str],
      query: dict[str, list[str]],
      auth: BrokerAuth,
  ) -> None:
    job = self.require_authorized_job(job_id, auth)
    if not job:
      return
    if not tail:
      self.send_json({"ok": True, "job": job.summary(False)})
      return
    if tail == ["items"]:
      since = parse_int(query.get("since", ["0"])[0], 0)
      limit = parse_int(query.get("limit", ["0"])[0], 0)
      with job.lock:
        items = [item for item in job.items if int(item.get("index", 0)) >= since]
        if limit > 0:
          items = items[:limit]
      self.send_json({"ok": True, "jobId": job.id, "count": len(items), "items": items})
      return
    if tail == ["events"]:
      self.stream_job_events(job, query)
      return

    file_map = {
        "items.jsonl": "itemsJsonl",
        "items.csv": "itemsCsv",
        "latest.html": "latestHtml",
        "latest.json": "latestJson",
        "feed.json": "feedJson",
        "feed.rss": "feedRss",
        "feed.atom": "feedAtom",
        "visible_text.txt": "visibleText",
        "raw.html": "rawHtml",
        "snapshot.mhtml": "snapshotMhtml",
        "manifest.json": "manifest",
        "events.jsonl": "eventsJsonl",
      }
    if len(tail) == 1 and tail[0] in file_map:
      if tail[0] in {
          "items.csv", "latest.html", "latest.json", "feed.json",
          "feed.rss", "feed.atom", "manifest.json",
      }:
        with job.lock:
          export_path = job.exports.get(file_map[tail[0]], "")
          needs_flush = (
              len(job.items) != job.last_output_item_count or
              not export_path or not Path(export_path).is_file())
        if needs_flush:
          write_outputs(job, force=True)
      with job.lock:
        path = job.exports.get(file_map[tail[0]], "")
      self.send_file(Path(path) if path else None)
      return
    self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

  def handle_apps_get(self, parts: list[str]) -> None:
    if parts == ["apps"]:
      self.send_json({
          "ok": True,
          "apps": [app.summary(False) for app in self.broker.list_apps()],
      })
      return
    if len(parts) == 2 and parts[0] == "apps":
      app_id = sanitize_path_part(parts[1])
      app = self.broker.get_app(app_id)
      if not app:
        self.send_json({"ok": False, "error": "app not found"}, HTTPStatus.NOT_FOUND)
        return
      self.send_json({"ok": True, "app": app.summary(False)})
      return
    if len(parts) == 3 and parts[0] == "apps" and parts[2] == "jobs":
      app_id = sanitize_path_part(parts[1])
      app = self.broker.get_app(app_id)
      if not app:
        self.send_json({"ok": False, "error": "app not found"}, HTTPStatus.NOT_FOUND)
        return
      self.send_json({
          "ok": True,
          "app": app.summary(False),
          "jobs": [job.summary(False) for job in self.broker.list_jobs(app_id)],
      })
      return
    self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

  def handle_schemas_get(self, parts: list[str], auth: BrokerAuth) -> None:
    app_scope = "" if auth.is_admin else auth.app_id
    if parts == ["schemas"]:
      self.send_json({
          "ok": True,
          "schemas": [
              schema.summary() for schema in self.broker.list_schemas(app_scope)
          ],
      })
      return
    if len(parts) == 2 and parts[0] == "schemas":
      schema = self.broker.get_schema(parts[1], app_scope)
      if not schema:
        self.send_json({"ok": False, "error": "schema not found"}, HTTPStatus.NOT_FOUND)
        return
      self.send_json({"ok": True, "schema": schema.summary()})
      return
    self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

  def handle_feeds_get(self, parts: list[str], auth: BrokerAuth) -> None:
    app_scope = "" if auth.is_admin else auth.app_id
    if parts == ["feeds"]:
      self.send_json({"ok": True, "feeds": self.broker.list_feeds(app_scope)})
      return
    if len(parts) == 2 and parts[0] == "feeds":
      feed = self.broker.get_feed(parts[1], app_scope)
      if not feed:
        self.send_json({"ok": False, "error": "feed not found"}, HTTPStatus.NOT_FOUND)
        return
      self.send_json({"ok": True, "feed": feed})
      return
    if len(parts) == 4 and parts[0] == "feeds" and parts[2] == "latest":
      schema = self.broker.get_schema(parts[1], app_scope)
      if not schema:
        self.send_json({"ok": False, "error": "feed not found"}, HTTPStatus.NOT_FOUND)
        return
      job = self.broker.latest_job_for_schema(schema.id, schema.app_id)
      if not job:
        self.send_json({"ok": False, "error": "feed has no runs yet"}, HTTPStatus.NOT_FOUND)
        return
      file_key = output_file_key(parts[3])
      if not file_key:
        self.send_json({"ok": False, "error": "unknown feed output"}, HTTPStatus.NOT_FOUND)
        return
      with job.lock:
        path = job.exports.get(file_key, "")
      self.send_file(Path(path) if path else None)
      return
    self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

  def stream_job_events(
      self,
      job: Job,
      query: dict[str, list[str]],
  ) -> None:
    cursor_value = (
        self.headers.get("Last-Event-ID", "").strip() or
        query.get("afterSequence", query.get("after_sequence", [""]))[0])
    has_sequence_cursor = bool(cursor_value)
    sequence = max(0, parse_int(cursor_value, 0))
    since_value = query.get("since", ["latest"])[0]
    delivered_item_indices: set[int] = set()
    self.send_response(HTTPStatus.OK)
    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
    self.send_header("Cache-Control", "no-store")
    self.send_header("Connection", "keep-alive")
    self.send_header("X-Accel-Buffering", "no")
    self.end_headers()

    # Preserve the original item-index cursor for existing clients. New
    # clients should use Last-Event-ID/afterSequence for exact replay.
    if not has_sequence_cursor:
      sequence = self.broker.event_hub.latest_sequence(job.id)
      with job.lock:
        items = list(job.items)
      next_index = (
          len(items) if since_value == "latest" else
          max(0, parse_int(since_value, 0)))
      for item in items:
        index = int(item.get("index", 0))
        if index < next_index:
          continue
        try:
          self.write_sse(
              "item", {"job_id": job.id, "index": index, "item": item})
        except (BrokenPipeError, ConnectionResetError):
          return
        delivered_item_indices.add(index)
    while True:
      try:
        events, expired = self.broker.event_hub.wait_after(
            job.id, sequence, STREAM_HEARTBEAT_SECONDS)
        if expired:
          latest = self.broker.event_hub.latest_sequence(job.id)
          self.write_sse("resync_required", {
              "jobId": job.id,
              "afterSequence": sequence,
              "latestSequence": latest,
          }, event_id=latest)
          sequence = latest
          events = []
        for event in events:
          event_sequence = int(event["sequence"])
          event_type = str(event["type"])
          data = event.get("data", {})
          if event_type == "item":
            index = int(data.get("index", 0))
            if index in delivered_item_indices:
              sequence = event_sequence
              continue
            delivered_item_indices.add(index)
            payload = {
                "job_id": job.id,
                "index": index,
                "item": data.get("item", {}),
                "sequence": event_sequence,
            }
          else:
            payload = event
          self.write_sse(event_type, payload, event_id=event_sequence)
          sequence = event_sequence
        with job.lock:
          status = job.status
        latest = self.broker.event_hub.latest_sequence(job.id)
        if status in TERMINAL_STATUSES and sequence >= latest:
          return
        if not events:
          self.write_sse("heartbeat", {
              "jobId": job.id,
              "status": status,
              "sequence": sequence,
              "timeEpoch": time.time(),
          })
      except (BrokenPipeError, ConnectionResetError):
        return

  def handle_websocket(self, auth: BrokerAuth) -> None:
    if self.headers.get("Upgrade", "").lower() != "websocket":
      self.send_json({"ok": False, "error": "WebSocket upgrade required"},
                     HTTPStatus.UPGRADE_REQUIRED)
      return
    key = self.headers.get("Sec-WebSocket-Key", "").strip()
    if not key or self.headers.get("Sec-WebSocket-Version", "") != "13":
      self.send_json({"ok": False, "error": "invalid WebSocket handshake"},
                     HTTPStatus.BAD_REQUEST)
      return

    # Stream messages are intentionally small and latency-sensitive. Avoid a
    # delayed-ACK/Nagle interaction adding tens of milliseconds to control and
    # event frames on loopback.
    with contextlib.suppress(OSError):
      self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
    self.send_header("Upgrade", "websocket")
    self.send_header("Connection", "Upgrade")
    self.send_header("Sec-WebSocket-Accept", websocket_accept_value(key))
    self.end_headers()
    self.wfile.flush()
    self.close_connection = True

    subscription = self.broker.event_hub.subscribe(auth)
    send_lock = threading.Lock()
    writer_stopped = threading.Event()

    def send_json_message(value: dict[str, Any]) -> None:
      payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
      with send_lock:
        send_websocket_frame(self.connection, payload)

    def writer() -> None:
      try:
        send_json_message({
            "type": "ready",
            "heartbeatSeconds": STREAM_HEARTBEAT_SECONDS,
            "replayEventsPerJob": STREAM_REPLAY_EVENTS_PER_JOB,
        })
        while not subscription.closed:
          message = subscription.pop(STREAM_HEARTBEAT_SECONDS)
          if message is not None:
            send_json_message(message)
            continue
          if subscription.closed:
            break
          send_json_message({"type": "heartbeat", "timeEpoch": time.time()})
        if subscription.close_reason == "slow_consumer":
          with send_lock:
            send_websocket_frame(
                self.connection,
                websocket_close_payload(1013, "slow consumer; reconnect with cursor"),
                opcode=0x8)
      except (BrokenPipeError, ConnectionResetError, OSError):
        pass
      finally:
        writer_stopped.set()
        with contextlib.suppress(OSError):
          self.connection.shutdown(socket.SHUT_RDWR)

    writer_thread = threading.Thread(
        target=writer,
        name=f"hardened-app-stream-{auth.app_id or auth.kind}",
        daemon=True)
    writer_thread.start()
    try:
      while not subscription.closed:
        fin, opcode, payload = receive_websocket_frame(self.connection)
        if opcode == 0x8:
          break
        if opcode == 0x9:
          with send_lock:
            send_websocket_frame(self.connection, payload, opcode=0xA)
          continue
        if opcode == 0xA:
          continue
        if not fin or opcode != 0x1:
          subscription.enqueue_control({
              "type": "error",
              "code": "unsupported_frame",
          })
          continue
        try:
          message = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
          subscription.enqueue_control({
              "type": "error", "code": "invalid_json"})
          continue
        if not isinstance(message, dict):
          subscription.enqueue_control({
              "type": "error", "code": "invalid_message"})
          continue
        self.handle_websocket_message(subscription, message)
    except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
      pass
    finally:
      self.broker.event_hub.unsubscribe(subscription)
      with contextlib.suppress(OSError):
        self.connection.shutdown(socket.SHUT_RDWR)
      writer_stopped.wait(timeout=2)
      writer_thread.join(timeout=2)

  def handle_websocket_message(
      self,
      subscription: StreamSubscription,
      message: dict[str, Any],
  ) -> None:
    message_type = str(message.get("type") or "")
    if message_type == "unsubscribe":
      job_ids = message.get("jobIds", [])
      if not isinstance(job_ids, list):
        job_ids = []
      for job_id in job_ids:
        self.broker.event_hub.remove_job(subscription, str(job_id))
      subscription.enqueue_control({
          "type": "unsubscribed", "jobIds": [str(value) for value in job_ids]})
      return
    if message_type != "subscribe":
      subscription.enqueue_control({
          "type": "error", "code": "unsupported_message_type"})
      return

    requested = message.get("subscriptions", [])
    if not isinstance(requested, list):
      subscription.enqueue_control({
          "type": "error", "code": "subscriptions_must_be_a_list"})
      return
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    jobs: list[tuple[Job, int]] = []
    for value in requested[:256]:
      if not isinstance(value, dict):
        continue
      job_id = str(value.get("jobId") or "")
      after_sequence = max(0, parse_int(value.get("afterSequence"), 0))
      job = self.broker.get_job(job_id)
      if not job:
        rejected.append({"jobId": job_id, "reason": "not_found"})
        continue
      if not subscription.auth.is_admin and job.app_id != subscription.auth.app_id:
        rejected.append({"jobId": job_id, "reason": "forbidden"})
        continue
      accepted.append({"jobId": job_id, "afterSequence": after_sequence})
      jobs.append((job, after_sequence))
    subscription.enqueue_control({
        "type": "subscribed", "accepted": accepted, "rejected": rejected})
    for job, after_sequence in jobs:
      self.broker.event_hub.add_job(subscription, job, after_sequence)

  def write_sse(
      self,
      event: str,
      data: dict[str, Any],
      event_id: int | None = None,
  ) -> None:
    payload = json.dumps(data, ensure_ascii=False)
    if event_id is not None:
      self.wfile.write(f"id: {event_id}\n".encode("utf-8"))
    self.wfile.write(f"event: {event}\n".encode("utf-8"))
    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
    self.wfile.flush()

  def index_document(self, auth: BrokerAuth) -> dict[str, Any]:
    return {
        "ok": True,
        "name": "Hardened Scrape Broker",
        "outputRoot": str(self.broker.config.output_root),
        "stateDir": str(self.broker.state_dir),
        "auth": {"kind": auth.kind, "appId": auth.app_id},
        "endpoints": [
            "GET /ui",
            "POST /stream-tickets",
            "GET /ws?ticket=<single-use-ticket>",
            "POST /apps",
            "GET /apps",
            "GET /apps/<app_id>",
            "GET /apps/<app_id>/jobs",
            "GET /feeds",
            "POST /feeds",
            "GET /feeds/<feed_id>",
            "DELETE /feeds/<feed_id>",
            "POST /feeds/<feed_id>/run",
            "GET /feeds/<feed_id>/latest/feed.json",
            "GET /feeds/<feed_id>/latest/feed.rss",
            "GET /feeds/<feed_id>/latest/feed.atom",
            "GET /feeds/<feed_id>/latest/items.csv",
            "GET /service/media-settings",
            "POST /service/media-settings",
            "GET /service/privacy-settings",
            "POST /service/privacy-settings",
            "POST /service/loop-video",
            "GET /service/location-settings",
            "POST /service/location-settings",
            "GET /service/privacy-rules",
            "POST /service/privacy-rules",
            "DELETE /service/privacy-rules/<rule_id>",
            "POST /service/restart",
            "GET /schemas",
            "POST /schemas",
            "GET /schemas/<schema_id>",
            "DELETE /schemas/<schema_id>",
            "POST /jobs",
            "GET /jobs",
            "GET /jobs/<job_id>",
            "GET /jobs/<job_id>/items",
            "GET /jobs/<job_id>/items.jsonl",
            "GET /jobs/<job_id>/items.csv",
            "GET /jobs/<job_id>/latest.html",
            "GET /jobs/<job_id>/latest.json",
            "GET /jobs/<job_id>/feed.json",
            "GET /jobs/<job_id>/feed.rss",
            "GET /jobs/<job_id>/feed.atom",
            "GET /jobs/<job_id>/visible_text.txt",
            "GET /jobs/<job_id>/raw.html",
            "GET /jobs/<job_id>/snapshot.mhtml",
            "GET /jobs/<job_id>/manifest.json",
            "GET /jobs/<job_id>/events",
            "GET /jobs/<job_id>/events.jsonl",
            "GET /events.jsonl",
            "POST /jobs/<job_id>/preview-schema",
            "POST /jobs/<job_id>/control",
            "POST /jobs/<job_id>/resume",
            "POST /jobs/<job_id>/stop",
            "POST /jobs/<job_id>/retry",
            "POST /jobs/<job_id>/close-tab",
            "POST /jobs/close-completed-tabs",
        ],
    }

  def authenticate(self, query: dict[str, list[str]]) -> BrokerAuth | None:
    if self.broker.config.no_auth:
      return BrokerAuth("no-auth")
    if self.admin_authorized(query):
      return BrokerAuth("admin")
    app_id = sanitize_path_part(self.headers.get("X-Hardened-App-Id", ""))
    app_secret = self.headers.get("X-Hardened-App-Secret", "")
    if self.broker.authenticate_app(app_id, app_secret):
      return BrokerAuth("app", app_id)
    return None

  def request_metadata_allowed(self) -> bool:
    """Reject DNS rebinding and cross-origin browser access in no-auth mode."""
    if not self.broker.config.no_auth:
      return True
    port = int(self.server.server_address[1])
    return (
        loopback_host_header_allowed(self.headers.get("Host", ""), port)
        and loopback_origin_allowed(self.headers.get("Origin", ""), port)
    )

  def send_request_metadata_error(self) -> None:
    self.send_json({
        "ok": False,
        "error": "no-auth broker accepts only native clients or its own loopback origin",
        "code": "forbidden_request_origin",
    }, HTTPStatus.FORBIDDEN)

  def admin_authorized(self, query: dict[str, list[str]]) -> bool:
    token = self.broker.config.token
    if not token:
      return True
    auth = self.headers.get("Authorization", "")
    if secrets.compare_digest(auth, f"Bearer {token}"):
      return True
    query_token = query.get("token", [""])[0]
    return secrets.compare_digest(query_token, token)

  def send_auth_error(self) -> None:
    self.send_json({
        "ok": False,
        "error": "missing or invalid broker token/app credentials",
    }, HTTPStatus.UNAUTHORIZED)

  def require_admin(self, auth: BrokerAuth) -> bool:
    if auth.is_admin:
      return True
    self.send_json({"ok": False, "error": "admin token required"}, HTTPStatus.FORBIDDEN)
    return False

  def require_authorized_job(self, job_id: str, auth: BrokerAuth) -> Job | None:
    job = self.broker.get_job(job_id)
    if not job:
      self.send_json({"ok": False, "error": "job not found"}, HTTPStatus.NOT_FOUND)
      return None
    if auth.is_admin or job.app_id == auth.app_id:
      return job
    self.send_json({"ok": False, "error": "job belongs to another app"},
                   HTTPStatus.FORBIDDEN)
    return None

  def read_json_body(self) -> dict[str, Any]:
    length = min(int(self.headers.get("Content-Length", "0") or "0"), 1024 * 1024)
    data = self.rfile.read(length) if length else b"{}"
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
      raise ValueError("request body must be a JSON object")
    return value

  def read_loop_video_upload(self) -> dict[str, Any]:
    length_text = self.headers.get("Content-Length", "")
    if not length_text:
      raise ValueError("Content-Length is required")
    try:
      length = int(length_text)
    except ValueError as error:
      raise ValueError("invalid Content-Length") from error
    if length <= 0:
      raise ValueError("uploaded loop video is empty")
    if length > MAX_LOOP_VIDEO_UPLOAD_BYTES:
      raise ValueError(
          f"uploaded loop video is too large; limit is "
          f"{MAX_LOOP_VIDEO_UPLOAD_BYTES // (1024 * 1024)} MiB")

    filename = unquote(self.headers.get("X-Hardened-Filename", "")).strip()
    if not filename:
      filename = "loop.y4m"
    destination = self.broker.imported_loop_video_path(filename)
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(4)}.tmp")
    destination.parent.mkdir(parents=True, exist_ok=True)

    remaining = length
    header = b""
    try:
      with temporary.open("wb") as file:
        while remaining:
          chunk = self.rfile.read(min(1024 * 1024, remaining))
          if not chunk:
            raise ValueError("upload ended before Content-Length bytes arrived")
          if len(header) < len(Y4M_MAGIC):
            header += chunk[:len(Y4M_MAGIC) - len(header)]
          file.write(chunk)
          remaining -= len(chunk)
      if header != Y4M_MAGIC:
        raise ValueError(
            "Chromium fake camera loop files must be Y4M. Convert the source "
            "video to .y4m first.")
      temporary.replace(destination)
      return self.broker.use_imported_loop_video(filename, destination)
    except Exception:
      with contextlib.suppress(OSError):
        temporary.unlink()
      raise

  def schedule_service_restart(self) -> dict[str, Any]:
    service_script = Path(__file__).with_name("hardened_scrape_service.py")
    if not service_script.exists():
      raise ValueError(f"service helper not found: {service_script}")
    host, port = self.server.server_address[:2]
    broker_host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else str(host)
    broker_url = f"http://{broker_host}:{int(port)}"
    command = [
        sys.executable,
        str(service_script),
        "--cdp",
        "auto",
        "--broker",
        broker_url,
        "--output-root",
        str(self.broker.config.output_root),
        "--timeout-seconds",
        "120",
    ]
    if self.broker.config.no_auth:
      command.append("--no-auth")
    command.extend(["--json", "restart"])
    log_path = self.broker.state_dir / "service_restart.log"

    def runner() -> None:
      time.sleep(0.5)
      log_path.parent.mkdir(parents=True, exist_ok=True)
      with log_path.open("ab", buffering=0) as log:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

    threading.Thread(target=runner, daemon=True).start()
    return {
        "ok": True,
        "message": "restart scheduled; reconnect to the UI in a few seconds",
        "command": command,
        "log": str(log_path),
    }

  def send_json(
      self,
      value: dict[str, Any],
      status: HTTPStatus = HTTPStatus.OK,
  ) -> None:
    body = json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)

  def send_broker_ui(self) -> None:
    body = MANAGEMENT_UI_HTML.encode("utf-8")
    self.send_response(HTTPStatus.OK)
    self.send_header("Content-Type", "text/html; charset=utf-8")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)

  def send_file(self, path: Path | None) -> None:
    if not path or not path.exists() or not path.is_file():
      self.send_json({"ok": False, "error": "file not found"}, HTTPStatus.NOT_FOUND)
      return
    data = path.read_bytes()
    content_type = {
        ".html": "text/html",
        ".json": "application/json",
        ".jsonl": "application/x-ndjson",
        ".csv": "text/csv",
        ".rss": "application/rss+xml",
        ".atom": "application/atom+xml",
        ".txt": "text/plain",
        ".png": "image/png",
        ".mhtml": "multipart/related",
    }.get(path.suffix, "application/octet-stream")
    self.send_response(HTTPStatus.OK)
    self.send_header("Content-Type", f"{content_type}; charset=utf-8")
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)


LEGACY_BROKER_UI_HTML = r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hardened Scrape Broker</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
    body { margin: 0; background: Canvas; color: CanvasText; }
    main { max-width: 1220px; margin: 0 auto; padding: 24px; }
    h1 { margin: 0 0 4px; font-size: 26px; }
    h2 { margin: 0 0 12px; font-size: 19px; }
    h3 { margin: 16px 0 8px; font-size: 15px; }
    input, textarea, button, select {
      box-sizing: border-box; font: inherit; padding: 9px 10px; border-radius: 8px;
      border: 1px solid color-mix(in srgb, CanvasText 22%, transparent);
      background: Canvas; color: CanvasText;
    }
    textarea { min-height: 92px; resize: vertical; }
    button { cursor: pointer; }
    button:hover { border-color: color-mix(in srgb, CanvasText 42%, transparent); }
    label { display: grid; gap: 6px; font-size: 13px; opacity: .9; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; }
    .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    .card {
      border: 1px solid color-mix(in srgb, CanvasText 16%, transparent);
      border-radius: 14px; padding: 16px; margin-top: 14px;
    }
    .muted { opacity: .7; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .secret, .wrap { overflow-wrap: anywhere; }
    table { width: 100%; border-collapse: collapse; margin-top: 10px; }
    th, td { text-align: left; vertical-align: top; padding: 9px 7px; border-bottom: 1px solid color-mix(in srgb, CanvasText 12%, transparent); }
    th { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; opacity: .75; }
    td { font-size: 14px; }
    a { color: LinkText; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 360px; overflow: auto; padding: 12px; border-radius: 10px; background: color-mix(in srgb, CanvasText 8%, transparent); }
    .status { display: inline-block; padding: 2px 7px; border-radius: 999px; background: color-mix(in srgb, CanvasText 10%, transparent); }
    .error { color: #c62828; }
    .ok { color: #2e7d32; }
  </style>
</head>
<body>
<main>
  <h1>Hardened Scrape Broker</h1>
  <div class="muted">Local UI for schema-backed extraction, live tab controls, app registration, and output links.</div>

  <section class="card">
    <h2>Broker</h2>
    <div class="grid">
      <label>Broker URL
        <input id="brokerUrl" value="http://127.0.0.1:8877">
      </label>
      <label>Bearer token
        <input id="token" placeholder="Leave empty in no-auth mode">
      </label>
    </div>
    <div class="row" style="margin-top:12px">
      <button id="saveToken">Save locally</button>
      <button id="refresh">Refresh</button>
      <span id="status" class="muted"></span>
    </div>
  </section>

  <section class="card">
    <h2>Submit job</h2>
    <div class="grid">
      <label>URL
        <input id="jobUrl" placeholder="https://example.com">
      </label>
      <label>App id
        <input id="jobAppId" placeholder="Optional owner label">
      </label>
      <label>Schema id
        <input id="jobSchemaId" placeholder="Optional saved schema id">
      </label>
      <label>Max items
        <input id="maxItems" type="number" min="0" max="5000" value="500">
      </label>
    </div>
    <div class="row" style="margin-top:12px">
      <button id="submitJob">Submit job</button>
    </div>
  </section>

  <section class="card">
    <h2>Selector schema builder</h2>
    <div class="grid">
      <label>Live job id for picker/preview
        <input id="schemaJobId" placeholder="Paste running job id">
      </label>
      <label>Schema name
        <input id="schemaName" placeholder="Example: X search feed">
      </label>
      <label>Schema id
        <input id="schemaId" placeholder="x_search_feed">
      </label>
      <label>Item root CSS selector
        <input id="schemaRoot" placeholder="article, .post, [role='article']">
      </label>
    </div>
    <label style="margin-top:12px">Fields, one per line: name | selector | mode | multiple | required
      <textarea id="schemaFields">text |  | text | false | true
author | .author, [rel="author"] | text | false | false
time | time, [datetime] | datetime | false | false
permalink | a[href] | href | false | false</textarea>
    </label>
    <div class="grid" style="margin-top:12px">
      <label>New field name
        <input id="pickFieldName" value="text">
      </label>
      <label>New field mode
        <select id="pickFieldMode">
          <option value="text">text</option>
          <option value="href">href</option>
          <option value="src">src</option>
          <option value="datetime">datetime</option>
          <option value="html">html</option>
          <option value="outer_html">outer_html</option>
        </select>
      </label>
    </div>
    <div class="row" style="margin-top:12px">
      <button id="pickRoot">Pick item root in tab</button>
      <button id="pickField">Pick field in tab</button>
      <button id="previewSchema">Preview schema</button>
      <button id="saveSchema">Save schema</button>
    </div>
    <pre id="schemaPreview" class="muted">Preview appears here.</pre>
  </section>

  <section class="card">
    <h2>Register app</h2>
    <div class="grid">
      <label>Name
        <input id="appName" placeholder="Example: research tool">
      </label>
      <label>Optional app id
        <input id="appId" placeholder="research_tool">
      </label>
    </div>
    <label style="margin-top:12px">Description
      <textarea id="appDescription" placeholder="What this local app uses the broker for"></textarea>
    </label>
    <div class="row" style="margin-top:12px">
      <button id="registerApp">Register</button>
      <span id="registeredSecret" class="mono secret muted"></span>
    </div>
  </section>

  <section class="card">
    <h2>Saved schemas</h2>
    <div id="schemas"></div>
  </section>

  <section class="card">
    <div class="row" style="justify-content:space-between">
      <h2>Apps</h2>
      <span class="muted">Secrets are only shown during registration.</span>
    </div>
    <div id="apps"></div>
  </section>

  <section class="card">
    <h2>Jobs</h2>
    <div id="jobs"></div>
  </section>
</main>

<script>
const $ = id => document.getElementById(id);
const state = {
  get base() { return $('brokerUrl').value.replace(/\/+$/, ''); },
  get token() { return $('token').value.trim(); },
};
$('brokerUrl').value = localStorage.getItem('hardenedBrokerUrl') || location.origin;
$('token').value = localStorage.getItem('hardenedBrokerToken') || '';

function setStatus(text, ok = true) {
  $('status').textContent = text;
  $('status').className = ok ? 'ok' : 'error';
}

function authHeaders() {
  return state.token ? {Authorization: `Bearer ${state.token}`} : {};
}

async function api(path, options = {}) {
  const response = await fetch(`${state.base}${path}`, {
    ...options,
    headers: {
      ...authHeaders(),
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = {ok: false, error: text}; }
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || response.statusText);
  }
  return data;
}

function tokenQuery() {
  return state.token ? `?token=${encodeURIComponent(state.token)}` : '';
}

function outputLinks(job) {
  const files = [
    ['latest.html', 'HTML'],
    ['latest.json', 'JSON'],
    ['items.jsonl', 'JSONL'],
    ['items.csv', 'CSV'],
    ['feed.json', 'JSON Feed'],
    ['feed.rss', 'RSS'],
    ['feed.atom', 'Atom'],
    ['visible_text.txt', 'Visible text'],
    ['events.jsonl', 'Events'],
    ['raw.html', 'Raw HTML'],
    ['snapshot.mhtml', 'MHTML'],
    ['manifest.json', 'Manifest'],
  ];
  return files.map(([file, label]) =>
    `<a href="${state.base}/jobs/${encodeURIComponent(job.id)}/${file}${tokenQuery()}" target="_blank" rel="noreferrer">${label}</a>`
  ).join(' · ');
}

function parseSchemaFromForm() {
  const fields = $('schemaFields').value.split('\n').map(line => {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) return null;
    const parts = trimmed.split('|').map(part => part.trim());
    return {
      name: parts[0] || '',
      selector: parts[1] || '',
      mode: parts[2] || 'text',
      multiple: /^(1|true|yes|on)$/i.test(parts[3] || ''),
      required: /^(1|true|yes|on)$/i.test(parts[4] || ''),
    };
  }).filter(Boolean);
  return {
    id: $('schemaId').value.trim(),
    name: $('schemaName').value.trim() || $('schemaId').value.trim(),
    sourceUrl: $('jobUrl').value.trim(),
    itemRoot: $('schemaRoot').value.trim(),
    fields,
  };
}

function appendField(name, selector, mode) {
  const line = `${name} | ${selector} | ${mode || 'text'} | false | false`;
  $('schemaFields').value = `${$('schemaFields').value.trim()}\n${line}`.trim();
}

function renderApps(apps) {
  if (!apps.length) {
    $('apps').innerHTML = '<div class="muted">No registered apps yet.</div>';
    return;
  }
  $('apps').innerHTML = `<table><thead><tr><th>App</th><th>Description</th><th>Created</th></tr></thead><tbody>${
    apps.map(app => `<tr>
      <td><div>${escapeHtml(app.name)}</div><div class="mono muted">${escapeHtml(app.id)}</div></td>
      <td>${escapeHtml(app.description || '')}</td>
      <td>${escapeHtml(app.createdAt || '')}</td>
    </tr>`).join('')
  }</tbody></table>`;
}

function renderSchemas(schemas) {
  if (!schemas.length) {
    $('schemas').innerHTML = '<div class="muted">No saved schemas yet.</div>';
    return;
  }
  $('schemas').innerHTML = `<table><thead><tr><th>Schema</th><th>Root</th><th>Fields</th><th>Actions</th></tr></thead><tbody>${
    schemas.map(schema => `<tr>
      <td><div>${escapeHtml(schema.name)}</div><div class="mono muted">${escapeHtml(schema.id)}</div><div class="muted wrap">${escapeHtml(schema.sourceUrl || schema.host || '')}</div></td>
      <td class="mono wrap">${escapeHtml(schema.itemRoot || '')}</td>
      <td>${(schema.fields || []).map(field => `<div><span class="mono">${escapeHtml(field.name)}</span>: ${escapeHtml(field.selector || '(root)')} / ${escapeHtml(field.mode || 'text')}</div>`).join('')}</td>
      <td class="row">
        <button data-schema-use="${escapeHtml(schema.id)}">Use</button>
        <button data-schema-load="${escapeHtml(schema.id)}">Load</button>
        <button data-schema-delete="${escapeHtml(schema.id)}">Delete</button>
      </td>
    </tr>`).join('')
  }</tbody></table>`;
}

function renderJobs(jobs) {
  if (!jobs.length) {
    $('jobs').innerHTML = '<div class="muted">No jobs yet.</div>';
    return;
  }
  $('jobs').innerHTML = `<table><thead><tr><th>Job</th><th>Status</th><th>Items</th><th>Outputs</th><th>Actions</th></tr></thead><tbody>${
    jobs.map(job => `<tr>
      <td><div class="mono">${escapeHtml(job.id)}</div><div class="wrap">${escapeHtml(job.url)}</div><div class="muted">${escapeHtml(job.title || '')}</div><div class="muted mono">${escapeHtml((job.config || {}).schema_id || '')}</div></td>
      <td><span class="status">${escapeHtml(job.status)}</span><div class="muted">${escapeHtml(job.reason || '')}</div></td>
      <td>${job.itemCount || 0}</td>
      <td>${outputLinks(job)}</td>
      <td class="row">
        <button data-job-use="${escapeHtml(job.id)}">Use in builder</button>
        <button data-action="resume" data-job="${escapeHtml(job.id)}">Resume</button>
        <button data-action="stop" data-job="${escapeHtml(job.id)}">Stop</button>
        <button data-action="close-tab" data-job="${escapeHtml(job.id)}">Close tab</button>
      </td>
    </tr>`).join('')
  }</tbody></table>`;
}

async function refresh() {
  try {
    const [apps, jobs, schemas] = await Promise.all([api('/apps'), api('/jobs'), api('/schemas')]);
    renderApps(apps.apps || []);
    renderJobs(jobs.jobs || []);
    renderSchemas(schemas.schemas || []);
    setStatus(`Refreshed ${new Date().toLocaleTimeString()}`);
  } catch (error) {
    setStatus(error.message, false);
  }
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

$('saveToken').onclick = () => {
  localStorage.setItem('hardenedBrokerUrl', state.base);
  localStorage.setItem('hardenedBrokerToken', state.token);
  setStatus('Saved locally');
};
$('refresh').onclick = refresh;
$('registerApp').onclick = async () => {
  try {
    const body = {
      name: $('appName').value.trim(),
      app_id: $('appId').value.trim(),
      description: $('appDescription').value.trim(),
    };
    const result = await api('/apps', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    $('registeredSecret').textContent = `app_id=${result.app.id} app_secret=${result.app.secret}`;
    $('jobAppId').value = result.app.id;
    await refresh();
  } catch (error) {
    setStatus(error.message, false);
  }
};
$('submitJob').onclick = async () => {
  try {
    const body = {
      url: $('jobUrl').value.trim(),
      app_id: $('jobAppId').value.trim(),
      schema_id: $('jobSchemaId').value.trim(),
      max_items: Number($('maxItems').value || 0),
    };
    const result = await api('/jobs', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    $('schemaJobId').value = result.job.id;
    setStatus(`Submitted ${result.job.id}`);
    await refresh();
  } catch (error) {
    setStatus(error.message, false);
  }
};
$('pickRoot').onclick = async () => {
  try {
    setStatus('Click the item/root element in the Chromium tab.');
    const jobId = $('schemaJobId').value.trim();
    const result = await api(`/jobs/${encodeURIComponent(jobId)}/control`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'pick_selector'}),
    });
    if (!result.result.ok) throw new Error(result.result.error || 'picker cancelled');
    $('schemaRoot').value = result.result.selector;
    setStatus(`Picked root ${result.result.selector}`);
  } catch (error) {
    setStatus(error.message, false);
  }
};
$('pickField').onclick = async () => {
  try {
    setStatus('Click the field element in the Chromium tab.');
    const jobId = $('schemaJobId').value.trim();
    const result = await api(`/jobs/${encodeURIComponent(jobId)}/control`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'pick_selector'}),
    });
    if (!result.result.ok) throw new Error(result.result.error || 'picker cancelled');
    appendField($('pickFieldName').value.trim() || 'field', result.result.selector, $('pickFieldMode').value);
    setStatus(`Picked field ${result.result.selector}`);
  } catch (error) {
    setStatus(error.message, false);
  }
};
$('previewSchema').onclick = async () => {
  try {
    const jobId = $('schemaJobId').value.trim();
    const result = await api(`/jobs/${encodeURIComponent(jobId)}/preview-schema`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({schema: parseSchemaFromForm()}),
    });
    $('schemaPreview').textContent = JSON.stringify(result.page, null, 2);
    setStatus(`Previewed ${result.count} items`);
  } catch (error) {
    setStatus(error.message, false);
  }
};
$('saveSchema').onclick = async () => {
  try {
    const result = await api('/schemas', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(parseSchemaFromForm()),
    });
    $('jobSchemaId').value = result.schema.id;
    setStatus(`Saved schema ${result.schema.id}`);
    await refresh();
  } catch (error) {
    setStatus(error.message, false);
  }
};
$('schemas').onclick = async event => {
  const use = event.target.closest('button[data-schema-use]');
  const load = event.target.closest('button[data-schema-load]');
  const del = event.target.closest('button[data-schema-delete]');
  try {
    if (use) {
      $('jobSchemaId').value = use.dataset.schemaUse;
      setStatus(`Using schema ${use.dataset.schemaUse}`);
      return;
    }
    if (load) {
      const result = await api(`/schemas/${encodeURIComponent(load.dataset.schemaLoad)}`);
      const schema = result.schema;
      $('schemaId').value = schema.id || '';
      $('schemaName').value = schema.name || '';
      $('schemaRoot').value = schema.itemRoot || '';
      $('schemaFields').value = (schema.fields || []).map(field =>
        `${field.name} | ${field.selector || ''} | ${field.mode || 'text'} | ${Boolean(field.multiple)} | ${Boolean(field.required)}`
      ).join('\n');
      setStatus(`Loaded schema ${schema.id}`);
      return;
    }
    if (del) {
      await api(`/schemas/${encodeURIComponent(del.dataset.schemaDelete)}`, {method: 'DELETE'});
      await refresh();
      setStatus(`Deleted schema ${del.dataset.schemaDelete}`);
    }
  } catch (error) {
    setStatus(error.message, false);
  }
};
$('jobs').onclick = async event => {
  const use = event.target.closest('button[data-job-use]');
  if (use) {
    $('schemaJobId').value = use.dataset.jobUse;
    setStatus(`Builder will use job ${use.dataset.jobUse}`);
    return;
  }
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  try {
    await api(`/jobs/${encodeURIComponent(button.dataset.job)}/${button.dataset.action}`, {
      method: 'POST',
    });
    await refresh();
  } catch (error) {
    setStatus(error.message, false);
  }
};
refresh();
</script>
</body>
</html>"""


BROKER_UI_HTML = r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hardened Feed Builder</title>
  <style>
    :root { color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
    body { margin: 0; background: Canvas; color: CanvasText; }
    button, input, select, textarea {
      box-sizing: border-box; font: inherit; color: inherit; background: Canvas;
      border: 1px solid color-mix(in srgb, CanvasText 18%, transparent); border-radius: 10px;
    }
    button { padding: 10px 14px; cursor: pointer; }
    button.primary { background: #c62828; border-color: #c62828; color: white; font-weight: 650; }
    button.secondary { background: color-mix(in srgb, CanvasText 7%, transparent); }
    button.ghost { border-color: transparent; background: transparent; }
    button:disabled { opacity: .45; cursor: not-allowed; }
    input, select, textarea { padding: 11px 12px; width: 100%; }
    textarea { min-height: 120px; resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    main { max-width: 1180px; margin: 0 auto; padding: 24px; }
    header.top { display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 18px; }
    h1 { margin: 0; font-size: 26px; }
    h2 { margin: 0 0 12px; font-size: 20px; }
    h3 { margin: 0 0 8px; font-size: 15px; }
    .muted { opacity: .68; }
    .small { font-size: 13px; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .wrap { overflow-wrap: anywhere; }
    nav { display: flex; gap: 8px; flex-wrap: wrap; }
    nav button[aria-current="page"] { background: #c62828; color: white; border-color: #c62828; }
    .screen { display: none; }
    .screen.active { display: block; }
    .card {
      border: 1px solid color-mix(in srgb, CanvasText 14%, transparent);
      border-radius: 18px; padding: 18px; margin: 14px 0;
      background: color-mix(in srgb, Canvas 92%, CanvasText 3%);
    }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
    .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .split { display: grid; grid-template-columns: minmax(280px, 380px) 1fr; gap: 16px; align-items: start; }
    .step {
      display: grid; grid-template-columns: 36px 1fr; gap: 12px; align-items: start;
      padding: 14px 0; border-top: 1px solid color-mix(in srgb, CanvasText 10%, transparent);
    }
    .step:first-child { border-top: 0; }
    .num { width: 32px; height: 32px; border-radius: 999px; display: grid; place-items: center; background: #c62828; color: white; font-weight: 700; }
    .pill { display: inline-flex; gap: 6px; align-items: center; border-radius: 999px; padding: 5px 9px; background: color-mix(in srgb, CanvasText 9%, transparent); }
    .field-chip { justify-content: space-between; border-radius: 12px; padding: 8px 10px; background: color-mix(in srgb, CanvasText 8%, transparent); }
    .field-chip button { padding: 2px 7px; border-radius: 999px; }
    .status { min-height: 24px; }
    .ok { color: #2e7d32; }
    .error { color: #c62828; }
    table { width: 100%; border-collapse: collapse; margin-top: 10px; }
    th, td { text-align: left; vertical-align: top; padding: 9px 7px; border-bottom: 1px solid color-mix(in srgb, CanvasText 12%, transparent); }
    th { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; opacity: .72; }
    td { font-size: 14px; }
    a { color: LinkText; }
    .empty { padding: 28px; text-align: center; border: 1px dashed color-mix(in srgb, CanvasText 20%, transparent); border-radius: 14px; }
    .preview { max-height: 480px; overflow: auto; }
    details { margin-top: 12px; }
    summary { cursor: pointer; font-weight: 650; }
    @media (max-width: 860px) { .split { grid-template-columns: 1fr; } header.top { align-items: stretch; flex-direction: column; } }
  </style>
</head>
<body>
<main>
  <header class="top">
    <div>
      <h1>Hardened Feed Builder</h1>
      <div class="muted">Create a reusable feed by clicking examples on a live webpage.</div>
    </div>
    <nav aria-label="Main">
      <button class="secondary" data-screen="create" aria-current="page">Create Feed</button>
      <button class="secondary" data-screen="feeds">My Feeds</button>
      <button class="secondary" data-screen="history">History</button>
      <button class="secondary" data-screen="settings">Settings</button>
    </nav>
  </header>

  <div id="globalStatus" class="status muted"></div>

  <section id="screen-create" class="screen active">
    <div class="split">
      <section class="card">
        <h2>Create Feed</h2>

        <div class="step">
          <div class="num">1</div>
          <div>
            <h3>Open the website</h3>
            <div class="grid">
              <label>Website URL
                <input id="feedUrl" placeholder="https://example.com/feed-or-search-page">
              </label>
              <label>Feed name
                <input id="feedName" placeholder="Example: My research feed">
              </label>
            </div>
            <div class="row" style="margin-top:10px">
              <button id="openPage" class="primary">Open page</button>
              <span id="openState" class="muted small"></span>
            </div>
          </div>
        </div>

        <div class="step">
          <div class="num">2</div>
          <div>
            <h3>Select repeated item</h3>
            <p class="muted small">Click one post/card/result in the Chromium tab. The builder will use similar items as rows.</p>
            <div class="row">
              <button id="pickItem" class="secondary" disabled>Select item</button>
              <span id="itemState" class="pill muted">No item selected</span>
            </div>
          </div>
        </div>

        <div class="step">
          <div class="num">3</div>
          <div>
            <h3>Select fields</h3>
            <p class="muted small">Choose what each row should contain, then click the matching part inside one item.</p>
            <div id="fieldButtons" class="row"></div>
            <div id="fieldsList" style="margin-top:10px"></div>
          </div>
        </div>

        <div class="step">
          <div class="num">4</div>
          <div>
            <h3>Preview and publish</h3>
            <div class="row">
              <button id="previewFeed" class="secondary" disabled>Preview table</button>
              <button id="saveRunFeed" class="primary" disabled>Save and run feed</button>
              <button id="resetBuilder" class="ghost">Reset</button>
            </div>
            <div id="publishLinks" class="small" style="margin-top:10px"></div>
          </div>
        </div>

        <details>
          <summary>Advanced: CSS and schema JSON</summary>
          <div class="grid" style="margin-top:12px">
            <label>Item CSS selector
              <input id="advancedRoot">
            </label>
            <label>Feed id
              <input id="feedId">
            </label>
          </div>
          <label style="margin-top:12px">Schema JSON
            <textarea id="advancedJson"></textarea>
          </label>
          <div class="row" style="margin-top:10px">
            <button id="applyAdvanced" class="secondary">Apply advanced JSON</button>
          </div>
        </details>
      </section>

      <section class="card">
        <h2>Live Preview</h2>
        <div id="previewSummary" class="muted">Open a page and pick fields to preview extracted rows.</div>
        <div id="previewTable" class="preview"></div>
      </section>
    </div>
  </section>

  <section id="screen-feeds" class="screen">
    <section class="card">
      <div class="row" style="justify-content:space-between">
        <div>
          <h2>My Feeds</h2>
          <div class="muted">Saved feeds can be rerun and consumed as JSON/RSS/Atom/CSV.</div>
        </div>
        <button id="refreshFeeds" class="secondary">Refresh</button>
      </div>
      <div id="feedsList"></div>
    </section>
  </section>

  <section id="screen-history" class="screen">
    <section class="card">
      <div class="row" style="justify-content:space-between">
        <div>
          <h2>History</h2>
          <div class="muted">Recent scrape runs and outputs.</div>
        </div>
        <button id="refreshHistory" class="secondary">Refresh</button>
      </div>
      <div id="jobsList"></div>
    </section>
  </section>

  <section id="screen-settings" class="screen">
    <section class="card">
      <h2>Settings</h2>
      <div class="grid">
        <label>Broker URL
          <input id="brokerUrl" value="http://127.0.0.1:8877">
        </label>
        <label>Bearer token
          <input id="token" placeholder="Leave empty in no-auth mode">
        </label>
      </div>
      <div class="row" style="margin-top:12px">
        <button id="saveSettings" class="primary">Save settings</button>
        <button id="refreshAll" class="secondary">Refresh all</button>
      </div>
      <p class="muted small">Normal no-auth local mode does not need a token. Token mode still works for existing secure broker starts.</p>
    </section>
  </section>
</main>

<script>
const $ = id => document.getElementById(id);
const screens = ['create', 'feeds', 'history', 'settings'];
const fieldTypes = [
  {name: 'title', label: 'Title', mode: 'text', required: false},
  {name: 'text', label: 'Main text', mode: 'text', required: true},
  {name: 'author', label: 'Author', mode: 'text', required: false},
  {name: 'time', label: 'Date/time', mode: 'datetime', required: false},
  {name: 'permalink', label: 'Link', mode: 'href', required: false},
  {name: 'image', label: 'Image', mode: 'src', required: false},
];
const state = {
  builderJobId: '',
  draft: {
    id: '',
    name: '',
    url: '',
    itemRoot: '',
    itemCount: 0,
    fields: [],
  },
  lastSavedFeedId: '',
  get base() { return $('brokerUrl').value.replace(/\/+$/, ''); },
  get token() { return $('token').value.trim(); },
};
$('brokerUrl').value = localStorage.getItem('hardenedBrokerUrl') || location.origin;
$('token').value = localStorage.getItem('hardenedBrokerToken') || '';

function setStatus(text, ok = true) {
  $('globalStatus').textContent = text || '';
  $('globalStatus').className = ok ? 'status ok' : 'status error';
}

function authHeaders() {
  return state.token ? {Authorization: `Bearer ${state.token}`} : {};
}

async function api(path, options = {}) {
  const response = await fetch(`${state.base}${path}`, {
    ...options,
    headers: {
      ...authHeaders(),
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = {ok: false, error: text}; }
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || response.statusText);
  }
  return data;
}

function showScreen(name) {
  for (const screen of screens) {
    $(`screen-${screen}`).classList.toggle('active', screen === name);
    document.querySelector(`button[data-screen="${screen}"]`).setAttribute(
      'aria-current', screen === name ? 'page' : 'false');
  }
  if (name === 'feeds') refreshFeeds();
  if (name === 'history') refreshHistory();
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

function slug(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9._-]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 80) || `feed_${Date.now()}`;
}

function makeSchema() {
  const url = $('feedUrl').value.trim() || state.draft.url;
  const name = $('feedName').value.trim() || state.draft.name || 'Untitled feed';
  const id = $('feedId').value.trim() || state.draft.id || slug(name);
  return {
    id,
    name,
    url,
    sourceUrl: url,
    itemRoot: $('advancedRoot').value.trim() || state.draft.itemRoot,
    fields: state.draft.fields.map(field => ({...field})),
  };
}

function refreshDraftUi() {
  $('advancedRoot').value = state.draft.itemRoot || '';
  $('feedId').value = state.draft.id || slug($('feedName').value || hostName($('feedUrl').value));
  const schema = makeSchema();
  $('advancedJson').value = JSON.stringify(schema, null, 2);
  $('pickItem').disabled = !state.builderJobId;
  $('previewFeed').disabled = !state.builderJobId || !schema.itemRoot || !schema.fields.length;
  $('saveRunFeed').disabled = !schema.itemRoot || !schema.fields.length || !schema.sourceUrl;
  $('itemState').textContent = schema.itemRoot
    ? `Found ${state.draft.itemCount || '?'} matching items`
    : 'No item selected';
  $('itemState').className = schema.itemRoot ? 'pill ok' : 'pill muted';
  renderFields();
}

function hostName(url) {
  try { return new URL(url).hostname; } catch { return ''; }
}

function renderFieldButtons() {
  $('fieldButtons').innerHTML = fieldTypes.map(field =>
    `<button class="secondary" data-pick-field="${field.name}">${escapeHtml(field.label)}</button>`
  ).join('');
}

function renderFields() {
  if (!state.draft.fields.length) {
    $('fieldsList').innerHTML = '<div class="muted small">No fields selected yet.</div>';
    return;
  }
  $('fieldsList').innerHTML = state.draft.fields.map(field => `
    <div class="row field-chip">
      <span><strong>${escapeHtml(field.name)}</strong> <span class="muted mono">${escapeHtml(field.selector || '(item)')}</span></span>
      <button class="ghost" data-remove-field="${escapeHtml(field.name)}">Remove</button>
    </div>
  `).join('');
}

async function openPage() {
  const url = $('feedUrl').value.trim();
  if (!url) throw new Error('Enter a URL first.');
  state.draft.url = url;
  state.draft.name = $('feedName').value.trim() || hostName(url) || 'New feed';
  state.draft.id = $('feedId').value.trim() || slug(state.draft.name);
  $('openState').textContent = 'Opening...';
  const result = await api('/jobs', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      url,
      app_id: 'feed_builder',
      max_items: 1,
      timeout_seconds: 30,
      no_progress_limit: 1,
      raw_snapshots: false,
    }),
  });
  state.builderJobId = result.job.id;
  $('openState').textContent = 'Page opened. Switch to Chromium when selecting.';
  setStatus(`Opened page for selection.`);
  refreshDraftUi();
}

async function pickSelector(message) {
  if (!state.builderJobId) throw new Error('Open a page first.');
  setStatus(message);
  const result = await api(`/jobs/${encodeURIComponent(state.builderJobId)}/control`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'pick_selector'}),
  });
  if (!result.result || !result.result.ok) {
    throw new Error((result.result && result.result.error) || 'Picker cancelled.');
  }
  return result.result;
}

async function pickItemRoot() {
  const picked = await pickSelector('Click one repeated post/card/result in Chromium.');
  state.draft.itemRoot = picked.selector;
  state.draft.itemCount = picked.matchingCount || 0;
  setStatus(`Selected repeated item. Found ${state.draft.itemCount || '?'} matches.`);
  refreshDraftUi();
}

async function pickField(name) {
  const config = fieldTypes.find(field => field.name === name) || {name, mode: 'text', required: false};
  const picked = await pickSelector(`Click the ${config.label || name} inside one item.`);
  const field = {
    name: config.name,
    selector: picked.selector,
    mode: config.mode,
    multiple: false,
    required: Boolean(config.required),
  };
  state.draft.fields = state.draft.fields.filter(existing => existing.name !== field.name);
  state.draft.fields.push(field);
  setStatus(`Selected ${config.label || name}.`);
  refreshDraftUi();
  await previewFeed();
}

async function previewFeed() {
  const schema = makeSchema();
  if (!state.builderJobId) throw new Error('Open a page first.');
  if (!schema.itemRoot) throw new Error('Select repeated item first.');
  if (!schema.fields.length) throw new Error('Select at least one field first.');
  const result = await api(`/jobs/${encodeURIComponent(state.builderJobId)}/preview-schema`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({schema}),
  });
  const items = (result.page && result.page.items) || [];
  renderPreview(items, schema.fields);
  $('previewSummary').textContent = `Previewing ${items.length} rows from ${schema.itemRoot}`;
  setStatus(`Previewed ${items.length} rows.`);
}

function renderPreview(items, fields) {
  if (!items.length) {
    $('previewTable').innerHTML = '<div class="empty muted">No rows matched yet. Pick a broader item or field.</div>';
    return;
  }
  const rows = items.slice(0, 25).map(item => {
    const values = item.fields || {};
    return `<tr>${fields.map(field => {
      const fallback = field.name === 'text' ? item.text :
        field.name === 'author' ? item.author :
        field.name === 'time' ? item.timeText :
        field.name === 'permalink' ? item.permalink : '';
      const value = values[field.name] || fallback || '';
      return `<td>${escapeHtml(Array.isArray(value) ? value.join(', ') : value)}</td>`;
    }).join('')}</tr>`;
  }).join('');
  $('previewTable').innerHTML = `<table><thead><tr>${
    fields.map(field => `<th>${escapeHtml(field.name)}</th>`).join('')
  }</tr></thead><tbody>${rows}</tbody></table>`;
}

async function saveAndRunFeed() {
  const schema = makeSchema();
  const saved = await api('/feeds', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(schema),
  });
  state.lastSavedFeedId = saved.feed.id;
  const run = await api(`/feeds/${encodeURIComponent(saved.feed.id)}/run`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      max_items: Number($('maxItems') ? $('maxItems').value || 500 : 500),
      timeout_seconds: 120,
      scroll_delay_ms: 700,
      raw_snapshots: true,
    }),
  });
  setStatus(`Saved ${saved.feed.name}; running feed now.`);
  await pollJob(run.job.id);
  await refreshFeeds();
  showFeedLinks(saved.feed.id);
}

async function pollJob(jobId) {
  for (let attempt = 0; attempt < 180; attempt++) {
    const status = await api(`/jobs/${encodeURIComponent(jobId)}`);
    const job = status.job;
    setStatus(`Running feed: ${job.itemCount || 0} items, status ${job.status}`);
    if (['completed', 'failed', 'stopped', 'interrupted'].includes(job.status)) {
      setStatus(`Feed run ${job.status}; captured ${job.itemCount || 0} items.`, job.status !== 'failed');
      return job;
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  setStatus('Feed is still running. Check History for progress.');
}

function outputUrlForFeed(feedId, filename) {
  return `${state.base}/feeds/${encodeURIComponent(feedId)}/latest/${filename}${state.token ? `?token=${encodeURIComponent(state.token)}` : ''}`;
}

function showFeedLinks(feedId) {
  const files = [
    ['feed.json', 'JSON Feed'],
    ['feed.rss', 'RSS'],
    ['feed.atom', 'Atom'],
    ['items.csv', 'CSV'],
    ['latest.html', 'Searchable HTML'],
  ];
  $('publishLinks').innerHTML = `<strong>Ready:</strong> ${files.map(([file, label]) =>
    `<a href="${outputUrlForFeed(feedId, file)}" target="_blank" rel="noreferrer">${label}</a>`
  ).join(' · ')}`;
}

async function refreshFeeds() {
  const result = await api('/feeds');
  const feeds = result.feeds || [];
  if (!feeds.length) {
    $('feedsList').innerHTML = '<div class="empty muted">No feeds saved yet. Create one from the Create Feed screen.</div>';
    return;
  }
  $('feedsList').innerHTML = feeds.map(feed => `
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <div>
          <h3>${escapeHtml(feed.name)}</h3>
          <div class="muted wrap">${escapeHtml(feed.sourceUrl || feed.url || '')}</div>
          <div class="small muted">${feed.fieldCount || 0} fields · ${escapeHtml(feed.itemRoot || '')}</div>
        </div>
        <div class="row">
          <button class="primary" data-run-feed="${escapeHtml(feed.id)}">Run</button>
          <button class="secondary" data-edit-feed="${escapeHtml(feed.id)}">Edit</button>
          <button class="ghost" data-delete-feed="${escapeHtml(feed.id)}">Delete</button>
        </div>
      </div>
      <div class="small" style="margin-top:10px">
        ${feed.latestJob ? feedLinks(feed.id) : '<span class="muted">No run yet.</span>'}
      </div>
    </div>
  `).join('');
}

function feedLinks(feedId) {
  return [
    ['feed.json', 'JSON'],
    ['feed.rss', 'RSS'],
    ['feed.atom', 'Atom'],
    ['items.csv', 'CSV'],
    ['latest.html', 'HTML'],
  ].map(([file, label]) =>
    `<a href="${outputUrlForFeed(feedId, file)}" target="_blank" rel="noreferrer">${label}</a>`
  ).join(' · ');
}

async function refreshHistory() {
  const result = await api('/jobs');
  const jobs = result.jobs || [];
  if (!jobs.length) {
    $('jobsList').innerHTML = '<div class="empty muted">No scrape runs yet.</div>';
    return;
  }
  $('jobsList').innerHTML = `<table><thead><tr><th>Run</th><th>Status</th><th>Items</th><th>Outputs</th><th>Actions</th></tr></thead><tbody>${
    jobs.map(job => `<tr>
      <td><div class="mono">${escapeHtml(job.id)}</div><div class="wrap">${escapeHtml(job.url)}</div><div class="muted">${escapeHtml((job.config || {}).schema_id || '')}</div></td>
      <td>${escapeHtml(job.status)}<div class="muted">${escapeHtml(job.reason || '')}</div></td>
      <td>${job.itemCount || 0}</td>
      <td>${jobOutputLinks(job)}</td>
      <td class="row">
        <button class="secondary" data-resume-job="${escapeHtml(job.id)}">Resume</button>
        <button class="ghost" data-stop-job="${escapeHtml(job.id)}">Stop</button>
        <button class="ghost" data-close-job="${escapeHtml(job.id)}">Close tab</button>
      </td>
    </tr>`).join('')
  }</tbody></table>`;
}

function jobOutputLinks(job) {
  const files = [
    ['latest.html', 'HTML'],
    ['feed.json', 'JSON'],
    ['feed.rss', 'RSS'],
    ['feed.atom', 'Atom'],
    ['items.csv', 'CSV'],
  ];
  return files.map(([file, label]) =>
    `<a href="${state.base}/jobs/${encodeURIComponent(job.id)}/${file}${state.token ? `?token=${encodeURIComponent(state.token)}` : ''}" target="_blank" rel="noreferrer">${label}</a>`
  ).join(' · ');
}

function resetBuilder() {
  state.builderJobId = '';
  state.lastSavedFeedId = '';
  state.draft = {id: '', name: '', url: '', itemRoot: '', itemCount: 0, fields: []};
  $('feedUrl').value = '';
  $('feedName').value = '';
  $('publishLinks').innerHTML = '';
  $('previewSummary').textContent = 'Open a page and pick fields to preview extracted rows.';
  $('previewTable').innerHTML = '';
  $('openState').textContent = '';
  refreshDraftUi();
}

function applyAdvancedJson() {
  const schema = JSON.parse($('advancedJson').value);
  state.draft.id = schema.id || '';
  state.draft.name = schema.name || '';
  state.draft.url = schema.sourceUrl || schema.url || '';
  state.draft.itemRoot = schema.itemRoot || '';
  state.draft.fields = Array.isArray(schema.fields) ? schema.fields : [];
  $('feedUrl').value = state.draft.url;
  $('feedName').value = state.draft.name;
  refreshDraftUi();
}

document.querySelectorAll('button[data-screen]').forEach(button => {
  button.onclick = () => showScreen(button.dataset.screen);
});
$('saveSettings').onclick = () => {
  localStorage.setItem('hardenedBrokerUrl', state.base);
  localStorage.setItem('hardenedBrokerToken', state.token);
  setStatus('Settings saved.');
};
$('refreshAll').onclick = async () => {
  await Promise.all([refreshFeeds(), refreshHistory()]);
  setStatus('Refreshed.');
};
$('openPage').onclick = () => openPage().catch(error => setStatus(error.message, false));
$('pickItem').onclick = () => pickItemRoot().catch(error => setStatus(error.message, false));
$('previewFeed').onclick = () => previewFeed().catch(error => setStatus(error.message, false));
$('saveRunFeed').onclick = () => saveAndRunFeed().catch(error => setStatus(error.message, false));
$('resetBuilder').onclick = resetBuilder;
$('applyAdvanced').onclick = () => {
  try { applyAdvancedJson(); setStatus('Advanced schema applied.'); }
  catch (error) { setStatus(error.message, false); }
};
$('fieldButtons').onclick = event => {
  const button = event.target.closest('button[data-pick-field]');
  if (button) pickField(button.dataset.pickField).catch(error => setStatus(error.message, false));
};
$('fieldsList').onclick = event => {
  const button = event.target.closest('button[data-remove-field]');
  if (!button) return;
  state.draft.fields = state.draft.fields.filter(field => field.name !== button.dataset.removeField);
  refreshDraftUi();
};
$('feedsList').onclick = async event => {
  const run = event.target.closest('button[data-run-feed]');
  const edit = event.target.closest('button[data-edit-feed]');
  const del = event.target.closest('button[data-delete-feed]');
  try {
    if (run) {
      const result = await api(`/feeds/${encodeURIComponent(run.dataset.runFeed)}/run`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({max_items: 500, timeout_seconds: 900}),
      });
      await pollJob(result.job.id);
      await refreshFeeds();
    } else if (edit) {
      const result = await api(`/feeds/${encodeURIComponent(edit.dataset.editFeed)}`);
      const feed = result.feed;
      const schema = feed.schema || feed;
      state.draft.id = feed.id;
      state.draft.name = feed.name;
      state.draft.url = feed.sourceUrl || feed.url || '';
      state.draft.itemRoot = schema.itemRoot || feed.itemRoot || '';
      state.draft.fields = schema.fields || feed.fields || [];
      $('feedUrl').value = state.draft.url;
      $('feedName').value = state.draft.name;
      showScreen('create');
      refreshDraftUi();
    } else if (del) {
      await api(`/feeds/${encodeURIComponent(del.dataset.deleteFeed)}`, {method: 'DELETE'});
      await refreshFeeds();
      setStatus('Feed deleted.');
    }
  } catch (error) {
    setStatus(error.message, false);
  }
};
$('jobsList').onclick = async event => {
  const resume = event.target.closest('button[data-resume-job]');
  const stop = event.target.closest('button[data-stop-job]');
  const close = event.target.closest('button[data-close-job]');
  try {
    if (resume) await api(`/jobs/${encodeURIComponent(resume.dataset.resumeJob)}/resume`, {method: 'POST'});
    if (stop) await api(`/jobs/${encodeURIComponent(stop.dataset.stopJob)}/stop`, {method: 'POST'});
    if (close) await api(`/jobs/${encodeURIComponent(close.dataset.closeJob)}/close-tab`, {method: 'POST'});
    await refreshHistory();
  } catch (error) {
    setStatus(error.message, false);
  }
};
$('refreshFeeds').onclick = () => refreshFeeds().catch(error => setStatus(error.message, false));
$('refreshHistory').onclick = () => refreshHistory().catch(error => setStatus(error.message, false));
$('feedName').addEventListener('input', refreshDraftUi);
$('feedUrl').addEventListener('input', refreshDraftUi);
$('advancedRoot').addEventListener('input', () => { state.draft.itemRoot = $('advancedRoot').value.trim(); refreshDraftUi(); });
$('feedId').addEventListener('input', () => { state.draft.id = $('feedId').value.trim(); refreshDraftUi(); });

renderFieldButtons();
refreshDraftUi();
refreshFeeds().catch(() => {});
refreshHistory().catch(() => {});
</script>
</body>
</html>"""


MANAGEMENT_UI_HTML = r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hardened Scrape Manager</title>
  <style>
    :root {
      color-scheme: light dark;
      --red: #c62828;
      --red-2: #ff3b22;
      --ok: #2e7d32;
      --warn: #b26a00;
      --bad: #c62828;
      --line: color-mix(in srgb, CanvasText 14%, transparent);
      --soft: color-mix(in srgb, CanvasText 7%, transparent);
      --softer: color-mix(in srgb, CanvasText 4%, transparent);
      --text-soft: color-mix(in srgb, CanvasText 68%, transparent);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: Canvas; color: CanvasText; }
    button, input, select, textarea {
      font: inherit; color: inherit; background: Canvas;
      border: 1px solid var(--line); border-radius: 10px;
    }
    button { padding: 9px 13px; cursor: pointer; }
    button:disabled { opacity: .45; cursor: not-allowed; }
    button.primary { background: var(--red); color: white; border-color: var(--red); font-weight: 700; }
    button.secondary { background: var(--soft); }
    button.ghost { background: transparent; border-color: transparent; }
    button.danger { color: #ff9a8f; border-color: color-mix(in srgb, var(--bad) 45%, transparent); background: color-mix(in srgb, var(--bad) 10%, transparent); }
    input, select, textarea { width: 100%; padding: 10px 11px; }
    textarea { min-height: 140px; resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    main { max-width: 1320px; margin: 0 auto; padding: 22px; }
    .topbar { display: grid; grid-template-columns: 1fr auto; gap: 16px; align-items: start; margin-bottom: 16px; }
    h1 { margin: 0; font-size: 28px; letter-spacing: -.02em; }
    h2 { margin: 0 0 12px; font-size: 20px; }
    h3 { margin: 0 0 8px; font-size: 15px; }
    a { color: LinkText; }
    .muted { color: var(--text-soft); }
    .small { font-size: 13px; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .wrap { overflow-wrap: anywhere; }
    .row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .between { justify-content: space-between; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
    .layout { display: grid; grid-template-columns: 260px minmax(0, 1fr); gap: 16px; align-items: start; }
    .side {
      position: sticky; top: 14px; border: 1px solid var(--line); border-radius: 20px;
      padding: 12px; background: color-mix(in srgb, Canvas 96%, CanvasText 2%);
    }
    .brand { padding: 8px 10px 14px; border-bottom: 1px solid var(--line); margin-bottom: 10px; }
    .nav { display: grid; gap: 6px; }
    .nav button { text-align: left; border-color: transparent; background: transparent; }
    .nav button.active { background: color-mix(in srgb, var(--red) 18%, Canvas); border-color: color-mix(in srgb, var(--red) 45%, transparent); }
    .card {
      border: 1px solid var(--line); border-radius: 18px; padding: 16px; margin-bottom: 14px;
      background: color-mix(in srgb, Canvas 95%, CanvasText 2%);
      box-shadow: 0 12px 40px rgba(0,0,0,.06);
    }
    .card.tight { padding: 12px; }
    .screen { display: none; }
    .screen.active { display: block; }
    .statusbar {
      min-height: 34px; border: 1px solid var(--line); border-radius: 14px; padding: 8px 11px;
      background: var(--softer); margin-bottom: 14px;
    }
    .statusbar.ok { border-color: color-mix(in srgb, var(--ok) 40%, transparent); }
    .statusbar.error { border-color: color-mix(in srgb, var(--bad) 48%, transparent); color: #ff9a8f; }
    .kpis { display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 12px; }
    .kpi { border: 1px solid var(--line); border-radius: 16px; padding: 14px; background: var(--softer); }
    .kpi .value { font-size: 28px; font-weight: 800; margin-top: 8px; }
    .split { display: grid; grid-template-columns: minmax(330px, 450px) minmax(0, 1fr); gap: 14px; align-items: start; }
    .step { display: grid; grid-template-columns: 34px 1fr; gap: 12px; padding: 14px 0; border-top: 1px solid var(--line); }
    .step:first-of-type { border-top: 0; padding-top: 0; }
    .num { width: 32px; height: 32px; display: grid; place-items: center; border-radius: 999px; background: var(--red); color: white; font-weight: 800; }
    .pill {
      display: inline-flex; gap: 6px; align-items: center; padding: 4px 9px; border-radius: 999px;
      background: var(--soft); border: 1px solid transparent; font-size: 13px;
    }
    .pill.ok { color: #8bdc91; border-color: color-mix(in srgb, var(--ok) 35%, transparent); }
    .pill.bad { color: #ff9a8f; border-color: color-mix(in srgb, var(--bad) 35%, transparent); }
    .pill.warn { color: #ffc46b; border-color: color-mix(in srgb, var(--warn) 35%, transparent); }
    .field-chip {
      display: flex; justify-content: space-between; align-items: center; gap: 8px;
      border: 1px solid var(--line); border-radius: 13px; padding: 8px 10px; background: var(--softer); margin: 7px 0;
    }
    .field-chip button { padding: 2px 7px; }
    .preview { max-height: 520px; overflow: auto; border: 1px solid var(--line); border-radius: 14px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; vertical-align: top; padding: 9px 8px; border-bottom: 1px solid var(--line); }
    th { position: sticky; top: 0; background: Canvas; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--text-soft); }
    td { font-size: 14px; }
    .empty { padding: 26px; text-align: center; border: 1px dashed var(--line); border-radius: 14px; color: var(--text-soft); }
    .feed-card { display: grid; gap: 12px; }
    .feed-card header { display: flex; justify-content: space-between; gap: 12px; align-items: start; }
    .toolbar { display: grid; grid-template-columns: minmax(220px, 1fr) repeat(2, minmax(150px, 210px)) auto; gap: 10px; align-items: end; }
    details { margin-top: 10px; }
    summary { cursor: pointer; font-weight: 700; }
    .drawer {
      border: 1px solid var(--line); border-radius: 14px; padding: 12px; margin-top: 10px;
      background: color-mix(in srgb, CanvasText 5%, transparent);
    }
    .copybox {
      display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: center;
      border: 1px solid var(--line); border-radius: 12px; padding: 8px; background: Canvas;
    }
    .copybox code { overflow-wrap: anywhere; }
    .right-panel { display: grid; gap: 14px; }
    .hidden { display: none !important; }
    @media (max-width: 980px) {
      .layout, .split { grid-template-columns: 1fr; }
      .side { position: static; }
      .kpis, .toolbar { grid-template-columns: 1fr; }
      .topbar { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
<main>
  <div class="topbar">
    <div>
      <h1>Hardened Scrape Manager</h1>
      <div class="muted">Manage visual feeds, runs, outputs, and local automation from one UI.</div>
    </div>
    <div class="row">
      <span id="connectionPill" class="pill">Checking broker</span>
      <button id="refreshNow" class="secondary">Refresh</button>
    </div>
  </div>

  <div class="layout">
    <aside class="side">
      <div class="brand">
        <div class="row between">
          <strong>Manager</strong>
          <span class="pill">local</span>
        </div>
        <div id="brokerMini" class="small muted wrap" style="margin-top:6px"></div>
      </div>
      <nav class="nav">
        <button data-screen="dashboard" class="active">Dashboard</button>
        <button data-screen="create">Create feed</button>
        <button data-screen="feeds">Feeds</button>
        <button data-screen="runs">Runs</button>
        <button data-screen="settings">Settings</button>
      </nav>
    </aside>

    <section>
      <div id="statusbar" class="statusbar muted">Ready.</div>

      <section id="screen-dashboard" class="screen active">
        <div class="kpis">
          <div class="kpi"><div class="muted small">Feeds</div><div id="kpiFeeds" class="value">0</div></div>
          <div class="kpi"><div class="muted small">Runs</div><div id="kpiRuns" class="value">0</div></div>
          <div class="kpi"><div class="muted small">Running</div><div id="kpiRunning" class="value">0</div></div>
          <div class="kpi"><div class="muted small">Items captured</div><div id="kpiItems" class="value">0</div></div>
        </div>
        <div class="grid" style="margin-top:14px">
          <div class="card">
            <div class="row between">
              <h2>Recent feeds</h2>
              <button class="secondary" data-go="feeds">Manage</button>
            </div>
            <div id="dashboardFeeds"></div>
          </div>
          <div class="card">
            <div class="row between">
              <h2>Recent runs</h2>
              <button class="secondary" data-go="runs">View all</button>
            </div>
            <div id="dashboardRuns"></div>
          </div>
        </div>
      </section>

      <section id="screen-create" class="screen">
        <div class="split">
          <section class="card">
            <div class="row between">
              <div>
                <h2>Create or edit feed</h2>
                <div class="muted small">No CSS required. Click examples in the Chromium tab.</div>
              </div>
              <button id="newDraft" class="secondary">New draft</button>
            </div>

            <div class="step">
              <div class="num">1</div>
              <div>
                <h3>Open website</h3>
                <div class="grid">
                  <label>Website URL
                    <input id="feedUrl" placeholder="https://example.com/search?q=topic">
                  </label>
                  <label>Feed name
                    <input id="feedName" placeholder="Example: Research feed">
                  </label>
                </div>
                <div class="row" style="margin-top:10px">
                  <button id="openPage" class="primary">Open page for selection</button>
                  <span id="builderJobState" class="small muted"></span>
                </div>
              </div>
            </div>

            <div class="step">
              <div class="num">2</div>
              <div>
                <h3>Select repeated item</h3>
                <p class="small muted">Click one post/card/result. The manager detects similar rows and shows the match count.</p>
                <div class="row">
                  <button id="pickItem" class="secondary" disabled>Select repeated item</button>
                  <span id="itemState" class="pill">No item selected</span>
                </div>
              </div>
            </div>

            <div class="step">
              <div class="num">3</div>
              <div>
                <h3>Select fields</h3>
                <div id="fieldButtons" class="row"></div>
                <div class="grid" style="margin-top:10px">
                  <label>Custom field name
                    <input id="customFieldName" placeholder="price, company, score">
                  </label>
                  <label>Custom field type
                    <select id="customFieldMode">
                      <option value="text">Text</option>
                      <option value="href">Link URL</option>
                      <option value="src">Media URL</option>
                      <option value="datetime">Date/time</option>
                      <option value="html">HTML</option>
                      <option value="outer_html">Outer HTML</option>
                    </select>
                  </label>
                </div>
                <div class="row" style="margin-top:10px">
                  <button id="pickCustomField" class="secondary" disabled>Pick custom field</button>
                  <button id="previewFeed" class="secondary" disabled>Preview</button>
                </div>
                <div id="fieldsList" style="margin-top:10px"></div>
              </div>
            </div>

            <div class="step">
              <div class="num">4</div>
              <div>
                <h3>Save and run</h3>
                <div class="row">
                  <button id="saveFeedOnly" class="secondary" disabled>Save feed</button>
                  <button id="saveRunFeed" class="primary" disabled>Save and run</button>
                  <button id="copySchema" class="ghost">Copy schema</button>
                </div>
                <div id="publishLinks" class="small" style="margin-top:10px"></div>
              </div>
            </div>

            <details>
              <summary>Advanced CSS / schema editor</summary>
              <div class="drawer">
                <div class="grid">
                  <label>Feed id
                    <input id="feedId">
                  </label>
                  <label>Item selector
                    <input id="advancedRoot">
                  </label>
                </div>
                <label style="margin-top:10px">Schema JSON
                  <textarea id="advancedJson"></textarea>
                </label>
                <div class="row" style="margin-top:10px">
                  <button id="applyAdvanced" class="secondary">Apply JSON</button>
                </div>
              </div>
            </details>
          </section>

          <section class="right-panel">
            <div class="card">
              <div class="row between">
                <div>
                  <h2>Preview</h2>
                  <div id="previewSummary" class="muted small">Open a page and select fields.</div>
                </div>
                <span id="previewPill" class="pill">0 rows</span>
              </div>
              <div id="previewTable" class="preview" style="margin-top:10px"></div>
            </div>
            <div class="card">
              <h2>Run settings</h2>
              <div class="grid">
                <label>Max items
                  <input id="defaultMaxItems" type="number" min="0" max="5000" value="500">
                </label>
                <label>Timeout seconds
                  <input id="defaultTimeout" type="number" min="10" max="14400" value="900">
                </label>
                <label>Scroll delay ms
                  <input id="defaultScrollDelay" type="number" min="250" max="30000" value="1500">
                </label>
                <label>No-progress limit
                  <input id="defaultNoProgress" type="number" min="1" max="100" value="8">
                </label>
              </div>
              <label class="row small" style="margin-top:10px">
                <input id="defaultRawSnapshots" type="checkbox" checked style="width:auto">
                Capture raw HTML/MHTML/visible text
              </label>
            </div>
          </section>
        </div>
      </section>

      <section id="screen-feeds" class="screen">
        <div class="card">
          <div class="row between">
            <div>
              <h2>Feeds</h2>
              <div class="muted small">Saved extraction definitions with latest outputs.</div>
            </div>
            <button class="primary" data-go="create">Create feed</button>
          </div>
          <div class="toolbar" style="margin-top:12px">
            <label>Search feeds
              <input id="feedSearch" placeholder="name, url, field, selector">
            </label>
            <label>Latest status
              <select id="feedStatusFilter">
                <option value="">Any status</option>
                <option value="completed">Completed</option>
                <option value="running">Running</option>
                <option value="failed">Failed</option>
                <option value="needs_user">Needs user</option>
                <option value="no_run">No run</option>
              </select>
            </label>
            <label>Sort
              <select id="feedSort">
                <option value="updated">Recently updated</option>
                <option value="name">Name</option>
                <option value="items">Most items</option>
              </select>
            </label>
            <button id="refreshFeeds" class="secondary">Refresh</button>
          </div>
          <div id="feedsList" style="margin-top:12px"></div>
        </div>
      </section>

      <section id="screen-runs" class="screen">
        <div class="card">
          <div class="row between">
            <div>
              <h2>Runs</h2>
              <div class="muted small">Monitor jobs, close tabs, resume manual-interaction pauses, and open outputs.</div>
            </div>
            <button id="refreshRuns" class="secondary">Refresh</button>
          </div>
          <div class="toolbar" style="margin-top:12px">
            <label>Search runs
              <input id="runSearch" placeholder="url, feed id, job id, title">
            </label>
            <label>Status
              <select id="runStatusFilter">
                <option value="">Any status</option>
                <option value="running">Running</option>
                <option value="needs_user">Needs user</option>
                <option value="completed">Completed</option>
                <option value="failed">Failed</option>
                <option value="stopped">Stopped</option>
              </select>
            </label>
            <label>Limit
              <select id="runLimit">
                <option value="25">25</option>
                <option value="50">50</option>
                <option value="100">100</option>
              </select>
            </label>
            <label class="row small">
              <input id="autoRefresh" type="checkbox" style="width:auto">
              Auto-refresh
            </label>
          </div>
          <div id="runsList" style="margin-top:12px"></div>
        </div>
      </section>

      <section id="screen-settings" class="screen">
        <div class="card">
          <h2>Settings</h2>
          <div class="grid">
            <label>Broker URL
              <input id="brokerUrl" value="http://127.0.0.1:8877">
            </label>
            <label>Bearer token
              <input id="token" placeholder="Leave empty in no-auth mode">
            </label>
          </div>
          <div class="row" style="margin-top:12px">
            <button id="saveSettings" class="primary">Save settings</button>
            <button id="checkHealth" class="secondary">Check health</button>
            <button id="exportState" class="secondary">Export UI state</button>
          </div>
          <pre id="healthOutput" class="drawer mono small wrap"></pre>
        </div>
        <div class="card">
          <h2>Camera and microphone privacy</h2>
          <p class="small muted">
            Choose whether websites receive real devices or privacy-preserving
            substitutes. Fake camera defaults to a looping Y4M video; Chromium
            synthetic capture and OBS remain available as advanced backends.
          </p>
          <div class="grid">
            <label>Camera source
              <select id="cameraSource">
                <option value="fake">Fake camera</option>
                <option value="real">Real camera</option>
              </select>
            </label>
            <label>Fake camera backend
              <select id="fakeCameraBackend">
                <option value="loop">Loop video file</option>
                <option value="synthetic">Chromium synthetic camera</option>
                <option value="obs">OBS virtual camera</option>
              </select>
            </label>
            <label>Microphone source
              <select id="microphoneSource">
                <option value="fake">Fake/synthetic microphone</option>
                <option value="real">Real system microphone</option>
              </select>
            </label>
            <label>Select Y4M file
              <input id="loopVideoFile" type="file" accept=".y4m,video/y4m">
            </label>
            <label>Existing absolute Y4M path
              <input id="loopVideoPath" placeholder="/home/user/Videos/loop.y4m">
            </label>
          </div>
          <div class="row" style="margin-top:12px">
            <button id="importLoopVideo" class="primary">Import selected file</button>
            <button id="saveMediaMode" class="secondary">Save media settings</button>
            <button id="restartService" class="secondary">Restart broker</button>
            <button id="refreshMediaSettings" class="ghost">Refresh media settings</button>
          </div>
          <pre id="mediaSettingsOutput" class="drawer mono small wrap"></pre>
        </div>
        <div class="card">
          <h2>Location privacy</h2>
          <p class="small muted">
            Real location follows Chromium's ordinary permission and provider
            path. Fake location changes only the coordinates returned after the
            user grants permission; it never auto-grants access.
          </p>
          <div class="grid">
            <label>Location source
              <select id="locationSource">
                <option value="fake">Fake location</option>
                <option value="real">Real location</option>
              </select>
            </label>
            <label>Latitude
              <input id="locationLatitude" type="number" min="-90" max="90" step="0.000001">
            </label>
            <label>Longitude
              <input id="locationLongitude" type="number" min="-180" max="180" step="0.000001">
            </label>
            <label>Accuracy meters
              <input id="locationAccuracy" type="number" min="1" max="100000" step="1">
            </label>
          </div>
          <div class="row" style="margin-top:12px">
            <button id="saveLocationSettings" class="secondary">Save location settings</button>
            <button id="refreshLocationSettings" class="ghost">Refresh location settings</button>
          </div>
          <pre id="locationSettingsOutput" class="drawer mono small wrap"></pre>
        </div>
        <div class="card">
          <div class="row between">
            <div>
              <h2>Remembered website rules</h2>
              <p class="small muted">Set independent defaults for a website. Allow or Block in Chromium remains authoritative.</p>
            </div>
            <button id="refreshPrivacyRules" class="ghost">Refresh rules</button>
          </div>
          <div class="grid">
            <label>Website origin
              <input id="privacyRuleOrigin" placeholder="https://example.com">
            </label>
            <label>Camera
              <select id="privacyRuleCamera"><option value="fake">Fake</option><option value="real">Real</option></select>
            </label>
            <label>Microphone
              <select id="privacyRuleMicrophone"><option value="fake">Fake</option><option value="real">Real</option></select>
            </label>
            <label>Location
              <select id="privacyRuleLocation"><option value="fake">Fake</option><option value="real">Real</option></select>
            </label>
          </div>
          <div class="row" style="margin-top:12px">
            <button id="savePrivacyRule" class="primary">Remember for website</button>
          </div>
          <div id="privacyRulesList" style="margin-top:12px"></div>
        </div>
      </section>
    </section>
  </div>
</main>

<script>
const $ = id => document.getElementById(id);
const screens = ['dashboard', 'create', 'feeds', 'runs', 'settings'];
const terminalStatuses = new Set(['completed', 'failed', 'stopped', 'interrupted']);
const fieldTypes = [
  {name: 'title', label: 'Title', mode: 'text', required: false},
  {name: 'text', label: 'Main text', mode: 'text', required: true},
  {name: 'author', label: 'Author', mode: 'text', required: false},
  {name: 'time', label: 'Date/time', mode: 'datetime', required: false},
  {name: 'permalink', label: 'Link', mode: 'href', required: false},
  {name: 'image', label: 'Image', mode: 'src', required: false},
];
const state = {
  feeds: [],
  jobs: [],
  health: null,
  mediaSettings: null,
  locationSettings: null,
  privacyRules: [],
  activeScreen: 'dashboard',
  builderJobId: '',
  busy: false,
  autoRefreshTimer: 0,
  draft: blankDraft(),
  get base() { return $('brokerUrl').value.replace(/\/+$/, ''); },
  get token() { return $('token').value.trim(); },
};

function blankDraft() {
  return {id: '', name: '', url: '', itemRoot: '', itemCount: 0, fields: []};
}

function init() {
  loadSettings();
  loadDraft();
  renderFieldButtons();
  bindEvents();
  refreshDraftUi();
  refreshAll({quiet: true});
}

function bindEvents() {
  document.querySelectorAll('[data-screen]').forEach(button => {
    button.onclick = () => showScreen(button.dataset.screen);
  });
  document.querySelectorAll('[data-go]').forEach(button => {
    button.onclick = () => showScreen(button.dataset.go);
  });
  $('refreshNow').onclick = () => refreshAll();
  $('refreshFeeds').onclick = () => refreshFeeds();
  $('refreshRuns').onclick = () => refreshJobs();
  $('saveSettings').onclick = saveSettings;
  $('checkHealth').onclick = () => checkHealth().catch(showError);
  $('exportState').onclick = exportUiState;
  $('refreshMediaSettings').onclick = () => refreshMediaSettings().catch(showError);
  $('importLoopVideo').onclick = () => withBusy($('importLoopVideo'), importLoopVideo);
  $('saveMediaMode').onclick = () => withBusy($('saveMediaMode'), saveMediaSettings);
  $('cameraSource').onchange = () => {
    state.mediaSettings = {...(state.mediaSettings || {}),
      cameraSource: $('cameraSource').value};
    renderMediaSettings();
  };
  $('fakeCameraBackend').onchange = () => {
    state.mediaSettings = {...(state.mediaSettings || {}),
      fakeCameraBackend: $('fakeCameraBackend').value};
    renderMediaSettings();
  };
  $('microphoneSource').onchange = () => {
    state.mediaSettings = {...(state.mediaSettings || {}),
      microphoneSource: $('microphoneSource').value};
    renderMediaSettings();
  };
  $('refreshLocationSettings').onclick = () => refreshLocationSettings().catch(showError);
  $('saveLocationSettings').onclick = () => withBusy($('saveLocationSettings'), saveLocationSettings);
  $('locationSource').onchange = () => {
    state.locationSettings = {...(state.locationSettings || {}),
      source: $('locationSource').value};
    renderLocationSettings();
  };
  $('refreshPrivacyRules').onclick = () => refreshPrivacyRules().catch(showError);
  $('savePrivacyRule').onclick = () => withBusy($('savePrivacyRule'), savePrivacyRule);
  $('privacyRulesList').onclick = event => {
    const button = event.target.closest('button[data-delete-privacy-rule]');
    if (button) withBusy(button, () => deletePrivacyRule(button.dataset.deletePrivacyRule));
  };
  $('restartService').onclick = () => withBusy($('restartService'), restartService);
  $('openPage').onclick = () => withBusy($('openPage'), openPage);
  $('pickItem').onclick = () => withBusy($('pickItem'), pickItemRoot);
  $('pickCustomField').onclick = () => withBusy($('pickCustomField'), () => pickField(customFieldConfig()));
  $('previewFeed').onclick = () => withBusy($('previewFeed'), previewFeed);
  $('saveFeedOnly').onclick = () => withBusy($('saveFeedOnly'), saveFeedOnly);
  $('saveRunFeed').onclick = () => withBusy($('saveRunFeed'), saveAndRunFeed);
  $('copySchema').onclick = () => copyText(JSON.stringify(makeSchema(), null, 2), 'Schema copied.');
  $('newDraft').onclick = resetBuilder;
  $('applyAdvanced').onclick = applyAdvancedJsonSafely;
  $('fieldButtons').onclick = event => {
    const button = event.target.closest('button[data-pick-field]');
    if (!button) return;
    const config = fieldTypes.find(field => field.name === button.dataset.pickField);
    if (config) withBusy(button, () => pickField(config));
  };
  $('fieldsList').onclick = event => {
    const remove = event.target.closest('button[data-remove-field]');
    if (!remove) return;
    state.draft.fields = state.draft.fields.filter(field => field.name !== remove.dataset.removeField);
    saveDraft();
    refreshDraftUi();
  };
  $('feedsList').onclick = handleFeedAction;
  $('runsList').onclick = handleRunAction;
  for (const id of ['feedSearch', 'feedStatusFilter', 'feedSort']) {
    $(id).addEventListener('input', renderFeeds);
  }
  for (const id of ['runSearch', 'runStatusFilter', 'runLimit']) {
    $(id).addEventListener('input', renderRuns);
  }
  for (const id of ['feedUrl', 'feedName', 'feedId', 'advancedRoot']) {
    $(id).addEventListener('input', () => { syncDraftFromInputs(); saveDraft(); refreshDraftUi(); });
  }
  for (const id of ['defaultMaxItems', 'defaultTimeout', 'defaultScrollDelay', 'defaultNoProgress', 'defaultRawSnapshots']) {
    $(id).addEventListener('input', saveSettings);
  }
  $('autoRefresh').addEventListener('change', configureAutoRefresh);
}

function loadSettings() {
  $('brokerUrl').value = localStorage.getItem('hardenedBrokerUrl') || location.origin;
  $('token').value = localStorage.getItem('hardenedBrokerToken') || '';
  const settings = safeJson(localStorage.getItem('hardenedFeedManagerSettings'), {});
  $('defaultMaxItems').value = settings.maxItems ?? 500;
  $('defaultTimeout').value = settings.timeoutSeconds ?? 900;
  $('defaultScrollDelay').value = settings.scrollDelayMs ?? 1500;
  $('defaultNoProgress').value = settings.noProgressLimit ?? 8;
  $('defaultRawSnapshots').checked = settings.rawSnapshots ?? true;
  $('autoRefresh').checked = Boolean(settings.autoRefresh);
  configureAutoRefresh();
}

function saveSettings() {
  localStorage.setItem('hardenedBrokerUrl', state.base);
  localStorage.setItem('hardenedBrokerToken', state.token);
  localStorage.setItem('hardenedFeedManagerSettings', JSON.stringify(runSettings()));
  showStatus('Settings saved.');
}

function loadDraft() {
  const draft = safeJson(localStorage.getItem('hardenedFeedManagerDraft'), null);
  if (draft && typeof draft === 'object') state.draft = {...blankDraft(), ...draft};
  $('feedUrl').value = state.draft.url || '';
  $('feedName').value = state.draft.name || '';
  $('feedId').value = state.draft.id || '';
  $('advancedRoot').value = state.draft.itemRoot || '';
}

function saveDraft() {
  localStorage.setItem('hardenedFeedManagerDraft', JSON.stringify(state.draft));
}

function runSettings() {
  return {
    maxItems: numberValue('defaultMaxItems', 500),
    timeoutSeconds: numberValue('defaultTimeout', 900),
    scrollDelayMs: numberValue('defaultScrollDelay', 1500),
    noProgressLimit: numberValue('defaultNoProgress', 8),
    rawSnapshots: $('defaultRawSnapshots').checked,
    autoRefresh: $('autoRefresh').checked,
  };
}

function jobRunBody(overrides = {}) {
  const settings = runSettings();
  return {
    max_items: settings.maxItems,
    timeout_seconds: settings.timeoutSeconds,
    scroll_delay_ms: settings.scrollDelayMs,
    no_progress_limit: settings.noProgressLimit,
    raw_snapshots: settings.rawSnapshots,
    ...overrides,
  };
}

function numberValue(id, fallback) {
  const value = Number($(id).value);
  return Number.isFinite(value) ? value : fallback;
}

function safeJson(text, fallback) {
  try { return text ? JSON.parse(text) : fallback; } catch { return fallback; }
}

function setConnection(ok, text) {
  $('connectionPill').textContent = text;
  $('connectionPill').className = ok ? 'pill ok' : 'pill bad';
  $('brokerMini').textContent = state.base;
}

function showStatus(text, ok = true) {
  $('statusbar').textContent = text || '';
  $('statusbar').className = ok ? 'statusbar ok' : 'statusbar error';
}

function showError(error) {
  showStatus(error && error.message ? error.message : String(error), false);
}

async function withBusy(button, fn) {
  if (state.busy) return;
  state.busy = true;
  if (button) button.disabled = true;
  try {
    await fn();
  } catch (error) {
    showError(error);
  } finally {
    state.busy = false;
    refreshDraftUi();
  }
}

async function api(path, options = {}, timeoutMs = 60000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${state.base}${path}`, {
      ...options,
      signal: controller.signal,
      headers: {
        ...authHeaders(),
        ...(options.headers || {}),
      },
    });
    const text = await response.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch { data = {ok: false, error: text}; }
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || response.statusText || `HTTP ${response.status}`);
    }
    setConnection(true, 'Connected');
    return data;
  } catch (error) {
    setConnection(false, 'Disconnected');
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function authHeaders() {
  return state.token ? {Authorization: `Bearer ${state.token}`} : {};
}

async function refreshAll(options = {}) {
  const quiet = Boolean(options.quiet);
  const [health, feeds, jobs, media, location, privacyRules] = await Promise.allSettled([
    checkHealth(),
    api('/feeds'),
    api('/jobs'),
    api('/service/media-settings'),
    api('/service/location-settings'),
    api('/service/privacy-rules'),
  ]);
  if (feeds.status === 'fulfilled') state.feeds = feeds.value.feeds || [];
  if (jobs.status === 'fulfilled') state.jobs = jobs.value.jobs || [];
  if (media.status === 'fulfilled') state.mediaSettings = media.value.settings || null;
  if (location.status === 'fulfilled') state.locationSettings = location.value.settings || null;
  if (privacyRules.status === 'fulfilled') state.privacyRules = privacyRules.value.rules || [];
  renderAll();
  if (!quiet) showStatus('Refreshed.');
}

async function checkHealth() {
  const health = await api('/health', {}, 10000);
  state.health = health;
  state.mediaSettings = health.mediaSettings || state.mediaSettings;
  state.locationSettings = health.locationSettings || state.locationSettings;
  $('healthOutput').textContent = JSON.stringify(health, null, 2);
  setConnection(true, health.auth === 'none' ? 'Connected, no-auth' : 'Connected, token');
  return health;
}

async function refreshMediaSettings() {
  const result = await api('/service/media-settings', {}, 10000);
  state.mediaSettings = result.settings || null;
  renderMediaSettings();
  showStatus('Media settings refreshed.');
}

async function refreshLocationSettings() {
  const result = await api('/service/location-settings', {}, 10000);
  state.locationSettings = result.settings || null;
  renderLocationSettings();
  showStatus('Location settings refreshed.');
}

async function refreshPrivacyRules() {
  const result = await api('/service/privacy-rules', {}, 10000);
  state.privacyRules = result.rules || [];
  renderPrivacyRules();
  showStatus('Website privacy rules refreshed.');
}

async function refreshFeeds() {
  const result = await api('/feeds');
  state.feeds = result.feeds || [];
  renderAll();
  showStatus('Feeds refreshed.');
}

async function refreshJobs() {
  const result = await api('/jobs');
  state.jobs = result.jobs || [];
  renderAll();
  showStatus('Runs refreshed.');
}

function renderAll() {
  renderDashboard();
  renderFeeds();
  renderRuns();
  renderMediaSettings();
  renderLocationSettings();
  renderPrivacyRules();
  refreshDraftUi();
}

function renderMediaSettings() {
  if (!$('mediaSettingsOutput')) return;
  const settings = state.mediaSettings || {};
  const cameraSource = settings.cameraSource ||
      ((settings.mediaMode === 'system' || settings.mediaMode === 'none')
          ? 'real' : 'fake');
  const fakeCameraBackend = settings.fakeCameraBackend ||
      (['loop', 'synthetic', 'obs'].includes(settings.mediaMode)
          ? settings.mediaMode : 'loop');
  const microphoneSource = settings.microphoneSource ||
      (settings.audioCaptureMode === 'system' ? 'real' : 'fake');
  $('cameraSource').value = cameraSource;
  $('fakeCameraBackend').value = fakeCameraBackend;
  $('microphoneSource').value = microphoneSource;
  $('fakeCameraBackend').disabled = cameraSource !== 'fake';
  $('loopVideoFile').disabled = cameraSource !== 'fake' ||
      fakeCameraBackend !== 'loop';
  $('loopVideoPath').disabled = cameraSource !== 'fake' ||
      fakeCameraBackend !== 'loop';
  if (!$('loopVideoPath').matches(':focus')) {
    $('loopVideoPath').value = settings.loopVideoFile || '';
  }
  const current = settings.loopVideoFile || '(generated default loop file)';
  const status = settings.loopVideoFile
      ? (settings.loopVideoExists ? 'file exists' : 'file missing')
      : 'generated on launch';
  $('mediaSettingsOutput').textContent = JSON.stringify({
    camera: cameraSource,
    fakeCameraBackend,
    microphone: microphoneSource,
    currentLoopVideo: current,
    status,
    selectedName: settings.loopVideoName || '',
    sizeBytes: settings.loopVideoBytes || 0,
    settingsFile: settings.settingsFile || '',
    restartRequired: 'yes; media flags apply when Chromium starts',
  }, null, 2);
}

function renderLocationSettings() {
  if (!$('locationSettingsOutput')) return;
  const settings = state.locationSettings || {};
  const source = settings.source || (settings.enabled === false ? 'real' : 'fake');
  $('locationSource').value = source;
  const fakeDisabled = source !== 'fake';
  $('locationLatitude').disabled = fakeDisabled;
  $('locationLongitude').disabled = fakeDisabled;
  $('locationAccuracy').disabled = fakeDisabled;
  if (!$('locationLatitude').matches(':focus')) {
    $('locationLatitude').value = settings.latitude ?? 28.6139;
  }
  if (!$('locationLongitude').matches(':focus')) {
    $('locationLongitude').value = settings.longitude ?? 77.2090;
  }
  if (!$('locationAccuracy').matches(':focus')) {
    $('locationAccuracy').value = settings.accuracy ?? 100;
  }
  $('locationSettingsOutput').textContent = JSON.stringify({
    source,
    latitude: Number($('locationLatitude').value),
    longitude: Number($('locationLongitude').value),
    accuracyMeters: Number($('locationAccuracy').value),
    settingsFile: settings.settingsFile || '',
    restartRequired: 'yes; the native source selector reads this at browser start',
  }, null, 2);
}

function renderPrivacyRules() {
  if (!$('privacyRulesList')) return;
  if (!state.privacyRules.length) {
    $('privacyRulesList').innerHTML = '<div class="empty">No remembered website rules.</div>';
    return;
  }
  $('privacyRulesList').innerHTML = state.privacyRules.map(rule => `
    <div class="row between" style="padding:10px 0;border-bottom:1px solid var(--line);gap:12px">
      <div class="wrap"><strong>${escapeHtml(rule.origin || '')}</strong>
        <div class="small muted">Camera ${escapeHtml(rule.cameraSource)} · Microphone ${escapeHtml(rule.microphoneSource)} · Location ${escapeHtml(rule.locationSource)}</div>
      </div>
      <button class="ghost" data-delete-privacy-rule="${escapeHtml(rule.id || '')}">Forget</button>
    </div>`).join('');
}

async function savePrivacyRule() {
  const origin = $('privacyRuleOrigin').value.trim();
  if (!origin) throw new Error('Enter a website origin first.');
  await api('/service/privacy-rules', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      origin,
      cameraSource: $('privacyRuleCamera').value,
      microphoneSource: $('privacyRuleMicrophone').value,
      locationSource: $('privacyRuleLocation').value,
      fakeCameraBackend: $('fakeCameraBackend').value,
    }),
  }, 30000);
  $('privacyRuleOrigin').value = '';
  await refreshPrivacyRules();
  showStatus('Website privacy rule saved.');
}

async function deletePrivacyRule(ruleId) {
  await api(`/service/privacy-rules/${encodeURIComponent(ruleId)}`, {method: 'DELETE'}, 30000);
  await refreshPrivacyRules();
  showStatus('Website privacy rule removed.');
}

async function importLoopVideo() {
  const file = $('loopVideoFile').files && $('loopVideoFile').files[0];
  if (!file) throw new Error('Choose a .y4m loop video first.');
  const result = await api('/service/loop-video', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/octet-stream',
      'X-Hardened-Filename': encodeURIComponent(file.name),
    },
    body: file,
  }, 10 * 60 * 1000);
  state.mediaSettings = result.settings || null;
  $('loopVideoFile').value = '';
  renderMediaSettings();
  showStatus('Loop video imported. Explicitly close and relaunch the shared browser to apply it.');
}

async function saveMediaSettings() {
  const payload = {
    cameraSource: $('cameraSource').value,
    fakeCameraBackend: $('fakeCameraBackend').value,
    microphoneSource: $('microphoneSource').value,
    loopVideoFile: $('loopVideoPath').value.trim(),
  };
  const result = await api('/service/media-settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  }, 30000);
  state.mediaSettings = result.settings || null;
  renderMediaSettings();
  showStatus('Media settings saved. Explicitly close and relaunch the shared browser to apply startup flags.');
}

async function saveLocationSettings() {
  const payload = {
    source: $('locationSource').value,
    latitude: Number($('locationLatitude').value),
    longitude: Number($('locationLongitude').value),
    accuracy: Number($('locationAccuracy').value),
  };
  const result = await api('/service/location-settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  }, 30000);
  state.locationSettings = result.settings || null;
  renderLocationSettings();
  showStatus('Location settings saved. Explicitly close and relaunch the shared browser to change startup defaults.');
}

async function restartService() {
  const result = await api('/service/restart', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: '{}',
  }, 10000);
  showStatus(result.message || 'Restart scheduled. Reconnect in a few seconds.');
  setTimeout(() => refreshAll({quiet: true}).catch(() => {}), 6000);
}

function showScreen(name) {
  state.activeScreen = name;
  for (const screen of screens) {
    $(`screen-${screen}`).classList.toggle('active', screen === name);
    const button = document.querySelector(`[data-screen="${screen}"]`);
    if (button) button.classList.toggle('active', screen === name);
  }
  if (name === 'feeds') refreshFeeds().catch(showError);
  if (name === 'runs') refreshJobs().catch(showError);
}

function renderDashboard() {
  const running = state.jobs.filter(job => !terminalStatuses.has(job.status)).length;
  const items = state.jobs.reduce((total, job) => total + Number(job.itemCount || 0), 0);
  $('kpiFeeds').textContent = String(state.feeds.length);
  $('kpiRuns').textContent = String(state.jobs.length);
  $('kpiRunning').textContent = String(running);
  $('kpiItems').textContent = String(items);
  $('dashboardFeeds').innerHTML = state.feeds.slice(0, 5).map(feedMini).join('') ||
      '<div class="empty">No feeds yet.</div>';
  $('dashboardRuns').innerHTML = state.jobs.slice(0, 8).map(runMini).join('') ||
      '<div class="empty">No runs yet.</div>';
}

function feedMini(feed) {
  const latest = feed.latestJob;
  return `<div class="row between" style="padding:8px 0;border-bottom:1px solid var(--line)">
    <div><strong>${escapeHtml(feed.name)}</strong><div class="small muted wrap">${escapeHtml(feed.sourceUrl || '')}</div></div>
    <span class="${statusClass(latest ? latest.status : 'no_run')}">${latest ? escapeHtml(latest.status) : 'no run'}</span>
  </div>`;
}

function runMini(job) {
  return `<div class="row between" style="padding:8px 0;border-bottom:1px solid var(--line)">
    <div><strong>${escapeHtml(job.title || job.url || job.id)}</strong><div class="small muted">${escapeHtml((job.config || {}).schema_id || job.id)}</div></div>
    <span class="${statusClass(job.status)}">${escapeHtml(job.status)} · ${job.itemCount || 0}</span>
  </div>`;
}

function renderFeeds() {
  const query = $('feedSearch') ? $('feedSearch').value.trim().toLowerCase() : '';
  const status = $('feedStatusFilter') ? $('feedStatusFilter').value : '';
  const sort = $('feedSort') ? $('feedSort').value : 'updated';
  let feeds = state.feeds.filter(feed => {
    const latestStatus = feed.latestJob ? feed.latestJob.status : 'no_run';
    const haystack = JSON.stringify(feed).toLowerCase();
    return (!query || haystack.includes(query)) && (!status || latestStatus === status);
  });
  feeds.sort((a, b) => {
    if (sort === 'name') return String(a.name).localeCompare(String(b.name));
    if (sort === 'items') return Number((b.latestJob || {}).itemCount || 0) - Number((a.latestJob || {}).itemCount || 0);
    return String(b.updatedAt || '').localeCompare(String(a.updatedAt || ''));
  });
  $('feedsList').innerHTML = feeds.length ? feeds.map(feedCard).join('') :
      '<div class="empty">No feeds match the current filter.</div>';
}

function feedCard(feed) {
  const latest = feed.latestJob;
  const latestStatus = latest ? latest.status : 'no_run';
  const fields = (feed.fields || []).map(field =>
    `<span class="pill">${escapeHtml(field.name)}: <span class="mono">${escapeHtml(field.selector || '(item)')}</span></span>`
  ).join(' ');
  return `<article class="card feed-card" data-feed-id="${escapeHtml(feed.id)}">
    <header>
      <div>
        <h3>${escapeHtml(feed.name)}</h3>
        <div class="small muted wrap">${escapeHtml(feed.sourceUrl || feed.url || '')}</div>
        <div class="small muted mono wrap">${escapeHtml(feed.id)}</div>
      </div>
      <span class="${statusClass(latestStatus)}">${latest ? escapeHtml(latest.status) : 'no run'}</span>
    </header>
    <div class="small">Item selector: <span class="mono wrap">${escapeHtml(feed.itemRoot || '')}</span></div>
    <div class="row">${fields || '<span class="muted small">No fields</span>'}</div>
    <div class="row between">
      <div class="small muted">${latest ? `${latest.itemCount || 0} latest items · ${escapeHtml(latest.updatedAt || '')}` : 'No outputs yet'}</div>
      <div class="row">
        <button class="primary" data-action="run-feed" data-feed-id="${escapeHtml(feed.id)}">Run</button>
        <button class="secondary" data-action="edit-feed" data-feed-id="${escapeHtml(feed.id)}">Edit</button>
        <button class="secondary" data-action="duplicate-feed" data-feed-id="${escapeHtml(feed.id)}">Duplicate</button>
        <button class="ghost" data-action="copy-feed-links" data-feed-id="${escapeHtml(feed.id)}">Copy links</button>
        <button class="danger" data-action="delete-feed" data-feed-id="${escapeHtml(feed.id)}">Delete</button>
      </div>
    </div>
    <details>
      <summary>Outputs and API</summary>
      <div class="drawer">
        ${latest ? feedLinks(feed.id) : '<div class="muted">Run this feed once to create outputs.</div>'}
        <div class="copybox" style="margin-top:10px">
          <code>curl -X POST ${escapeHtml(state.base)}/feeds/${escapeHtml(feed.id)}/run</code>
          <button data-action="copy-run-curl" data-feed-id="${escapeHtml(feed.id)}">Copy</button>
        </div>
      </div>
    </details>
  </article>`;
}

function renderRuns() {
  const query = $('runSearch') ? $('runSearch').value.trim().toLowerCase() : '';
  const status = $('runStatusFilter') ? $('runStatusFilter').value : '';
  const limit = Number($('runLimit') ? $('runLimit').value : 25);
  const jobs = state.jobs.filter(job => {
    const haystack = JSON.stringify(job).toLowerCase();
    return (!query || haystack.includes(query)) && (!status || job.status === status);
  }).slice(0, limit);
  if (!jobs.length) {
    $('runsList').innerHTML = '<div class="empty">No runs match the current filter.</div>';
    return;
  }
  $('runsList').innerHTML = `<div class="preview"><table><thead><tr>
    <th>Run</th><th>Status</th><th>Items</th><th>Outputs</th><th>Actions</th>
  </tr></thead><tbody>${jobs.map(runRow).join('')}</tbody></table></div>`;
}

function runRow(job) {
  return `<tr data-job-id="${escapeHtml(job.id)}">
    <td><div class="mono">${escapeHtml(job.id)}</div><div class="wrap">${escapeHtml(job.url)}</div><div class="small muted">${escapeHtml(job.title || '')}</div><div class="small muted mono">${escapeHtml((job.config || {}).schema_id || '')}</div></td>
    <td><span class="${statusClass(job.status)}">${escapeHtml(job.status)}</span><div class="small muted">${escapeHtml(job.reason || '')}</div></td>
    <td>${job.itemCount || 0}</td>
    <td>${jobOutputLinks(job)}</td>
    <td><div class="row">
      <button class="secondary" data-action="resume-job" data-job-id="${escapeHtml(job.id)}">Resume</button>
      <button class="ghost" data-action="stop-job" data-job-id="${escapeHtml(job.id)}">Stop</button>
      <button class="ghost" data-action="close-job" data-job-id="${escapeHtml(job.id)}">Close tab</button>
      <button class="ghost" data-action="inspect-job" data-job-id="${escapeHtml(job.id)}">Inspect</button>
    </div></td>
  </tr>`;
}

function statusClass(status) {
  if (status === 'completed') return 'pill ok';
  if (status === 'failed' || status === 'interrupted') return 'pill bad';
  if (status === 'needs_user' || status === 'running' || status === 'starting') return 'pill warn';
  return 'pill';
}

function refreshDraftUi() {
  $('feedUrl').value = state.draft.url || $('feedUrl').value;
  $('feedName').value = state.draft.name || $('feedName').value;
  $('feedId').value = state.draft.id || slug($('feedName').value || hostName($('feedUrl').value));
  $('advancedRoot').value = state.draft.itemRoot || $('advancedRoot').value;
  const schema = makeSchema();
  $('advancedJson').value = JSON.stringify(schema, null, 2);
  const canPick = Boolean(state.builderJobId) && !state.busy;
  const valid = validateSchema(schema, false).ok;
  $('pickItem').disabled = !canPick;
  $('pickCustomField').disabled = !canPick;
  $('previewFeed').disabled = !canPick || !valid;
  $('saveFeedOnly').disabled = !valid || state.busy;
  $('saveRunFeed').disabled = !valid || state.busy;
  $('itemState').textContent = schema.itemRoot ? `Found ${state.draft.itemCount || '?'} matching items` : 'No item selected';
  $('itemState').className = schema.itemRoot ? 'pill ok' : 'pill';
  $('builderJobState').textContent = state.builderJobId ? `Selection tab: ${state.builderJobId}` : '';
  renderFields();
}

function syncDraftFromInputs() {
  state.draft.url = $('feedUrl').value.trim();
  state.draft.name = $('feedName').value.trim();
  state.draft.id = $('feedId').value.trim();
  state.draft.itemRoot = $('advancedRoot').value.trim();
}

function makeSchema() {
  const url = $('feedUrl').value.trim() || state.draft.url;
  const name = $('feedName').value.trim() || state.draft.name || hostName(url) || 'Untitled feed';
  const id = $('feedId').value.trim() || state.draft.id || slug(name);
  return {
    id,
    name,
    url,
    sourceUrl: url,
    itemRoot: $('advancedRoot').value.trim() || state.draft.itemRoot,
    fields: state.draft.fields.map(field => ({...field})),
  };
}

function validateSchema(schema, throwOnError = true) {
  let message = '';
  if (!isHttpUrl(schema.sourceUrl || schema.url)) message = 'Enter a valid http:// or https:// URL.';
  else if (!schema.itemRoot) message = 'Select a repeated item first.';
  else if (!schema.fields.length) message = 'Select at least one field.';
  else if (!schema.fields.some(field => field.name === 'text' || field.required)) message = 'Pick Main text or mark at least one field required.';
  if (message && throwOnError) throw new Error(message);
  return {ok: !message, message};
}

function isHttpUrl(value) {
  try { const url = new URL(value); return url.protocol === 'http:' || url.protocol === 'https:'; } catch { return false; }
}

function hostName(url) {
  try { return new URL(url).hostname; } catch { return ''; }
}

function slug(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9._-]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 80) || `feed_${Date.now()}`;
}

function renderFieldButtons() {
  $('fieldButtons').innerHTML = fieldTypes.map(field =>
    `<button class="secondary" data-pick-field="${field.name}">${escapeHtml(field.label)}</button>`
  ).join('');
}

function renderFields() {
  if (!state.draft.fields.length) {
    $('fieldsList').innerHTML = '<div class="empty small">No fields selected yet.</div>';
    return;
  }
  $('fieldsList').innerHTML = state.draft.fields.map(field => `
    <div class="field-chip">
      <span><strong>${escapeHtml(field.name)}</strong> <span class="muted">${escapeHtml(field.mode || 'text')}</span><br><span class="mono small wrap">${escapeHtml(field.selector || '(item)')}</span></span>
      <button class="ghost" data-remove-field="${escapeHtml(field.name)}">Remove</button>
    </div>
  `).join('');
}

function customFieldConfig() {
  const name = slug($('customFieldName').value || 'field');
  return {name, label: name, mode: $('customFieldMode').value || 'text', required: false};
}

async function openPage() {
  syncDraftFromInputs();
  if (!isHttpUrl(state.draft.url)) throw new Error('Enter a valid http:// or https:// URL.');
  if (!state.draft.name) state.draft.name = hostName(state.draft.url) || 'New feed';
  if (!state.draft.id) state.draft.id = slug(state.draft.name);
  saveDraft();
  showStatus('Opening page in Chromium...');
  const result = await api('/jobs', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      url: state.draft.url,
      app_id: 'feed_manager',
      max_items: 1,
      timeout_seconds: 30,
      no_progress_limit: 1,
      raw_snapshots: false,
    }),
  });
  state.builderJobId = result.job.id;
  showStatus('Page opened. Switch to Chromium when the selector picker asks you to click.');
  refreshDraftUi();
  await refreshJobs();
}

async function pickSelector(message) {
  if (!state.builderJobId) throw new Error('Open a page first.');
  showStatus(message);
  const result = await api(`/jobs/${encodeURIComponent(state.builderJobId)}/control`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'pick_selector'}),
  }, 180000);
  if (!result.result || !result.result.ok) throw new Error((result.result && result.result.error) || 'Picker cancelled.');
  return result.result;
}

async function pickItemRoot() {
  const picked = await pickSelector('Click one repeated post/card/result in Chromium.');
  state.draft.itemRoot = picked.selector;
  state.draft.itemCount = picked.matchingCount || 0;
  saveDraft();
  showStatus(`Selected repeated item. ${state.draft.itemCount || '?'} matching elements detected.`);
  refreshDraftUi();
}

async function pickField(config) {
  if (!state.draft.itemRoot) throw new Error('Select repeated item first.');
  const picked = await pickSelector(`Click ${config.label || config.name} inside one item.`);
  const field = {
    name: config.name,
    selector: picked.selector,
    mode: config.mode || 'text',
    multiple: false,
    required: Boolean(config.required),
  };
  state.draft.fields = state.draft.fields.filter(existing => existing.name !== field.name);
  state.draft.fields.push(field);
  saveDraft();
  refreshDraftUi();
  showStatus(`Selected ${config.label || config.name}.`);
  await previewFeed().catch(() => {});
}

async function previewFeed() {
  const schema = makeSchema();
  validateSchema(schema);
  if (!state.builderJobId) throw new Error('Open a page first.');
  const result = await api(`/jobs/${encodeURIComponent(state.builderJobId)}/preview-schema`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({schema}),
  });
  const items = (result.page && result.page.items) || [];
  renderPreview(items, schema.fields);
  $('previewSummary').textContent = `Previewing ${items.length} rows from ${schema.itemRoot}`;
  $('previewPill').textContent = `${items.length} rows`;
  showStatus(`Previewed ${items.length} rows.`);
}

function renderPreview(items, fields) {
  if (!items.length) {
    $('previewTable').innerHTML = '<div class="empty">No rows matched yet. Pick a broader item or field.</div>';
    return;
  }
  const rows = items.slice(0, 50).map(item => {
    const values = item.fields || {};
    return `<tr>${fields.map(field => {
      const fallback = field.name === 'text' ? item.text :
        field.name === 'author' ? item.author :
        field.name === 'time' ? item.timeText :
        field.name === 'permalink' ? item.permalink : '';
      const value = values[field.name] || fallback || '';
      return `<td>${escapeHtml(Array.isArray(value) ? value.join(', ') : value)}</td>`;
    }).join('')}</tr>`;
  }).join('');
  $('previewTable').innerHTML = `<table><thead><tr>${fields.map(field => `<th>${escapeHtml(field.name)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table>`;
}

async function saveFeedOnly() {
  const schema = makeSchema();
  validateSchema(schema);
  const saved = await api('/feeds', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(schema),
  });
  state.draft.id = saved.feed.id;
  saveDraft();
  showStatus(`Saved feed: ${saved.feed.name}`);
  await refreshFeeds();
  showScreen('feeds');
}

async function saveAndRunFeed() {
  const schema = makeSchema();
  validateSchema(schema);
  const saved = await api('/feeds', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(schema),
  });
  const run = await api(`/feeds/${encodeURIComponent(saved.feed.id)}/run`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(jobRunBody()),
  });
  showStatus(`Saved ${saved.feed.name}; running now.`);
  await pollJob(run.job.id);
  await refreshAll({quiet: true});
  $('publishLinks').innerHTML = feedLinks(saved.feed.id);
}

async function pollJob(jobId) {
  for (let attempt = 0; attempt < 240; attempt++) {
    const result = await api(`/jobs/${encodeURIComponent(jobId)}`, {}, 15000);
    const job = result.job;
    showStatus(`Running: ${job.itemCount || 0} items · ${job.status}`);
    if (terminalStatuses.has(job.status) || job.status === 'needs_user') return job;
    await delay(1000);
  }
  showStatus('Run is still active. Monitor it in Runs.');
}

async function handleFeedAction(event) {
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  const action = button.dataset.action;
  const feedId = button.dataset.feedId;
  await withBusy(button, async () => {
    if (action === 'run-feed') {
      const run = await api(`/feeds/${encodeURIComponent(feedId)}/run`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(jobRunBody()),
      });
      await pollJob(run.job.id);
      await refreshAll({quiet: true});
    } else if (action === 'edit-feed') {
      await loadFeedIntoBuilder(feedId);
      showScreen('create');
    } else if (action === 'duplicate-feed') {
      await duplicateFeed(feedId);
    } else if (action === 'delete-feed') {
      if (!confirm(`Delete feed ${feedId}?`)) return;
      await api(`/feeds/${encodeURIComponent(feedId)}`, {method: 'DELETE'});
      await refreshFeeds();
      showStatus('Feed deleted.');
    } else if (action === 'copy-feed-links') {
      copyText(outputLinksText(feedId), 'Feed links copied.');
    } else if (action === 'copy-run-curl') {
      copyText(`curl -X POST ${state.base}/feeds/${feedId}/run`, 'Run command copied.');
    }
  });
}

async function handleRunAction(event) {
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  const action = button.dataset.action;
  const jobId = button.dataset.jobId;
  await withBusy(button, async () => {
    if (action === 'resume-job') await api(`/jobs/${encodeURIComponent(jobId)}/resume`, {method: 'POST'});
    if (action === 'stop-job') await api(`/jobs/${encodeURIComponent(jobId)}/stop`, {method: 'POST'});
    if (action === 'close-job') await api(`/jobs/${encodeURIComponent(jobId)}/close-tab`, {method: 'POST'});
    if (action === 'inspect-job') {
      const job = await api(`/jobs/${encodeURIComponent(jobId)}`);
      copyText(JSON.stringify(job.job, null, 2), 'Job JSON copied.');
    }
    await refreshJobs();
  });
}

async function loadFeedIntoBuilder(feedId) {
  const result = await api(`/feeds/${encodeURIComponent(feedId)}`);
  const feed = result.feed;
  const schema = feed.schema || feed;
  state.draft = {
    id: feed.id || schema.id || '',
    name: feed.name || schema.name || '',
    url: feed.sourceUrl || feed.url || schema.sourceUrl || '',
    itemRoot: schema.itemRoot || feed.itemRoot || '',
    itemCount: 0,
    fields: schema.fields || feed.fields || [],
  };
  $('feedUrl').value = state.draft.url;
  $('feedName').value = state.draft.name;
  $('feedId').value = state.draft.id;
  $('advancedRoot').value = state.draft.itemRoot;
  saveDraft();
  refreshDraftUi();
  showStatus(`Loaded ${state.draft.name} for editing.`);
}

async function duplicateFeed(feedId) {
  const result = await api(`/feeds/${encodeURIComponent(feedId)}`);
  const feed = result.feed;
  const schema = feed.schema || feed;
  const copy = {
    ...schema,
    id: slug(`${feed.id}_copy_${Date.now()}`),
    name: `${feed.name || feed.id} copy`,
  };
  const saved = await api('/feeds', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(copy),
  });
  await refreshFeeds();
  showStatus(`Duplicated as ${saved.feed.name}.`);
}

function feedLinks(feedId) {
  return ['feed.json', 'feed.rss', 'feed.atom', 'items.csv', 'latest.html'].map(file =>
    `<a href="${outputUrlForFeed(feedId, file)}" target="_blank" rel="noreferrer">${labelForFile(file)}</a>`
  ).join(' · ');
}

function jobOutputLinks(job) {
  return ['latest.html', 'feed.json', 'feed.rss', 'feed.atom', 'items.csv', 'visible_text.txt'].map(file =>
    `<a href="${state.base}/jobs/${encodeURIComponent(job.id)}/${file}${tokenQuery()}" target="_blank" rel="noreferrer">${labelForFile(file)}</a>`
  ).join(' · ');
}

function outputUrlForFeed(feedId, filename) {
  return `${state.base}/feeds/${encodeURIComponent(feedId)}/latest/${filename}${tokenQuery()}`;
}

function tokenQuery() {
  return state.token ? `?token=${encodeURIComponent(state.token)}` : '';
}

function labelForFile(file) {
  return {
    'feed.json': 'JSON Feed',
    'feed.rss': 'RSS',
    'feed.atom': 'Atom',
    'items.csv': 'CSV',
    'latest.html': 'HTML',
    'visible_text.txt': 'Text',
  }[file] || file;
}

function outputLinksText(feedId) {
  return ['feed.json', 'feed.rss', 'feed.atom', 'items.csv', 'latest.html']
      .map(file => `${labelForFile(file)}: ${outputUrlForFeed(feedId, file)}`)
      .join('\n');
}

function resetBuilder() {
  state.builderJobId = '';
  state.draft = blankDraft();
  $('feedUrl').value = '';
  $('feedName').value = '';
  $('feedId').value = '';
  $('advancedRoot').value = '';
  $('customFieldName').value = '';
  $('publishLinks').innerHTML = '';
  $('previewSummary').textContent = 'Open a page and select fields.';
  $('previewPill').textContent = '0 rows';
  $('previewTable').innerHTML = '';
  localStorage.removeItem('hardenedFeedManagerDraft');
  refreshDraftUi();
  showStatus('Draft reset.');
}

function applyAdvancedJsonSafely() {
  try {
    const schema = JSON.parse($('advancedJson').value);
    state.draft = {
      id: schema.id || '',
      name: schema.name || '',
      url: schema.sourceUrl || schema.url || '',
      itemRoot: schema.itemRoot || '',
      itemCount: 0,
      fields: Array.isArray(schema.fields) ? schema.fields : [],
    };
    $('feedUrl').value = state.draft.url;
    $('feedName').value = state.draft.name;
    $('feedId').value = state.draft.id;
    $('advancedRoot').value = state.draft.itemRoot;
    saveDraft();
    refreshDraftUi();
    showStatus('Advanced JSON applied.');
  } catch (error) {
    showError(error);
  }
}

function configureAutoRefresh() {
  if (state.autoRefreshTimer) clearInterval(state.autoRefreshTimer);
  state.autoRefreshTimer = 0;
  if ($('autoRefresh').checked) {
    state.autoRefreshTimer = setInterval(() => refreshAll({quiet: true}).catch(() => {}), 5000);
  }
  saveSettings();
}

function exportUiState() {
  const payload = {
    settings: runSettings(),
    mediaSettings: state.mediaSettings,
    locationSettings: state.locationSettings,
    draft: state.draft,
    feeds: state.feeds,
    jobs: state.jobs,
  };
  copyText(JSON.stringify(payload, null, 2), 'UI state copied.');
}

async function copyText(text, message) {
  try {
    await navigator.clipboard.writeText(text);
    showStatus(message || 'Copied.');
  } catch {
    showStatus(text, true);
  }
}

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

init();
</script>
</body>
</html>"""


def cdp_http_json(
    endpoint: str,
    path: str,
    method: str = "GET",
    timeout: float = 10,
) -> Any:
  url = endpoint.rstrip("/") + path
  request = urllib.request.Request(url, method=method)
  with urllib.request.urlopen(request, timeout=timeout) as response:
    text = response.read().decode("utf-8")
  if not text:
    return {}
  return json.loads(text)


def cdp_create_tab(endpoint: str, url: str) -> dict[str, Any]:
  path = "/json/new?" + quote(url, safe=":/?#[]@!$&'()*+,;=%")
  try:
    return cdp_http_json(endpoint, path, "PUT")
  except urllib.error.HTTPError as error:
    if error.code not in (HTTPStatus.METHOD_NOT_ALLOWED, HTTPStatus.NOT_FOUND):
      raise
    return cdp_http_json(endpoint, path, "GET")


def cdp_close_tab(endpoint: str, target_id: str) -> None:
  try:
    cdp_http_json(endpoint, "/json/close/" + quote(target_id, safe=""), "GET")
  except Exception:
    pass


def wait_for_document_ready(cdp: CdpWebSocket, timeout: float) -> None:
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    try:
      state = evaluate(cdp, "document.readyState", timeout=5)
      if state in ("interactive", "complete"):
        return
    except Exception:
      pass
    time.sleep(0.5)


def evaluate(cdp: CdpWebSocket, expression: str, timeout: float = 30) -> Any:
  result = cdp.command("Runtime.evaluate", {
      "expression": expression,
      "returnByValue": True,
      "awaitPromise": True,
  }, timeout=timeout)
  if "exceptionDetails" in result:
    raise CdpError(str(result["exceptionDetails"]))
  remote = result.get("result", {})
  if "value" in remote:
    return remote["value"]
  return remote.get("description")


def connect_job_cdp(job: Job) -> CdpWebSocket:
  if not job.websocket_url:
    raise ValueError("job has no live websocket URL")
  cdp = CdpWebSocket(job.websocket_url)
  cdp.connect()
  cdp.command("Runtime.enable")
  cdp.command("Page.enable")
  return cdp


def run_job_control(
    job: Job,
    cdp: CdpWebSocket,
    action: str,
    request: dict[str, Any],
) -> dict[str, Any]:
  if action == "pick_selector":
    timeout = clamp_int(request.get("timeout_seconds", request.get("timeoutSeconds", 120)), 5, 300)
    result = evaluate(cdp, SELECTOR_PICKER_JS, timeout=timeout + 5)
    return result if isinstance(result, dict) else {"ok": True, "value": result}

  if action == "page_summary":
    result = evaluate(cdp, page_summary_js(), timeout=30)
    return result if isinstance(result, dict) else {"value": result}

  if action == "visible_text":
    result = evaluate(cdp, visible_text_js(
        clamp_int(request.get("max_chars", request.get("maxChars", 200000)), 1000, 5_000_000)),
                      timeout=30)
    return result if isinstance(result, dict) else {"value": result}

  if action == "wait_for_selector":
    selector = str(request.get("selector") or "").strip()
    if not selector:
      raise ValueError("selector is required")
    result = evaluate(cdp, wait_for_selector_js(selector), timeout=35)
    return result if isinstance(result, dict) else {"value": result}

  if action == "scroll":
    x = clamp_int(request.get("x", 0), -100000, 100000)
    y = clamp_int(request.get("y", request.get("pixels", 800)), -100000, 100000)
    expression = f"""
(() => {{
  const scroller = document.scrollingElement || document.documentElement || document.body;
  const before = {{x: scroller.scrollLeft, y: scroller.scrollTop}};
  scroller.scrollBy({{left: {x}, top: {y}, behavior: 'instant'}});
  return {{ok: true, before, after: {{x: scroller.scrollLeft, y: scroller.scrollTop}},
      scrollHeight: scroller.scrollHeight, scrollWidth: scroller.scrollWidth}};
}})()
"""
    result = evaluate(cdp, expression, timeout=10)
    return result if isinstance(result, dict) else {"value": result}

  if action == "click":
    selector = str(request.get("selector") or "").strip()
    if not selector:
      raise ValueError("selector is required")
    expression = r"""
(() => {
  const selector = __SELECTOR__;
  const node = document.querySelector(selector);
  if (!node) return {ok: false, error: 'selector not found', selector};
  node.scrollIntoView({block: 'center', inline: 'center'});
  node.dispatchEvent(new MouseEvent('mouseover', {bubbles: true, cancelable: true, view: window}));
  node.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
  node.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
  node.click();
  return {ok: true, selector, text: String(node.innerText || node.textContent || '').slice(0, 1000)};
})()
""".replace("__SELECTOR__", json.dumps(selector))
    result = evaluate(cdp, expression, timeout=10)
    return result if isinstance(result, dict) else {"value": result}

  if action == "type":
    selector = str(request.get("selector") or "").strip()
    text = str(request.get("text") or "")
    if not selector:
      raise ValueError("selector is required")
    expression = r"""
(() => {
  const selector = __SELECTOR__;
  const text = __TEXT__;
  const node = document.querySelector(selector);
  if (!node) return {ok: false, error: 'selector not found', selector};
  node.focus();
  if ('value' in node) {
    node.value = text;
    node.dispatchEvent(new Event('input', {bubbles: true}));
    node.dispatchEvent(new Event('change', {bubbles: true}));
  } else {
    node.textContent = text;
    node.dispatchEvent(new InputEvent('input', {bubbles: true, data: text}));
  }
  return {ok: true, selector};
})()
""".replace("__SELECTOR__", json.dumps(selector)).replace("__TEXT__", json.dumps(text))
    result = evaluate(cdp, expression, timeout=10)
    return result if isinstance(result, dict) else {"value": result}

  if action == "evaluate":
    expression = str(request.get("expression") or "")
    if not expression:
      raise ValueError("expression is required")
    result = evaluate(cdp, expression, timeout=clamp_int(request.get("timeout_seconds", 30), 1, 300))
    return {"ok": True, "value": result}

  if action == "screenshot":
    full_page = bool(request.get("full_page", request.get("fullPage", False)))
    result = cdp.command("Page.captureScreenshot", {
        "format": "png",
        "captureBeyondViewport": full_page,
        "fromSurface": True,
    }, timeout=30)
    data = str(result.get("data") or "")
    if data:
      job.output_dir.mkdir(parents=True, exist_ok=True)
      path = job.output_dir / f"screenshot-{int(time.time())}.png"
      path.write_bytes(base64.b64decode(data))
      job.exports["lastScreenshot"] = str(path)
      return {"ok": True, "path": str(path), "base64": data}
    return {"ok": False, "error": "CDP returned no screenshot data"}

  raise ValueError(f"unsupported control action: {action}")


def add_items(job: Job, raw_items: Any, seen_keys: set[str]) -> int:
  if not isinstance(raw_items, list):
    return 0
  added = 0
  new_items: list[dict[str, Any]] = []
  with job.lock:
    max_items = job.config["max_items"]
    for raw in raw_items:
      if max_items and len(job.items) >= max_items:
        break
      if not isinstance(raw, dict):
        continue
      item = normalize_item(raw, job, len(job.items))
      key = item["key"]
      if not key or key in seen_keys:
        continue
      seen_keys.add(key)
      job.items.append(item)
      new_items.append(item)
      added += 1
    if added:
      job.updated_at = time.time()
  if new_items:
    append_items_jsonl(job, new_items)
  return added


def append_items_jsonl(job: Job, items: list[dict[str, Any]]) -> None:
  if not items:
    return
  path = job.output_dir / "items.jsonl"
  text = "".join(
      json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
      for item in items)
  with job.output_lock:
    job.output_dir.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
      file.write(text)
  with job.lock:
    job.exports["itemsJsonl"] = str(path)


def normalize_item(raw: dict[str, Any], job: Job, index: int) -> dict[str, Any]:
  text = limit_text(raw.get("text", ""), 20000)
  author = limit_text(raw.get("author", ""), 300)
  time_text = limit_text(raw.get("timeText", raw.get("time_text", "")), 300)
  permalink = clean_url(raw.get("permalink", ""))
  links = clean_url_list(raw.get("links", []), 80)
  media_urls = clean_url_list(raw.get("media", raw.get("media_urls", [])), 80)
  adapter = limit_text(raw.get("adapter", "generic"), 80) or "generic"
  key = limit_text(raw.get("key") or permalink or
                   f"{adapter}:{author}:{time_text}:{text[:600]}", 1200)
  fields = clean_item_fields(raw.get("fields", {}), 20000)
  html_fields = clean_item_fields(raw.get("htmlFields", raw.get("html_fields", {})), 100000)
  item = {
      "schema_version": SCHEMA_VERSION,
      "job_id": job.id,
      "app_id": job.app_id,
      "source_url": job.url,
      "current_url": job.current_url or job.url,
      "index": index,
      "key": key,
      "adapter": adapter,
      "captured_at": iso_time(time.time()),
      "author": author,
      "time_text": time_text,
      "text": text,
      "permalink": permalink,
      "links": links,
      "media_urls": media_urls,
  }
  schema_id = limit_text(raw.get("schemaId", raw.get("schema_id", "")), 120)
  schema_name = limit_text(raw.get("schemaName", raw.get("schema_name", "")), 200)
  if schema_id:
    item["schema_id"] = schema_id
  if schema_name:
    item["schema_name"] = schema_name
  if fields:
    item["fields"] = fields
  if html_fields:
    item["html_fields"] = html_fields
  return item


def should_pause_for_user(job: Job, page_state: dict[str, Any], no_progress: int) -> bool:
  if len(job.items) > 0:
    return False
  if no_progress < 2:
    return False
  body_text = str(page_state.get("bodyText", "")).lower()
  return any(marker in body_text for marker in BLOCKED_TEXT_MARKERS)


def sleep_interruptibly(job: Job, seconds: float) -> None:
  deadline = time.monotonic() + seconds
  while not job.stop_event.is_set() and time.monotonic() < deadline:
    time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def capture_raw_outputs(job: Job, cdp: CdpWebSocket) -> None:
  raw = evaluate(cdp, raw_html_js(job.config["max_raw_html_chars"]), timeout=30)
  if isinstance(raw, dict):
    job.current_url = str(raw.get("href") or job.current_url)
    job.title = str(raw.get("title") or job.title)
    raw_html = str(raw.get("html") or "")
    job.raw_html_truncated = bool(raw.get("truncated"))
    job.raw_html_length = int(raw.get("length") or len(raw_html))
    write_text_file(job, "rawHtml", "raw.html", raw_html)
  try:
    snapshot = cdp.command("Page.captureSnapshot", {"format": "mhtml"}, timeout=45)
    data = str(snapshot.get("data") or "")
    if data:
      write_text_file(job, "snapshotMhtml", "snapshot.mhtml", data)
  except Exception as error:
    job.exports["snapshotMhtmlError"] = str(error)
  try:
    visible = evaluate(cdp, visible_text_js(job.config["max_raw_html_chars"]), timeout=30)
    if isinstance(visible, dict):
      write_text_file(job, "visibleText", "visible_text.txt", str(visible.get("text") or ""))
  except Exception as error:
    job.exports["visibleTextError"] = str(error)


def write_outputs(job: Job, *, force: bool = False) -> bool:
  with job.lock:
    item_count = len(job.items)
    dirty_items = item_count - job.last_output_item_count
    elapsed = time.monotonic() - job.last_output_flush_monotonic
    if not force and (
        dirty_items < job.config["checkpoint_items"] or
        elapsed < DERIVED_CHECKPOINT_MIN_SECONDS):
      return False

  # Derived archives are intentionally isolated from the browser/broker process.
  # The global lock coalesces checkpoint pressure into one exporter at a time.
  with EXPORT_PROCESS_LOCK, job.output_lock:
    with job.lock:
      item_count = len(job.items)
      dirty_items = item_count - job.last_output_item_count
      elapsed = time.monotonic() - job.last_output_flush_monotonic
      if not force and (
          dirty_items < job.config["checkpoint_items"] or
          elapsed < DERIVED_CHECKPOINT_MIN_SECONDS):
        return False
      snapshot = {"job": job_record(job)}
      job.output_dir.mkdir(parents=True, exist_ok=True)
      items_path = job.output_dir / "items.jsonl"
      if not items_path.exists():
        atomic_write_text(items_path, "")
      snapshot_path = job.output_dir / (
          f".export-{secrets.token_hex(6)}.json")
      atomic_write_json(snapshot_path, snapshot)
    try:
      result = subprocess.run(
          [sys.executable, str(Path(__file__).resolve()),
           "--export-snapshot", str(snapshot_path)],
          stdin=subprocess.DEVNULL,
          stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,
          text=True,
          timeout=180,
          check=False)
      if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"export worker failed: {detail}")
    finally:
      with contextlib.suppress(OSError):
        snapshot_path.unlink()
    with job.lock:
      job.exports.update(expected_export_paths(job.output_dir))
      job.last_output_item_count = item_count
      job.last_output_flush_monotonic = time.monotonic()
  return True


def expected_export_paths(output_dir: Path) -> dict[str, str]:
  exports = {
      "itemsJsonl": str(output_dir / "items.jsonl"),
      "itemsCsv": str(output_dir / "items.csv"),
      "latestJson": str(output_dir / "latest.json"),
      "latestHtml": str(output_dir / "latest.html"),
      "feedJson": str(output_dir / "feed.json"),
      "feedRss": str(output_dir / "feed.rss"),
      "feedAtom": str(output_dir / "feed.atom"),
      "manifest": str(output_dir / "manifest.json"),
  }
  for filename, key in (
      ("visible_text.txt", "visibleText"),
      ("raw.html", "rawHtml"),
      ("snapshot.mhtml", "snapshotMhtml"),
      ("events.jsonl", "eventsJsonl"),
  ):
    if (output_dir / filename).exists():
      exports[key] = str(output_dir / filename)
  return exports


def write_outputs_in_worker(job: Job) -> None:
  with job.output_lock:
    with job.lock:
      item_count = len(job.items)
      job.output_dir.mkdir(parents=True, exist_ok=True)
      job.exports.update(expected_export_paths(job.output_dir))
      items = list(job.items)
      summary = job.summary(False)

    job.output_dir.mkdir(parents=True, exist_ok=True)
    archive = {
        "schemaVersion": SCHEMA_VERSION,
        "format": "hardened-scrape-broker-archive",
        "job": summary,
        "items": items,
    }
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "format": "hardened-scrape-broker-manifest",
        "generatedAt": iso_time(time.time()),
        "job": summary,
        "files": {
            "itemsJsonl": str(job.output_dir / "items.jsonl"),
            "itemsCsv": str(job.output_dir / "items.csv"),
            "latestJson": str(job.output_dir / "latest.json"),
            "latestHtml": str(job.output_dir / "latest.html"),
            "feedJson": str(job.output_dir / "feed.json"),
            "feedRss": str(job.output_dir / "feed.rss"),
            "feedAtom": str(job.output_dir / "feed.atom"),
            "visibleText": str(job.output_dir / "visible_text.txt"),
            "rawHtml": str(job.output_dir / "raw.html"),
            "snapshotMhtml": str(job.output_dir / "snapshot.mhtml"),
            "manifest": str(job.output_dir / "manifest.json"),
            "eventsJsonl": str(job.output_dir / "events.jsonl"),
        },
        "api": {
            "defaultHost": "127.0.0.1",
            "defaultPort": DEFAULT_PORT,
            "endpoints": [
                "GET /jobs/<job_id>",
                "GET /jobs/<job_id>/items",
                "GET /jobs/<job_id>/items.jsonl",
                "GET /jobs/<job_id>/items.csv",
                "GET /jobs/<job_id>/latest.html",
                "GET /jobs/<job_id>/feed.json",
                "GET /jobs/<job_id>/feed.rss",
                "GET /jobs/<job_id>/feed.atom",
                "GET /jobs/<job_id>/visible_text.txt",
                "GET /jobs/<job_id>/raw.html",
                "GET /jobs/<job_id>/snapshot.mhtml",
                "GET /jobs/<job_id>/events.jsonl",
                "GET /jobs/<job_id>/events",
            ],
        },
    }

    items_jsonl = job.output_dir / "items.jsonl"
    if not items_jsonl.exists():
      atomic_write_text(items_jsonl, "".join(
          json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
          for item in items))
    atomic_write_text(job.output_dir / "items.csv", build_items_csv(items))
    atomic_write_text(
        job.output_dir / "latest.json",
        json.dumps(archive, indent=2, ensure_ascii=False))
    atomic_write_text(
        job.output_dir / "feed.json",
        json.dumps(build_json_feed(job, items), indent=2, ensure_ascii=False))
    atomic_write_text(job.output_dir / "feed.rss", build_rss_feed(job, items))
    atomic_write_text(job.output_dir / "feed.atom", build_atom_feed(job, items))
    atomic_write_text(
        job.output_dir / "manifest.json",
        json.dumps(manifest, indent=2, ensure_ascii=False))
    atomic_write_text(
        job.output_dir / "latest.html", build_archive_html(job, items))

    with job.lock:
      job.last_output_item_count = item_count
      job.last_output_flush_monotonic = time.monotonic()


def export_snapshot(snapshot_path: Path) -> int:
  try:
    value = json.loads(snapshot_path.read_text(encoding="utf-8"))
    raw = value["job"]
    output_dir = Path(str(raw["outputDir"])).expanduser().resolve()
    job = Job(
        id=str(raw["id"]),
        app_id=str(raw.get("appId") or "app"),
        url=str(raw["url"]),
        output_dir=output_dir,
        config=dict(raw.get("config") or {}),
        status=str(raw.get("status") or "running"),
        reason=str(raw.get("reason") or ""),
        current_url=str(raw.get("currentUrl") or ""),
        title=str(raw.get("title") or ""),
        exports=dict(raw.get("exports") or {}),
        raw_html_truncated=bool(raw.get("rawHtmlTruncated")),
        raw_html_length=int(raw.get("rawHtmlLength") or 0))
    job.items = load_items_file(output_dir / "items.jsonl")
    write_outputs_in_worker(job)
    return 0
  except Exception as error:  # pylint: disable=broad-except
    print(str(error), file=sys.stderr)
    return 1


def write_text_file(job: Job, export_key: str, filename: str, text: str) -> None:
  job.output_dir.mkdir(parents=True, exist_ok=True)
  path = job.output_dir / filename
  path.write_text(text, encoding="utf-8")
  job.exports[export_key] = str(path)


def build_archive_html(job: Job, items: list[dict[str, Any]]) -> str:
  articles = []
  for item in items:
    links = "".join(
        f'<a href="{escape_attr(link)}" rel="noreferrer">{html.escape(link)}</a>'
        for link in item.get("links", []))
    media = "".join(
        f'<a href="{escape_attr(link)}" rel="noreferrer">{html.escape(link)}</a>'
        for link in item.get("media_urls", []))
    search = " ".join(str(value) for value in [
        item.get("author", ""),
        item.get("time_text", ""),
        item.get("text", ""),
        " ".join(item.get("links", [])),
    ]).lower()
    permalink = item.get("permalink") or ""
    open_link = (f'<a class="permalink" href="{escape_attr(permalink)}" '
                 f'rel="noreferrer">Open original</a>') if permalink else ""
    articles.append(f"""<article class="item" data-search="{escape_attr(search)}">
  <header>
    <div>
      <div class="author">{html.escape(str(item.get("author") or "Unknown"))}</div>
      <div class="meta">{html.escape(str(item.get("time_text") or item.get("captured_at") or ""))}</div>
    </div>
    {open_link}
  </header>
  <pre>{html.escape(str(item.get("text") or ""))}</pre>
  {f'<section><h2>Links</h2>{links}</section>' if links else ''}
  {f'<section><h2>Media URLs</h2>{media}</section>' if media else ''}
</article>""")
  return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hardened Scrape - {html.escape(job.url)}</title>
  <style>
    :root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
    body {{ margin: 0; background: Canvas; color: CanvasText; }}
    main {{ max-width: 980px; margin: 0 auto; padding: 24px; }}
    .top {{ position: sticky; top: 0; background: Canvas; padding: 16px 0; border-bottom: 1px solid color-mix(in srgb, CanvasText 18%, transparent); }}
    h1 {{ margin: 0 0 6px; font-size: 24px; }}
    .source {{ overflow-wrap: anywhere; opacity: .75; }}
    input {{ box-sizing: border-box; width: 100%; margin-top: 14px; padding: 10px 12px; font: inherit; }}
    .item {{ border-bottom: 1px solid color-mix(in srgb, CanvasText 14%, transparent); padding: 18px 0; }}
    .item[hidden] {{ display: none; }}
    header {{ display: flex; justify-content: space-between; gap: 16px; align-items: start; }}
    .author {{ font-weight: 650; }}
    .meta {{ font-size: 13px; opacity: .7; }}
    .permalink {{ white-space: nowrap; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.45; }}
    section a {{ display: block; overflow-wrap: anywhere; margin: 3px 0; }}
  </style>
</head>
<body>
<main>
  <div class="top">
    <h1>Hardened Scrape</h1>
    <div class="source">{html.escape(job.url)}</div>
    <input id="search" type="search" placeholder="Search captured text">
    <div><span id="shown">{len(items)}</span> / {len(items)} items</div>
  </div>
  {"".join(articles)}
</main>
<script>
const input = document.getElementById('search');
const shown = document.getElementById('shown');
const items = [...document.querySelectorAll('.item')];
input.addEventListener('input', () => {{
  const query = input.value.trim().toLowerCase();
  let count = 0;
  for (const item of items) {{
    const match = !query || item.dataset.search.includes(query);
    item.hidden = !match;
    if (match) count++;
  }}
  shown.textContent = String(count);
}});
</script>
</body>
</html>"""


def build_items_csv(items: list[dict[str, Any]]) -> str:
  buffer = io.StringIO()
  fieldnames = [
      "index",
      "captured_at",
      "adapter",
      "schema_id",
      "author",
      "time_text",
      "text",
      "permalink",
      "links",
      "media_urls",
      "fields_json",
  ]
  writer = csv.DictWriter(buffer, fieldnames=fieldnames)
  writer.writeheader()
  for item in items:
    writer.writerow({
        "index": item.get("index", ""),
        "captured_at": item.get("captured_at", ""),
        "adapter": item.get("adapter", ""),
        "schema_id": item.get("schema_id", ""),
        "author": item.get("author", ""),
        "time_text": item.get("time_text", ""),
        "text": item.get("text", ""),
        "permalink": item.get("permalink", ""),
        "links": json.dumps(item.get("links", []), ensure_ascii=False),
        "media_urls": json.dumps(item.get("media_urls", []), ensure_ascii=False),
        "fields_json": json.dumps(item.get("fields", {}), ensure_ascii=False),
    })
  return buffer.getvalue()


def build_json_feed(job: Job, items: list[dict[str, Any]]) -> dict[str, Any]:
  return {
      "version": "https://jsonfeed.org/version/1.1",
      "title": job.title or job.url,
      "home_page_url": job.current_url or job.url,
      "feed_url": "",
      "description": f"Hardened scrape feed for {job.url}",
      "items": [{
          "id": str(item.get("key") or item.get("index")),
          "url": item.get("permalink") or "",
          "title": feed_item_title(item),
          "content_text": str(item.get("text") or ""),
          "date_published": item.get("captured_at") or iso_time(time.time()),
          "author": {"name": item.get("author") or ""},
          "attachments": [
              {"url": url, "mime_type": ""}
              for url in item.get("media_urls", [])
          ],
          "_hardened": item,
      } for item in items],
  }


def build_rss_feed(job: Job, items: list[dict[str, Any]]) -> str:
  item_xml = []
  for item in items:
    title = html.escape(feed_item_title(item))
    link = html.escape(str(item.get("permalink") or job.current_url or job.url))
    description = html.escape(str(item.get("text") or ""))
    guid = html.escape(str(item.get("key") or item.get("index") or link))
    pub_date = html.escape(str(item.get("captured_at") or ""))
    item_xml.append(f"""<item>
  <title>{title}</title>
  <link>{link}</link>
  <guid isPermaLink="false">{guid}</guid>
  <pubDate>{pub_date}</pubDate>
  <description>{description}</description>
</item>""")
  return f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
<channel>
  <title>{html.escape(job.title or job.url)}</title>
  <link>{html.escape(job.current_url or job.url)}</link>
  <description>Hardened scrape feed</description>
  {"".join(item_xml)}
</channel>
</rss>
"""


def build_atom_feed(job: Job, items: list[dict[str, Any]]) -> str:
  updated = iso_time(time.time()) or ""
  entries = []
  for item in items:
    title = html.escape(feed_item_title(item))
    link = html.escape(str(item.get("permalink") or job.current_url or job.url))
    identifier = html.escape(str(item.get("key") or item.get("index") or link))
    content = html.escape(str(item.get("text") or ""))
    published = html.escape(str(item.get("captured_at") or updated))
    entries.append(f"""<entry>
  <title>{title}</title>
  <id>{identifier}</id>
  <link href="{link}"/>
  <updated>{published}</updated>
  <content type="text">{content}</content>
</entry>""")
  return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>{html.escape(job.title or job.url)}</title>
  <id>{html.escape(job.url)}</id>
  <updated>{html.escape(updated)}</updated>
  <link href="{html.escape(job.current_url or job.url)}"/>
  {"".join(entries)}
</feed>
"""


def feed_item_title(item: dict[str, Any]) -> str:
  fields = item.get("fields", {})
  if isinstance(fields, dict):
    for name in ("title", "headline", "name"):
      value = fields.get(name)
      if isinstance(value, list):
        value = next((entry for entry in value if entry), "")
      if value:
        return limit_text(value, 160)
  text = str(item.get("text") or "").strip()
  if text:
    return limit_text(text, 120)
  return str(item.get("permalink") or f"Item {item.get('index', '')}")


OUTPUT_FILE_KEYS = {
    "items.jsonl": "itemsJsonl",
    "items.csv": "itemsCsv",
    "latest.html": "latestHtml",
    "latest.json": "latestJson",
    "feed.json": "feedJson",
    "feed.rss": "feedRss",
    "feed.atom": "feedAtom",
    "visible_text.txt": "visibleText",
    "raw.html": "rawHtml",
    "snapshot.mhtml": "snapshotMhtml",
    "manifest.json": "manifest",
    "events.jsonl": "eventsJsonl",
}


def output_file_key(filename: str) -> str:
  return OUTPUT_FILE_KEYS.get(filename, "")


def latest_output_links(job: Job | None) -> dict[str, str]:
  if not job:
    return {}
  with job.lock:
    exports = dict(job.exports)
  return {
      filename: exports[key]
      for filename, key in OUTPUT_FILE_KEYS.items()
      if exports.get(key)
  }


def require_job(broker: Broker, job_id: str) -> Job:
  job = broker.get_job(job_id)
  if not job:
    raise ValueError("job not found")
  return job


def schema_from_record(raw: dict[str, Any]) -> ExtractorSchema:
  return validate_schema_request({
      "id": raw.get("id", ""),
      "appId": raw.get("appId", raw.get("app_id", "admin")),
      "name": raw.get("name", ""),
      "description": raw.get("description", ""),
      "sourceUrl": raw.get("sourceUrl", raw.get("source_url", "")),
      "host": raw.get("host", ""),
      "itemRoot": raw.get("itemRoot", raw.get("item_root", "")),
      "includeHidden": raw.get("includeHidden", raw.get("include_hidden", False)),
      "fields": raw.get("fields", []),
      "createdAtEpoch": raw.get("createdAtEpoch"),
      "updatedAtEpoch": raw.get("updatedAtEpoch"),
  })


def validate_schema_request(request: dict[str, Any]) -> ExtractorSchema:
  name = limit_text(request.get("name", ""), 120)
  item_root = str(request.get("itemRoot", request.get("item_root", ""))).strip()
  if not item_root:
    raise ValueError("schema itemRoot is required")
  if len(item_root) > 2000:
    raise ValueError("schema itemRoot is too long")
  fields_request = request.get("fields", [])
  if not isinstance(fields_request, list) or not fields_request:
    raise ValueError("schema fields must be a non-empty list")
  fields: list[dict[str, Any]] = []
  used_names: set[str] = set()
  for raw_field in fields_request[:64]:
    if not isinstance(raw_field, dict):
      continue
    raw_name = str(raw_field.get("name") or "").strip()
    field_name = sanitize_path_part(raw_name).lower()
    if not field_name:
      continue
    while field_name in used_names:
      field_name = f"{field_name}_{len(used_names)}"
    used_names.add(field_name)
    selector = str(raw_field.get("selector") or "").strip()
    if len(selector) > 2000:
      raise ValueError(f"field {field_name} selector is too long")
    mode = str(raw_field.get("mode") or "text").strip().lower()
    if mode.startswith("attr:"):
      attr = sanitize_attribute_name(mode[5:])
      if not attr:
        raise ValueError(f"field {field_name} has invalid attr mode")
      mode = f"attr:{attr}"
    elif mode not in SCHEMA_FIELD_MODES:
      raise ValueError(f"field {field_name} has unsupported mode: {mode}")
    fields.append({
        "name": field_name,
        "selector": selector,
        "mode": mode,
        "multiple": bool(raw_field.get("multiple", False)),
        "required": bool(raw_field.get("required", False)),
    })
  if not fields:
    raise ValueError("schema must contain at least one valid field")

  requested_id = str(request.get("id") or request.get("schema_id") or request.get("schemaId") or name).strip()
  schema_id = sanitize_path_part(requested_id or name or item_root).lower()
  source_url = clean_url(request.get("sourceUrl", request.get("source_url", "")))
  host = limit_text(request.get("host", ""), 200)
  if source_url and not host:
    host = urlparse(source_url).hostname or ""
  schema = ExtractorSchema(
      id=schema_id,
      name=name or schema_id,
      app_id=sanitize_path_part(
          str(request.get("appId") or request.get("app_id") or "admin")),
      description=limit_text(request.get("description", ""), 500),
      source_url=source_url,
      host=host,
      item_root=item_root,
      include_hidden=bool(request.get("includeHidden", request.get("include_hidden", False))),
      fields=fields,
      created_at=float(request.get("createdAtEpoch") or time.time()),
      updated_at=float(request.get("updatedAtEpoch") or time.time()),
  )
  return schema


def sanitize_attribute_name(value: str) -> str:
  return "".join(
      char for char in str(value).strip().lower()
      if char.isalnum() or char in ":-_")[:120]


def clean_item_fields(value: Any, limit: int) -> dict[str, Any]:
  if not isinstance(value, dict):
    return {}
  output: dict[str, Any] = {}
  for key, raw in value.items():
    name = sanitize_path_part(str(key)).lower()
    if not name:
      continue
    if isinstance(raw, list):
      output[name] = [limit_text(entry, limit) for entry in raw[:100]]
    else:
      output[name] = limit_text(raw, limit)
  return output


def job_record(job: Job) -> dict[str, Any]:
  with job.lock:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": job.id,
        "appId": job.app_id,
        "url": job.url,
        "outputDir": str(job.output_dir),
        "config": dict(job.config),
        "status": job.status,
        "reason": job.reason,
        "error": job.error,
        "createdAtEpoch": job.created_at,
        "startedAtEpoch": job.started_at,
        "updatedAtEpoch": job.updated_at,
        "finishedAtEpoch": job.finished_at,
        "targetId": job.target_id,
        "webSocketDebuggerUrl": job.websocket_url,
        "currentUrl": job.current_url,
        "title": job.title,
        "exports": dict(job.exports),
        "rawHtmlTruncated": job.raw_html_truncated,
        "rawHtmlLength": job.raw_html_length,
        "itemCount": len(job.items),
        "eventSequence": job.event_sequence,
    }


def load_items_file(path: Path) -> list[dict[str, Any]]:
  if not path.exists() or not path.is_file():
    return []
  items: list[dict[str, Any]] = []
  try:
    with path.open("r", encoding="utf-8") as file:
      for line in file:
        line = line.strip()
        if not line:
          continue
        value = json.loads(line)
        if isinstance(value, dict):
          items.append(value)
  except Exception as error:  # pylint: disable=broad-except
    print(f"Warning: failed to load item log {path}: {error}", file=sys.stderr)
  return items


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
  atomic_write_text(
      path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def atomic_write_text(path: Path, text: str) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
  temporary.write_text(text, encoding="utf-8")
  temporary.replace(path)


def default_output_root() -> Path:
  return Path.home() / "Downloads" / "Hardened Scrape Broker"


def make_job_id() -> str:
  random = secrets.token_hex(3)
  return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{random}"


def sanitize_path_part(value: str) -> str:
  cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
  cleaned = cleaned.strip("_")[:80]
  return cleaned or "value"


def clamp_int(value: Any, minimum: int, maximum: int) -> int:
  try:
    number = int(value)
  except (TypeError, ValueError):
    number = minimum
  return max(minimum, min(maximum, number))


def parse_int(value: str, default: int) -> int:
  try:
    return max(0, int(value))
  except (TypeError, ValueError):
    return default


def bool_value(value: Any, default: bool = False) -> bool:
  if isinstance(value, bool):
    return value
  if value is None:
    return default
  text = str(value).strip().lower()
  if text in ("1", "true", "yes", "on", "enabled"):
    return True
  if text in ("0", "false", "no", "off", "disabled"):
    return False
  return default


def clamp_float(value: Any, minimum: float, maximum: float,
                default: float) -> float:
  try:
    number = float(value)
  except (TypeError, ValueError):
    number = default
  return max(minimum, min(maximum, number))


def limit_text(value: Any, limit: int) -> str:
  text = str(value or "").strip()
  return text if len(text) <= limit else text[:limit] + "..."


def clean_url(value: Any) -> str:
  try:
    parsed = urlparse(str(value or ""))
    if parsed.scheme in ("http", "https"):
      return parsed.geturl()
  except Exception:
    pass
  return ""


def normalize_site_origin(value: Any) -> str:
  text = str(value or "").strip()
  if text and "://" not in text:
    text = "https://" + text
  try:
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
      return ""
    origin = f"{parsed.scheme}://{parsed.hostname.lower()}"
    if parsed.port is not None:
      origin += f":{parsed.port}"
    return origin
  except (TypeError, ValueError):
    return ""


def privacy_source_value(value: Any, default: str) -> str:
  source = str(value or default).strip().lower()
  if source not in PRIVACY_SOURCES:
    raise ValueError("privacy sources must be real or fake")
  return source


def fake_camera_backend_value(value: Any, default: str) -> str:
  backend = str(value or default).strip().lower()
  if backend not in FAKE_CAMERA_BACKENDS:
    raise ValueError(
        "fake camera backend must be loop, synthetic, or obs")
  return backend


def clean_url_list(values: Any, limit: int) -> list[str]:
  if not isinstance(values, list):
    return []
  output = []
  seen = set()
  for value in values:
    url = clean_url(value)
    if url and url not in seen:
      seen.add(url)
      output.append(url)
    if len(output) >= limit:
      break
  return output


def normalize_media_settings(
    settings: dict[str, Any],
    settings_file: Path,
) -> dict[str, Any]:
  media_mode = str(settings.get("mediaMode") or settings.get("media_mode") or "loop")
  media_mode = media_mode.strip().lower()
  if media_mode not in MEDIA_MODES:
    media_mode = "loop"
  camera_source = str(
      settings.get("cameraSource") or settings.get("camera_source") or
      ("real" if media_mode in ("system", "none") else "fake"))
  camera_source = camera_source.strip().lower()
  if camera_source not in PRIVACY_SOURCES:
    camera_source = "fake"
  fake_camera_backend = str(
      settings.get("fakeCameraBackend") or
      settings.get("fake_camera_backend") or
      (media_mode if media_mode in FAKE_CAMERA_BACKENDS else "loop"))
  fake_camera_backend = fake_camera_backend.strip().lower()
  if fake_camera_backend not in FAKE_CAMERA_BACKENDS:
    fake_camera_backend = "loop"
  media_mode = "system" if camera_source == "real" else fake_camera_backend
  audio_capture_mode = str(
      settings.get("audioCaptureMode") or settings.get("audio_capture_mode") or
      "fake").strip().lower()
  if audio_capture_mode not in AUDIO_CAPTURE_MODES:
    audio_capture_mode = "fake"
  microphone_source = str(
      settings.get("microphoneSource") or
      settings.get("microphone_source") or
      ("real" if audio_capture_mode == "system" else "fake"))
  microphone_source = microphone_source.strip().lower()
  if microphone_source not in PRIVACY_SOURCES:
    microphone_source = "fake"
  audio_capture_mode = (
      "system" if microphone_source == "real" else "fake")
  loop_video_file = str(
      settings.get("loopVideoFile") or settings.get("loop_video_file") or
      default_loop_video_file()).strip()
  loop_video_exists = bool(loop_video_file and Path(loop_video_file).exists())
  loop_video_bytes = int(settings.get("loopVideoBytes") or 0)
  loop_video_name = str(settings.get("loopVideoName") or "")
  if loop_video_exists:
    loop_video_path = Path(loop_video_file)
    loop_video_name = loop_video_name or loop_video_path.name
    with contextlib.suppress(OSError):
      loop_video_bytes = loop_video_path.stat().st_size
  return {
      "schemaVersion": PRIVACY_SETTINGS_SCHEMA_VERSION,
      "cameraSource": camera_source,
      "fakeCameraBackend": fake_camera_backend,
      "microphoneSource": microphone_source,
      "mediaMode": media_mode,
      "audioCaptureMode": audio_capture_mode,
      "loopVideoFile": loop_video_file,
      "loopVideoName": loop_video_name,
      "loopVideoBytes": loop_video_bytes,
      "loopVideoExists": loop_video_exists,
      "settingsFile": str(settings_file),
      "updatedAt": str(settings.get("updatedAt") or ""),
      "updatedAtEpoch": float(settings.get("updatedAtEpoch") or 0),
      "restartRequired": True,
  }


def normalize_location_settings(
    settings: dict[str, Any],
    settings_file: Path,
) -> dict[str, Any]:
  source = str(
      settings.get("source") or settings.get("locationSource") or
      ("fake" if bool_value(
          settings.get("enabled"), DEFAULT_FAKE_LOCATION["enabled"])
       else "real")).strip().lower()
  if source not in PRIVACY_SOURCES:
    source = "fake"
  return {
      "schemaVersion": PRIVACY_SETTINGS_SCHEMA_VERSION,
      "source": source,
      "enabled": source == "fake",
      "latitude": clamp_float(
          settings.get("latitude"), -90.0, 90.0,
          DEFAULT_FAKE_LOCATION["latitude"]),
      "longitude": clamp_float(
          settings.get("longitude"), -180.0, 180.0,
          DEFAULT_FAKE_LOCATION["longitude"]),
      "accuracy": clamp_float(
          settings.get("accuracy"), 1.0, 100000.0,
          DEFAULT_FAKE_LOCATION["accuracy"]),
      "settingsFile": str(settings_file),
      "updatedAt": str(settings.get("updatedAt") or ""),
      "updatedAtEpoch": float(settings.get("updatedAtEpoch") or 0),
      "restartRequired": True,
  }


def validate_y4m_file(path: Path) -> None:
  if not path.exists() or not path.is_file():
    raise ValueError(f"loop video file not found: {path}")
  try:
    with path.open("rb") as file:
      header = file.read(len(Y4M_MAGIC))
  except OSError as error:
    raise ValueError(f"could not read loop video file: {error}") from error
  if header != Y4M_MAGIC:
    raise ValueError(
        "Chromium fake camera loop files must be Y4M. Convert the source "
        "video to .y4m first.")


def escape_attr(value: Any) -> str:
  return html.escape(str(value), quote=True).replace("`", "&#96;")


def iso_time(timestamp: float | None) -> str | None:
  if timestamp is None:
    return None
  return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def redact_token(value: str) -> str:
  redacted = value
  for name in ("token", "ticket"):
    marker = f"{name}="
    if marker not in redacted:
      continue
    prefix, suffix = redacted.split(marker, 1)
    for delimiter in ("&", " ", "\t"):
      if delimiter in suffix:
        _, rest = suffix.split(delimiter, 1)
        redacted = f"{prefix}{marker}REDACTED{delimiter}{rest}"
        break
    else:
      redacted = f"{prefix}{marker}REDACTED"
  return redacted


def is_truthy_env(name: str) -> bool:
  return os.environ.get(name, "").strip().lower() in (
      "1", "true", "yes", "on")


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(
      description="Share one visible Hardened Chromium profile across local scraping apps.")
  parser.add_argument(
      "--cdp", required=True,
      help="Private visible-Chromium CDP endpoint supplied by the supervisor.")
  parser.add_argument("--root", type=Path, default=default_output_root(),
                      help="Output root for broker job artifacts.")
  parser.add_argument("--host", default="127.0.0.1")
  parser.add_argument("--port", type=int, default=DEFAULT_PORT)
  parser.add_argument("--token", default="",
                      help="Bearer token. If omitted, an ephemeral random token is generated.")
  parser.add_argument("--no-auth", action="store_true",
                      default=is_truthy_env("HARDENED_BROKER_NO_AUTH"),
                      help="Disable broker auth for loopback-only local use.")
  parser.add_argument("--allow-non-loopback", action="store_true",
                      help="Allow binding the broker to a non-loopback address.")
  parser.add_argument(
      "--max-active-jobs", type=int,
      default=int(os.environ.get("HARDENED_BROKER_MAX_ACTIVE_JOBS", "8")),
      help="Maximum number of simultaneously active browser jobs (default: 8).")
  parser.add_argument(
      "--max-active-jobs-per-app", type=int,
      default=int(os.environ.get(
          "HARDENED_BROKER_MAX_ACTIVE_JOBS_PER_APP", "2")),
      help="Per-app simultaneous job limit (default: 2).")
  return parser.parse_args()


def main() -> int:
  if len(sys.argv) == 3 and sys.argv[1] == "--export-snapshot":
    return export_snapshot(Path(sys.argv[2]))
  args = parse_args()
  if args.no_auth and args.host not in LOOPBACK_HOSTS:
    print(
        "--no-auth is only allowed on loopback hosts.",
        file=sys.stderr,
    )
    return 2
  if args.host not in LOOPBACK_HOSTS and not args.allow_non_loopback:
    print(
        f"Refusing to bind to non-loopback host {args.host!r}. "
        "Use --allow-non-loopback only on a trusted network.",
        file=sys.stderr,
    )
    return 2
  if args.max_active_jobs < 1 or args.max_active_jobs_per_app < 1:
    print("Job concurrency limits must be positive.", file=sys.stderr)
    return 2
  if args.max_active_jobs_per_app > args.max_active_jobs:
    print("Per-app job limit cannot exceed the global limit.", file=sys.stderr)
    return 2

  token = "" if args.no_auth else args.token or secrets.token_urlsafe(24)
  config = BrokerConfig(
      cdp_endpoint=args.cdp.rstrip("/"),
      output_root=args.root.expanduser().resolve(),
      token=token,
      no_auth=args.no_auth,
      max_active_jobs=args.max_active_jobs,
      max_active_jobs_per_app=args.max_active_jobs_per_app,
  )
  broker = Broker(config)
  server = ThreadingHTTPServer((args.host, args.port), BrokerRequestHandler)
  server.broker = broker  # type: ignore[attr-defined]

  print(f"Hardened Scrape Broker listening on http://{args.host}:{args.port}")
  print("Browser backend: private shared Hardened Chromium instance")
  print(f"Output root: {config.output_root}")
  print(f"State dir: {broker.state_dir}")
  print(f"Web UI: http://{args.host}:{args.port}/ui")
  if args.no_auth:
    print("Authorization: disabled for loopback no-auth mode")
  else:
    print(f"Authorization: Bearer {token}")
  print("Example:")
  if args.no_auth:
    print(
        "  curl -H 'Content-Type: application/json' "
        "-d '{\"url\":\"https://example.com\",\"app_id\":\"demo\"}' "
        f"http://{args.host}:{args.port}/jobs")
  else:
    print(
        f"  curl -H 'Authorization: Bearer {token}' "
        f"-H 'Content-Type: application/json' "
        f"-d '{{\"url\":\"https://example.com\",\"app_id\":\"demo\"}}' "
        f"http://{args.host}:{args.port}/jobs")
    print("Register app:")
    print(
        f"  curl -H 'Authorization: Bearer {token}' "
        f"-H 'Content-Type: application/json' "
        f"-d '{{\"name\":\"demo\",\"app_id\":\"demo\"}}' "
        f"http://{args.host}:{args.port}/apps")

  try:
    server.serve_forever()
  except KeyboardInterrupt:
    print("\nStopping Hardened Scrape Broker")
  finally:
    server.server_close()
    broker.close()
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
