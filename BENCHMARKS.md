# Benchmarks

All commands below are deterministic local-loopback measurements from
`tools/hardened_chromium`; they use fixed rates and duration.

## Stream transport baseline (2026-09-09)

Command:

```text
python3 benchmark_stream_transport.py --sockets 32 --events-per-second 1000 --duration-seconds 3 --require-gates
```

Result before the persistence and payload-sharing changes: 3,000 of 3,000
events delivered; 1,000.3 events/s; p95 0.13654715 ms; p99 0.29393701 ms;
maximum 0.50676 ms.

The post-change run is recorded below after validation completes. Results are
environment-specific and are not treated as a performance claim without a
same-environment comparison.

## Stream transport after hardening (2026-09-09)

Same command and configuration: 3,000 of 3,000 events delivered; 1,000.3
events/s; p50 0.116140 ms; p95 0.176237 ms; p99 0.218070 ms; maximum
1.724085 ms. The run passed its configured gates (p95 <= 25 ms, p99 <= 75
ms). This is one same-environment run; it confirms the hardening did not
violate the benchmark gates, but is not treated as a general performance claim.

## Adapter dispatch after implementation (2026-09-09)

Command:

```text
PYTHONPATH=. python3 benchmark_adapters.py --iterations 20000 --require-gates
```

Result: pack verification 0.500 ms; domain resolution median 411,512/s;
schema-to-JavaScript compilation median 240,613/s. All configured gates passed.
The cProfile run changed interpreter overhead and measured 76,429 resolutions/s
and 159,053 compilations/s; its cumulative profile identified URL parsing as
the dominant dispatch cost. These are local control-plane measurements, not a
claim about live-site network or rendering throughput.

## Patch fixture I/O validation (2026-09-09)

The original `git clone --shared --no-checkout` fixture failed under the final
suite with `Disk quota exceeded`. After replacing pack cloning with `git init`
and a Git objects alternate, this focused command passed in 0.426 seconds:

```text
PYTHONPATH=. python3 -m unittest patch_bundle_test.PatchBundleTest.test_bundles_apply_in_order_to_pinned_base_fixture
```

The measurement demonstrates bounded fixture I/O in this environment; it is
not a general Git performance claim.
