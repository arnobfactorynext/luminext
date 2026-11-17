# Ten-minute detection recheck — 1 November 2026

Observed 15:23:23–15:33:23 Asia/Dhaka. Camera and automation settings were unchanged during the window. The user identified disappearing/missing person boxes as the remaining symptom.

## Evidence

- 600 one-second persisted-state samples and 245 periodic one-second UI samples were collected. The UI samples cover parts of the ten-minute interval, not every frame.
- 30 UI samples contained no displayed person boxes. The longest continuously sampled zero-box run was approximately ten seconds. These are box-display observations, not a measured model false-negative rate; people also moved into and out of view.
- The room stayed occupied: no occupancy transitions and no automation runs occurred in the interval. No incorrect light-off was observed.
- Images were loaded in all 245 UI samples. Periodic screenshots showed advancing camera timestamps. A loaded image alone does not prove every video frame arrived on time.
- The worker log recorded one camera-pipeline restart at 15:25:10 (`live preview`, `RuntimeError`). Its initiating cause cannot be established from the existing log. A separate raw-score diagnostic stream attempt failed, so it did not provide a valid raw-versus-filtered accuracy comparison and could not be used to quantify model misses.
- Median sampled host CPU use was 11.1%. This is not a twelve-camera/N95 performance benchmark.

## Findings

1. The current retail detector remains inconsistent for seated, bent or partly hidden people in this room. The observed changes in visible boxes support this concern, but a frame-labelled evaluation would be needed to measure accuracy.
2. The current 0.85 starting confidence and three consecutive strong confirmations deliberately reject weak chair detections. They also delay or reject real people whose confidence is lower. An established track can use weaker matching observations for only three seconds; unmatched tracks expire after 1.5 seconds. The display-only hold lasts 800 milliseconds. Those bounds explain why uncertain boxes can disappear rather than remain indefinitely.
3. The publication code had an avoidable recovery failure: temporary controller HTTP/read errors propagated out of the preview or detection consumer and restarted the entire RTSP decode/track pipeline. This is a code finding, not proof that it caused the single logged restart above.

## Follow-up change

Transient transport failures, HTTP 5xx, 408 and 429 now drop the affected publication and retry with fresh queued data while preserving capture and tracking. Queue sizes remain bounded. Permanent errors such as invalid credentials still propagate. The change does not turn missed model detections into confirmed people and does not alter confidence, camera mappings or automation timing.

Regression tests cover preview recovery, server-error handling with credential rejection, and a detection-publication interruption recovering without spawning another capture process. The remaining pose/occlusion accuracy issue is not claimed solved by this recovery fix. A stronger detector/tracker should be evaluated against both occupied seats and the empty chair before replacement; previous alternate-model trials showed why occupied-room recall alone is insufficient.

Numeric diagnostics are under `data/diagnostics/recheck-10m-*`, excluded from Git. No camera video was saved.

Validation: full suite passed 46 tests in 25.61 seconds. The local camera worker was restarted to apply publication recovery. Camera confidence, confirmations, mappings, off-delay and automation YAML were unchanged in this recheck.

## Follow-up pose check

The user agreed to a seated/standing check. Forty-nine periodic UI samples were collected; two had no displayed boxes. Displayed confidence ranged from 64% to 99%, with five below-85% score observations. A screenshot showed the seated foreground person at approximately 74% and the standing person at approximately 88%. Occupancy did not change. The exact requested 30-second seated/15-second standing sequence was not independently timed, so this is supporting evidence rather than a frame-labelled controlled accuracy benchmark.

The current filter requires three consecutive starting observations at 85% or higher. Lower-confidence matches can only continue an established track, and weak-only continuation is capped at three seconds. Thus a real person whose pose keeps confidence lower can lose the displayed box and then fail to restart a track. The earlier empty-chair false positives mean lowering this threshold alone is not a validated solution. No settings or model were changed during this pose check.
