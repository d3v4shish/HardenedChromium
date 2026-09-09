# Performance hotspots

- Event fanout is sensitive to payload serialization and number of subscribers.
  The broker now encodes each published event once and reuses the bytes for
  every WebSocket recipient.
- Durable event storage can be slower than producers. Its queue is limited to
  4,096 messages or 16 MiB and applies backpressure instead of growing without
  bound or losing persisted events. Disk-write failure is reported by health
  checks and causes later writes to fail explicitly.
- A full `chrome` build is a large native target and normally dominates build
  wall time; use the targeted Python test suite for fast iteration.
- The ordered-patch fixture must not clone or fetch a Git pack. A shared clone
  exhausted the temporary filesystem's quota; the fixture now initializes an
  empty repository and references the source object database through Git's
  alternates mechanism.
- Adapter domain dispatch is dominated by `urllib.parse.urlparse` (0.723 s of
  1.456 s cumulative in the profiled 100,000-call run). It remains well above
  the 20,000/s gate and is used only at job/target boundaries, so caching would
  add complexity without a measured workload benefit.
- Live-site collection cost is dominated by DOM traversal and site rendering.
  The collector observes mutations and intersections, caps pending roots at
  5,000, processes at most 200 roots per cycle, and performs a recovery scan
  every tenth cycle rather than rescanning on every pass. When an adapter
  declares a scope root, observation and recovery scans stay inside that root;
  this prevents WhatsApp sidebar churn from entering the collector pipeline.
- Account mode can discover many rendered URLs. It is bounded to 1,000 targets,
  5,000 items, and the job deadline; per-target navigation is expected to
  dominate CPU-side adapter dispatch.
