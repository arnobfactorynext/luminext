'use strict';
// Device, camera and energy tools share the same authenticated API and state registry.
pages.energy = ['Energy & runtime','Understand what your controls save.'];
let showDetectionBoxes = true;
let cameraEditorDirty = false;
const detectionCache = new Map();
const originalRender = render;
const originalBind = bind;
const originalRenderPage = renderPage;
const originalDeviceDialog = deviceDialog;
const originalCameraDetail = cameraDetail;

renderPage = function(){
  if(page==='energy')return renderEnergy();
  if(page==='devices')return renderAllDevices();
  return originalRenderPage();
};
render = function(){
  // Reuse existing image elements during state refreshes rather than reopening RTSP previews.
  const previews = new Map($$('.camera-visual img').map(img=>[img.closest('[data-camera]')?.dataset.camera,img]));
  originalRender();
  $$('.camera-visual img').forEach(img=>{const old=previews.get(img.closest('[data-camera]')?.dataset.camera);if(old&&old.getAttribute('src')===img.getAttribute('src'))img.replaceWith(old);});
  installDetectionLayers();
};
bind = function(){
  originalBind();
  if($('#logout'))$('#logout').onclick=()=>guarded(async()=>{await api('/api/auth/logout',{});clearTimeout(refreshTimer);refreshTimer=null;me=null;state=null;currentAuto=null;cloudSite=null;detectionCache.clear();if(ws){ws.onclose=null;ws.close();}if($('#dialog').open)$('#dialog').close();renderAuth(false);});
  const navigation=$$('.sidebar nav').at(-1);
  if(navigation&&!$('#energy-nav')&&(!isRemote())&&(mode==='edge')){
    const button=document.createElement('button');button.id='energy-nav';button.title='Energy & runtime';button.setAttribute('aria-label','Energy & runtime');button.className=page==='energy'?'active':'';button.innerHTML=icon('bolt')+'<span>Energy & runtime</span>';button.onclick=()=>setPage('energy');navigation.append(button);
  }
  if(!$('#logout-top')){
    const button=document.createElement('button');button.id='logout-top';button.className='compact';button.innerHTML=icon('logout')+'Log out';button.onclick=()=>$('#logout')?.click();$('.topbar-right')?.append(button);
  }
  if(['cameras','devices'].includes(page)&&localAdmin()){
    const button=document.createElement('button');button.id='camera-config-editor';button.innerHTML=icon('code')+'Camera YAML';button.onclick=()=>guarded(openCameraConfig);$('.page-head .actions')?.prepend(button);
  }
  if(['cameras','overview'].includes(page)){
    const label=document.createElement('label');label.className='checkbox-line overlay-toggle';label.innerHTML=`<input id="boxes-visible" type="checkbox" ${showDetectionBoxes?'checked':''}>Person boxes`;label.onchange=()=>{showDetectionBoxes=$('input',label).checked;installDetectionLayers();};$('.page-head .actions')?.append(label);
  }
  if(localAdmin()){
    $$('.relay-mini [data-relay]').forEach(toggle=>{
      const button=document.createElement('button');button.className='map-button rename-button';button.setAttribute('aria-label','Rename '+(entities().find(e=>e.id===toggle.dataset.relay)?.name||'relay'));button.title='Edit friendly name';button.innerHTML=icon('edit');button.onclick=()=>renameEntity(toggle.dataset.relay);toggle.before(button);
    });
  }
  $$('[data-rename-entity]').forEach(button=>button.onclick=()=>renameEntity(button.dataset.renameEntity));
  $$('[data-control-entity]').forEach(button=>button.onclick=()=>controlEntity(button.dataset.controlEntity));
  $$('[data-energy-profile]').forEach(button=>button.onclick=()=>energyProfileDialog(button.dataset.energyProfile));
  $('#energy-days')?.addEventListener('change',()=>guarded(loadEnergyReport));
  if(page==='energy'&&!isRemote())guarded(loadEnergyReport);
  if(page==='devices'&&localAdmin()){
    const inventory=document.createElement('section');inventory.className='panel';inventory.style.marginTop='24px';inventory.innerHTML=`<div class="panel-head"><h2>Camera configuration</h2><small>Camera YAML or individual forms</small></div><div class="table-wrap"><table><thead><tr><th>Camera</th><th>Detection / preview target</th><th>Area</th><th></th></tr></thead><tbody>${cameras().map(c=>`<tr><td>${esc(c.name)}</td><td>${c.fps} / ${c.live_fps||10} fps</td><td>${esc(c.area)}</td><td><button class="compact" data-feature-camera-edit="${esc(c.id)}">Configure</button></td></tr>`).join('')}</tbody></table></div>`;$('#content .footer-note').before(inventory);$$('[data-feature-camera-edit]').forEach(button=>button.onclick=()=>cameraDialog(button.dataset.featureCameraEdit));
  }
};

deviceDialog = function(id){
  originalDeviceDialog(id);
  $('.dialog-head h2').textContent=id?'Configure ESPHome device':'Connect an ESPHome device';
  const note=$('.dialog-body .help');
  if(note)note.textContent='Enter the native API address and encryption key. Entities are discovered from the firmware: relays, temperature/humidity readings, binary sensors, buttons, lights and other supported types. Internal firmware entities are not exposed. Reset/restart and configuration controls are excluded.';
};

async function openCameraConfig(){
  const result=await api('/api/cameras/config');
  const sample=`\n  sewing_line:\n    name: Sewing line\n    area: Production\n    ffmpeg:\n      inputs:\n        - path: rtsp://USER:PASSWORD@192.168.0.102:554/stream\n          roles: [detect]\n    detect:\n      width: 640\n      height: 360\n      fps: 5\n    live_fps: 10\n    confirm_frames: 2\n    disappear_frames: 10\n    off_delay: 10\n`;
  modal('Camera configuration YAML',`<p class="form-note">Add or update multiple cameras here. Omitted cameras are kept. Stored URLs use <code>!secret camera_id_rtsp</code> and are never revealed. Paste a new RTSP URL only when adding/changing it. This is a supported camera subset, not the complete Frigate configuration language.</p><textarea id="camera-yaml" class="editor camera-yaml" aria-label="Camera configuration YAML" spellcheck="false"></textarea><details class="rule-guide"><summary>Example for a new camera</summary><pre class="trace">cameras:${esc(sample)}</pre></details><p id="camera-yaml-result" class="help" role="status"></p><div class="dialog-actions"><button id="validate-cameras">Validate</button><button id="save-cameras" class="primary">Validate & apply</button></div>`,dialog=>{
    dialog.addEventListener('close',()=>{cameraEditorDirty=false;},{once:true});
    $('#camera-yaml',dialog).value=result.yaml;
    $('#camera-yaml',dialog).oninput=()=>{cameraEditorDirty=true;};
    $('#validate-cameras',dialog).onclick=()=>guarded(async()=>{const response=await api('/api/cameras/config/validate',{yaml:$('#camera-yaml',dialog).value});$('#camera-yaml-result',dialog).textContent='Valid. Cameras: '+response.cameras.join(', ');});
    $('#save-cameras',dialog).onclick=()=>guarded(async()=>{const response=await api('/api/cameras/config',{yaml:$('#camera-yaml',dialog).value});$('#camera-yaml',dialog).value=response.yaml;cameraEditorDirty=false;$('#camera-yaml-result',dialog).textContent='Applied '+response.saved.length+' camera(s). Worker configuration reloads automatically.';await refresh();toast('Camera YAML applied');});
  });
}

function renameEntity(id){
  const entity=entities().find(e=>e.id===id);
  modal('Edit friendly name',`<form id="rename-form"><label>Friendly name<input name="name" maxlength="120" required value="${esc(entity.name)}"></label><p class="help" style="margin-top:15px">Entity ID stays <code>${esc(id)}</code>. Your automations and mappings keep working, and this name survives device reconnects.</p><div class="dialog-actions"><button class="primary" type="submit">Save name</button></div></form>`,dialog=>{
    $('#rename-form',dialog).onsubmit=event=>{event.preventDefault();guarded(async()=>{await api(`/api/entities/${encodeURIComponent(id)}`,{name:new FormData(event.target).get('name')},'PATCH');dialog.close();await refresh();toast('Friendly name saved');});};
  });
}

function valueLabel(entity){
  if(!entity.available)return 'Unavailable';
  const value=entity.state;
  if(typeof value==='number')return value.toFixed(Math.max(0,Math.min(4,entity.capabilities?.accuracy_decimals??1)))+(entity.unit?' '+esc(entity.unit):'');
  if(typeof value==='boolean')return value?'On':'Off';
  return esc(typeof value==='object'?JSON.stringify(value):value);
}

function renderAllDevices(){
  const counts=entities().filter(e=>e.device_id);
  return pageHead(localAdmin()?'<button class="primary" data-add-device>'+icon('plus')+'Add ESPHome device</button>':'')+simulationBanner()+filterRow()+`<div class="notice" style="margin-bottom:20px">Native ESPHome discovery is based on exposed entities, not the ESP chip model. Sensor readings are read-only. Buttons such as IR AC On/Off send commands; they do not confirm an appliance is running.</div><div class="device-grid">${matching(devices()).map(device=>{
    const items=counts.filter(e=>e.device_id===device.id);
    return `<section class="device-card"><div class="section-line"><h3>${esc(device.name)}</h3>${badge(device.simulated?'Simulation':items.some(e=>e.available)?'Online':'Offline',device.simulated?'amber':'neutral')}</div><p class="help">${esc(device.area)} · ${device.simulated?'Simulated device':esc(device.host)} · ${items.length} entities</p><div class="device-entities">${items.map(e=>`<div class="device-entity"><div class="entity-description"><strong>${esc(e.friendly_name||e.native_name||e.name)}</strong><small>${esc(e.type)}${e.device_class?' · '+esc(e.device_class):''}</small></div><strong class="entity-value">${valueLabel(e)}</strong><div class="entity-actions">${localAdmin()?`<button class="compact" data-rename-entity="${esc(e.id)}" aria-label="Rename ${esc(e.name)}">${icon('edit')}</button>`:''}${e.type==='switch'?`<button class="toggle ${e.state===true?'on':''}" data-relay="${esc(e.id)}" role="switch" aria-checked="${e.state===true}" aria-label="Toggle ${esc(e.name)}" ${!e.available?'disabled':''}></button>`:e.controllable?`<button class="compact" data-control-entity="${esc(e.id)}" ${!e.available||(isRemote()&&!cloudOnline)?'disabled':''}>${e.type==='button'?'Send':'Control'}</button>`:''}</div></div>`).join('')||'<p class="help" style="margin-top:15px">Awaiting native API discovery. Make sure the device is reachable and its encryption key is correct.</p>'}</div><div class="device-card-footer"><code>${esc(device.id)}</code>${localAdmin()?`<button data-device-edit="${esc(device.id)}">Edit device</button>`:''}</div></section>`;
  }).join('')||emptyInstall()}</div>`;
}

function controlEntity(id){
  const entity=entities().find(e=>e.id===id),type=entity.type,c=entity.capabilities||{};
  let fields='';
  if(type==='number')fields=`<label>Value (${esc(entity.unit||'')})<input name="value" type="number" min="${c.min_value}" max="${c.max_value}" step="${c.step||'any'}" value="${typeof entity.state==='number'?entity.state:c.min_value}" required></label>`;
  else if(type==='select')fields=`<label>Option<select name="value">${(c.options||[]).map(value=>`<option ${value===entity.state?'selected':''}>${esc(value)}</option>`).join('')}</select></label>`;
  else if(type==='text')fields=`<label>Text<input name="value" minlength="${c.min_length||0}" maxlength="${c.max_length||255}" value="${esc(entity.state)}"></label>`;
  else if(['light','fan'].includes(type))fields='<label>State<select name="state"><option value="true">On</option><option value="false">Off</option></select></label>'+(type==='fan'&&c.supported_speed_count?`<label>Speed level<input name="speed_level" type="number" min="1" max="${c.supported_speed_count}" value="1"></label>`:'');
  else if(type==='cover')fields='<label>Position<select name="position"><option value="1">Fully open</option><option value="0">Fully closed</option></select></label>';
  else if(type==='climate')fields=`<label>Mode<select name="mode">${(c.supported_modes||[]).map(mode=>`<option>${esc(mode)}</option>`).join('')}</select></label><label>Target temperature<input name="target_temperature" type="number" min="${c.visual_min_temperature??5}" max="${c.visual_max_temperature??35}" step="0.5" value="${entity.state_attributes?.target_temperature||24}"></label>`;
  else if(type!=='button'){toast('This entity is read-only',true);return;}
  modal(entity.name,`<form id="entity-control" class="form-stack">${fields}<p class="form-note">${type==='button'?'This sends a momentary button command, such as an IR code. It cannot confirm the physical appliance state.':'The displayed state updates from ESPHome feedback; sending a command is not an optimistic state change.'}</p><div class="dialog-actions"><button class="primary" type="submit">Send command</button></div></form>`,dialog=>{
    $('#entity-control',dialog).onsubmit=event=>{event.preventDefault();guarded(async()=>{const data=Object.fromEntries(new FormData(event.target));for(const key of ['value','position','speed_level','target_temperature'])if(key in data&&!(key==='value'&&['select','text'].includes(type)))data[key]=Number(data[key]);if('state'in data)data.state=data.state==='true';const result=isRemote()?await api(`/api/cloud/sites/${cloudSite}/commands`,{kind:'entity',data:{entity_id:id,options:data}}):await api(`/api/entities/${encodeURIComponent(id)}/command`,data);toast(result.note||(isRemote()?'Command queued; check the site acknowledgement.':'Command sent'));dialog.close();});};
  });
}

function renderEnergy(){
  return pageHead()+`<div class="notice" style="margin-bottom:20px">Use each load's rated watts and the hours it would normally stay on without occupancy control. Savings are an estimate against that baseline, not a utility measurement. Unknown/offline periods and simulations are excluded. Manual off time is included and shown separately.</div><div class="filter-row"><label>Report window<select id="energy-days"><option value="1">Last 24 hours</option><option value="7">Last 7 days</option><option value="30" selected>Last 30 days</option><option value="90">Last 90 days</option></select></label></div><div class="panel"><div class="panel-head"><h2>Observed loads</h2><small>Starts when a profile is configured · no invented history</small></div><div id="energy-summary" class="panel-body"><p class="help">Loading observed runtime…</p></div></div><div class="section-line" style="margin-top:25px"><h2>Load configuration</h2></div><div class="panel table-wrap"><table><thead><tr><th>Relay / load</th><th>Rated load</th><th>Baseline schedule</th><th>Tariff</th><th></th></tr></thead><tbody>${switches().map(e=>`<tr><td><strong>${esc(e.name)}</strong>${e.simulated?' '+badge('Simulation excluded','amber'):''}</td><td>${e.energy_profile?e.energy_profile.watts+' W':'Not configured'}</td><td>${e.energy_profile?esc(e.energy_profile.start+'–'+e.energy_profile.end)+' · '+e.energy_profile.weekdays.length+' days/week':'—'}</td><td>${e.energy_profile?.tariff!=null?e.energy_profile.tariff+' '+esc(e.energy_profile.currency)+'/kWh':'Not entered'}</td><td>${localAdmin()?`<button class="compact" data-energy-profile="${esc(e.id)}">Configure</button>`:''}</td></tr>`).join('')||'<tr><td colspan="5">Add a relay first.</td></tr>'}</tbody></table></div><p class="help" style="margin-top:18px">Estimated kWh avoided = rated watts ÷ 1000 × confirmed off-hours within the baseline schedule. Estimated cost avoided = avoided kWh × your tariff. Use an ESPHome power/energy meter for actual electrical measurements; its W/kWh readings appear under Devices. Positive net kWh delta means below baseline; negative means above baseline, including running outside the schedule. Scheduled-off cost avoided is not a net electricity-bill saving. AC IR button commands alone are not reliable energy measurements.</p>`;
}

async function loadEnergyReport(){
  const response=await api('/api/energy?days='+($('#energy-days')?.value||30));
  if(page!=='energy'||!$('#energy-summary'))return;
  $('#energy-summary').innerHTML=response.loads.length?`<div class="table-wrap"><table><thead><tr><th>Load</th><th>Known runtime</th><th>Avoided runtime</th><th>Manual share</th><th>Estimated baseline / actual</th><th>Estimated net kWh delta</th><th>Scheduled-off cost avoided</th></tr></thead><tbody>${response.loads.map(load=>`<tr><td>${esc(load.name)}</td><td>${load.on_hours.toFixed(2)} h on / ${load.observed_hours.toFixed(2)} h observed</td><td>${load.avoided_hours.toFixed(2)} h</td><td>${load.manual_avoided_hours.toFixed(2)} h</td><td>${load.estimated_baseline_kwh.toFixed(3)} / ${load.estimated_actual_kwh.toFixed(3)} kWh</td><td>${load.estimated_net_kwh_delta.toFixed(3)}</td><td>${load.estimated_cost_avoided==null?'Set a tariff':load.estimated_cost_avoided.toFixed(2)+' '+esc(load.currency)}</td></tr>`).join('')}</tbody></table></div>`:'<p class="help">No eligible observation history yet. Configure a real relay’s rated load and baseline schedule to start tracking. Downtime and simulated devices never create savings.</p>';
}

function energyProfileDialog(id){
  const e=entities().find(item=>item.id===id),p=e.energy_profile||{watts:'',tariff:'',currency:'BDT',start:'08:00',end:'18:00',weekdays:[0,1,2,3,4],timezone:state.settings.timezone,enabled:true};
  modal('Load profile · '+e.name,`<form id="energy-profile" class="form-stack"><div class="field-grid"><label>Rated load (watts)<input name="watts" type="number" min="0.01" max="1000000" step="any" required value="${p.watts}" placeholder="Total watts controlled by this relay"></label><label>Tariff per kWh (optional)<input name="tariff" type="number" min="0" step="any" value="${p.tariff??''}"></label><label>Currency<input name="currency" required maxlength="8" pattern="[A-Z]+" value="${esc(p.currency)}"></label><label>Timezone<input name="timezone" required value="${esc(p.timezone)}"></label><label>Baseline starts<input name="start" type="time" required value="${p.start}"></label><label>Baseline ends<input name="end" type="time" required value="${p.end}"></label></div><label>Baseline weekdays</label><div class="weekday-options">${['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map((day,i)=>`<label class="checkbox-line"><input type="checkbox" name="weekdays" value="${i}" ${p.weekdays.includes(i)?'checked':''}>${day}</label>`).join('')}</div><label class="checkbox-line"><input type="checkbox" name="enabled" ${p.enabled?'checked':''}>Track this profile</label><p class="form-note">Choose the schedule this load would normally stay on without these controls. Equal start/end means 24 hours. Earlier end means an overnight shift. Use combined rated watts for all fixtures on this relay. A new profile does not recalculate past observations.</p><div class="dialog-actions"><button class="primary" type="submit">Save load profile</button></div></form>`,dialog=>{
    $('#energy-profile',dialog).onsubmit=event=>{event.preventDefault();guarded(async()=>{const form=new FormData(event.target);const payload=Object.fromEntries(form);payload.watts=Number(payload.watts);payload.tariff=payload.tariff===''?null:Number(payload.tariff);payload.weekdays=form.getAll('weekdays').map(Number);payload.enabled=form.has('enabled');await api(`/api/energy/${encodeURIComponent(id)}`,payload,'PUT');dialog.close();await refresh();toast('Load profile saved; tracking begins from known relay observations');});};
  });
}

const overlayTracks = new Map();
function updateDetections(message){
  const tracks=overlayTracks.get(message.camera_id)||new Map();
  for(const d of message.detections){if(d.track_id)tracks.set(d.track_id,{d,seen:Date.now()});}
  for(const [key,track] of tracks){if(Date.now()-track.seen>800)tracks.delete(key);}
  overlayTracks.set(message.camera_id,tracks);
  detectionCache.set(message.camera_id,{...message,received:Date.now()});
  paintDetections(message.camera_id);
}
function paintDetections(id){
  const item=detectionCache.get(id);
  const fresh=item&&Date.now()-item.received<1500;
  const available=entities().find(e=>e.camera_id===id&&e.type==='occupancy_sensor'&&!e.zone_id)?.available;
  $$('[data-person-count]').filter(element=>element.dataset.personCount===id).forEach(element=>{
    const text=`People detected: ${fresh&&available?item.detections.length:'—'}`;
    if(element.textContent!==text)element.textContent=text;
  });
  const alive=showDetectionBoxes&&fresh;
  const boxes=alive?[...item.detections]:[];
  const tracks=overlayTracks.get(id);
  if(tracks){
    const current=new Set(boxes.map(d=>d.track_id));
    for(const [key,track] of tracks){
      if(Date.now()-track.seen>800){tracks.delete(key);continue;}
      if(alive&&!current.has(key))boxes.push({...track.d,held:true});
    }
  }
  $$('[data-detection-camera]').filter(layer=>layer.dataset.detectionCamera===id).forEach(layer=>{
    layer.innerHTML=boxes.map(d=>{const [x1,y1,x2,y2]=d.box;return `<div class="person-box ${d.held?'last-seen':''}" style="left:${x1*100}%;top:${y1*100}%;width:${(x2-x1)*100}%;height:${(y2-y1)*100}%"><span>${d.held?'Last seen':`Person ${Math.round(d.score*100)}%`}</span></div>`;}).join('');
  });
}
function installDetectionLayers(){
  $$('.camera-visual[data-camera]').forEach(visual=>addDetectionLayer(visual,visual.dataset.camera));
  for(const id of detectionCache.keys())paintDetections(id);
}
function addDetectionLayer(visual,id){
  if(!visual||$('[data-detection-camera]',visual))return;
  const camera=cameras().find(c=>c.id===id);
  if(!camera)return;
  visual.style.aspectRatio=`${camera.width}/${camera.height}`;
  const layer=document.createElement('div');layer.className='detection-layer';layer.dataset.detectionCamera=id;layer.setAttribute('aria-label','Detected people');visual.append(layer);
  if(!isRemote())guarded(async()=>{const response=await api(`/api/cameras/${encodeURIComponent(id)}/detections`);updateDetections(response);});
}
cameraDetail = function(id){
  originalCameraDetail(id);
  const count=document.createElement('span');count.className='badge neutral';count.dataset.personCount=id;count.textContent='People detected: —';count.title='People detected in the latest camera update.';$('.dialog-body .pill-row').append(count);
  addDetectionLayer($('.live-large'),id);
  paintDetections(id);
  const label=document.createElement('label');label.className='checkbox-line overlay-toggle';label.style.marginTop='15px';label.innerHTML=`<input type="checkbox" ${showDetectionBoxes?'checked':''}>Show person bounding boxes`;label.onchange=()=>{showDetectionBoxes=$('input',label).checked;paintDetections(id);};$('.dialog-body').append(label);
  if(localAdmin()){const edit=document.createElement('button');edit.className='compact';edit.style.marginTop='15px';edit.textContent='Configure camera';edit.onclick=()=>{$('#dialog').close();cameraDialog(id);};$('.dialog-body').append(edit);}
};
setInterval(()=>{for(const id of detectionCache.keys())paintDetections(id);},500);
setInterval(()=>{if(me&&page==='energy'&&!$('#dialog').open)guarded(loadEnergyReport);},30000);
window.addEventListener('beforeunload',event=>{if(cameraEditorDirty){event.preventDefault();event.returnValue='';}});
if(me)render();
