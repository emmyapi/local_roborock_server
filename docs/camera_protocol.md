# QRevo Curv "Remote Viewing" — Protocol Reverse Engineering

Device: `roborock.vacuum.a135` (QRevo Curv), firmware behavior as of 2026-04-17.
App: patched Roborock Android 4.60.06 pointed at `api-roborock.mkb.dk`.
Server: `local_roborock_server` (this repo), vacuum + app both speaking to the
local MQTT broker and local HTTPS API.

Raw captures are under `research/camera-2026-04-17/`.

## TL;DR

- "Remote viewing" / "pet monitoring" is driven by **plain JSON-RPC** over the
  existing MQTT topics (`rr/m/i/...`, `rr/d/o/...`). No extra encryption layer
  beyond the standard librrcodec envelope this server already decodes — `rpc` is
  visible in `decompiled_mqtt.jsonl` today, no new work required.
- The vacuum is the **WebRTC initiator**. On receiving `start_camera_preview`
  it tries to reach a TURN server; if that fails it returns
  `{"code":-10014,"message":"request turnserver failed"}` and no media is sent.
- TURN endpoints are **not hardcoded into firmware** — the vacuum actively
  requests one. We never see that request on `api-roborock.mkb.dk` (neither
  HTTPS nor MQTT), so the vacuum is reaching the TURN coordinator through some
  other channel: direct TCP to a Roborock IP, or DNS to a hostname we don't
  yet intercept. The vacuum-side endpoint-discovery path is the next unknown.

## Phase 0 outcome: **PASS**

Per `plans/roborock-front-camera-local.md` Phase 0 gate: "if signaling payloads
are end-to-end encrypted with a key only Roborock cloud holds, the project ends
here." The payloads are plaintext JSON-RPC after the standard codec unwrap —
this is the best-case outcome.

## Observed RPC sequence (app → vacuum)

Captured at 2026-04-17T17:16–17 while the user toggled "Remote Viewing" in the
patched app:

| id   | dir | method                       | params                                                             | response                                               |
|------|-----|------------------------------|--------------------------------------------------------------------|--------------------------------------------------------|
| 1013 | →   | `set_camera_status`          | `[387]`                                                            | `["ok"]`                                               |
| 1029 | →   | `set_camera_status`          | `[419]`                                                            | `["ok"]`                                               |
| 1030 | →   | `set_camera_status`          | `[4515]`                                                           | `["ok"]`                                               |
| 1054 | →   | `check_homesec_password`     | `{"password":"<md5>"}`                                             | `["ok"]`                                               |
| 1055 | →   | `get_homesec_connect_status` | `[]`                                                               | `{"status":0,"client_id":"none"}`                      |
| 1056 | →   | `switch_video_quality`       | `{"quality":"HD"}`                                                 | `["ok"]`                                               |
| 1057 | →   | `start_camera_preview`       | `{"client_id":"gWlOQ7VE","password":"<md5>","quality":"HD"}`       | `{"code":-10014,"message":"request turnserver failed"}`|
| 1058 | →   | `stop_camera_preview`        | `{"client_id":"gWlOQ7VE"}`                                         | `["ok"]`                                               |

The app then retries the `get_homesec_connect_status` → `start_camera_preview` →
`stop_camera_preview` triplet several times with escalating delay, always
getting the same `-10014` error.

### Notes on individual methods

- `set_camera_status` — single integer, appears to be a bitmask of enable flags.
  Observed values 387, 419, 4515 are close in low bits: 387=0x183, 419=0x1A3
  (adds 0x20), 4515=0x11A3 (adds 0x1000). Likely bit flags for camera-on,
  mic-on, quality, privacy-ack etc. Needs more samples to decode.
- `check_homesec_password` / `get_homesec_connect_status` — **"homesec"** is
  Roborock's internal name for the camera/monitor feature. The MD5 is computed
  client-side; the vacuum just returns `["ok"]` regardless of value in our
  capture, so the check happens on the *vacuum* side (not our server).
- `switch_video_quality` — enum `"HD"` seen; likely also `"SD"`.
- `start_camera_preview` — `client_id` is an 8-char random per-session token.
  The `password` matches `check_homesec_password`. `quality` duplicates the
  earlier `switch_video_quality` call. Return is either the error above or
  (presumably, against cloud) an SDP-bearing payload we have not yet captured.

Status fields visible in `get_prop get_status` responses that relate:
`camera_status`, `monitor_status`, `voice_chat_status`, `home_sec_status`,
`home_sec_enable_password`. `camera_status=385` during the test, matching the
`set_camera_status` bitmask neighborhood.

## HTTPS traffic during the test

App → local server (phone `192.168.10.248`):

- `GET /user/homes/12060926/rooms` — routine room listing.
- **`PUT /user/devices/{duid}/extra` body `key=RRMonitorPrivacyVersion&value=1`**
  — one-time privacy consent ack for **RRMonitor** (= Roborock Monitor, the
  camera feature). Current `get_device_extra` handler returns this correctly.
- 2× `GET /ota/firmware/{duid}/updatev2?lang=en` — fell into `catchall`
  route. App possibly gates the feature on firmware version; stub returns
  `{ok:true}` so this doesn't block the test.

Vacuum → local server (vacuum `192.168.10.110`) during the same window:
**none**. The vacuum makes no HTTP request related to the camera session. Its
TURN-server discovery path bypasses `api-roborock.mkb.dk` entirely.

## The open question: where does the vacuum fetch its TURN config?

`-10014 "request turnserver failed"` fires ~130ms after
`start_camera_preview` — too fast for a DNS timeout, consistent with a
connection refused/cached-endpoint-unreachable. Candidate paths the vacuum might
use to reach a TURN coordinator:

1. **Direct TCP/TLS** to a hardcoded Roborock IP (no DNS).
2. **DNS to a non-`api-` hostname** that our intercept doesn't cover
   (e.g. `iot.roborock.com`, `turn.roborock.com`, `srs-eu.roborock.com`).
   Needs packet capture on the LAN gateway (MikroTik) to confirm.
3. **A cached TURN endpoint** baked into firmware config from the last cloud
   pairing, now unreachable because that endpoint's IP is firewalled.
4. **A Roborock-specific discovery protocol** we haven't yet identified.

To resolve: either (a) cloud reference capture — pair the vacuum to real
Roborock cloud for one session, mitmproxy + tcpdump at the gateway, observe the
full successful handshake — or (b) packet-capture on the MikroTik during a local
failed-`start_camera_preview` attempt to see where the vacuum is dialing out.

Option (b) is much cheaper and sufficient to identify the endpoint hostname; it
may or may not yield the response payload (depends on TLS). Option (a) is
definitive.

## What upstream has and doesn't

Searched `src/` for `turn|stun|webrtc|sdp|camera_preview|homesec` — no matches
in code (only incidental substring matches like "return"). The entire camera
flow is unimplemented in upstream as of the checkout used here. Confirms the
upstream writeup's "Maps remain cloud-dependent" caveat generalizes: anything
touching media/TURN is cloud-dependent too.

## python-roborock knows more method names than we've captured

`python-roborock`'s `roborock_typing.py` `RoborockCommand` enum lists the full
WebRTC flow as named-but-unimplemented constants:

- `GET_TURN_SERVER = "get_turn_server"` — fetch TURN config
- `GET_DEVICE_SDP = "get_device_sdp"` — vacuum's SDP offer
- `GET_DEVICE_ICE = "get_device_ice"` — vacuum's ICE candidates
- `SEND_ICE_TO_ROBOT = "send_ice_to_robot"` — app's ICE → vacuum
- `START_VOICE_CHAT` / `STOP_VOICE_CHAT` / `SET_VOICE_CHAT_VOLUME` — two-way audio
- `ENABLE_HOMESEC_VOICE`, `SET_FDS_ENDPOINT` — related

So the full session shape is: `start_camera_preview` → `get_turn_server` →
`get_device_sdp` / `get_device_ice` / `send_ice_to_robot` → media via TURN.
In our failed capture the session died at step 1 because the vacuum couldn't
fetch a TURN server; steps 2–4 never fired.

## What's unknown after this capture

The single remaining unknown is **where the vacuum dials out to fetch the TURN
config**. We've ruled out:

- The MQTT broker we control — vacuum sent no `get_turn_server` or similar RPC
  on `rr/d/i/...` (we grep'd the full 1364-line capture and scanned vacuum
  outbound methods — zero RPC requests initiated by the device).
- Our HTTPS API (`api-roborock.mkb.dk`) — vacuum's only hits during the window
  were `time`, `region`, `location`, `nc`, and `devices/.../info`; none
  camera-related.

Remaining candidates (in rough order of likelihood):

1. **DNS to a non-`api-` Roborock hostname** our Cloudflare intercept doesn't
   cover — e.g. `euiot.roborock.com`, `mqtt-eu.roborock.com`, `i-eu.roborock.com`,
   `turn.roborock.com`, `srs-eu.roborock.com`. These would resolve to real
   Roborock IPs; the vacuum would try to authenticate there with its *local*
   credentials and fail.
2. **A cached IP** from the last cloud pairing, now firewalled.
3. **A hardcoded IP** compiled into firmware.

**To resolve**: packet capture on the MikroTik during a failed
`start_camera_preview` attempt. That's a one-shot, zero-setup-cost observation
that tells us the hostname/IP the vacuum dials. The plan's Phase 0 step 2
(full cloud reference capture via mitmproxy on am5gamingpc) is still the
definitive move for getting the *successful* protocol bytes, but is much
heavier and can wait.

## Raw captures

- `research/camera-2026-04-17/mqtt_capture.jsonl` — decoded MQTT slice (1364 lines)
- `research/camera-2026-04-17/http_capture.jsonl` — HTTPS slice (4 lines)
- `research/camera-2026-04-17/mqtt_server_slice.log` — mqtt server log slice
- `research/camera-2026-04-17/api_server_slice.log` — api server log slice
- `research/camera-2026-04-17/capture-start.txt` — UTC start timestamp
- `research/camera-2026-04-17/log-line-cutoffs.txt` — pre-tap line counts
