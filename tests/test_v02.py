import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from aioesphomeapi import model as m

from luminext.app import CameraInput
from luminext.camera_config import export_cameras,parse_cameras
from luminext.energy import EnergyProfile,EnergyTracker,scheduled_seconds
from luminext.esphome import describe,decode,validate_command
from luminext.runtime import Runtime
from luminext.store import Store
from luminext.video import latest_put,publish_latest
from test_system import client,add_simulated
from fastapi.testclient import TestClient
from luminext.app import create_app


def test_friendly_name_survives_discovery_and_device_rename(client):
    add_simulated(client)
    id='switch.wall_a_1'
    client.patch('/api/entities/'+id,json={'name':'Packing table lights'}).raise_for_status()
    rt=client.app.state.rt
    rt.add_entity({'id':id,'type':'switch','name':'Rediscovered relay name','device_id':'wall_a','simulated':True})
    assert rt.entities[id]['name']=='Packing table lights'
    client.post('/api/devices',json={'id':'wall_a','name':'Friendly wall panel','simulated':True}).raise_for_status()
    saved=rt.store.get('entity',id)
    assert saved['name']=='Packing table lights' and saved['occupancy_sources']==['occupancy.cam_a']
    assert client.patch('/api/entities/'+id,json={'name':'   '}).status_code==422


def test_camera_yaml_redaction_merge_and_atomic_validation(client):
    client.post('/api/cameras',json={'id':'existing','name':'Existing','rtsp_url':'rtsp://private:secret@192.168.0.1/live'}).raise_for_status()
    exported=client.get('/api/cameras/config').json()['yaml']
    assert '!secret' in exported and 'private' not in exported and '192.168' not in exported
    encrypted=client.app.state.rt.store.get('camera','existing')['rtsp_url']
    client.post('/api/cameras/config',json={'yaml':exported}).raise_for_status()
    assert client.app.state.rt.store.get('camera','existing')['rtsp_url']==encrypted
    text='''cameras:
  new_camera:
    name: New camera
    ffmpeg:
      inputs:
        - path: rtsp://user:secret@192.168.0.2/live
          roles: [detect]
    detect:
      fps: 5
      width: 640
      height: 360
    live_fps: 12
  invalid:
    width: 7
'''
    assert client.post('/api/cameras/config',json={'yaml':text}).status_code==422
    assert client.app.state.rt.store.get('camera','new_camera') is None
    text=text[:text.index('  invalid:')]
    response=client.post('/api/cameras/config',json={'yaml':text})
    assert response.status_code==200
    assert 'user:secret' not in response.text
    assert len(client.get('/api/state').json()['cameras'])==2


def test_unchanged_camera_yaml_does_not_reset_occupancy(client):
    add_simulated(client)
    client.post('/api/simulation/cam_a',json={'detections':[{'box':[.2,.2,.5,.8],'score':.9}]}).raise_for_status()
    yaml=client.get('/api/cameras/config').json()['yaml']
    client.post('/api/cameras/config',json={'yaml':yaml}).raise_for_status()
    assert client.app.state.rt.entities['occupancy.cam_a']['state'] is True


def test_yaml_rejects_duplicate_unknown_keys_and_missing_secret():
    for text in ['cameras:\n  a: {}\n  a: {}','cameras:\n  a:\n    record: true','cameras:\n  a:\n    rtsp_url: !secret a_rtsp','cameras:\n  a:\n    rtsp_url: 42']:
        with pytest.raises(ValueError):parse_cameras(text,{},CameraInput)


def test_export_import_preserves_sensor_rules_names_and_load_profiles(client,tmp_path):
    client.post('/api/devices',json={'id':'rht','name':'RHT','simulated':True,'relay_count':1}).raise_for_status()
    rt=client.app.state.rt;device=rt.store.get('device','rht')
    for info in (m.SensorInfo(key=1,name='Temperature'),m.ButtonInfo(key=2,name='AC On')):
        entity=describe(device,info);entity['simulated']=True;rt.add_entity(entity)
    client.patch('/api/entities/sensor.rht_1',json={'name':'Office temperature'}).raise_for_status()
    client.put('/api/energy/switch.rht_1',json={'watts':100,'tariff':10}).raise_for_status()
    yaml='alias: Cooling\ntrigger:\n  - platform: numeric_state\n    entity_id: sensor.rht_1\n    above: 28\naction:\n  - service: button.press\n    target:\n      entity_id: button.rht_2\n'
    client.post('/api/automations',json={'yaml':yaml}).raise_for_status()
    exported=client.get('/api/config/export').json()
    with TestClient(create_app(tmp_path/'restored'),headers={'X-LumiNext':'1'}) as other:
        credentials={'username':'installer','password':'test-only-long-password'}
        other.post('/api/auth/setup',json=credentials);other.post('/api/auth/login',json=credentials)
        other.post('/api/config/import',json=exported).raise_for_status()
        entities=other.app.state.rt.entities
        assert entities['sensor.rht_1']['name']=='Office temperature'
        assert entities['switch.rht_1']['energy_profile']['watts']==100
        assert not entities['button.rht_2']['available'] # No fictional live state on import.
        assert len(other.get('/api/state').json()['automations'])==1


@pytest.mark.asyncio
async def test_rht_discovery_states_and_safe_button_control(tmp_path):
    store=Store(tmp_path);rt=Runtime(store)
    device={'id':'rht','name':'Room RHT','area':'Office'}
    infos=[m.SensorInfo(key=1,name='Temperature',unit_of_measurement='°C',device_class='temperature',accuracy_decimals=1),m.SensorInfo(key=2,name='Humidity',unit_of_measurement='%',device_class='humidity'),m.SensorInfo(key=3,name='5V Input',unit_of_measurement='V'),m.TextSensorInfo(key=4,name='STM32 Firmware'),m.BinarySensorInfo(key=5,name='STM32 Link'),m.ButtonInfo(key=6,name='AC On')]
    try:
        for info in infos:rt.add_entity(describe(device,info))
        assert describe(device,m.ButtonInfo(key=7,name='Learn AC On',entity_category=m.EntityCategory.CONFIG,disabled_by_default=True)) is None
        assert describe(device,m.SwitchInfo(key=8,name='Factory Reset')) is None
        assert describe(device,m.SensorInfo(key=9,name='Uptime',entity_category=m.EntityCategory.DIAGNOSTIC))['type']=='sensor'
        await rt.generic_device_state('rht',m.SensorState(key=1,state=26.5))
        await rt.generic_device_state('rht',m.SensorState(key=2,state=65))
        await rt.generic_device_state('rht',m.TextSensorState(key=4,state='0.5.0'))
        await rt.generic_device_state('rht',m.BinarySensorState(key=5,state=True))
        assert rt.entities['sensor.rht_1']['state']==26.5
        assert rt.entities['sensor.rht_1']['unit']=='°C'
        assert rt.entities['text_sensor.rht_4']['state']=='0.5.0'
        await rt.generic_device_state('rht',m.SensorState(key=1,state=float('nan')))
        assert rt.entities['sensor.rht_1']['state']=='unknown' and not rt.entities['sensor.rht_1']['available']
        calls=[]
        class Driver:
            def button_command(self,**data):calls.append(data)
        rt.drivers['rht']=Driver()
        await rt.set_state('button.rht_6','ready',available=True)
        result=await rt.entity_command('button.rht_6',{},'operator')
        assert result['result']=='sent' and calls[0]['key']==6
        assert rt.entities['button.rht_6']['state']=='ready' # No fictional AC state.
    finally:store.db.close()


def test_controls_validate_capabilities_and_bridge_entity_ids():
    device={'id':'node','name':'Node','area':'Office'}
    entity=describe(device,m.NumberInfo(key=42,device_id=3,name='Setpoint',min_value=10,max_value=30,step=.5))
    assert entity['id']=='number.node_3_42'
    assert validate_command(entity,{'value':24.5})[1]['device_id']==3
    for value in [31,24.2,float('nan')]:
        with pytest.raises(ValueError):validate_command(entity,{'value':value})
    entity=describe(device,m.SelectInfo(key=5,name='Mode',options=['auto','manual']))
    with pytest.raises(ValueError):validate_command(entity,{'value':'invalid'})
    assert validate_command(entity,{'value':'auto'})[1]['state']=='auto'
    with pytest.raises(ValueError):validate_command({'type':'sensor'}, {'value':1})


def at(hour,minute=0):
    return datetime(2026,11,2,hour,minute,tzinfo=ZoneInfo('Asia/Dhaka')).timestamp()


def test_energy_excludes_gaps_and_simulation_preserves_profile_history(tmp_path):
    store=Store(tmp_path);tracker=EnergyTracker(store)
    profile=EnergyProfile(watts=100,tariff=10,weekdays=list(range(7))).model_dump()
    entity={'id':'switch.a','type':'switch','name':'Lights','state':True,'available':True,'energy_profile':profile}
    try:
        tracker.observe(entity,at(8))
        entity.update(state=False,override=True);tracker.observe(entity,at(9))
        entity.update(state='unknown',available=False);tracker.observe(entity,at(10))
        entity.update(state=True,available=True,override=False);tracker.observe(entity,at(12))
        tracker.observe(entity,at(13),force=True)
        report=tracker.report([entity],days=1,at=at(13))['loads'][0]
        assert report['observed_hours']==3
        assert report['on_hours']==2
        assert report['avoided_hours']==1
        assert report['manual_avoided_hours']==1
        assert report['estimated_avoided_kwh']==pytest.approx(.1)
        assert report['estimated_cost_avoided']==pytest.approx(1)
        simulated={**entity,'id':'switch.demo','simulated':True}
        tracker.observe(simulated,at(8));assert 'switch.demo' not in tracker.active
        entity['energy_profile']={**profile,'watts':200,'tariff':None};entity['state']=False
        tracker.observe(entity,at(13))
        report=tracker.report([entity],days=1,at=at(14))['loads'][0]
        assert report['estimated_avoided_kwh']==pytest.approx(.3)
        assert report['estimated_cost_avoided'] is None
    finally:store.db.close()


def test_overnight_baseline_and_time_outside_working_hours():
    profile=EnergyProfile(watts=50,start='22:00',end='06:00',weekdays=[6]).model_dump()
    zone=ZoneInfo('Asia/Dhaka')
    start=datetime(2026,11,1,23,tzinfo=zone).timestamp();end=datetime(2026,11,2,1,tzinfo=zone).timestamp()
    assert scheduled_seconds(start,end,profile)==7200
    assert scheduled_seconds(at(10),at(12),profile)==0


def test_manual_energy_attribution_survives_override_release_and_net_delta_can_be_negative(tmp_path):
    store=Store(tmp_path);tracker=EnergyTracker(store)
    entity={'id':'switch.a','type':'switch','name':'Light','available':True,'state':False,'override':True,'control_source':'manual','energy_profile':EnergyProfile(watts=100,weekdays=list(range(7))).model_dump()}
    try:
        tracker.observe(entity,at(8))
        entity['override']=False;tracker.observe(entity,at(9))
        assert tracker.report([entity],1,at=at(10))['loads'][0]['manual_avoided_hours']==2
        entity.update(state=True,control_source='automation');tracker.observe(entity,at(10))
        report=tracker.report([entity],1,at=at(23))['loads'][0]
        assert report['estimated_net_kwh_delta']<0
        assert report['estimated_cost_avoided'] is None
    finally:store.db.close()


def test_latest_frame_queue_drops_backlog():
    queue=asyncio.Queue(maxsize=1)
    latest_put(queue,'old');latest_put(queue,'new')
    assert queue.qsize()==1 and queue.get_nowait()=='new'


@pytest.mark.asyncio
async def test_preview_publishes_without_waiting_for_detector():
    pytest.importorskip('cv2')
    import numpy as np
    queue=asyncio.Queue(maxsize=1);telemetry={'preview_frames':0,'preview_pipeline_ms':0}
    calls=[]
    class Client:
        async def post(self,url,content,headers):
            calls.append((url,content));latest_put(queue,None)
            class Response:
                def raise_for_status(self):pass
            return Response()
    import time
    latest_put(queue,(time.monotonic(),np.zeros((120,160,3),dtype=np.uint8).tobytes()))
    await asyncio.wait_for(publish_latest(queue,Client(),'camera',160,120,telemetry),2)
    assert len(calls)==1 and calls[0][1].startswith(b'\xff\xd8') and telemetry['preview_frames']==1


def test_detection_metadata_is_authenticated_and_expires(client):
    add_simulated(client)
    client.post('/api/simulation/cam_a',json={'detections':[{'box':[.2,.2,.5,.8],'score':.9}]}).raise_for_status()
    result=client.get('/api/cameras/cam_a/detections').json()
    assert result['detections'][0]['score']==.9
    client.app.state.rt.detections['cam_a']['observed_at']-=3
    assert client.get('/api/cameras/cam_a/detections').json()['detections']==[]
    client.post('/api/auth/logout',json={})
    assert client.get('/api/cameras/cam_a/detections').status_code==401


@pytest.mark.asyncio
async def test_live_pipeline_keeps_preview_moving_during_slow_inference(monkeypatch):
    pytest.importorskip('cv2')
    import numpy as np,time
    from luminext.video import camera_loop
    width,height=160,120
    raw=np.zeros((height,width,3),dtype=np.uint8).tobytes()
    class Reader:
        async def readexactly(self,size):
            await asyncio.sleep(.02)
            return raw
    class Process:
        stdout=Reader();returncode=None
        def kill(self):self.returncode=0
        async def wait(self):return 0
    async def spawn(*args,**kwargs):return Process()
    monkeypatch.setattr('asyncio.create_subprocess_exec',spawn)
    ready=asyncio.Event();calls={'frames':0,'detections':0}
    class Client:
        async def post(self,path,**kwargs):
            if path.endswith('/frame'):
                calls['frames']+=1
                if calls['frames']>=5:ready.set()
            else:calls['detections']+=1
            class Response:
                def raise_for_status(self):pass
            return Response()
    class Detector:
        device='test'
        def infer(self,frame,config):
            time.sleep(.8)
            return [],800
    config={'id':'test','width':width,'height':height,'fps':5,'live_fps':10,'rtsp_url':'rtsp://test.invalid/live','masks':[]}
    task=asyncio.create_task(camera_loop(config,Client(),Detector(),asyncio.Semaphore(1),'ffmpeg'))
    try:
        await asyncio.wait_for(ready.wait(),2)
        assert calls['detections']==0 # Preview did not wait for the 800ms inference.
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)
