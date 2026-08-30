// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "content/public/browser/hardened_privacy_source.h"

#include <optional>
#include <string_view>
#include <unordered_map>

#include "base/command_line.h"
#include "base/files/file_util.h"
#include "base/functional/bind.h"
#include "base/json/json_reader.h"
#include "base/no_destructor.h"
#include "base/synchronization/lock.h"
#include "base/task/task_traits.h"
#include "base/task/thread_pool.h"
#include "base/time/time.h"
#include "content/public/browser/navigation_handle.h"
#include "content/public/browser/web_contents.h"
#include "content/public/browser/web_contents_observer.h"
#include "content/public/browser/web_contents_user_data.h"
#include "content/public/common/content_switches.h"
#include "url/origin.h"

namespace content {

namespace {

HardenedLocationSource DefaultLocationSource() {
  return base::CommandLine::ForCurrentProcess()->GetSwitchValueASCII(
             switches::kHardenedDefaultLocationSource) == "fake"
             ? HardenedLocationSource::kFake
             : HardenedLocationSource::kReal;
}

class HardenedPrivacySourceState
    : public WebContentsObserver,
      public WebContentsUserData<HardenedPrivacySourceState> {
 public:
  ~HardenedPrivacySourceState() override = default;

  void SetLocationSource(HardenedLocationSource source) {
    location_source_ = source;
  }
  void SetCameraSource(HardenedMediaSource source) { camera_source_ = source; }
  void SetMicrophoneSource(HardenedMediaSource source) {
    microphone_source_ = source;
  }

  HardenedLocationSource location_source() const {
    if (location_source_) {
      return *location_source_;
    }
    return DefaultLocationSource();
  }

  std::optional<HardenedLocationSource> explicit_location_source() const {
    return location_source_;
  }
  std::optional<HardenedMediaSource> camera_source() const {
    return camera_source_;
  }
  std::optional<HardenedMediaSource> microphone_source() const {
    return microphone_source_;
  }

  void DidFinishNavigation(NavigationHandle* navigation_handle) override {
    if (navigation_handle->IsInPrimaryMainFrame() &&
        navigation_handle->HasCommitted() &&
        !navigation_handle->IsSameDocument()) {
      location_source_.reset();
      camera_source_.reset();
      microphone_source_.reset();
    }
  }

 private:
  explicit HardenedPrivacySourceState(WebContents* web_contents)
      : WebContentsObserver(web_contents),
        WebContentsUserData<HardenedPrivacySourceState>(*web_contents) {}

  friend class WebContentsUserData<HardenedPrivacySourceState>;
  WEB_CONTENTS_USER_DATA_KEY_DECL();

  std::optional<HardenedLocationSource> location_source_;
  std::optional<HardenedMediaSource> camera_source_;
  std::optional<HardenedMediaSource> microphone_source_;
};

WEB_CONTENTS_USER_DATA_KEY_IMPL(HardenedPrivacySourceState);

HardenedPrivacySourceState* GetOrCreateState(WebContents* web_contents) {
  if (!web_contents) {
    return nullptr;
  }
  HardenedPrivacySourceState::CreateForWebContents(web_contents);
  return HardenedPrivacySourceState::FromWebContents(web_contents);
}

class PrivacyRuleCache {
 public:
  struct LookupResult {
    std::optional<std::string> value;
    // False only while a configured rules file has not produced a snapshot.
    // Callers must use a privacy-preserving fallback in that small window.
    bool current_rules_loaded = true;
  };

  LookupResult Get(const url::Origin& origin, std::string_view key) {
    const base::FilePath rules_path =
        base::CommandLine::ForCurrentProcess()->GetSwitchValuePath(
            switches::kHardenedPrivacyRulesFile);
    if (rules_path.empty()) {
      return {};
    }
    base::AutoLock lock(lock_);
    const base::TimeTicks now = base::TimeTicks::Now();
    if (rules_path != requested_path_) {
      requested_path_ = rules_path;
      // Never apply a rule from the previous file to the new file. Until the
      // replacement is read, callers deliberately fall back to fake sources.
      loaded_path_.clear();
      values_.clear();
      refresh_after_ = base::TimeTicks();
      StartReloadLocked();
    } else if (now >= refresh_after_) {
      StartReloadLocked();
    }
    const auto found =
        values_.find(origin.Serialize() + "\n" + std::string(key));
    return {found == values_.end() ? std::nullopt
                                   : std::optional(found->second),
            rules_path == loaded_path_};
  }

 private:
  using PrivacyValues = std::unordered_map<std::string, std::string>;

  // This cache is queried by permission callbacks, where Chromium expressly
  // disallows blocking. Keep all file work off that path and only publish a
  // complete, parsed snapshot while holding the lock.
  void StartReloadLocked() EXCLUSIVE_LOCKS_REQUIRED(lock_) {
    if (reload_in_flight_) {
      return;
    }
    reload_in_flight_ = true;
    base::ThreadPool::PostTask(
        FROM_HERE,
        {base::MayBlock(), base::TaskPriority::USER_VISIBLE},
        base::BindOnce(&PrivacyRuleCache::ReloadOnWorker,
                       base::Unretained(this), requested_path_));
  }

  static PrivacyValues ReadRules(const base::FilePath& rules_path) {
    PrivacyValues values;
    std::string json;
    if (!base::ReadFileToString(rules_path, &json)) {
      return values;
    }
    const std::optional<base::Value> value =
        base::JSONReader::Read(json, base::JSON_PARSE_RFC);
    const base::ListValue* rules = value && value->is_dict()
                                       ? value->GetDict().FindList("rules")
                                       : nullptr;
    if (!rules) {
      return values;
    }
    for (const auto& rule_value : *rules) {
      if (!rule_value.is_dict()) {
        continue;
      }
      const auto& rule = rule_value.GetDict();
      const std::string* rule_origin = rule.FindString("origin");
      if (!rule_origin) {
        continue;
      }
      for (std::string_view source_key :
           {"locationSource", "cameraSource", "microphoneSource"}) {
        const std::string* source = rule.FindString(source_key);
        if (source && (*source == "real" || *source == "fake")) {
          values[*rule_origin + "\n" + std::string(source_key)] = *source;
        }
      }
    }
    return values;
  }

  void ReloadOnWorker(base::FilePath rules_path) {
    PrivacyValues values = ReadRules(rules_path);
    base::AutoLock lock(lock_);
    reload_in_flight_ = false;
    if (rules_path == requested_path_) {
      values_ = std::move(values);
      loaded_path_ = rules_path;
      refresh_after_ = base::TimeTicks::Now() + base::Seconds(1);
      return;
    }
    // A caller selected another file while this reload was running. Start a
    // fresh read immediately; never expose data from the old file as the new
    // file's rules.
    StartReloadLocked();
  }

  base::Lock lock_;
  base::FilePath requested_path_ GUARDED_BY(lock_);
  base::FilePath loaded_path_ GUARDED_BY(lock_);
  base::TimeTicks refresh_after_ GUARDED_BY(lock_);
  bool reload_in_flight_ GUARDED_BY(lock_) = false;
  PrivacyValues values_ GUARDED_BY(lock_);
};

std::optional<std::string> GetRememberedSource(const url::Origin& origin,
                                               std::string_view key,
                                               bool* current_rules_loaded) {
  static base::NoDestructor<PrivacyRuleCache> cache;
  PrivacyRuleCache::LookupResult result = cache->Get(origin, key);
  if (current_rules_loaded) {
    *current_rules_loaded = result.current_rules_loaded;
  }
  return result.value;
}

std::optional<std::string> GetRememberedSource(WebContents* web_contents,
                                               std::string_view key,
                                               bool* current_rules_loaded) {
  if (!web_contents) {
    return std::nullopt;
  }
  return GetRememberedSource(
      url::Origin::Create(web_contents->GetLastCommittedURL()), key,
      current_rules_loaded);
}

}  // namespace

void SetHardenedLocationSource(WebContents* web_contents,
                               HardenedLocationSource source) {
  if (auto* state = GetOrCreateState(web_contents)) {
    state->SetLocationSource(source);
  }
}

HardenedLocationSource GetHardenedLocationSource(WebContents* web_contents) {
  if (auto* state = GetOrCreateState(web_contents)) {
    if (state->explicit_location_source()) {
      return *state->explicit_location_source();
    }
    bool current_rules_loaded = true;
    if (auto remembered = GetRememberedSource(
            web_contents, "locationSource", &current_rules_loaded)) {
      return *remembered == "real" ? HardenedLocationSource::kReal
                                   : HardenedLocationSource::kFake;
    }
    if (!current_rules_loaded) {
      return HardenedLocationSource::kFake;
    }
    return state->location_source();
  }
  return DefaultLocationSource();
}

std::optional<HardenedMediaSource> GetRememberedHardenedCameraSource(
    WebContents* web_contents) {
  bool current_rules_loaded = true;
  if (auto source = GetRememberedSource(web_contents, "cameraSource",
                                        &current_rules_loaded)) {
    return *source == "real" ? HardenedMediaSource::kReal
                             : HardenedMediaSource::kFake;
  }
  if (!current_rules_loaded) {
    return HardenedMediaSource::kFake;
  }
  return std::nullopt;
}

std::optional<HardenedMediaSource> GetRememberedHardenedMicrophoneSource(
    WebContents* web_contents) {
  bool current_rules_loaded = true;
  if (auto source = GetRememberedSource(web_contents, "microphoneSource",
                                        &current_rules_loaded)) {
    return *source == "real" ? HardenedMediaSource::kReal
                             : HardenedMediaSource::kFake;
  }
  if (!current_rules_loaded) {
    return HardenedMediaSource::kFake;
  }
  return std::nullopt;
}

std::optional<HardenedMediaSource> GetRememberedHardenedCameraSourceForOrigin(
    const url::Origin& origin) {
  bool current_rules_loaded = true;
  if (auto source = GetRememberedSource(origin, "cameraSource",
                                        &current_rules_loaded)) {
    return *source == "real" ? HardenedMediaSource::kReal
                             : HardenedMediaSource::kFake;
  }
  if (!current_rules_loaded) {
    return HardenedMediaSource::kFake;
  }
  return std::nullopt;
}

std::optional<HardenedMediaSource>
GetRememberedHardenedMicrophoneSourceForOrigin(const url::Origin& origin) {
  bool current_rules_loaded = true;
  if (auto source = GetRememberedSource(origin, "microphoneSource",
                                        &current_rules_loaded)) {
    return *source == "real" ? HardenedMediaSource::kReal
                             : HardenedMediaSource::kFake;
  }
  if (!current_rules_loaded) {
    return HardenedMediaSource::kFake;
  }
  return std::nullopt;
}

void SetHardenedCameraSource(WebContents* web_contents,
                             HardenedMediaSource source) {
  if (auto* state = GetOrCreateState(web_contents)) {
    state->SetCameraSource(source);
  }
}

void SetHardenedMicrophoneSource(WebContents* web_contents,
                                 HardenedMediaSource source) {
  if (auto* state = GetOrCreateState(web_contents)) {
    state->SetMicrophoneSource(source);
  }
}

std::optional<HardenedMediaSource> GetHardenedCameraSource(
    WebContents* web_contents) {
  if (auto* state = GetOrCreateState(web_contents)) {
    if (state->camera_source()) {
      return state->camera_source();
    }
  }
  return GetRememberedHardenedCameraSource(web_contents);
}

std::optional<HardenedMediaSource> GetHardenedMicrophoneSource(
    WebContents* web_contents) {
  if (auto* state = GetOrCreateState(web_contents)) {
    if (state->microphone_source()) {
      return state->microphone_source();
    }
  }
  return GetRememberedHardenedMicrophoneSource(web_contents);
}

}  // namespace content
