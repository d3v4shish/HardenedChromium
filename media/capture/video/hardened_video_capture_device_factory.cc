// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "media/capture/video/hardened_video_capture_device_factory.h"

#include <algorithm>
#include <iterator>
#include <string_view>
#include <utility>

#include "base/command_line.h"
#include "base/functional/bind.h"
#include "media/base/media_switches.h"

namespace media {

namespace {

constexpr std::string_view kPrivatePlatformDeviceIdPrefix =
    "hardened-private-camera:platform:";

bool IsPrivateDeviceId(std::string_view device_id) {
  return device_id.starts_with(kHardenedPrivateVideoDeviceIdPrefix);
}

bool IsPrivatePlatformDeviceId(std::string_view device_id) {
  return device_id.starts_with(kPrivatePlatformDeviceIdPrefix);
}

VideoCaptureDeviceDescriptor UnwrapPrivateDescriptor(
    const VideoCaptureDeviceDescriptor& descriptor) {
  VideoCaptureDeviceDescriptor unwrapped(descriptor);
  unwrapped.device_id.erase(
      0, std::string_view(kHardenedPrivateVideoDeviceIdPrefix).size());
  return unwrapped;
}

}  // namespace

HardenedVideoCaptureDeviceFactory::HardenedVideoCaptureDeviceFactory(
    std::unique_ptr<VideoCaptureDeviceFactory> platform_factory,
    std::unique_ptr<VideoCaptureDeviceFactory> private_factory)
    : platform_factory_(std::move(platform_factory)),
      private_factory_(std::move(private_factory)) {}

HardenedVideoCaptureDeviceFactory::~HardenedVideoCaptureDeviceFactory() =
    default;

VideoCaptureErrorOrDevice HardenedVideoCaptureDeviceFactory::CreateDevice(
    const VideoCaptureDeviceDescriptor& device_descriptor) {
  DCHECK(thread_checker_.CalledOnValidThread());
  if (IsPrivatePlatformDeviceId(device_descriptor.device_id)) {
    VideoCaptureDeviceDescriptor unwrapped(device_descriptor);
    unwrapped.device_id.erase(0, kPrivatePlatformDeviceIdPrefix.size());
    return platform_factory_->CreateDevice(unwrapped);
  }
  if (IsPrivateDeviceId(device_descriptor.device_id)) {
    return private_factory_->CreateDevice(
        UnwrapPrivateDescriptor(device_descriptor));
  }
  return platform_factory_->CreateDevice(device_descriptor);
}

void HardenedVideoCaptureDeviceFactory::GetDevicesInfo(
    GetDevicesInfoCallback callback) {
  DCHECK(thread_checker_.CalledOnValidThread());
  platform_factory_->GetDevicesInfo(
      base::BindOnce(&HardenedVideoCaptureDeviceFactory::OnPlatformDevicesInfo,
                     weak_factory_.GetWeakPtr(), std::move(callback)));
}

void HardenedVideoCaptureDeviceFactory::OnPlatformDevicesInfo(
    GetDevicesInfoCallback callback,
    std::vector<VideoCaptureDeviceInfo> platform_devices) {
  private_factory_->GetDevicesInfo(
      base::BindOnce(&HardenedVideoCaptureDeviceFactory::OnPrivateDevicesInfo,
                     weak_factory_.GetWeakPtr(), std::move(callback),
                     std::move(platform_devices)));
}

void HardenedVideoCaptureDeviceFactory::OnPrivateDevicesInfo(
    GetDevicesInfoCallback callback,
    std::vector<VideoCaptureDeviceInfo> platform_devices,
    std::vector<VideoCaptureDeviceInfo> private_devices) {
  const base::CommandLine* command_line =
      base::CommandLine::ForCurrentProcess();
  if (command_line->GetSwitchValueASCII(
          switches::kHardenedPrivateCameraBackend) == "obs") {
    const std::string requested_name =
        command_line->GetSwitchValueASCII(switches::kHardenedPrivateCameraName);
    auto obs_device =
        std::ranges::find_if(platform_devices, [&](const auto& device) {
          return !requested_name.empty() &&
                 device.descriptor.display_name().find(requested_name) !=
                     std::string::npos;
        });
    if (obs_device != platform_devices.end()) {
      VideoCaptureDeviceInfo private_obs = std::move(*obs_device);
      platform_devices.erase(obs_device);
      private_obs.descriptor.device_id =
          std::string(kPrivatePlatformDeviceIdPrefix) +
          private_obs.descriptor.device_id;
      private_obs.descriptor.set_display_name("Private virtual camera");
      private_devices.clear();
      private_devices.push_back(std::move(private_obs));
    }
  }
  for (auto& device : private_devices) {
    if (!IsPrivatePlatformDeviceId(device.descriptor.device_id)) {
      device.descriptor.device_id =
          std::string(kHardenedPrivateVideoDeviceIdPrefix) +
          device.descriptor.device_id;
      device.descriptor.set_display_name("Private virtual camera");
    }
  }
  // Fresh profiles default to the configured source. Chromium's normal device
  // preference ranking can still move an explicitly selected device first.
  if (command_line->GetSwitchValueASCII(
          switches::kHardenedDefaultCameraSource) == "real") {
    platform_devices.insert(platform_devices.end(),
                            std::make_move_iterator(private_devices.begin()),
                            std::make_move_iterator(private_devices.end()));
    std::move(callback).Run(std::move(platform_devices));
  } else {
    private_devices.insert(private_devices.end(),
                           std::make_move_iterator(platform_devices.begin()),
                           std::make_move_iterator(platform_devices.end()));
    std::move(callback).Run(std::move(private_devices));
  }
}

}  // namespace media
