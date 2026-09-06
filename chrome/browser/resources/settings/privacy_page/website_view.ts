// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

/**
 * Profile-wide, exact-origin Website View editor. The raw document is exposed
 * deliberately: users can set every supported expert value without a preset
 * silently modifying it. Settings are applied to future navigations; the
 * browser keeps a page's identity stable until it reloads.
 */
import 'chrome://resources/cr_elements/cr_button/cr_button.js';
import 'chrome://resources/cr_elements/cr_input/cr_input.js';
import '../settings_shared.css.js';

import {sendWithPromise} from 'chrome://resources/js/cr.js';
import {PolymerElement} from 'chrome://resources/polymer/v3_0/polymer/polymer_bundled.min.js';

import {getTemplate} from './website_view.html.js';

export class SettingsWebsiteViewElement extends PolymerElement {
  static get is() {
    return 'settings-website-view';
  }

  static get template() {
    return getTemplate();
  }

  static get properties() {
    return {
      documentText_: {type: String, value: ''},
      status_: {type: String, value: ''},
    };
  }

  declare private documentText_: string;
  declare private status_: string;

  override connectedCallback() {
    super.connectedCallback();
    void this.reload_();
  }

  private async reload_() {
    const document = await sendWithPromise<Record<string, unknown>>(
        'getHardenedWebsiteView');
    this.documentText_ = JSON.stringify(document, null, 2);
    this.status_ = 'Changes apply after a website reload.';
  }

  private async save_() {
    let document: Record<string, unknown>;
    try {
      document = JSON.parse(this.documentText_) as Record<string, unknown>;
    } catch {
      this.status_ = 'The Website View document is not valid JSON.';
      return;
    }
    try {
      const saved = await sendWithPromise<Record<string, unknown>>(
          'setHardenedWebsiteView', document);
      this.documentText_ = JSON.stringify(saved, null, 2);
      this.status_ = 'Saved. Reload affected websites; automation changes need a browser restart.';
    } catch {
      this.status_ = 'The browser rejected the Website View document.';
    }
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'settings-website-view': SettingsWebsiteViewElement;
  }
}

customElements.define(SettingsWebsiteViewElement.is, SettingsWebsiteViewElement);
