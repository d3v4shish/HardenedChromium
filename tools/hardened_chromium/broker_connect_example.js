// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.
//
// Usage:
//   HARDENED_APP_ID=... HARDENED_APP_SECRET=... \
//     node broker_connect_example.js https://example.com

const {spawnSync} = require('node:child_process');
const path = require('node:path');

const serviceScript = process.env.HARDENED_SCRAPE_SERVICE ||
    path.join(__dirname, 'hardened_scrape_service.py');
const ensured = spawnSync(
    process.env.HARDENED_SCRAPE_PYTHON || 'python3',
    [serviceScript, '--json', 'ensure'],
    {encoding: 'utf8', timeout: 100000});
if (ensured.error || ensured.status !== 0) {
  throw new Error(
      ensured.error?.message || ensured.stderr || 'failed to ensure broker');
}
const service = JSON.parse(ensured.stdout);
const broker = process.env.HARDENED_BROKER_URL ||
    service.broker?.url || 'http://127.0.0.1:8877';
const appId = process.env.HARDENED_APP_ID || '';
const appSecret = process.env.HARDENED_APP_SECRET || '';
const token = process.env.HARDENED_BROKER_TOKEN ||
    (appSecret ? '' : service.token || '');
const targetUrl = process.argv[2] || 'https://example.com';

const headers = {'Content-Type': 'application/json'};
if (token) headers.Authorization = `Bearer ${token}`;
if (appId) headers['X-Hardened-App-Id'] = appId;
if (appSecret) headers['X-Hardened-App-Secret'] = appSecret;

(async () => {
  const response = await fetch(`${broker}/jobs`, {
    method: 'POST',
    headers,
    body: JSON.stringify({url: targetUrl}),
  });
  const result = await response.json();
  if (!response.ok || result.ok === false) {
    throw new Error(result.error || `broker returned HTTP ${response.status}`);
  }
  console.log(JSON.stringify(result, null, 2));

  // Authenticate the HTTP request, then upgrade with a short-lived ticket so
  // no durable app secret or administrator token appears in the WebSocket URL.
  const ticketResponse = await fetch(`${broker}/stream-tickets`, {
    method: 'POST',
    headers,
    body: '{}',
  });
  const ticket = await ticketResponse.json();
  if (!ticketResponse.ok || ticket.ok === false) {
    throw new Error(ticket.error ||
        `ticket request returned HTTP ${ticketResponse.status}`);
  }
  if (typeof WebSocket === 'undefined') {
    throw new Error('this Node version does not provide the global WebSocket API');
  }
  const stream = new WebSocket(ticket.webSocketUrl);
  stream.addEventListener('open', () => {
    stream.send(JSON.stringify({
      type: 'subscribe',
      subscriptions: [{jobId: result.job.id, afterSequence: 0}],
    }));
  });
  stream.addEventListener('message', event => {
    const message = JSON.parse(String(event.data));
    console.log(JSON.stringify({stream: message}, null, 2));
    const status = message.type === 'status' ? message.data?.status : '';
    if (['completed', 'failed', 'stopped', 'interrupted'].includes(status)) {
      stream.close();
    }
  });
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
