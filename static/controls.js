'use strict';
let livePrinter=null,liveRequest=null,liveTimer=null,liveUrl=null,liveGeneration=0;
function stopLive(){liveGeneration++;livePrinter=null;clearTimeout(liveTimer);if(liveRequest)liveRequest.abort();liveRequest=null;if(liveUrl)URL.revokeObjectURL(liveUrl);liveUrl=null;$('liveImage').removeAttribute('src');$('liveImage').hidden=true;$('liveStatus').textContent='Live view stopped.';$('startLive').disabled=false;}
async function fetchLiveFrame(generation,version=0,feed=''){
 if(generation!==liveGeneration||!livePrinter)return;
 const controller=new AbortController();liveRequest=controller;
 const timeout=setTimeout(()=>controller.abort(),30000);
 try{
  const response=await fetch('/api/liveframe/'+encodeURIComponent(livePrinter)+'?after='+version+'&feed='+encodeURIComponent(feed),{signal:controller.signal,cache:'no-store'});
  if(!response.ok){let reason='Camera request failed ('+response.status+').';try{reason=(await response.json()).error||reason;}catch{}if(response.status===401){stopLive();showLogin();return;}throw new Error(reason);}
  const blob=await response.blob();if(generation!==liveGeneration)return;
  const next=URL.createObjectURL(blob),old=liveUrl;liveUrl=next;$('liveImage').src=next;$('liveImage').hidden=false;if(old)URL.revokeObjectURL(old);
  $('liveStatus').textContent='Live frames · '+new Date().toLocaleTimeString();
  version=Number(response.headers.get('X-Camera-Version'))||0;feed=response.headers.get('X-Camera-Feed')||'';
 }catch(e){if(generation!==liveGeneration)return;$('liveImage').hidden=true;$('liveStatus').textContent=(e.name==='AbortError'?'Camera request timed out.':e.message)+' Retrying…';}
 finally{clearTimeout(timeout);if(liveRequest===controller)liveRequest=null;}
 if(generation===liveGeneration)liveTimer=setTimeout(()=>fetchLiveFrame(generation,version,feed),750);
}
function startLiveView(){stopLive();livePrinter=selectedPrinter;$('liveStatus').textContent='Connecting to printer camera…';$('startLive').disabled=true;fetchLiveFrame(liveGeneration);}
// The panel shows the latest still snapshot straight away; ▶ starts live view only when asked (#40).
const snapshotUrl=p=>'/api/snapshot/'+encodeURIComponent(p.name)+'?t='+p.snapshot.time;
function renderStill(p){const still=$('stillImage'),snap=p?.snapshot||{};
 if(!p?.has_camera||livePrinter||!snap.time){still.hidden=true;return;}
 const url=snapshotUrl(p);if(still.dataset.url!==url){still.dataset.url=url;still.src=url;}still.hidden=false;}
function showCamera(){const p=devPrinter();$('startLive').hidden=!p?.has_camera;
 const snap=p?.snapshot||{};
 $('cameraHint').textContent=!p?.has_camera?(state?.demo?'No camera in demo mode.':(p?.limits?.camera_type?`No camera configured. For this ${p.limits.model} add "camera_type": "${p.limits.camera_type}" to its entry in config.json.`:'No camera configured for this printer (camera_type in config.json).'))
  :snap.error&&!snap.time?snap.error:'Live view: about one frame per second. Nothing is recorded.';
 if(!livePrinter)$('liveStatus').textContent=!p?.has_camera?'No camera.':snap.time?'Snapshot from '+new Date(snap.time*1000).toLocaleTimeString()+' · ▶ for live view':(snap.error||'Waiting for the first snapshot…');
 renderStill(p);}
$('startLive').onclick=()=>{$('stillImage').hidden=true;startLiveView();};
$('stopLive').onclick=()=>{stopLive();showCamera();};
$('detailDialog').addEventListener('close',stopLive);
document.addEventListener('visibilitychange',()=>{if(document.hidden)stopLive();});
$('logout').addEventListener('click',stopLive);
setInterval(()=>{if(livePrinter&&(!state||!$('detailDialog').open))stopLive();},1000);
// ---- Printer panel (Bambu Handy style) ----------------------------------------------------
const SPEEDS={1:'silent',2:'standard',3:'sport',4:'ludicrous'};
// jogNote: a result message stays visible for a while instead of being replaced by the checkbox hint.
let jogNote={text:'',until:0};
let editingTile=null,jogBusy=false,jogStep=null,jogArmTimer=null,jogReadyAt=0;
const devPrinter=()=>state?.printers.find(p=>p.name===selectedPrinter);
function num(v){const n=Number(v);return v===null||v===undefined||v===''||!Number.isFinite(n)?null:n;}
function fmtTemp(v){const n=num(v);return n===null?'—':Math.round(n)+'°';}
// Newer firmware (bit 38 of the hex "fun" flags) only jogs in fixed 1 / 10 mm steps.
function axisCtrl(d){try{return ((BigInt('0x'+(d.fun||'0'))>>38n)&1n)===1n;}catch{return false;}}
async function control(kind,body,label){const r=await api('printers/'+encodeURIComponent(selectedPrinter)+'/'+kind,body);notice(r.message||label||'Submitted.');return r;}
// Everyday controls apply straight away; the server still checks limits. Only Pause and Stop ask first.
async function sendControl(kind,value){try{return await control(kind,{value,confirmed:true});}catch(e){notice(e.message);throw e;}finally{refresh();}}

function tempDefs(p){const d=p.data||{},l=p.limits||{},defs=[
 {kind:'nozzle',label:'Nozzle',icon:'🔥',now:d.nozzle_temper,target:d.nozzle_target_temper,max:l.nozzle||300,hint:'Active nozzle. 0 turns heating off.'},
 {kind:'bed',label:'Bed',icon:'▭',now:d.bed_temper,target:d.bed_target_temper,max:l.bed||100,hint:'0 turns heating off.'}];
 if(l.chamber)defs.push({kind:'chamber',label:'Chamber',icon:'◫',now:d.chamber_temper,target:d.ctt,max:l.chamber,min:40,hint:'0 = off, or 40–65 °C. Firmware may refuse for low-temperature filament.'});
 return defs;}
function renderTiles(p){const box=$('tempTiles'),defs=tempDefs(p);
 if(box.dataset.printer!==p.name||box.children.length!==defs.length){box.dataset.printer=p.name;editingTile=null;
  box.innerHTML=defs.map(t=>`<div class="tile" data-kind="${t.kind}"><button type="button" class="tile-main" data-edit="${t.kind}" aria-label="Set ${t.label} temperature"><span class="tile-icon">${t.icon}</span><span class="tile-label">${t.label}</span><strong class="tile-now">—</strong><small class="tile-target">→ —</small></button><form class="tile-edit" hidden><input type="number" inputmode="numeric" min="0" max="${t.max}" step="1" aria-label="${t.label} target °C"><div class="tile-edit-actions"><button type="submit" class="primary">Set</button><button type="button" data-cancel>✕</button></div><small>${esc(t.hint)} Max ${t.max} °C.</small></form></div>`).join('');}
 for(const t of defs){const tile=box.querySelector(`[data-kind="${t.kind}"]`);tile.querySelector('.tile-now').textContent=fmtTemp(t.now);
  const target=num(t.target),key='temp|'+p.name+'|'+t.kind,waiting=isPending(key,p);
  tile.querySelector('.tile-target').textContent=waiting?'Setting '+(pending[key].value?pending[key].value+'°':'off')+'…':target?'→ '+Math.round(target)+'°':'Off';
  tile.classList.toggle('heating',!!target&&!waiting);tile.classList.toggle('pending',waiting);}
}
$('tempTiles').addEventListener('click',e=>{const edit=e.target.closest('[data-edit]'),cancel=e.target.closest('[data-cancel]');
 if(edit){const tile=edit.closest('.tile'),form=tile.querySelector('.tile-edit'),input=form.querySelector('input');
  $('tempTiles').querySelectorAll('.tile-edit').forEach(f=>{f.hidden=true;f.previousElementSibling.hidden=false;});
  const t=tempDefs(devPrinter()).find(x=>x.kind===tile.dataset.kind);input.value=Math.round(num(t.target)||0);edit.hidden=true;form.hidden=false;editingTile=t.kind;input.focus();input.select();}
 if(cancel){const form=cancel.closest('.tile-edit');form.hidden=true;form.previousElementSibling.hidden=false;editingTile=null;}});
$('tempTiles').addEventListener('submit',e=>{e.preventDefault();const form=e.target,tile=form.closest('.tile'),kind=tile.dataset.kind,value=Number(form.querySelector('input').value);
 form.hidden=true;form.previousElementSibling.hidden=false;editingTile=null;
 const name=selectedPrinter,key='temp|'+name+'|'+kind;
 setPending(key,20000,p=>Math.round(num(tempDefs(p).find(x=>x.kind===kind)?.target)||0)===value,printerLabel(name)+' '+kind+' temperature',{value});renderTiles(devPrinter());
 sendControl(kind,value).then(pollSoon,()=>{delete pending[key];renderTiles(devPrinter());});});

function renderSpeed(p){const level=SPEEDS[(p.data||{}).spd_lvl],key='speed|'+p.name,want=isPending(key,p)?pending[key].level:null;
 $('speedControl').querySelectorAll('button').forEach(b=>{b.classList.toggle('active',b.dataset.speed===level&&!want);b.classList.toggle('pending',b.dataset.speed===want);});
 $('detailSpeedLabel').textContent='Speed '+(level?level[0].toUpperCase()+level.slice(1):'—');}
$('speedControl').addEventListener('click',e=>{const b=e.target.closest('[data-speed]');if(!b||b.classList.contains('active'))return;
 const name=selectedPrinter,key='speed|'+name,level=b.dataset.speed;
 setPending(key,20000,p=>SPEEDS[(p.data||{}).spd_lvl]===level,printerLabel(name)+' speed',{level});renderSpeed(devPrinter());
 sendControl('speed',level).then(pollSoon,()=>{delete pending[key];renderSpeed(devPrinter());});});

function fanPercent(p,f){const d=p.data||{};const demo=(d.demo_fan_targets||{})[f.id];return num(f.percent)??num(demo);}
const fanPending={};let draggingFan=null;
setInterval(()=>{const row=document.querySelector('#fanList .fan-row.all.pending');if(row&&!Object.keys(fanPending).some(k=>k.startsWith(selectedPrinter+'|')))row.classList.remove('pending');},500);
function fanShown(p,f){const key=p.name+'|'+f.key,pend=fanPending[key],reported=fanPercent(p,f);
 if(pend&&(reported===pend.value||Date.now()>pend.until))delete fanPending[key];
 return fanPending[key]?fanPending[key].value:reported;}
function renderFans(p){const box=$('fanList'),fans=(p.limits||{}).fans||[],signature=p.name+':'+fans.map(f=>f.key+(f.manual?'m':'a')).join(',');
 if(box.dataset.signature!==signature){box.dataset.signature=signature;draggingFan=null;
  box.innerHTML=fans.map(f=>`<div class="fan-row${f.manual?'':' auto'}" data-fan="${esc(f.key)}"><div class="fan-name"><span>🌀 ${esc(f.label)}</span><small></small></div>${f.manual?`<input type="range" min="${f.minimum??0}" max="${f.maximum??100}" step="10" value="0" aria-label="${esc(f.label)} speed"><output>0%</output>`:''}</div>`).join('')
   +(fans.some(f=>f.manual)?`<div class="fan-row all" data-fan="__all"><div class="fan-name"><span>All manual fans</span><small>Sets every fan above at once</small></div><input type="range" min="0" max="100" step="10" value="0" aria-label="All manual fans speed"><output>0%</output></div>`:'')||'<p class="muted">No fans reported.</p>';}
 for(const f of fans){const row=[...box.querySelectorAll('.fan-row')].find(r=>r.dataset.fan===f.key);if(!row)continue;
  const pct=fanShown(p,f),waiting=!!fanPending[p.name+'|'+f.key];row.classList.toggle('pending',waiting);
  row.querySelector('small').textContent=!f.manual?'Automatic (firmware)':waiting?'Setting '+pct+'%…':pct===null?'Not reported':pct+'%';
  const input=row.querySelector('input');if(input&&input!==draggingFan){input.value=pct??0;row.querySelector('output').textContent=(pct??0)+'%';}}}
$('fanList').addEventListener('input',e=>{if(e.target.type!=='range')return;draggingFan=e.target;e.target.closest('.fan-row').querySelector('output').textContent=e.target.value+'%';});
// 'change' fires when the slider is released (or stepped with the keyboard): apply it then, no Set button.
$('fanList').addEventListener('change',async e=>{if(e.target.type!=='range')return;draggingFan=null;
 const p=devPrinter(),row=e.target.closest('.fan-row'),value=Number(e.target.value),all=row.dataset.fan==='__all';
 const targets=all?((p.limits||{}).fans||[]).filter(f=>f.manual):((p.limits||{}).fans||[]).filter(f=>f.key===row.dataset.fan);
 for(const f of targets)fanPending[p.name+'|'+f.key]={value,until:Date.now()+20000};renderFans(p);
 if(all)row.classList.add('pending');
 try{await sendControl(all?'fanall':'fan_'+row.dataset.fan,value);pollSoon();}catch{for(const f of targets)delete fanPending[p.name+'|'+f.key];renderFans(devPrinter());}});

// Axes the printer reports as not homed (home_flag bits 0-2 = X, Y, Z; 0 = unknown). The firmware ignores jogs on them.
function unhomed(d){const f=Number(d.home_flag||0);return f?['X','Y','Z'].filter((a,i)=>!((f>>i)&1)):[];}
function jogSteps(p){return axisCtrl(p.data||{})?{xy:[1,10],z:[1]}:{xy:[1,10],z:[0.1,1]};}
function renderJog(p){const steps=jogSteps(p),box=$('jogSteps'),key=JSON.stringify(steps);
 if(box.dataset.steps!==key){box.dataset.steps=key;if(!steps.xy.includes(jogStep))jogStep=steps.xy[0];
  box.innerHTML=steps.xy.map(s=>`<button type="button" data-step="${s}">${s} mm</button>`).join('');}
 box.querySelectorAll('button').forEach(b=>b.classList.toggle('active',Number(b.dataset.step)===jogStep));
 const zStep=steps.z.includes(jogStep)?jogStep:steps.z[steps.z.length-1];if(Date.now()>=jogReadyAt)$('jogCenter').textContent=jogStep+' mm';$('jogZStep').textContent='Z '+zStep+' mm';
 const idle=['IDLE','FINISH'].includes(p.state)&&p.connected&&!p.error,armed=$('jogArm').checked;
 const cooling=Date.now()<jogReadyAt,notHomed=unhomed(p.data||{});
 document.querySelectorAll('#jogPad button,#jogZ button').forEach(b=>b.disabled=!armed||!idle||jogBusy||cooling||notHomed.includes(b.dataset.axis));
 // Homing doesn't need the "already homed" checkbox, only an idle printer.
 $('jogHome').disabled=!idle||jogBusy||cooling;
 if(Date.now()<jogNote.until)$('jogStatus').textContent=jogNote.text;
 else if(!idle)$('jogStatus').textContent='Movement is only available while the printer is idle, connected and error-free.';
 else if(notHomed.length)$('jogStatus').textContent=`The printer reports ${notHomed.join(', ')} not homed, so it would ignore those moves. Press Home first.`;
 else if(!armed)$('jogStatus').textContent='Tick the box above to unlock the movement buttons. Directions follow printer coordinates.';}
$('jogSteps').addEventListener('click',e=>{const b=e.target.closest('[data-step]');if(!b)return;jogStep=Number(b.dataset.step);renderJog(devPrinter());});
$('jogArm').onchange=()=>{clearTimeout(jogArmTimer);if($('jogArm').checked){jogArmTimer=setTimeout(()=>{$('jogArm').checked=false;renderJog(devPrinter());},5*60*1000);$('jogStatus').textContent='Unlocked for 5 minutes or until this panel closes.';}renderJog(devPrinter());};
async function jog(axis,dir,button){button?.classList.add('pending');try{await jogMove(axis,dir);}finally{button?.classList.remove('pending');}}
async function jogMove(axis,dir){const p=devPrinter(),steps=jogSteps(p),step=axis==='Z'?(steps.z.includes(jogStep)?jogStep:steps.z[steps.z.length-1]):jogStep,value=dir*step;
 jogBusy=true;renderJog(p);$('jogStatus').textContent=`Moving ${axis} ${value>0?'+':''}${value} mm…`;
 try{const r=await api('printers/'+encodeURIComponent(selectedPrinter)+'/move',{value,axis,confirmed:true,homed:true});$('jogStatus').textContent=r.message;
  // The server allows one move every 3 seconds; wait it out instead of showing an error.
  jogReadyAt=Date.now()+3100;const tick=()=>{const left=Math.ceil((jogReadyAt-Date.now())/1000);if(left>0){$('jogCenter').textContent=left+' s';setTimeout(tick,250);}else renderJog(devPrinter());};tick();}
 catch(e){$('jogStatus').textContent=e.message;}finally{jogBusy=false;renderJog(devPrinter());}}
document.querySelectorAll('#jogPad [data-axis],#jogZ [data-axis]').forEach(b=>b.onclick=()=>jog(b.dataset.axis,Number(b.dataset.dir),b));
$('jogHome').onclick=async()=>{const b=$('jogHome');b.classList.add('pending');jogBusy=true;jogNote={text:'Homing X, Y and Z…',until:Date.now()+30000};renderJog(devPrinter());
 try{const r=await api('printers/'+encodeURIComponent(selectedPrinter)+'/home',{confirmed:true});jogNote={text:r.message+' Wait for homing to finish before moving.',until:Date.now()+15000};
  jogReadyAt=Date.now()+3100;setTimeout(()=>renderJog(devPrinter()),3200);}
 catch(e){jogNote={text:e.message,until:Date.now()+15000};}finally{jogBusy=false;b.classList.remove('pending');renderJog(devPrinter());}};

function swatch(t,active,ams,slot,waiting,tag=''){const color=/^[0-9a-f]{6}/i.test(t?.tray_color||'')?'#'+t.tray_color.slice(0,6):null,empty=!t||!t.tray_type,remain=num(t?.remain);
 return `<button type="button" class="slot${empty?' is-empty':''}${active?' active':''}${waiting?' pending':''}" data-ams="${esc(ams)}" data-slot="${esc(slot)}" title="${esc((empty?'Empty':t.tray_type+(remain!==null&&remain>=0?' · '+remain+'%':''))+' — click to edit')}"><span class="swatch"${color&&!empty?` data-color="${color}"`:''}></span><strong>${empty?'Empty':esc(t.tray_type)}</strong><small>${empty?'—':remain!==null&&remain>=0?remain+'%':'?'}</small>${!empty&&remain!==null&&remain>=0?`<i class="remain" data-width="${Math.max(0,Math.min(100,remain))}"></i>`:''}${tag}</button>`;}
function exists(bits,unit,slot){try{return bits==null||Number(unit)>=32||((BigInt('0x'+bits)>>BigInt(Number(unit)*4+Number(slot)))&1n)===1n;}catch{return true;}}
// Dual-nozzle printers (H2D): p.sides (filament_sides.py) says which nozzle each AMS / external spool feeds
// and what each nozzle has loaded (#5). Nozzle 0 is the right one, 1 the left.
const sideName=n=>n===0?'Right':n===1?'Left':n==='both'?'Both':'';
const externalKey=id=>Number(id)===254?'external_left':'external';
function renderAms(p){const d=p.data||{},ams=Array.isArray(d.ams)?{ams:d.ams}:(d.ams||{}),units=ams.ams||[],now=num(ams.tray_now),sides=p.sides||{},dual=!!sides.dual;
 const wait=(a,s)=>isPending('fil|'+p.name+'|'+a+'|'+s,p);
 const loadedIn=(a,s)=>(sides.loaded||[]).find(l=>l.ams===Number(a)&&l.slot===Number(s));
 const active=(a,s)=>dual?!!loadedIn(a,s):a>=254?now===254||now===255:now===Number(a)*4+Number(s);
 const tag=(a,s)=>{const l=dual&&loadedIn(a,s);return l?`<em class="nozzle-tag">${sideName(l.nozzle)} nozzle</em>`:'';};
 const badge=n=>dual&&sideName(n)?`<span class="side-badge">${sideName(n)}${n==='both'?' nozzles':' nozzle'}</span>`:'';
 let html=dual&&sideName(sides.active)?`<p class="nozzle-now">Nozzle in use: <strong>${sideName(sides.active)}</strong></p>`:'';
 html+=units.map(u=>`<div class="ams-unit"><div class="ams-head"><strong>AMS ${esc(Number(u.id)+1||u.id)} ${badge((sides.ams||{})[String(u.id)])}</strong><small>💧 ${esc(u.humidity??'—')} · ${esc(u.temp??'—')}°C</small></div><div class="slots">${(u.tray||[]).map(t=>swatch(exists(ams.tray_exist_bits,u.id,t.id)?t:null,active(u.id,t.id),u.id,t.id,wait(u.id,t.id),tag(u.id,t.id))).join('')}</div></div>`).join('');
 const externals=sides.external||(d.vt_tray?[{ams:255,nozzle:null,tray:d.vt_tray}]:[]);
 html+=externals.map(x=>`<div class="ams-unit external"><div class="ams-head"><strong>External spool ${badge(x.nozzle)}</strong></div><div class="slots">${swatch(x.tray,active(x.ams,0),externalKey(x.ams),0,wait(externalKey(x.ams),0),tag(x.ams,0))}</div></div>`).join('');
 $('amsView').innerHTML=html||'<p class="muted">No filament data reported yet.</p>';applyStyles($('amsView'));}

// ---- Edit a slot's filament (material and colour) ----------------------------------------
const MATERIALS=['PLA','PETG','ABS','ASA','TPU','PC','PA','PVA'];
const PRESET_COLOURS=['FFFFFF','161616','8E9089','E8412C','F28C28','F4D03F','39B54A','2E7DD1','7D4CDB','E86FA8','8B5A2B','C0C0C0'];
let editingSlot=null;
$('feType').innerHTML=MATERIALS.map(m=>`<option>${m}</option>`).join('');
$('fePresets').innerHTML=PRESET_COLOURS.map(c=>`<button type="button" class="fe-preset" data-preset="${c}" data-color="#${c}" aria-label="Colour #${c}" title="#${c}"></button>`).join('');applyStyles($('fePresets'));
$('fePresets').addEventListener('click',e=>{const b=e.target.closest('[data-preset]');if(b)$('feColor').value='#'+b.dataset.preset.toLowerCase();});
function slotTray(p,ams,slot){const d=p.data||{};if(ams==='external'||ams==='external_left'){const id=ams==='external'?255:254;return ((p.sides||{}).external||[]).find(x=>x.ams===id)?.tray||(id===255?d.vt_tray:null);}const a=Array.isArray(d.ams)?{ams:d.ams}:(d.ams||{});
 return (a.ams||[]).find(u=>String(u.id)===String(ams))?.tray?.find(t=>String(t.id)===String(slot));}
$('amsView').addEventListener('click',e=>{const b=e.target.closest('.slot[data-ams]');if(!b)return;const p=devPrinter(),t=slotTray(p,b.dataset.ams,b.dataset.slot);
 editingSlot={ams:b.dataset.ams,slot:b.dataset.slot};
 $('feTitle').textContent=b.dataset.ams.startsWith('external')?'External spool'+((p.sides||{}).dual?(b.dataset.ams==='external'?' · right nozzle':' · left nozzle'):''):`AMS ${Number(b.dataset.ams)+1} · slot ${Number(b.dataset.slot)+1}`;
 $('feType').value=MATERIALS.includes(String(t?.tray_type||'').toUpperCase())?t.tray_type.toUpperCase():'PLA';
 $('feColor').value=/^[0-9a-f]{6}/i.test(t?.tray_color||'')?'#'+t.tray_color.slice(0,6).toLowerCase():'#ffffff';
 $('filamentEditor').hidden=false;$('filamentEditor').scrollIntoView({block:'nearest',behavior:'smooth'});});
$('feCancel').onclick=()=>{$('filamentEditor').hidden=true;editingSlot=null;};
$('filamentEditor').onsubmit=e=>{e.preventDefault();if(!editingSlot)return;const name=selectedPrinter,{ams,slot}=editingSlot,type=$('feType').value,color=$('feColor').value.slice(1).toUpperCase(),key='fil|'+name+'|'+ams+'|'+slot;
 $('filamentEditor').hidden=true;editingSlot=null;
 setPending(key,20000,p=>{const t=slotTray(p,ams,slot);return String(t?.tray_type||'').toUpperCase()===type&&String(t?.tray_color||'').slice(0,6).toUpperCase()===color;},printerLabel(name)+' filament');
 renderAms(devPrinter());
 sendControl('filament',{ams:ams.startsWith('external')?ams:Number(ams),slot:Number(slot),type,color}).then(pollSoon,()=>{delete pending[key];renderAms(devPrinter());});};
$('detailDialog').addEventListener('close',()=>{$('filamentEditor').hidden=true;editingSlot=null;});

// ---- Nozzle diameter and type -----------------------------------------------------------
const NOZZLE_TYPES={stainless_steel:'Stainless steel',hardened_steel:'Hardened steel',tungsten_carbide:'Tungsten carbide'};
function renderNozzle(p){const d=p.data||{},dia=num(d.nozzle_diameter),type=d.nozzle_type,key='nozzle|'+p.name,waiting=isPending(key,p);
 $('nozzleNow').textContent=dia?`${dia} mm · ${NOZZLE_TYPES[type]||type||'type not reported'}`:'Not reported';
 $('nozzleSave').classList.toggle('pending',waiting);$('nozzleSave').disabled=['RUNNING','PAUSE','PREPARE'].includes(p.state)||!p.connected;
 const form=$('nozzleForm');if(form.dataset.printer!==p.name||(!form.contains(document.activeElement)&&!waiting&&form.dataset.dirty!=='1')){form.dataset.printer=p.name;
  if(dia)$('nozzleDiameter').value=String(dia);if(NOZZLE_TYPES[type])$('nozzleType').value=type;}}
$('nozzleForm').addEventListener('change',()=>{$('nozzleForm').dataset.dirty='1';});
$('nozzleForm').onsubmit=e=>{e.preventDefault();const name=selectedPrinter,diameter=Number($('nozzleDiameter').value),type=$('nozzleType').value,key='nozzle|'+name;
 $('nozzleForm').dataset.dirty='';setPending(key,20000,p=>num(p.data?.nozzle_diameter)===diameter&&p.data?.nozzle_type===type,printerLabel(name)+' nozzle');renderNozzle(devPrinter());
 sendControl('nozzle_size',{diameter,type}).then(pollSoon,()=>{delete pending[key];renderNozzle(devPrinter());});};

function renderDevice(){const p=devPrinter();if(!p)return;const d=p.data||{},progress=Math.max(0,Math.min(100,num(d.mc_percent)||0)),active=['RUNNING','PAUSE','PREPARE'].includes(p.state);
 $('detailTitle').textContent=p.display_name||p.name;$('detailModel').textContent=((p.limits||{}).chamber?'H2D · ':'')+(p.connected?'ONLINE':'OFFLINE');
 $('detailSeen').textContent=p.last_seen?'Last report '+new Date(p.last_seen*1000).toLocaleTimeString():'No report yet';
 const st=$('detailState');st.textContent=p.connected?p.state:'OFFLINE';st.className='state '+statusClass(p.state,p.connected);
 $('detailFile').textContent=active?(d.subtask_name||'Unknown file'):(p.connected?'Ready for the next job':'Printer offline');
 $('detailFileLabel').textContent=active?(p.state==='PAUSE'?'PAUSED':'NOW PRINTING'):(p.state==='FINISH'?'FINISHED':'STATUS');
 document.querySelector('#detailDialog .now-card').classList.toggle('idle',!active);
 $('detailProgress').value=progress;$('detailPercent').textContent=progress+'%';$('detailRing').style.setProperty('--p',progress);
 $('detailLayers').textContent=`Layer ${d.layer_num??'—'} / ${d.total_layer_num??'—'}`;
 const left=num(d.mc_remaining_time);$('detailRemaining').textContent=left===null?'— remaining':(left>=60?Math.floor(left/60)+' h '+left%60+' min':left+' min')+' remaining';
 $('detailError').hidden=!p.error_text;$('detailError').textContent=p.error_text||'';
 $('devPause').hidden=p.state!=='RUNNING'&&p.state!=='PREPARE';$('devResume').hidden=p.state!=='PAUSE';$('devStop').hidden=!active;
 const lt=lightShown(p);$('devLight').setAttribute('aria-pressed',String(lt.on));$('devLight').classList.toggle('pending',lt.busy);
 for(const a of ['pause','resume','stop'])$('dev'+a[0].toUpperCase()+a.slice(1)).classList.toggle('pending',isPending(a+'|'+p.name,p));
 showCamera();renderTiles(p);renderNozzle(p);renderSpeed(p);renderFans(p);renderJog(p);renderAms(p);}
document.querySelector('#detailDialog .now-actions').addEventListener('click',async e=>{const b=e.target.closest('[data-dev]');if(!b)return;const name=selectedPrinter,action=b.dataset.dev;
 if(action==='stop'){confirmStop(name);return;}
 if(action==='pause'){confirmPause(name);return;}
 if(action==='light'){await toggleLight(name);return;}
 if(action==='resume'){try{await printAction(name,'resume');}catch(err){notice(err.message);}return;}
 const target=action;
 b.disabled=true;try{await api('printers/'+encodeURIComponent(name)+'/'+target,{});notice('Command submitted. Waiting for printer telemetry.');await refresh();}catch(err){notice(err.message);}finally{b.disabled=false;}});
$('devFiles').onclick=()=>downloadFileListing(selectedPrinter,$('devFiles'));
$('detailDialog').addEventListener('close',()=>{$('jogArm').checked=false;clearTimeout(jogArmTimer);editingTile=null;$('tempTiles').dataset.printer='';});
// The play button shows whenever live view isn't running, however it stopped.
setInterval(()=>{$('cameraPlaceholder').hidden=!!livePrinter;$('stopLive').hidden=!livePrinter;},300);

function loadSwapSettings(){const cfg=state?.printers.find(p=>p.name===selectedPrinter)?.plate_swap||{};$('swapEnabled').checked=!!cfg.enabled;$('swapModel').value=cfg.model||'A1 mini';$('swapSpares').value=cfg.spares||0;renderSwapStatus();}
// Swapmod is only offered for A-series printers (A1 / A1 mini) (#17).
function renderSwapStatus(){const cfg=state?.printers.find(p=>p.name===selectedPrinter)?.plate_swap;$('swapBlock').hidden=!cfg?.available;$('swapStatus').textContent=cfg?.enabled?`${cfg.model} kit enabled · ${cfg.spares} estimated magazine plates · ${cfg.verified?'Starting setup checked':'Starting setup check needed'}`:'Disabled for this printer — manual queue workflow.';}
setInterval(()=>{if($('detailDialog').open&&state)renderSwapStatus();},1000);
$('swapForm').onsubmit=e=>{e.preventDefault();const name=selectedPrinter,data={enabled:$('swapEnabled').checked,model:$('swapModel').value,spares:Number($('swapSpares').value),confirmed:true};confirmAction('Save plate-swap settings?',printerLabel(name)+' — '+(data.enabled?'enable '+data.model+' kit':'disable kit')+'; '+data.spares+' magazine plates. Existing plate checks and file approvals will be reset.','I verified the installed hardware and actual spare count.',async()=>{await api('plateswap/'+encodeURIComponent(name)+'/configure',data);notice('Saved for this printer. Approve the prepared Swaplist batch and check its starting setup before starting.');});};
$('swapChecked').onclick=()=>{const name=selectedPrinter;confirmAction('Swapmod starting setup checked?',printerLabel(name)+' — this records your inspection; no swap movement is sent.','I checked the starting setup against Swaplist instructions, loaded the required magazine plates and cleared the ejection path.',async()=>{await api('plateswap/'+encodeURIComponent(name)+'/check',{confirmed:true});notice('Setup checked. Start the approved Swaplist batch when ready.');});};

let swapApprovalJob=null;
function openSwapApproval(id){const j=state.jobs.find(x=>x.id===id);swapApprovalJob=id;$('swapApproveText').textContent=j.label+' — '+printerLabel(j.printer);$('swapBatchPlates').value=j.options.swap_plates||1;$('swapBatchConfirm').checked=false;$('swapApproveDialog').showModal();}
$('swapApproveForm').onsubmit=async e=>{e.preventDefault();try{await api('jobs/'+swapApprovalJob+'/swapapprove',{confirmed:$('swapBatchConfirm').checked,plates:Number($('swapBatchPlates').value)});$('swapApproveDialog').close();notice('Swaplist batch approved.');await refresh();}catch(e){notice(e.message);}};
