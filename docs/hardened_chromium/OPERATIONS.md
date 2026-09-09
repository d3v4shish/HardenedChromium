# Operations, privacy, and recovery

## Browser roles and visual boundaries

The default Automation profile is `out/HardenedAutomationProfile`. It is one
visible process shared by the user and broker applications. Its blue boundary
means **application-accessible backend**. The separate Privacy binary uses
`out/HardenedPrivacyProfile`, is always red, and cannot expose CDP.

Privacy browsing is launched explicitly:

```sh
tools/hardened_chromium/run_privacy.sh
```

Named profiles passed to the Automation launcher remain blue Automation
profiles even when they omit shared-backend flags. Profile role markers prevent
accidentally opening either product's default profile with the other. The
explicit `HARDENED_ALLOW_PROFILE_SHARING=1` override allows sequential reuse;
a product-tagged process lock still rejects a concurrent cross-product owner.

## Privacy controls

Fresh profiles select fake camera, fake microphone, and fake location. The
permission UI keeps Chromium's ordinary allow/block decision, then lets the
user choose private or real source for camera, microphone, and location.
Per-origin source rules are managed at `http://127.0.0.1:8877/ui` or through
the `/service/privacy-*` APIs. Source choice never grants a permission.

Rules are read asynchronously from the browser's rules file. Permission paths
use an in-memory snapshot and fall back to fake sources while a new snapshot
loads. Missing or malformed policy also keeps the launcher/default source on
fake, preventing a blocking UI callback or a transient real-source leak.

Media/location defaults need a shared-browser restart. The ordinary `restart`
command restarts only the broker and intentionally leaves browser windows open.

## Normal commands

```sh
hardened-chromium-service capabilities --json  # discover without starting
hardened-chromium-service ensure --json        # start/reuse browser + broker
hardened-chromium-service status --json        # current readiness
hardened-chromium-service diagnostics --json   # safe crash/app-open summary
hardened-chromium-service logs --tail 12000 --json
```

`diagnostics` includes recent supervisor lifecycle events, app tab-open events,
and browser fatal/signal indicators. It redacts token-like values and is the
preferred artifact for issue reports. Raw logs can contain page URLs and should
be handled as potentially sensitive.

## Stopping and recovery

```sh
hardened-chromium-service stop
```

stops only the broker. To close the shared browser, an explicit confirmation
is required because it closes every window in that default profile:

```sh
hardened-chromium-service stop-browser --confirm-shared-browser
hardened-chromium-service ensure --json
```

If `ensure` returns `browser_unreachable`, Chromium is still alive but its
private CDP endpoint is unavailable. The service deliberately refuses to
spawn a competing browser or kill a user-visible window. Inspect diagnostics,
then use the confirmed stop command if the user approves recovery.

If Chromium is actually closed, `ensure` removes only stale Chromium runtime
markers after proving no live process owns the profile, starts one replacement
browser, retires a broker pointing to the old CDP endpoint, and starts a new
broker. Concurrent calls are protected by the service lock.

## Rendering stability

On Wayland, the launcher disables Vulkan by default because Chromium reports
the combination as unsupported on affected drivers; missing presentation
feedback can otherwise fill renderer callback queues and trigger a DCHECK.
Set `HARDENED_DISABLE_VULKAN=0` only after validating the local driver stack.

## Log locations

The state directory defaults to `~/.local/state/hardened-chromium-scrape`:

- `browser.log`: Chromium stderr and crash stacks;
- `broker.log`: broker HTTP/server diagnostics;
- `lifecycle.jsonl`: service start, recovery, and failure chronology;
- `<output root>/_broker_state/events.jsonl`: persisted job lifecycle events.

The default output root is `~/Downloads/Hardened Scrape Broker`. Both paths can
be overridden with the documented `HARDENED_SCRAPE_*` environment variables.
