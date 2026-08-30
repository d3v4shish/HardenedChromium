// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef CONTENT_PUBLIC_BROWSER_HARDENED_PRIVACY_SOURCE_H_
#define CONTENT_PUBLIC_BROWSER_HARDENED_PRIVACY_SOURCE_H_

#include <optional>

#include "content/common/content_export.h"

namespace url {
class Origin;
}

namespace content {

class WebContents;

enum class HardenedLocationSource {
  kFake,
  kReal,
};

enum class HardenedMediaSource {
  kFake,
  kReal,
};

// The selection is scoped to the current primary-page navigation. A newly
// committed navigation starts from the privacy-preserving default again.
CONTENT_EXPORT void SetHardenedLocationSource(WebContents* web_contents,
                                              HardenedLocationSource source);
CONTENT_EXPORT HardenedLocationSource
GetHardenedLocationSource(WebContents* web_contents);
CONTENT_EXPORT std::optional<HardenedMediaSource>
GetRememberedHardenedCameraSource(WebContents* web_contents);
CONTENT_EXPORT std::optional<HardenedMediaSource>
GetRememberedHardenedMicrophoneSource(WebContents* web_contents);
CONTENT_EXPORT std::optional<HardenedMediaSource>
GetRememberedHardenedCameraSourceForOrigin(const url::Origin& origin);
CONTENT_EXPORT std::optional<HardenedMediaSource>
GetRememberedHardenedMicrophoneSourceForOrigin(const url::Origin& origin);
CONTENT_EXPORT void SetHardenedCameraSource(WebContents* web_contents,
                                            HardenedMediaSource source);
CONTENT_EXPORT void SetHardenedMicrophoneSource(WebContents* web_contents,
                                                HardenedMediaSource source);
CONTENT_EXPORT std::optional<HardenedMediaSource> GetHardenedCameraSource(
    WebContents* web_contents);
CONTENT_EXPORT std::optional<HardenedMediaSource> GetHardenedMicrophoneSource(
    WebContents* web_contents);

}  // namespace content

#endif  // CONTENT_PUBLIC_BROWSER_HARDENED_PRIVACY_SOURCE_H_
