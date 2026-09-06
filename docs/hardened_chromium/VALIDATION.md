# Validation and troubleshooting checklist

## Static and build checks

Run these from the Chromium source root after applying the overlay:

```sh
git diff --check
gn gen out/Hardened --args='is_debug=false is_component_build=false dcheck_always_on=true symbol_level=1'
third_party/ninja/ninja -C out/Hardened chrome
```

The native build is required after changes to `content`, `media`, `chrome`,
`components`, or Blink. A successful Python broker test cannot validate those
changes.

## Service and broker suite

```sh
cd tools/hardened_chromium
python3 -m unittest hardened_scrape_broker_test.py \
  hardened_scrape_service_test.py hardened_website_view_test.py \
  hardened_scrape_security_test.py \
  hardened_scrape_performance_test.py hardened_scrape_stream_test.py \
  hardened_scrape_install_test.py
```

These cover scheduler limits, authorization isolation, event replay, slow
consumer behavior, stream persistence, installer discovery, service ownership,
browser recovery, and no-auth/token-mode conflicts.

## Runtime acceptance test

1. Start the backend with `hardened-chromium-service ensure --json`.
2. Start two independent applications or run:

   ```sh
   python3 manual_multi_app_test.py --close-tabs
   ```

3. Confirm the reported shared browser PID is identical for both callers,
   while each submitted job receives a distinct target/tab ID.
4. Confirm output files and streamed items arrive, then close only completed
   job tabs.
5. Run `hardened-chromium-service diagnostics --json`.

For a trusted no-auth setup, repeat consistently with `--no-auth`. Never run
one client in token mode and another in no-auth mode against the same broker.

## Privacy acceptance test

Use `hardened_mode_test.html` in a named profile and test camera only,
microphone only, both, and location. Verify that changing source selection does
not bypass the ordinary permission prompt. Save a per-origin fake-source rule,
restart the browser, and confirm it remains fake from the first request. Also
open `chrome://settings/privacy`, save a Website View rule for
`https://example.test`, and confirm it does not affect
`https://sub.example.test` or an embedded third-party frame. Start through the
automation launcher, then verify `navigator.webdriver` is `false`; repeat with
`HARDENED_WEBDRIVER_MODE=report` to confirm the user-visible control works.

## Crash triage

`diagnostics --json` is the preferred first artifact. It returns the browser
and broker status, recent supervisor events, app tab-open events, and fatal
indicators while redacting token-like values.

- `browser_unreachable`: the profile is visibly owned but private CDP is dead.
  Obtain user approval, then use `stop-browser --confirm-shared-browser` and
  run `ensure` again.
- `auth_mode_conflict` or `broker_conflict`: choose a single authentication
  mode/port; do not silently replace a broker with different security policy.
- `tab_open_failed`: inspect CDP/browser crash indicators before retrying the
  job.
- `FATAL` with blocking restrictions: verify the privacy-rule file is only
  read through the asynchronous snapshot cache and rebuild the native binary.
- Wayland presentation failures: keep Vulkan disabled unless the graphics
  stack has been explicitly validated.

Raw browser and broker logs may include page URLs or local paths. Keep them out
of public issue reports unless they have been reviewed.
