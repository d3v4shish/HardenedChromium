# Real and private source selection

Hardened Chromium keeps its ordinary Allow/Block permission decision. When a
site requests camera or microphone access, the native permission preview lists
both the private virtual source and available system devices. Location prompts
add a separate **Private location / Real location** selector.

Fresh profiles default to private camera, private microphone, and private
location. A selection made in the permission preview is reused by Chromium's
normal device preference ranking. Location selection is scoped to the current
primary-page navigation.

The browser-owned content boundary shows the compiled product role. Privacy
builds are always red. Automation builds are always blue whether privacy
sources are real or private, warning that broker applications can control tabs
in that process. Runtime switches cannot change either product's boundary.

## Management UI

Open `http://127.0.0.1:8877/ui`, then use **Settings** to configure:

- the default camera source and private backend (loop, synthetic, or OBS);
- the default microphone source;
- private latitude, longitude, and accuracy;
- remembered source rules for individual website origins.

Media and location defaults require an explicit shared-browser restart to
apply. The ordinary service restart deliberately leaves browser windows open.
Remembered website rules refresh from a process cache within one second. Rules
select a source; they never grant a permission.

## API

The compatibility endpoints remain available:

- `GET|POST /service/media-settings`
- `GET|POST /service/location-settings`

Schema-v2 clients can use:

- `GET|POST /service/privacy-settings`
- `GET|POST /service/privacy-rules`
- `DELETE /service/privacy-rules/<rule_id>`

All endpoints are loopback-only under the broker's existing request-security
rules.

## Test page

Open `tools/hardened_chromium/hardened_mode_test.html`. The media section can
request camera only, microphone only, both independently, or repeat the last
request. The API inspector reports the values visible to page JavaScript.
After permission, device enumeration and track labels are normalized to
generic **Camera** and **Microphone** identities.
