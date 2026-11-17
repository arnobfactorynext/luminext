# Detection investigation — 1 November 2026

Current running configuration: original retail detector, starting confidence **0.85**, **3** consecutive strong per-person confirmations, and **30-second** empty delay. Earlier alternate-model settings below describe rejected trials. The final controlled empty-room/re-entry test passed: occupancy cleared, all four target relays reported OFF, and occupancy returned on re-entry.

## Observation

A 30-minute baseline ran from 13:41:55 to 14:11:55 Asia/Dhaka before changing the running settings. It combined periodic live-view screenshots, 1,225 one-second UI samples and 1,798 persisted-state samples. No camera video was recorded. Numeric diagnostics remain local under `data/diagnostics/`, excluded from Git.

There were 73 UI samples with no boxes; the longest continuously sampled empty-box run was approximately 11 seconds. These are not frame-by-frame accuracy measurements or person-count ground truth. Three brief clear-to-occupied cycles occurred (clear intervals approximately 2.0, 3.2 and 1.0 seconds). These were not independently established to be false empty-room decisions. The user subsequently clarified that the reported symptom was disappearing person boxes, not incorrect room-clear/light-off behavior. Persisted availability was true in all samples; short reconnects could still occur between samples. The old off-delay was five seconds.

The retail detector often missed a visibly seated foreground person. The worker also replayed cached results when motion was low, counting repeated results as separate confirmation frames. Its stationary inference scan was only once per second.

## Changes (v0.2.1)

- Every published result now represents a new inference, including stationary scenes. The bounded latest-frame queue and independent preview remain.
- A weaker candidate (70% of the starting threshold) can continue a recent overlapping strong match, but cannot start a person track. Weak-only continuation expires after three seconds; a track expires after 1.5 seconds without a matching observation. Scores remain the actual model scores. Missing observations are not invented.
- Boxes can remain visible for up to 800 milliseconds as dashed **Last seen** boxes. This is display-only and never establishes occupancy.
- Camera gaps reset empty confirmation timers/counters. Disconnected time cannot establish an empty room.
- Business Room off-delay changed from 5 to **30 seconds**. Detection/preview targets remain 5/10 fps; disappearance confirmation remains 10 fresh inference frames.
- Compared official OpenVINO `person-detection-0202` and `person-detection-retail-0013` on identical live frames, without saving frames. The initial 116-frame comparison averaged 17.1 ms for retail and 28.6 ms for general on this laptop CPU. Different box counts alone do not establish accuracy. A second short comparison localized improved seated-person coverage and a weaker false box on a bag. Temporarily selected the general model with **0.75** starting confidence. The later controlled empty-room test exposed an intermittent chair false positive (up to 92% in sampled UI readings), preventing a continuous 30-second clear interval. This trial model was rejected and the original retail model restored, initially at 0.50 starting confidence, then tuned to **0.85 with three per-person confirmations** after the repeat test also exposed chair false positives on retail. Fresh-inference and bounded-continuity fixes remain. Improved seated-person recall alone was insufficient to accept the alternate model.
- Native model selection and class label are paired in `data/detection-settings.json`. General uses **person label 0**; retail uses label 1. Worker logs include timestamps; metrics include model name, fresh inference count and detection pipeline time.

## Optional trial model (not the final selection)

Download the model:

```powershell
.\.venv\Scripts\python.exe tools/download_model.py --model person-detection-0202
```

The downloader verifies official SHA-384 checksums before writing this model. Create `data/detection-settings.json`:

```json
{"model":"models/person-detection-0202.xml","person_label":0}
```

The final local profile is `{"model":"models/person-detection-retail-0013.xml","person_label":1}`. The 0202 example above is retained only to reproduce the rejected comparison, not as the recommended final setting. Restart the native controller after changing a profile. `LUMINEXT_MODEL` and `LUMINEXT_PERSON_LABEL` environment variables override this file. A separate Docker worker needs explicit arguments `--model /models/person-detection-0202.xml --person-label 0`; the native supervisor profile does not configure a separately managed worker. Model files and site data are excluded from Git. Installations without a profile continue using the retail default.

To revert, set the profile to `{"model":"models/person-detection-retail-0013.xml","person_label":1}`, restore the desired confidence in Camera configuration, and restart. Keep the longer empty delay unless controlled testing supports shortening it.

## Verification and limits

Regression checks cover fresh stationary inference, weak-candidate rejection, bounded continuation, interruption recovery, delayed clear, and native model/label pairing. Existing data was backed up and passed SQLite integrity checking before the update.

Live post-update observation checks boxes and room state while people remain visible. A controlled completely empty room and re-entry check is still needed if everyone cannot leave during this session. Simulated continuous emptiness clears after the configured delay. A model can miss fully occluded people or produce false positives; a 30-second delay cannot prove physical absence. Twelve-camera throughput on the N95 was not benchmarked here. Continuous inference does more work than the previous motion-gated path. No direct manual relay command was sent during this investigation.

References: [retail coverage](https://docs.openvino.ai/2023.3/omz_models_model_person_detection_retail_0013.html), [general model and class label](https://github.com/openvinotoolkit/open_model_zoo/blob/master/models/intel/person-detection-0202/README.md), [official checksums](https://github.com/openvinotoolkit/open_model_zoo/blob/master/models/intel/person-detection-0202/model.yml).

Final verification: 40 tests passed (25.80 seconds); JavaScript syntax and whitespace checks passed. The model download/checksum smoke check succeeded. After the final camera save, the first 192 seconds contained only initial unknown-to-occupied confirmation and no clear transition; later live windows also remained occupied. People stayed in the room, so real empty-room/re-entry verification remains pending.

Follow-up: during the first controlled empty-room test, intermittent boxes on the right-hand empty chair restarted the timer. No occupancy-clear transition or off-command trace occurred. Original retail model and 0.50 threshold were restored before requesting a repeat test. A trial 0203 comparison was not adopted. Its boxes/labels output contract is now handled and unit tested, but it was not validated for deployment.

Second follow-up: the original model also classified the empty right-hand chair as a person at the 0.50 threshold. Sampled empty-chair scores included occasional spikes up to 87%. Added per-track consecutive strong confirmation (missing or weak frames reset a pending streak). Final site settings: original retail model, confidence 0.85, confirm_frames 3, off_delay 30 seconds. No exclusion mask was added over a usable seating location. The full suite passed 42 tests; the final pending-streak adjustment also passed the eight targeted detection tests. A further controlled empty/re-entry check was requested to validate these final settings.

Final controlled test completed: occupancy became clear 33.87 seconds after test start, all four target relays reported OFF, and all four manual overrides released. Occupancy returned 87.99 seconds after test start when people re-entered. The chair did not prevent clearing with the final settings. The old automation trace reported a five-second confirmation timeout because one relay was already OFF; fixed command handling now returns explicit already_in_state without waiting for an unchanged ESPHome state event. A regression test verifies no redundant command is sent and offline devices are still rejected. The physical state transitions were verified before this reporting fix; the reporting fix was verified by automated tests.
