# Implementation and verification status

This file distinguishes implemented behavior from testing still needed. It is not a production certification.

Version 0.1.1 update: one real RTSP camera now supplies live frames and detection results on the Windows development host. Native detection is automatically supervised, a Windows FFmpeg fallback is installed, and the RTSP timeout compatibility issue is fixed. The owner reports a real ESPHome switch is working. The original verification limits below still apply to the full N95 installation, Linux/GPU deployment and WebRTC. See TEST_REPORT.md for the new camera evidence and 20 passing tests.

| Area | Implemented | Verification / limit |
| --- | --- | --- |
| Local web app | Responsive dashboard, camera grid/detail, controls, device admin, history, health, settings | Browser exercised on development PC at compact and desktop widths |
| First-run setup / roles | Administrator setup, operator/admin accounts, login/logout, sessions, API checks | Automated API tests; no default production account |
| Entities | Stable camera/zone occupancy and discovered relay IDs; state, availability, area, history | API/runtime tests and simulated 144-relay workspace |
| ESPHome | Encrypted native API, relay discovery, state subscription, confirmation timeout, reconnect; reset excluded | APIClient calls checked against installed client; real 36-device hardware test pending |
| Overrides | UI/physical changes pause relay automation; all mapped parent camera views must be confirmed empty | Runtime tests including unavailable sources, zone clear with parent occupied and clear-triggered off rules |
| Automations | Validated HA-style YAML subset, visual generator, four modes, conditions, delays, branches, enable/disable, live apply, run traces | Runtime tests and browser occupancy-to-relay loop; full HA syntax is not implemented |
| Camera ingest / inference | Independent worker, FFmpeg per stream, latest-frame queues, motion gating, OpenVINO SSD/YOLO adapter, CPU fallback | Official SSD model downloaded and compiled; one blank-frame CPU inference succeeded; real RTSP and sustained inference pending |
| GPU / decode | VAAPI decode and OpenVINO GPU selection with explicit CPU/software fallback | Linux/iGPU driver test on the N95 pending |
| Zones / masks | Polygon editor, relative coordinates, bottom-centre zone membership, model filters | Runtime geometry test; alignment on real camera views pending |
| Live video | RAM-only JPEG fallback; authenticated go2rtc WebRTC signaling and shared RTSP restream integration | Real media server, browser codecs and ICE path pending; no cloud video relay |
| History / audit | State transitions, user commands, config edits, traces, retention | API tests; long-duration disk growth/load tests pending |
| Config / recovery | Secret-free export/import, SQLite backups, matching-key restore procedure | Configuration round-trip tested; full N95 recovery drill pending |
| Cloud console | Multiple sites, enrollment tokens, sanitized config/state sync, acknowledged commands, local independence | Real HTTP edge/cloud integration test; no external deployment yet by owner's instruction |
| Packaging | Native Windows/Linux entry points; controller/detection Compose services; HTTPS cloud configuration | Syntax / Compose static checks; Docker Linux engine unavailable for image build/run |
| Capacity / latency | Configuration and simulation for 12 cameras / 36 four-relay switches | No claim that 12×5 fps or ≤1 second is achieved on N95; benchmark required |

## Remaining commissioning work

1. Build and run the Linux containers on the target N95. Verify Intel compute/media drivers, GPU device/group permissions, decode fallback and restart behavior.
2. Connect one real camera and one real ESPHome wall switch. Check relay discovery excludes factory reset; validate physical buttons and confirmed command states.
3. Map the camera to all intended relays. Exercise stationary people, occlusion, lighting changes, masks, debounce and manual override until the whole camera view clears.
4. Disconnect/reconnect camera, switch, worker, streaming service, API and internet independently. Check that unknown occupancy never means an empty room and that manual control survives detection faults.
5. Scale from one camera to 12 and from one switch to 36. Record sustained processed fps, CPU/RAM, end-to-end latency and thermal behavior on the N95. Adjust stream resolution/fps/model only from those measurements.
6. Test native WebRTC and JPEG fallback on intended desktop/tablet/phone browsers. Verify no media recording/persistent frame storage and no public access to go2rtc's management/RTSP ports.
7. Run a backup-and-restore drill with the matching encryption key, power-loss recovery and a multi-day operation test.
8. Complete security and cloud deployment review, including site-token rotation/revocation, organization/site permissions if required, HTTPS, audit access, backup protection, limits and update strategy.

Optional/later features from the brief: scenes and virtual groups, multiple model adapters beyond the supported SSD/YOLO formats, object identity tracking, iGPU utilization telemetry, a richer visual automation editor and additional HA services/triggers. They are not represented as completed features.
