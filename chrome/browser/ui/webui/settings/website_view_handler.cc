// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "chrome/browser/ui/webui/settings/website_view_handler.h"

#include <set>
#include <string>
#include <string_view>
#include <utility>

#include "base/files/file_util.h"
#include "base/files/important_file_writer.h"
#include "base/functional/bind.h"
#include "base/json/json_reader.h"
#include "base/json/json_writer.h"
#include "base/values.h"
#include "chrome/browser/profiles/profile.h"
#include "content/public/browser/web_ui.h"
#include "url/gurl.h"
#include "url/origin.h"

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

bool NormalizeSource(base::DictValue* policy,
                     std::string_view key,
                     std::string_view fallback) {
  const base::Value* raw_value = policy->Find(key);
  if (raw_value &&
      (!raw_value->is_string() || (raw_value->GetString() != "fake" &&
                                   raw_value->GetString() != "real"))) {
    return false;
  }
  if (!raw_value) {
    policy->Set(key, fallback);
  }
  return true;
}

bool NormalizeDefaultAutomation(base::DictValue* policy) {
  base::Value* raw_exposures = policy->Find("exposures");
  if (!raw_exposures) {
    policy->Set("exposures", base::DictValue());
    raw_exposures = policy->Find("exposures");
  }
  if (!raw_exposures->is_dict()) {
    return false;
  }
  base::DictValue& exposures = raw_exposures->GetDict();
  const base::Value* automation = exposures.Find("automation");
  if (automation &&
      (!automation->is_string() || (automation->GetString() != "hide" &&
                                    automation->GetString() != "report"))) {
    return false;
  }
  if (!automation) {
    exposures.Set("automation", "hide");
  }
  return true;
}

bool CanonicalizeOrigin(std::string_view input, std::string* output) {
  GURL url(input);
  if (!url.is_valid() || !url.SchemeIsHTTPOrHTTPS() || !url.has_host() ||
      url.has_username() || url.has_password()) {
    return false;
  }
  const url::Origin origin = url::Origin::Create(url);
  if (origin.opaque()) {
    return false;
  }
  *output = origin.Serialize();
  return true;
}

bool NormalizeWebsiteView(base::DictValue* document,
                          bool reject_duplicate_origins) {
  base::DictValue* defaults = document->FindDict("default");
  base::ListValue* rules = document->FindList("rules");
  if (!defaults || !rules) {
    return false;
  }

  for (std::string_view key :
       {"cameraSource", "microphoneSource", "locationSource"}) {
    if (!NormalizeSource(defaults, key, "fake")) {
      return false;
    }
  }
  if (!NormalizeDefaultAutomation(defaults)) {
    return false;
  }

  const std::string* default_camera = defaults->FindString("cameraSource");
  const std::string* default_microphone =
      defaults->FindString("microphoneSource");
  const std::string* default_location = defaults->FindString("locationSource");
  if (!default_camera || !default_microphone || !default_location) {
    return false;
  }

  base::ListValue normalized_rules;
  std::set<std::string> seen_origins;
  for (const base::Value& rule_value : *rules) {
    if (!rule_value.is_dict()) {
      return false;
    }
    base::DictValue rule = rule_value.GetDict().Clone();
    const std::string* rule_origin = rule.FindString("origin");
    std::string origin;
    if (!rule_origin || !CanonicalizeOrigin(*rule_origin, &origin)) {
      return false;
    }
    if (!seen_origins.insert(origin).second) {
      if (reject_duplicate_origins) {
        return false;
      }
      continue;
    }
    if (!NormalizeSource(&rule, "cameraSource", *default_camera) ||
        !NormalizeSource(&rule, "microphoneSource", *default_microphone) ||
        !NormalizeSource(&rule, "locationSource", *default_location)) {
      return false;
    }
    rule.Set("origin", origin);
    if (!rule.FindString("id")) {
      rule.Set("id", origin);
    }
    normalized_rules.Append(std::move(rule));
  }
  document->Set("rules", std::move(normalized_rules));
  document->Set("schemaVersion", kWebsiteViewSchemaVersion);
  return true;
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
  if (!NormalizeWebsiteView(&result, false)) {
    return CreateDefaultWebsiteView();
  }
  result.Set("schemaVersion", kWebsiteViewSchemaVersion);
  return result;
}

bool WriteWebsiteView(const base::FilePath& path, base::DictValue document) {
  if (!NormalizeWebsiteView(&document, true)) {
    return false;
  }
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
      args[0],
      ReadWebsiteView(profile_->GetPath().AppendASCII(kWebsiteViewFileName)));
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
      args[0],
      ReadWebsiteView(profile_->GetPath().AppendASCII(kWebsiteViewFileName)));
}

}  // namespace settings
