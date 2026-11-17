# LumiNext 0.2 changes and usage

Implemented on 1 November 2026. Start the controller normally and refresh the browser to load the new frontend. Existing accounts, device credentials and stable entity IDs are retained. No external cloud deployment was performed.

## Friendly names

Use the pencil next to a relay in Overview/Switch controls, or next to any entity under Devices. Enter a friendly name such as `Office ceiling lights` or `Office temperature`.

Names persist through native ESPHome rediscovery, reconnects and controller restarts. IDs remain unchanged so automation references keep working. Device-level names remain editable through Edit device. A name-only device change no longer forces an ESPHome reconnect.

## Camera YAML editor

Open **Camera YAML** from Live cameras or Devices. Validate first, then use **Validate & apply**. The whole batch is validated before applying configuration changes. Saving adds/updates the camera IDs present in the YAML; omitted cameras are kept. Unchanged definitions do not reset their occupancy state. Individual camera forms remain available under Devices and camera detail.

Example for a new camera:

```yaml
cameras:
  sewing_line:
    name: Sewing line
    area: Production
    ffmpeg:
      inputs:
        - path: rtsp://YOUR_USER:YOUR_PASSWORD@CAMERA_IP:554/stream
          roles: [detect]
    detect:
      enabled: true
      width: 640
      height: 360
      fps: 5
    live_fps: 10
    threshold: 0.5
    confirm_frames: 2
    disappear_frames: 10
    off_delay: 10
    zones:
      - id: work_area
        name: Work area
        points: [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]
    masks: []
```

The editor also accepts the direct LumiNext fields `rtsp_url`, `enabled`, `width`, `height` and `fps` instead of the corresponding ffmpeg/detect aliases. Polygon zones/masks use `id`, `name` and relative `points`. Only one RTSP detection input is supported per camera. This is a Frigate-style camera subset, not an importer for the complete Frigate configuration language: recording, arbitrary FFmpeg arguments, detectors and other unknown settings are rejected.

Existing stream credentials appear as `!secret sewing_line_rtsp`; saving that reference preserves the encrypted URL without revealing it. A new camera needs its actual URL entered locally. The editor only accepts the stored secret belonging to the same camera ID. It never returns stored plaintext passwords. Exports and cloud snapshots still exclude RTSP/device credentials.

## Manual override

Behavior remains per relay:

1. A manual UI command or an observed physical button change starts an override.
2. Automation actions targeting that relay are skipped. Other relays continue to operate.
3. Every mapped source **and its entire parent camera view** must be available and confirmed empty before the override releases. A clear zone alone is insufficient if another part of its camera view is occupied.
4. Disappearance frames and camera off-delay are respected. Offline/unknown sources never mean an empty room.
5. With no occupancy mapping, an override stays active until a valid mapping becomes clear. Map coverage before commissioning.
6. Clearing an override does not invent a switch action; the corresponding YAML clear/off rule can run after release.

This feedback-based override applies to relay switches. A momentary IR transmit button does not provide appliance-state feedback and is not treated as a confirmed relay or AC power state.

## Logout

The top bar now has a text-labeled **Log out** button, including on small screens. Logout revokes the session, closes the realtime connection and active media dialog, and clears the current frontend state. The sidebar sign-out control remains available.

## Live preview and person boxes

The former fallback sent at most two JPEG frames per second and shared its processing path with inference. Live preview now has a separate bounded latest-frame consumer and a configurable `live_fps` target, default 10. Inference still follows `detect.fps`, default 5. Slow inference or slow consumers cause old frames to be dropped rather than queued for later display.

The JPEG endpoint no longer polls at the old five-fps ceiling. FFmpeg uses low-delay/bounded-probe RTSP settings. The WebRTC option remains available when the private go2rtc service is configured. Preview rate is an output target, not a guarantee of new camera frames or N95 performance; source FPS, RTSP keyframes, host load and network quality still matter. A high preview target increases decode/network work; tune it for the final 12-camera host.

Person boxes and confidence labels appear over the grid/detail video from actual detection metadata. Toggle **Person boxes** or **Show person bounding boxes** to hide them. Box updates do not trigger a full page refresh. Metadata is held in RAM and expires when fresh detections stop; no video recording was added. Normalized coordinates follow the configured detection image dimensions. Detection-to-video synchronization can vary with network transport and inference cadence; this is not a tracked-object video compositor.

## Energy and avoided runtime

Open **Energy & runtime** and configure each real relay's:

- Total rated watts for everything controlled by that relay.
- The weekdays, hours and timezone it would normally stay on without these controls.
- Optional electricity tariff per kWh and currency.

Only observed, available relay states after configuration contribute. Simulated loads, controller downtime and unknown/offline intervals are excluded. History is checkpointed approximately every five minutes and at state/profile/availability transitions; an abrupt crash can lose the uncheckpointed tail rather than fabricating usage. The database retains 90 days of interval data. Graceful shutdown flushes active observations.

The calculation is:

```text
Estimated use       = rated watts / 1000 × observed on-hours
Estimated baseline  = rated watts / 1000 × observed hours inside the baseline schedule
Scheduled-off energy avoided = rated watts / 1000 × confirmed off-hours inside that schedule
Net estimated delta = estimated baseline − estimated use
Scheduled-off cost avoided  = scheduled-off energy avoided × entered tariff
```

For example, a fixed 100 W lighting load that stays off for four baseline working hours avoids an estimated 0.4 kWh. Cost depends on the tariff entered; no tariff is assumed.

Positive net delta means estimated usage below baseline; negative means above baseline, including running outside scheduled hours. Scheduled-off cost avoided is not a net bill-saving claim. Manual off-hours are shown separately and are not falsely attributed exclusively to AI automation. Missing tariffs or mixed currencies suppress incomparable cost totals. Profile changes only affect subsequent observations.

These are rated-power estimates, not utility measurements. Variable loads such as AC compressors can differ greatly from their nameplate rating. Use ESPHome-exposed W/kWh meter readings for actual electrical measurements; they appear under Devices. A real power meter still needs a defined baseline to estimate causal savings. No phantom consumption or savings is inferred from IR button presses.

## ESPHome device integration

Use **Add ESPHome device** with its IP/hostname, native API port and encryption key. Integration is based on the standard entities exposed by the firmware and is not restricted to wall switches, RHT units, ESP8266 or a particular ESP32 model.

| Entity category | Current behavior |
| --- | --- |
| Switch/relay | Confirmed on/off control, state feedback, physical override detection |
| Numeric sensor | Read temperature, humidity, voltage, power, energy and other numeric readings with units |
| Binary/text sensor | Read status, link/health and firmware/text readings |
| Number/select/text | Commands validated against advertised ranges/options/lengths; feedback updates state |
| Light/fan | Basic on/off; native command adapter supports bounded brightness/speed options where advertised |
| Cover/climate | Basic native position/stop or mode/temperature adapter; UI offers common controls |
| Button | Momentary commands such as the RHT AC On/Off IR replay buttons; no invented appliance state |
| Lock/valve/date/time/datetime/siren/media player/alarm/water heater/update/event | Discovery and read-only native state presentation; additional command adapters are still needed |
| ESP camera/video, Bluetooth/voice/proxy/custom native services | Not automatically implemented as full device-specific features; camera streaming uses the existing RTSP path |

This provides broad ESPHome integration, not a claim that every possible ESPHome firmware feature is fully controlled. Internal firmware entities are not exposed by ESPHome. Maintenance/reset/reboot/configuration controls and password text fields are excluded. Bridged child device IDs are included in stable identities and native commands. Rediscovery is authoritative: stale/filtered controls are not re-enabled by arbitrary state messages.

For the supplied RHT design, standard exposed readings include SHT40 temperature/humidity, supply voltage, supervisor health, firmware text and link/status sensors. AC On/Off are standard button entities; Learn buttons are configuration/disabled and remain excluded. The installed native API client also answers time requests; actual STM32 clock/OLED behavior and IR transmission still need a physical device test. LumiNext does not compile, flash or execute the custom firmware components.

The YAML supplied in chat was used to identify entity types. Its Wi-Fi passwords and encryption key were not copied into source or test fixtures. Enter credentials locally when connecting the actual device.

## Automations with sensors and buttons

Numeric/state triggers can use the discovered sensors. Read the stable entity IDs shown under each reading. `button.press` and `luminext.entity_command` are now available alongside relay actions. Example, after replacing the IDs with actual device IDs:

```yaml
alias: Cooling command when warm
mode: single
trigger:
  - platform: numeric_state
    entity_id: sensor.YOUR_DEVICE_TEMPERATURE_KEY
    above: 28
action:
  - service: button.press
    target:
      entity_id: button.YOUR_DEVICE_AC_ON_KEY
```

An IR send result means sent, not confirmed compressor operation. Generic native commands are also supported through the authenticated cloud queue for connected sites. Role checks, expiration and acknowledgement handling continue to apply.

Site exports now retain the entity catalog, friendly names and load profiles so sensor/button rules can round-trip. Current physical states, device credentials and energy observation history are not exported as site templates. Restored real entities remain unavailable until native discovery/state feedback. Full database backup remains the recovery path for measured observation history.

## Verification limits for this update

Automated tests cover batch YAML validation/secret preservation, unchanged camera state, rediscovery naming, RHT numeric/text/binary readings, missing/NaN states, IR-button semantics, command bounds/bridge IDs, template round-trip, energy gaps/manual time/overnight schedules and a slow-inference preview test. Browser QA uses an isolated simulation workspace; it covers rename, YAML add/apply, energy profile, person boxes, generic button handling and logout.

Cam 102's RTSP port was unreachable from the laptop during this update. The physical video smoothness and real detection overlay therefore still need checking on the camera LAN. The actual RHT device and its custom firmware were not connected during development. The full N95 12-camera benchmark, Linux containers, go2rtc/WebRTC and multi-day energy accuracy checks remain commissioning work.

## Live person count

Camera detail now displays People detected beside Live/Occupied and the area. It updates from current detections independently of the bounding-box visibility checkbox. Last-seen display boxes are excluded. Unavailable or stale detection data shows a dash. This is a current detection count, not a cumulative entry/exit counter or a guaranteed physical occupant count. Verified in the live browser with four current boxes and one last-seen box: count four. JavaScript syntax check passed.
