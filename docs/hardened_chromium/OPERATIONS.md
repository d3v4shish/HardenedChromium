# Operations, privacy, and recovery

## Browser roles and visual boundaries

The default automation profile is `out/HardenedAutomationProfile`. It is one
visible Chromium process shared by the user and all broker applications. Its
blue boundary means **application-accessible backend**. It remains blue even
when private sources are active.

Named profiles are isolated user browsing sessions:

```sh
tools/hardened_chromium/run_for_automation.sh --named-profile research
```

They do not expose the broker backend. A hardened named profile uses the red
boundary while privacy protection is active. The distinction matters: blue
means an application can create tabs in that profile; red means it cannot.

## Privacy controls

Fresh profiles select fake camera, fake microphone, and fake location. The
permission UI keeps Chromium's ordinary allow/block decision, then lets the
user choose private or real source for camera, microphone, and location.
Per-origin source rules are managed at `http://127.0.0.1:8877/ui` or through
the `/service/privacy-*` APIs. Source choice never grants a permission.

Rules are read asynchronously from the browser's rules file. Permission paths
use an in-memory snapshot and fall back to fake sources while a new snapshot
loads, preventing a blocking UI callback or a transient real-source leak.

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
