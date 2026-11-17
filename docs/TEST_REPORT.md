# Development verification

## Version 0.2.0 verification on 1 November 2026

- Full suite: **34 passed** in 24.60 seconds. After final input-validation hardening, targeted camera/config round-trip checks also passed. The existing Starlette/httpx test-client deprecation warning remains non-fatal.
- Added checks for persisted friendly names, YAML redaction/batch validation, unchanged-camera state preservation, RHT numeric/text/binary readings, missing/NaN state, momentary button semantics, bounded commands/bridged identities, entity-catalog/name/profile/rule round-trip, energy gaps/manual-origin attribution/overnight schedules/negative net delta, stale boxes, and independent preview under an 800 ms slow inference.
- Browser QA: edited a sensor name, added a camera through Validate & apply YAML, configured a 100 W/10 BDT-per-kWh simulated load and verified it produced no savings, displayed a Person 95% test box, sent a simulated IR button command without inventing AC state, and used the visible Log out button successfully.
- All browser fixtures are in the ignored `data/qa-v02` workspace, separate from the installation's device credentials and configuration. Actual RHT firmware credentials from chat were not copied into source or fixtures.
- The RTSP port for Cam 102 was unreachable from the laptop, so this version's real-stream latency/overlay could not be benchmarked. RHT physical I/O, Linux/WebRTC and N95 load remain unverified.

The sections below describe earlier releases, not a claim that their counts/limitations are the current suite.

## Camera connection fix in version 0.1.1

After the initial test run, the owner configured one real camera. Its RTSP TCP port was reachable and OpenCV decoded a 1920×1080 frame, confirming the saved URL worked. LumiNext initially showed it offline because the native app had no running detection worker, Windows lacked FFmpeg, and the packaged FFmpeg rejected the original `rw_timeout` RTSP option.

The native launcher now supervises an independent camera worker, Windows has a packaged FFmpeg fallback, and RTSP ingest uses the supported socket `timeout` option. The corrected FFmpeg probe returned a complete 640×360 BGR frame (691,200 bytes). The running worker then successfully published live frames and person-detection results; the camera's occupancy entity changed from unavailable/unknown to available/clear.

The updated automated suite reports **20 passed** (13.45 seconds), including three new detection-supervisor/FFmpeg resolution checks. The owner separately reports successful ESPHome switch integration. Full 12-camera N95 load, Linux containers and WebRTC remain unverified. The sections below record the earlier v0.1.0 baseline rather than the current camera commissioning status.

Verified on 26 October 2026 in the Windows development workspace. These results apply to this implementation and environment, not the target N95 hardware installation.

## Automated checks

- `python -m pytest -q`: **17 passed** in the final functional test run (12.20 seconds).
- One test-client deprecation warning from the installed Starlette/httpx combination. No failed tests.
- `python -m compileall -q luminext tools`: passed.
- `node --check web/app.js`: passed.
- `docker compose config --quiet`: passed.
- `docker compose -f compose.cloud.yaml config --quiet` with an example domain: passed. This validates Compose configuration, not image build or runtime behavior.

Tests include authenticated roles, user/session revocation, request header and origin checks, encrypted/redacted RTSP URLs, worker credentials, explicit simulation seeding, invalid rules/imports, whole-view manual override semantics, unavailable sources, physical button classification, real-state confirmation using a test ESPHome client, stale camera handling, frame debounce/zone geometry, restart/queued rules, configuration round-trip, cloud token/site isolation, and actual edge/cloud HTTP synchronization with acknowledged relay control and local control after cloud shutdown.

## Browser checks

The isolated QA installation contained 12 simulated cameras and 36 simulated four-relay switches. The main installation was left empty for the owner's first-run setup.

- Signed in, validated/saved an occupancy YAML automation, simulated a person in the first camera and observed its relay turn on with a completed trace.
- Manually toggled that relay, observed manual override, cleared the camera view and observed the override release.
- Drew, named and saved a three-vertex polygon through the UI.
- Signed into the separate cloud console and opened the synchronized site dashboard. A remote relay command was acknowledged as completed by the local controller.
- Checked compact navigation, including accessible labels and sign-out.
- Checked desktop layout at 1440 pixels and phone layout with a 375-pixel CSS viewport. After correcting filter wrapping, the phone document width equaled its viewport width, with no horizontal overflow on Overview.
- Saved `simulation-preview.jpg` from the QA workspace. Its camera scenes and occupancy are explicitly labeled as simulation.

## Inference adapter smoke check

Downloaded the official Open Model Zoo FP16 `person-detection-retail-0013` model using the included downloader. The model compiled on this development PC's CPU with input shape `[1, 3, 320, 544]`; a blank BGR frame produced zero person detections. The observed first inference was approximately 14.5 ms. This single synthetic-frame result is an adapter check, **not a latency/accuracy/throughput benchmark or N95 measurement**.

Model download URLs, sizes and SHA-256 fingerprints are stored in `models/download-manifest.json`.

## Unverified paths

No real RTSP cameras or ESPHome switches were available for this test session. The 12-camera/5-fps workload, physical relay operations, Intel iGPU/VAAPI, go2rtc/WebRTC, long-duration operation, full target-host restore, power-loss recovery and external cloud deployment remain to be verified. Docker Desktop's Linux engine was unavailable, so Linux images were not built or run here. See STATUS.md for the commissioning sequence.

Final local application check: three targeted ESPHome/name/command tests passed after the last hardening changes; JavaScript syntax and git diff whitespace checks passed. Mobile Devices page had matching viewport/document widths (375 px). Desktop simulation screenshot: `v02-devices-preview.jpg`. Production SQLite backup passed integrity checking before restart; the updated edge server returned healthy, preserved the existing account, camera and device, and migrated to schema 2. Camera YAML and energy routes required authentication. Additional native entities were discovered after reconnect. Physical camera latency and RHT hardware remain unverified.

Detection update v0.2.1: the full suite passed 40 tests in 25.80 seconds. JavaScript syntax and diff whitespace checks passed. The alternate model downloader passed its real checksum/download smoke check. See DETECTION_REVIEW_2026_11_01.md for the 30-minute baseline, applied model/threshold/delay, and live validation limits.

Follow-up after controlled room tests: both tested models produced chair false positives at lower thresholds. The alternate model was rejected. Original model restored with 0.85 starting confidence and three consecutive per-person confirmations; weak or missing observations reset pending confirmation streaks. Full suite: 42 passed in 26.71 seconds; final targeted detection suite: 8 passed. Final real empty-room/re-entry verification remains pending.

Final controlled test passed: clear at 33.87 seconds after test start; all four target relays reported OFF and overrides released; occupied returned at 87.99 seconds on re-entry. Corrected an already-OFF relay causing a misleading confirmation timeout. Full suite: 43 passed in 26.08 seconds. Reporting fix deployed after database backup; live physical transitions were verified before this final restart.

Ten-minute recheck: 600 state samples, 245 UI samples, 30 zero-box samples; no occupancy transitions or automation runs. Fixed transient publication failures restarting RTSP capture/tracking. Full suite 46 passed in 25.61 seconds. Pose/occlusion misses remain an acknowledged limitation; see DETECTION_RECHECK_10_MINUTES.md.
