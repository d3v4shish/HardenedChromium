#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import tempfile
import unittest
from pathlib import Path

from hardened_website_view import (
    DEFAULT_DOCUMENT,
    atomic_write_document,
    enforcement_metadata,
    load_document,
    resolve_policy,
    upsert_rule,
    validate_document_for_save,
    warnings_for_policy,
)


class HardenedWebsiteViewTest(unittest.TestCase):

  def test_exact_origin_rule_does_not_apply_to_subdomain_or_frame(self) -> None:
    document = upsert_rule(DEFAULT_DOCUMENT, {
        "origin": "https://example.test",
        "cameraSource": "real",
        "persona": {"platform": "ExampleOS"},
        "exposures": {"webRtc": "actual"},
    })
    exact = resolve_policy(document, "https://example.test/path")
    self.assertEqual("real", exact["cameraSource"])
    self.assertEqual("ExampleOS", exact["persona"]["platform"])
    self.assertEqual("actual", exact["exposures"]["webRtc"])

    subdomain = resolve_policy(document, "https://login.example.test")
    self.assertEqual("fake", subdomain["cameraSource"])
    self.assertEqual("block", subdomain["exposures"]["webRtc"])

  def test_expert_values_round_trip_without_silent_replacement(self) -> None:
    document = upsert_rule(DEFAULT_DOCUMENT, {
        "origin": "https://example.test:8443",
        "persona": {
            "userAgent": "My arbitrary value",
            "languages": ["zz-ZZ"],
        "screen": {"width": 1234, "height": 567},
        "custom": {"webglRenderer": "Custom Renderer"},
        "futurePersonaField": {"kept": True},
      },
        "exposures": {
            "canvas": "custom",
            "futureExposure": "kept",
            "webgl": "a future custom mode",
        },
    })
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "HardenedWebsiteView.json"
      atomic_write_document(path, document)
      loaded = load_document(path)
    resolved = resolve_policy(loaded, "https://example.test:8443")
    self.assertEqual("My arbitrary value", resolved["persona"]["userAgent"])
    self.assertEqual(1234, resolved["persona"]["screen"]["width"])
    self.assertEqual("Custom Renderer",
                     resolved["persona"]["custom"]["webglRenderer"])
    self.assertEqual("custom", resolved["exposures"]["canvas"])
    self.assertEqual({"kept": True}, resolved["persona"]["futurePersonaField"])
    self.assertEqual("kept", resolved["exposures"]["futureExposure"])
    self.assertEqual("a future custom mode", resolved["exposures"]["webgl"])
    self.assertTrue(any("webgl=" in warning
                        for warning in warnings_for_policy(resolved)))

  def test_url_rule_is_canonicalized_without_broadening(self) -> None:
    document = upsert_rule(DEFAULT_DOCUMENT, {
        "origin": "https://Example.test:443/path?query=value#fragment",
    })
    self.assertEqual("https://example.test", document["rules"][0]["origin"])
    self.assertEqual("fake", resolve_policy(
        document, "https://sub.example.test/path")["cameraSource"])

  def test_invalid_origins_never_create_broad_rules(self) -> None:
    with self.assertRaises(ValueError):
      upsert_rule(DEFAULT_DOCUMENT, {"origin": "file:///tmp/private"})
    with self.assertRaises(ValueError):
      upsert_rule(DEFAULT_DOCUMENT, {"origin": "https://user@example.test/"})

  def test_enforcement_metadata_does_not_claim_future_fields_apply(self) -> None:
    metadata = enforcement_metadata()
    self.assertIn("cameraSource", metadata["enforced"]["default"])
    self.assertIn("persona", metadata["retainedOnly"])
    self.assertIn("rules[].exposures.automation", metadata["retainedOnly"])

  def test_save_validation_rejects_invalid_enforced_values(self) -> None:
    invalid = {
        "default": {"cameraSource": "camera-is-not-a-mode"},
        "rules": [],
    }
    with self.assertRaisesRegex(ValueError, "cameraSource"):
      validate_document_for_save(invalid)


if __name__ == "__main__":
  unittest.main()
