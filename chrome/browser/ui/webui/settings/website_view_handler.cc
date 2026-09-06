// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "chrome/browser/ui/webui/settings/website_view_handler.h"

#include <string>
#include <utility>

#include "base/files/file_util.h"
#include "base/files/important_file_writer.h"
#include "base/functional/bind.h"
#include "base/json/json_reader.h"
#include "base/json/json_writer.h"
#include "base/values.h"
#include "chrome/browser/profiles/profile.h"
#include "content/public/browser/web_ui.h"

namespace settings {

namespace {

constexpr int kWebsiteViewSchemaVersion = 3;
constexpr char kWebsiteViewFileName[] = "HardenedWebsiteView.json";

base::DictValue CreateDefaultWebsiteView() {
  base::DictValue persona;
  persona.Set("userAgent", "");
  persona.Set("platform", "");
  persona.Set("languages", base::ListValue());
  persona.Set("timezone", "");
  persona.Set("hardwareConcurrency", base::Value());
  persona.Set("deviceMemory", base::Value());
  persona.Set("maxTouchPoints", base::Value());
  persona.Set("screen", base::DictValue());
  persona.Set("custom", base::DictValue());

  base::DictValue exposures;
  exposures.Set("automation", "hide");
  exposures.Set("canvas", "normalize");
  exposures.Set("webgl", "normalize");
  exposures.Set("audio", "normalize");
  exposures.Set("webRtc", "block");
  exposures.Set("localFonts", "block");
  exposures.Set("highEntropyApis", "block");
  exposures.Set("deviceEnumeration", "normalize");
  exposures.Set("behavior", "hardened");

  base::DictValue defaults;
  defaults.Set("cameraSource", "fake");
  defaults.Set("microphoneSource", "fake");
  defaults.Set("locationSource", "fake");
  defaults.Set("persona", std::move(persona));
  defaults.Set("exposures", std::move(exposures));

  base::DictValue document;
  document.Set("schemaVersion", kWebsiteViewSchemaVersion);
  document.Set("default", std::move(defaults));
  document.Set("rules", base::ListValue());
  return document;
}

base::DictValue ReadWebsiteView(const base::FilePath& path) {
  std::string json;
  if (!base::ReadFileToString(path, &json)) {
    return CreateDefaultWebsiteView();
  }
  std::optional<base::Value> parsed =
      base::JSONReader::Read(json, base::JSON_PARSE_RFC);
  if (!parsed || !parsed->is_dict()) {
    return CreateDefaultWebsiteView();
  }
  base::DictValue result = std::move(*parsed).TakeDict();
  if (!result.FindDict("default") || !result.FindList("rules")) {
    return CreateDefaultWebsiteView();
  }
  result.Set("schemaVersion", kWebsiteViewSchemaVersion);
  return result;
}

bool WriteWebsiteView(const base::FilePath& path, base::DictValue document) {
  if (!document.FindDict("default") || !document.FindList("rules")) {
    return false;
  }
  document.Set("schemaVersion", kWebsiteViewSchemaVersion);
  std::string json;
  if (!base::JSONWriter::Write(document, &json)) {
    return false;
  }
  if (!base::CreateDirectory(path.DirName())) {
    return false;
  }
  return base::ImportantFileWriter::WriteFileAtomically(path, json);
}

}  // namespace

WebsiteViewHandler::WebsiteViewHandler(Profile* profile) : profile_(profile) {}
WebsiteViewHandler::~WebsiteViewHandler() = default;

void WebsiteViewHandler::RegisterMessages() {
  web_ui()->RegisterMessageCallback(
      "getHardenedWebsiteView",
      base::BindRepeating(&WebsiteViewHandler::HandleGetWebsiteView,
                          base::Unretained(this)));
  web_ui()->RegisterMessageCallback(
      "setHardenedWebsiteView",
      base::BindRepeating(&WebsiteViewHandler::HandleSetWebsiteView,
                          base::Unretained(this)));
}

void WebsiteViewHandler::OnJavascriptAllowed() {}
void WebsiteViewHandler::OnJavascriptDisallowed() {}

void WebsiteViewHandler::HandleGetWebsiteView(const base::ListValue& args) {
  CHECK_EQ(1U, args.size());
  AllowJavascript();
  ResolveJavascriptCallback(
      args[0], ReadWebsiteView(profile_->GetPath().AppendASCII(
                   kWebsiteViewFileName)));
}

void WebsiteViewHandler::HandleSetWebsiteView(const base::ListValue& args) {
  CHECK_EQ(2U, args.size());
  AllowJavascript();
  if (!args[1].is_dict() ||
      !WriteWebsiteView(profile_->GetPath().AppendASCII(kWebsiteViewFileName),
                        args[1].GetDict().Clone())) {
    RejectJavascriptCallback(args[0], base::Value());
    return;
  }
  ResolveJavascriptCallback(
      args[0], ReadWebsiteView(profile_->GetPath().AppendASCII(
                   kWebsiteViewFileName)));
}

}  // namespace settings
