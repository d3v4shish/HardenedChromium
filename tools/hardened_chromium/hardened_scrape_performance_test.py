#!/usr/bin/env python3
"""Performance-path regressions for incremental scrape persistence."""

from pathlib import Path
import tempfile
import time
import unittest

from hardened_scrape_broker import (
    COLLECT_ITEMS_JS,
    CUSTOM_COLLECT_ITEMS_JS_TEMPLATE,
    Job,
    add_items,
    write_outputs,
)
from benchmark_performance import choose_variant, passes_gates


def raw_item(index: int) -> dict:
  return {
      "key": f"item-{index}",
      "text": f"Dynamic item number {index}",
      "author": "fixture",
      "permalink": f"https://example.test/items/{index}",
      "links": [f"https://example.test/items/{index}"],
  }


class IncrementalOutputTest(unittest.TestCase):

  def setUp(self) -> None:
    self.temporary = tempfile.TemporaryDirectory()
    self.job = Job(
        id="performance-test",
        app_id="test",
        url="https://example.test/",
        output_dir=Path(self.temporary.name) / "job",
        config={"max_items": 100, "checkpoint_items": 2},
    )
    self.seen: set[str] = set()

  def tearDown(self) -> None:
    self.temporary.cleanup()

  def test_jsonl_is_appended_once_per_unique_item(self) -> None:
    self.assertEqual(add_items(self.job, [raw_item(1), raw_item(2)], self.seen), 2)
    self.assertEqual(add_items(self.job, [raw_item(2), raw_item(3)], self.seen), 1)

    lines = (self.job.output_dir / "items.jsonl").read_text(
        encoding="utf-8").splitlines()
    self.assertEqual(len(lines), 3)
    self.assertEqual(self.job.exports["itemsJsonl"],
                     str(self.job.output_dir / "items.jsonl"))

  def test_checkpoint_is_coalesced_but_force_flushes_all_formats(self) -> None:
    add_items(self.job, [raw_item(1), raw_item(2)], self.seen)
    self.assertFalse(write_outputs(self.job))
    self.assertTrue(write_outputs(self.job, force=True))

    for filename in (
        "items.jsonl", "items.csv", "latest.json", "latest.html",
        "feed.json", "feed.rss", "feed.atom", "manifest.json"):
      self.assertTrue((self.job.output_dir / filename).is_file(), filename)

    before = (self.job.output_dir / "items.jsonl").read_text(encoding="utf-8")
    add_items(self.job, [raw_item(3)], self.seen)
    write_outputs(self.job, force=True)
    after = (self.job.output_dir / "items.jsonl").read_text(encoding="utf-8")
    self.assertEqual(after.count("\n"), 3)
    self.assertTrue(after.startswith(before))

  def test_elapsed_checkpoint_flushes_without_force(self) -> None:
    add_items(self.job, [raw_item(1), raw_item(2)], self.seen)
    self.job.last_output_flush_monotonic = time.monotonic() - 20
    self.assertTrue(write_outputs(self.job))
    self.assertEqual(self.job.last_output_item_count, 2)


class IncrementalCollectorSourceTest(unittest.TestCase):

  def test_collectors_use_observers_sets_and_fallback_scans(self) -> None:
    for source in (COLLECT_ITEMS_JS, CUSTOM_COLLECT_ITEMS_JS_TEMPLATE):
      self.assertIn("new MutationObserver", source)
      self.assertIn("new IntersectionObserver", source)
      self.assertIn("state.cycles % 10", source)
      self.assertIn("const seen = new Set()", source)
      self.assertNotIn("urls.includes(url)", source)


class VariantSelectionTest(unittest.TestCase):

  @staticmethod
  def metrics(startup: float, speedometer: float, scrape: float,
              rss: float = 500) -> dict:
    return {
        "startup": {"value": startup, "peakRssMiB": rss},
        "speedometer": {"value": speedometer, "peakRssMiB": rss},
        "dynamicScrape": {
            "value": scrape, "peakRssMiB": rss, "completeness": 1.0},
    }

  def test_zen4_requires_two_percent_balanced_gain(self) -> None:
    results = {
        "legacy": self.metrics(100, 100, 100),
        "portable": self.metrics(90, 110, 120),
        "zen4": self.metrics(85, 115, 130),
    }
    selected, _decision = choose_variant(results)
    self.assertEqual(selected, "zen4")

  def test_single_metric_regression_rejects_candidate(self) -> None:
    results = {
        "legacy": self.metrics(100, 100, 100),
        "portable": self.metrics(80, 120, 90),
    }
    selected, _decision = choose_variant(results)
    self.assertEqual(selected, "legacy")

  def test_regression_gate_allows_five_percent_and_fifteen_percent_rss(self) -> None:
    baseline = self.metrics(100, 100, 100, rss=100)
    candidate = self.metrics(105, 95, 95, rss=115)
    self.assertTrue(passes_gates(candidate, baseline))
    candidate["dynamicScrape"]["peakRssMiB"] = 116
    self.assertFalse(passes_gates(candidate, baseline))


if __name__ == "__main__":
  unittest.main()
