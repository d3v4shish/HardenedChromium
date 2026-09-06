// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef CHROME_BROWSER_UI_WEBUI_SETTINGS_WEBSITE_VIEW_HANDLER_H_
#define CHROME_BROWSER_UI_WEBUI_SETTINGS_WEBSITE_VIEW_HANDLER_H_

#include "base/memory/raw_ptr.h"
#include "base/values.h"
#include "chrome/browser/ui/webui/settings/settings_page_ui_handler.h"

class Profile;

namespace settings {

// Reads and writes the profile-owned Website View document used by the
// hardened browser path. The document is deliberately not a WebUI preference:
// the content process hot-reloads the same file for permission-time decisions.
class WebsiteViewHandler : public SettingsPageUIHandler {
 public:
  explicit WebsiteViewHandler(Profile* profile);
  WebsiteViewHandler(const WebsiteViewHandler&) = delete;
  WebsiteViewHandler& operator=(const WebsiteViewHandler&) = delete;
  ~WebsiteViewHandler() override;

  void RegisterMessages() override;
  void OnJavascriptAllowed() override;
  void OnJavascriptDisallowed() override;

 private:
  void HandleGetWebsiteView(const base::ListValue& args);
  void HandleSetWebsiteView(const base::ListValue& args);

  raw_ptr<Profile> profile_;
};

}  // namespace settings

#endif  // CHROME_BROWSER_UI_WEBUI_SETTINGS_WEBSITE_VIEW_HANDLER_H_
