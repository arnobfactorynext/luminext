# Development verification

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
