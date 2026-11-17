import asyncio

import pytest

from luminext.tracking import PersonContinuity
from test_system import runtime


def person(score=.9, box=(.2, .2, .4, .8)):
    return {'score': score, 'box': list(box)}


def test_weak_candidates_cannot_start_or_indefinitely_sustain_a_person():
    tracker = PersonContinuity()
    assert tracker.update([person(.4)], .5, at=0) == []
    strong = tracker.update([person()], .5, at=1)[0]
    weak = tracker.update([person(.4)], .5, at=2)[0]
    assert weak['track_id'] == strong['track_id'] and weak['score'] == .4
    assert tracker.update([person(.4)], .5, at=3)
    assert tracker.update([person(.4)], .5, at=4)
    assert tracker.update([person(.4)], .5, at=4.1) == []


def test_missing_or_unrelated_boxes_are_not_invented_as_detections():
    tracker = PersonContinuity()
    tracker.update([person()], .5, at=0)
    assert tracker.update([], .5, at=.2) == []
    assert tracker.update([person(.4, (.7, .2, .9, .8))], .5, at=.5) == []
    assert tracker.update([person(.4)], .5, at=2) == []


def test_single_confidence_spikes_cannot_confirm_a_person_track():
    tracker = PersonContinuity(confirmations=3)
    assert tracker.update([person(.9)], .8, at=0) == []
    assert tracker.update([person(.7)], .8, at=.2) == []
    assert tracker.update([person(.9)], .8, at=.4) == []
    assert tracker.update([person(.9)], .8, at=.6) == []
    assert tracker.update([person(.9)], .8, at=.8)
    tracker = PersonContinuity(confirmations=2)
    assert tracker.update([person(.9)], .8, at=0) == []
    assert tracker.update([], .8, at=.2) == []
    assert tracker.update([person(.9)], .8, at=.4) == []
    assert tracker.update([person(.9)], .8, at=.6)


def test_both_ssd_output_contracts_are_decoded_with_correct_labels_and_coordinates():
    import numpy as np
    from luminext.worker import decode_ssd
    legacy = np.array([[[[0, 1, .9, .2, .1, .6, .8], [-1, 0, 0, 0, 0, 0, 0]]]])
    assert decode_ssd([legacy], 1, .5, 864, 480)[0]['box'] == [.2, .1, .6, .8]
    boxes = np.array([[172.8, 48, 518.4, 384, .9], [0, 0, 100, 100, .99]])
    labels = np.array([1, 0])
    result = decode_ssd([labels, boxes], 1, .5, 864, 480)
    assert len(result) == 1
    assert result[0]['box'] == pytest.approx([.2, .1, .6, .8])
    with pytest.raises(ValueError): decode_ssd([boxes], 1, .5, 864, 480)
    with pytest.raises(ValueError): decode_ssd([np.zeros((1, 4))], 1, .5, 864, 480)


@pytest.mark.asyncio
async def test_stationary_frames_get_fresh_inference_not_cached_votes(monkeypatch):
    pytest.importorskip('cv2')
    import numpy as np
    from luminext.video import camera_loop
    width, height = 160, 120
    raw = np.zeros((height, width, 3), dtype=np.uint8).tobytes()
    class Reader:
        async def readexactly(self, size):
            await asyncio.sleep(.015)
            return raw
    class Process:
        stdout = Reader()
        returncode = None
        def kill(self): self.returncode = 0
        async def wait(self): return 0
    async def spawn(*args, **kwargs): return Process()
    monkeypatch.setattr('asyncio.create_subprocess_exec', spawn)
    done = asyncio.Event()
    calls = {'infer': 0, 'posted': 0}
    class Detector:
        device = 'test'
        def infer(self, frame, config):
            calls['infer'] += 1
            return [], 1
    class Client:
        async def post(self, path, **kwargs):
            if path.endswith('/detections'):
                calls['posted'] += 1
                if calls['posted'] >= 4: done.set()
            class Response:
                def raise_for_status(self): pass
            return Response()
    config = {'id': 'test', 'width': width, 'height': height, 'fps': 10, 'live_fps': 10,
              'rtsp_url': 'rtsp://test.invalid/live', 'masks': [], 'threshold': .5}
    task = asyncio.create_task(camera_loop(config, Client(), Detector(), asyncio.Semaphore(1), 'ffmpeg'))
    try:
        await asyncio.wait_for(done.wait(), 2)
        assert calls['infer'] == calls['posted'] == 4
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_camera_gap_restarts_empty_confirmation(runtime):
    rt = runtime
    camera = rt.store.get('camera', 'a')
    camera.update(off_delay=30, disappear_frames=2)
    await rt.occupancy(camera, [person()])
    assert rt.entities['occupancy.a']['state'] is True
    rt.samples['occupancy.a'].update(absent=100, empty_since=1)
    rt.camera_seen['a'] -= 20
    await rt.occupancy(camera, [])
    assert rt.entities['occupancy.a']['state'] is True
    assert rt.samples['occupancy.a']['absent'] == 1


@pytest.mark.asyncio
async def test_unchanged_real_relay_does_not_wait_for_a_duplicate_state(runtime):
    rt = runtime
    rt.entities['switch.r'].update(simulated=False, available=True, state=False, override=False, key=1)
    class Driver:
        def switch_command(self, *args, **kwargs):
            raise AssertionError('No redundant command should be sent')
    rt.drivers['d'] = Driver()
    result = await rt.command_switch('switch.r', 'turn_off', 'automation')
    assert result == {'result': 'already_in_state', 'state': False, 'sent': False}
    assert 'switch.r' not in rt.expected
    rt.entities['switch.r']['available'] = False
    with pytest.raises(ValueError, match='offline'):
        await rt.command_switch('switch.r', 'turn_off', 'automation')


@pytest.mark.asyncio
async def test_short_misses_do_not_clear_but_continuous_empty_does(runtime, monkeypatch):
    rt = runtime
    camera = rt.store.get('camera', 'a')
    camera.update(off_delay=30, disappear_frames=2)
    clock = [1000.]
    monkeypatch.setattr('luminext.runtime.now', lambda: clock[0])
    await rt.occupancy(camera, [person()])
    for second in range(1, 12):
        clock[0] = 1000. + second
        await rt.occupancy(camera, [])
        assert rt.entities['occupancy.a']['state'] is True
    clock[0] = 1012.
    await rt.occupancy(camera, [person()])
    for second in range(13, 44):
        clock[0] = 1000. + second
        await rt.occupancy(camera, [])
    assert rt.entities['occupancy.a']['state'] is False


@pytest.mark.asyncio
async def test_native_model_profile_passes_correct_person_label(tmp_path, monkeypatch):
    import json
    from luminext.runtime import Runtime
    from luminext.store import Store
    from luminext.supervisor import DetectionSupervisor
    monkeypatch.setenv('LUMINEXT_AUTOSTART_DETECTION', '1')
    monkeypatch.delenv('LUMINEXT_MODEL', raising=False)
    monkeypatch.delenv('LUMINEXT_PERSON_LABEL', raising=False)
    model = tmp_path/'candidate.xml'
    model.write_text('test fixture; never loaded')
    (tmp_path/'detection-settings.json').write_text(json.dumps({'model': str(model), 'person_label': 0}))
    store = Store(tmp_path)
    store.put('camera', {'id': 'a', 'enabled': True, 'simulated': False, 'rtsp_url': 'configured'})
    rt = Runtime(store)
    supervisor = DetectionSupervisor(rt)
    commands = []
    monkeypatch.setattr('luminext.worker.ffmpeg_path', lambda configured=None: 'ffmpeg')
    class Process: returncode = None
    async def spawn(*args, **kwargs):
        commands.append(args)
        return Process()
    monkeypatch.setattr('asyncio.create_subprocess_exec', spawn)
    try:
        await supervisor.check()
        command = commands[0]
        assert command[command.index('--person-label')+1] == '0'
        assert command[command.index('--model')+1] == str(model.resolve())
    finally:
        if supervisor.log: supervisor.log.close()
        store.db.close()
