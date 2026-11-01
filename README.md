# LumiNext

LumiNext is a local camera occupancy and ESPHome lighting controller with a web application and a separate multi-site cloud console. The application source is in this folder. No Home Assistant, Frigate or MQTT broker is required to run it. The independent streaming service uses go2rtc.

This is version 0.1.1, a working implementation for development and hardware commissioning. It is **not yet certified against the 12-camera / 5 fps / one-second latency target on the N95**. One real RTSP camera has now been verified end to end on the Windows development host; the owner also reports an ESPHome switch integrated successfully. Read [verification and remaining work](docs/STATUS.md) before commissioning physical loads.

## Start on this Windows development PC

The Python environment is already installed in `.venv`.

```powershell
Set-Location D:\LumiNext
.\start.ps1
```

Open [the local controller](http://127.0.0.1:8000). On first launch, create your administrator account. There is no default production password. Your real installation starts empty.

To run the cloud console locally in another terminal:

```powershell
.\.venv\Scripts\python.exe -m luminext --mode cloud --port 8001 --data data/cloud
```

Open [the local cloud console](http://127.0.0.1:8001) and create its separate administrator account. This development mode binds to loopback and uses HTTP. Production cloud hosting requires HTTPS, secure cookies and a server/domain; deployment is intentionally deferred until testing is finished.

## Explore without physical devices

1. Create the local administrator account.
2. Click **Explore simulation** in Overview. This is available only on an empty installation.
3. It creates 12 labeled simulated cameras, 36 simulated wall switches and 144 relays. No automatic lighting rules are installed.
4. In Automations, validate and save the example YAML or use the visual builder.
5. Open the first camera and click **Simulate a person**. The example rule turns on the first relay.
6. Toggle that relay manually. The relay displays a manual override and ignores automation actions.
7. Open the camera and click **Set view clear**. The override releases after this simulated stable-clear observation.
8. Write a separate off rule if you want a cleared camera to turn off a relay automatically. Examples are in `examples/`.

Simulation buttons represent stable observations after debounce, so their responses are immediate. They do not measure camera latency or inference throughput. Simulation data is kept in that installation's data directory; use a **different, empty data directory** for the real installation. Do not run demo devices as if they were physical loads.

## Manual override behavior

Every relay has an explicit **occupancy mapping**, configured with the link button beside the relay. This mapping governs override release; it does not generate lighting rules.

- A UI change or an observed physical wall-button change starts an override for that relay.
- Automation commands to that relay are skipped while the override is active; other relays continue normally.
- Every mapped occupancy source and its **entire parent camera view** must be available and confirmed clear before the override releases. Mapping a zone never allows an override to release while a person is still elsewhere in the parent camera's unmasked view.
- An offline camera, stale data or `unknown` state does not count as clear.
- No mapping means the override stays active. Map the covering cameras before commissioning.
- Overrides survive a controller restart. Real device and occupancy states become unknown until fresh observations arrive. Initial ESPHome state synchronization does not start a new override.
- Camera disappearance frames and off-delay must both be satisfied. Masked areas are excluded from detection.
- When a clear observation releases an override, off rules responding to that observation may run. Clearing an override alone does not invent a relay action.

The firmware supplied in chat is ESP8266, not ESP32, with four GPIO relays and an encrypted ESPHome native API. Discovery excludes the factory-reset switch and configuration/diagnostic switches. Keys and relay IDs come from the native API rather than assuming GPIO numbers are API keys.

The supplied firmware has `restore_mode: ALWAYS_OFF`. A device reboot therefore turns its relays off independently of LumiNext. Controller communication loss does not itself send a relay action. Firmware restart behavior must be chosen deliberately before deployment.

## Connect a real wall switch

In Devices, add its friendly name, reserved IP, API port (normally 6053) and ESPHome `api.encryption.key`. The key is entered locally, encrypted at rest, never returned by the browser API, and excluded from exports and cloud sync.

LumiNext connects over the native ESPHome API, discovers relay keys, subscribes to real states and reconnects after communication failures. A command must receive state confirmation within five seconds. Timeout and offline responses are reported as errors, with no optimistic success state. Physical button changes are observed through the same subscription.

Do not use the unprotected ESPHome web server as the remote control interface. This implementation does not depend on that web server or OTA access.

## Automations

The YAML editor implements an explicit **HA-style subset**, not the complete Home Assistant language. Unsupported options and unknown entity IDs are rejected. Existing Home Assistant automations may need conversion.

| Feature | Supported syntax |
| --- | --- |
| Triggers | `state`, `numeric_state`, `time` at `HH:MM:SS`, `manual` |
| Conditions | `state`, `numeric_state`, `time` before/after, nested `and` / `or` / `not` |
| Relay actions | `switch.turn_on`, `switch.turn_off`, `switch.toggle`; one entity or a list |
| Other actions | `delay`, `choose` / `default`, `luminext.notify` to the local activity log |
| Run modes | `single`, `restart`, `queued`, `parallel`; `max` of 1..50 |
| Editing | YAML validation, visual occupancy-rule generator, enable/disable, manual run, live apply |
| Debugging | Completed/failed/cancelled traces with trigger, condition result, steps and timestamps |

Templates/Jinja, blueprints, arbitrary HA services, scripts/scenes, `for` triggers, calendar/sun triggers and external notifications are not implemented. Use per-camera off-delay for occupancy lingering. Delay timers and in-flight runs are cancelled on restart or rule replacement; they are not durable jobs. Queued conditions are evaluated when a queued run begins. Schedules use the site's IANA timezone, defaulting to `Asia/Dhaka`.

Do not create reflexive relay-toggle rules that trigger themselves. The engine has per-rule concurrency limits but is not a general-purpose feedback-loop analyzer.

## Ubuntu/Debian local installation

For a native Python installation:

```bash
sudo apt-get update
sudo apt-get install python3-venv ffmpeg vainfo intel-opencl-icd intel-media-va-driver
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-detection.txt
.venv/bin/python tools/download_model.py
bash start.sh
```

Initial installation and model downloads require internet. Runtime inference, control, rules and the local UI do not. All UI assets are served locally with no CDN dependency. Model installation is a separate explicit step.

Add cameras with their RTSP substream URLs. Set the relevant network routing/reserved IPs outside LumiNext. The app does not provide DHCP or act as a router. ESPHome and camera addresses must be reachable from the host/container network.

Native installations now automatically start and supervise the independent detection process when an enabled real camera has an RTSP URL. Install `requirements-detection.txt` and download the model first. On Windows, the packaged `imageio-ffmpeg` executable is used if FFmpeg is not on PATH. The worker log is `data/detection.log`; missing packages/model/FFmpeg are reported in the controller's health data. Set `LUMINEXT_AUTOSTART_DETECTION=0` to manage detection externally, as Docker Compose does.

For an externally managed detection process, use:

```bash
.venv/bin/python -m luminext.worker \
  --api http://127.0.0.1:8000 \
  --data data \
  --model models/person-detection-retail-0013.xml \
  --device GPU
```

The worker gets a private credential from the shared local data directory, opens one FFmpeg process per camera, attempts VAAPI decode on Linux, and runs OpenVINO. Software decode and CPU inference fallback are explicitly reported. Make `/dev/dri/renderD128` accessible to the service account and verify the Intel compute/media drivers on the target host. GPU availability on this development PC does not establish N95 driver readiness.

The default downloaded model is Open Model Zoo's **person-detection-retail-0013 FP16**, a MobileNetV2-like SSD person detector with `1×3×320×544` BGR input. This is a supported concrete default rather than the optional 300×300 model suggested in the brief. The adapter also accepts raw YOLOv8/11 exports; configure `--model-type yolo --person-label 0 --color RGB --scale 0.003921568627`. End-to-end/NMS-export models and other output formats need a new adapter. Model configuration must match the actual artifact.

The worker uses a shared inference semaphore and a bounded latest-frame queue per camera, dropping queued video frames under pressure rather than accumulating an unbounded delay. Motion gates inference, with at least a one-second periodic refresh for stationary people. Camera/zone occupancy has frame confirmation, disappearance and time delay in the controller. Latest JPEGs are kept only in RAM for a live fallback; no frame/video recording is enabled.

The current worker does not export per-process iGPU utilization or object tracking identities. Commission actual coverage and stationary-person behavior carefully rather than treating a model output as a complete occupancy guarantee.

## Docker Compose on the N95

```bash
# Download the model first, or transfer the models folder from development.
python3 tools/download_model.py

# Initialize the controller and private streaming service on loopback.
docker compose up -d --build

# Determine actual host device group IDs.
stat -c '%g' /dev/dri/renderD128
getent group video

# Set these in a local .env (do not commit it).
# RENDER_GID=<renderD128 numeric group>
# VIDEO_GID=<video numeric group>
# LUMINEXT_BIND=<mini-PC LAN IP, or 127.0.0.1 for development>
# LUMINEXT_MEDIA_IP=<mini-PC LAN IP used by WebRTC viewers>

docker compose --profile hardware up -d --build
```

Docker containers restart on failure. The API and detection services are separate. Configuration changes are picked up by the detection worker in approximately five seconds. Mounts retain configuration across updates. A new app image does not erase the named data volume.

The streaming API and RTSP restream ports stay private to the Compose network. Go2rtc's runtime configuration is on tmpfs so stream credentials are not persisted as a plaintext configuration file. WebRTC signaling passes through the authenticated LumiNext API; media uses encrypted WebRTC on port 8555. Set the media IP to a reachable LAN/overlay address and restrict the port to that network. No public STUN/TURN server is configured.

The camera grid uses the two-fps shared JPEG fallback; camera detail offers **Play low-latency WebRTC** when go2rtc is configured. Browser codec, LAN ICE connectivity and H.265 support must be checked on the target devices. The cloud console deliberately does not relay or store video. Opening local video remotely requires reaching the local controller and WebRTC address over a private network/overlay.

Normal operation shares one upstream camera pull through go2rtc for detection and live viewing. While the media service is unavailable, detection falls back to the camera directly. Once the media service returns, the worker uses the restream again.

The Linux images and go2rtc/VAAPI/WebRTC paths have not been executed here because Docker Desktop's Linux engine is not running. Compose configuration was checked statically; do not interpret that as a successful image build.

## Test cloud management locally

Run the local controller at port 8000 and a separate cloud-mode process at port 8001 as shown above.

1. In the cloud console, enroll an installation and save its one-time site token.
2. In the local controller's Settings, enter `http://127.0.0.1:8001` and that token.
3. The edge initiates outbound HTTPS in production (loopback HTTP is allowed for development), synchronizes sanitized configuration/state every two seconds, and polls commands.
4. Open the site in the cloud console to view its synchronized dashboard, operate relays and submit YAML automations.
5. Remote commands expire after 30 seconds, are rejected when the site is stale/offline, and are shown as pending until the edge acknowledges execution. Results appear in remote command history.
6. Stop the cloud process and verify local controls/rules continue working.

Device secrets, RTSP URLs, API keys, local accounts and camera frames are not synchronized. Site tokens are hashed in the cloud and encrypted on the edge. Each site can only retrieve/acknowledge its own command queue. The edge keeps command IDs and results to avoid executing a redelivered command twice; an interrupted command is marked failed rather than replaying a physical action blindly.

This console is for **one organization's multiple sites**. All administrators/operators in that console can see its sites according to their role. Separate-customer tenancy, per-site user membership, MFA, token rotation UI and high-availability cloud operation need additional work before offering this as a public SaaS.

## Future cloud hosting

Deployment is deferred at the owner's request until the application is tested. The cloud service runs SQLite on persistent storage and must be deployed on a server with a durable volume, not a stateless static-site host.

When a domain and Linux server are ready:

```bash
export LUMINEXT_DOMAIN=console.your-domain.example

# Provision the first administrator before the HTTPS listener is public.
docker compose -f compose.cloud.yaml run --rm cloud \
  python -m luminext.admin --data /data

# After DNS points to the server and 80/443 are available:
docker compose -f compose.cloud.yaml --profile https up -d --build
```

The included Caddy configuration terminates HTTPS; the cloud app binds only to loopback on the host and uses secure session cookies in this deployment. Keep the private data volume backed up. Cloud does not need access to camera subnets and local controllers do not need inbound ports exposed publicly.

## Accounts and persistence

All API control/configuration routes and WebSocket connections require authentication. Browser writes require the application header and matching origin. Session cookies are HttpOnly and SameSite Strict. Passwords use salted scrypt, login attempts are rate limited and logout/deleting users revokes their sessions. Cloud and edge use different cookie names so they can be tested on separate localhost ports.

Admins manage devices, automations, users and configuration. Operators can view state/history and manually control relays. Device credentials are Fernet-encrypted in SQLite. The encryption key is a separate file (`data/encryption.key`) with restricted permissions on Linux; encryption does not protect against a compromised host that can read that key.

SQLite uses WAL and a versioned schema initialized at version 1. Future schema changes must add explicit migrations; this version has no legacy LumiNext schema to migrate.

## Backups, restore and updates

The controller creates a consistent SQLite backup at startup and daily, retains seven backups, and prunes event history to the configured retention. **Download database backup** creates another consistent copy. Database backups contain account records and encrypted device secrets. Site exports are portable templates without device credentials or current relay/override state.

To recover a full installation:

1. Stop controller, detection and cloud agent processes using the relevant deployment mechanism. Do not replace a live SQLite database.
2. Save the current data directory elsewhere before changing it.
3. Restore the selected database as `luminext.db` in a fresh data directory, together with the **matching `encryption.key`**. Do not reuse WAL/SHM files from the previous database.
4. Validate the restored database with SQLite `PRAGMA integrity_check` and clear `sessions` if existing sessions should be revoked.
5. Start the controller with that directory. Verify devices, mappings, fresh camera states and relay confirmations before enabling rules.

Without the matching encryption key, encrypted device credentials and site connection tokens cannot be recovered. Keep key backups protected separately. The app's backup download intentionally does not return the encryption key through the browser.

For upgrades, back up both database and key, retain the data volume, rebuild the image and restart. Test updates against a restored copy before applying them to a live installation.

## Development and tests

```powershell
.\.venv\Scripts\python.exe -m pip install pytest pytest-asyncio
.\.venv\Scripts\python.exe -m pytest -q
node --check web/app.js
```

Tests cover the actual API/runtime, not only the UI: role restrictions, session revocation, CSRF origin/header checks, secret redaction, worker tokens, debounce, zone geometry, camera-wide override release, queued/restart automations, persistence, configuration round-trip and cloud command isolation. A separate test launches two real HTTP servers and verifies outbound sync, remote acknowledgment, and local control after the cloud stops.

Frontend code is browser-native JavaScript and CSS; no build or internet connection is needed for the UI. The development browser test uses an isolated `data/qa` directory. Production starts in `data/` with no preloaded account, credentials or hardware assumptions.

Implementation references: [ESPHome native API client](https://github.com/esphome/aioesphomeapi), [Open Model Zoo detector specification](https://docs.openvino.ai/2023.3/omz_models_model_person_detection_retail_0013.html), [go2rtc streaming API](https://github.com/AlexxIT/go2rtc/blob/master/website/api/openapi.yaml). The supplied Frigate and Home Assistant reviews informed the service and entity boundaries; their source code was not copied into this application.
