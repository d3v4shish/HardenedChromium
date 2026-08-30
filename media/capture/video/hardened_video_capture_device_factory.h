// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef MEDIA_CAPTURE_VIDEO_HARDENED_VIDEO_CAPTURE_DEVICE_FACTORY_H_
#define MEDIA_CAPTURE_VIDEO_HARDENED_VIDEO_CAPTURE_DEVICE_FACTORY_H_

#include <memory>

#include "base/memory/weak_ptr.h"
#include "media/capture/video/video_capture_device_factory.h"

namespace media {

// Exposes the platform camera factory and one private virtual camera through a
// single factory. The virtual device ID is namespaced so device creation can be
// routed without leaking the backing file path to callers.
class CAPTURE_EXPORT HardenedVideoCaptureDeviceFactory
    : public VideoCaptureDeviceFactory {
 public:
  HardenedVideoCaptureDeviceFactory(
      std::unique_ptr<VideoCaptureDeviceFactory> platform_factory,
      std::unique_ptr<VideoCaptureDeviceFactory> private_factory);
  ~HardenedVideoCaptureDeviceFactory() override;

  VideoCaptureErrorOrDevice CreateDevice(
      const VideoCaptureDeviceDescriptor& device_descriptor) override;
  void GetDevicesInfo(GetDevicesInfoCallback callback) override;

 private:
  void OnPlatformDevicesInfo(
      GetDevicesInfoCallback callback,
      std::vector<VideoCaptureDeviceInfo> platform_devices);
  void OnPrivateDevicesInfo(
      GetDevicesInfoCallback callback,
      std::vector<VideoCaptureDeviceInfo> platform_devices,
      std::vector<VideoCaptureDeviceInfo> private_devices);

  std::unique_ptr<VideoCaptureDeviceFactory> platform_factory_;
  std::unique_ptr<VideoCaptureDeviceFactory> private_factory_;
  base::WeakPtrFactory<HardenedVideoCaptureDeviceFactory> weak_factory_{this};
};

}  // namespace media

#endif  // MEDIA_CAPTURE_VIDEO_HARDENED_VIDEO_CAPTURE_DEVICE_FACTORY_H_
