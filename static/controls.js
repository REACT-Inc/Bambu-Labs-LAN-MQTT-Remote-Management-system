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
$('startLive').onclick=()=>{stopLive();livePrinter=selectedPrinter;$('liveStatus').textContent='Connecting to printer camera…';$('startLive').disabled=true;fetchLiveFrame(liveGeneration);};
$('stopLive').onclick=stopLive;
$('detailDialog').addEventListener('close',stopLive);
document.addEventListener('visibilitychange',()=>{if(document.hidden)stopLive();});
$('logout').addEventListener('click',stopLive);
setInterval(()=>{if(livePrinter&&(!state||!$('detailDialog').open))stopLive();},1000);
// ---- Printer panel (Bambu Handy style) ----------------------------------------------------
const SPEEDS={1:'silent',2:'standard',3:'sport',4:'ludicrous'};
let editingTile=null,jogBusy=false,jogStep=null,jogArmTimer=null,jogReadyAt=0;
const devPrinter=()=>state?.printers.find(p=>p.name===selectedPrinter);
function num(v){const n=Number(v);return v===null||v===undefined||v===''||!Number.isFinite(n)?null:n;}
function fmtTemp(v){const n=num(v);return n===null?'—':Math.round(n)+'°';}
// Newer firmware (bit 38 of the hex "fun" flags) only jogs in fixed 1 / 10 mm steps.
function axisCtrl(d){try{return ((BigInt('0x'+(d.fun||'0'))>>38n)&1n)===1n;}catch{return false;}}
async function control(kind,body,label){const r=await api('printers/'+encodeURIComponent(selectedPrinter)+'/'+kind,body);notice(r.message||label||'Submitted.');return r;}
function askControl(title,text,kind,value,label){confirmAction(title,printerLabel(selectedPrinter)+' — '+text,label||'I checked this setting and want to apply it.',()=>control(kind,{value,confirmed:true}));}

function tempDefs(p){const d=p.data||{},l=p.limits||{},defs=[
 {kind:'nozzle',label:'Nozzle',icon:'🔥',now:d.nozzle_temper,target:d.nozzle_target_temper,max:l.nozzle||300,hint:'Active nozzle. 0 turns heating off.'},
 {kind:'bed',label:'Bed',icon:'▭',now:d.bed_temper,target:d.bed_target_temper,max:l.bed||100,hint:'0 turns heating off.'}];
 if(l.chamber)defs.push({kind:'chamber',label:'Chamber',icon:'◫',now:d.chamber_temper,target:d.ctt,max:l.chamber,min:40,hint:'0 = off, or 40–65 °C. Firmware may refuse for low-temperature filament.'});
 return defs;}
function renderTiles(p){const box=$('tempTiles'),defs=tempDefs(p);
 if(box.dataset.printer!==p.name||box.children.length!==defs.length){box.dataset.printer=p.name;editingTile=null;
  box.innerHTML=defs.map(t=>`<div class="tile" data-kind="${t.kind}"><button type="button" class="tile-main" data-edit="${t.kind}" aria-label="Set ${t.label} temperature"><span class="tile-icon">${t.icon}</span><span class="tile-label">${t.label}</span><strong class="tile-now">—</strong><small class="tile-target">→ —</small></button><form class="tile-edit" hidden><input type="number" inputmode="numeric" min="0" max="${t.max}" step="1" aria-label="${t.label} target °C"><div class="tile-edit-actions"><button type="submit" class="primary">Set</button><button type="button" data-cancel>✕</button></div><small>${esc(t.hint)} Max ${t.max} °C.</small></form></div>`).join('');}
 for(const t of defs){const tile=box.querySelector(`[data-kind="${t.kind}"]`);tile.querySelector('.tile-now').textContent=fmtTemp(t.now);
  const target=num(t.target);tile.querySelector('.tile-target').textContent=target?'→ '+Math.round(target)+'°':'Off';tile.classList.toggle('heating',!!target);}
}
$('tempTiles').addEventListener('click',e=>{const edit=e.target.closest('[data-edit]'),cancel=e.target.closest('[data-cancel]');
 if(edit){const tile=edit.closest('.tile'),form=tile.querySelector('.tile-edit'),input=form.querySelector('input');
  $('tempTiles').querySelectorAll('.tile-edit').forEach(f=>{f.hidden=true;f.previousElementSibling.hidden=false;});
  const t=tempDefs(devPrinter()).find(x=>x.kind===tile.dataset.kind);input.value=Math.round(num(t.target)||0);edit.hidden=true;form.hidden=false;editingTile=t.kind;input.focus();input.select();}
 if(cancel){const form=cancel.closest('.tile-edit');form.hidden=true;form.previousElementSibling.hidden=false;editingTile=null;}});
$('tempTiles').addEventListener('submit',e=>{e.preventDefault();const form=e.target,tile=form.closest('.tile'),kind=tile.dataset.kind,value=Number(form.querySelector('input').value);
 form.hidden=true;form.previousElementSibling.hidden=false;editingTile=null;
 askControl('Set '+kind+' temperature?',`${kind} target → ${value ? value+' °C' : 'off'}`,kind,value);});

function renderSpeed(p){const level=SPEEDS[(p.data||{}).spd_lvl];$('speedControl').querySelectorAll('button').forEach(b=>b.classList.toggle('active',b.dataset.speed===level));
 $('detailSpeedLabel').textContent='Speed '+(level?level[0].toUpperCase()+level.slice(1):'—');}
$('speedControl').addEventListener('click',e=>{const b=e.target.closest('[data-speed]');if(!b||b.classList.contains('active'))return;askControl('Change print speed?','speed → '+b.textContent,'speed',b.dataset.speed);});

function fanPercent(p,f){const d=p.data||{};const demo=(d.demo_fan_targets||{})[f.id];return num(f.percent)??num(demo);}
function renderFans(p){const box=$('fanList'),fans=(p.limits||{}).fans||[];
 if(box.contains(document.activeElement)&&box.dataset.printer===p.name)return; // don't disturb a slider being dragged
 box.dataset.printer=p.name;
 box.innerHTML=fans.map(f=>{const pct=fanPercent(p,f);return `<div class="fan-row${f.manual?'':' auto'}" data-fan="${esc(f.key)}"><div class="fan-name"><span>🌀 ${esc(f.label)}</span><small>${f.manual?(pct===null?'Not reported':pct+'%'):'Automatic (firmware)'}</small></div>${f.manual?`<input type="range" min="0" max="100" step="10" value="${pct??0}" aria-label="${esc(f.label)} speed"><output>${pct??0}%</output><button type="button" class="small-btn" data-fan-set hidden>Set</button>`:''}</div>`;}).join('')+(fans.some(f=>f.manual)?`<div class="fan-row all" data-fan="__all"><div class="fan-name"><span>All manual fans</span><small>Sets every fan above at once</small></div><input type="range" min="0" max="100" step="10" value="0" aria-label="All manual fans speed"><output>0%</output><button type="button" class="small-btn" data-fan-set hidden>Set</button></div>`:'')||'<p class="muted">No fans reported.</p>';}
$('fanList').addEventListener('input',e=>{if(e.target.type!=='range')return;const row=e.target.closest('.fan-row');row.querySelector('output').textContent=e.target.value+'%';row.querySelector('[data-fan-set]').hidden=false;});
$('fanList').addEventListener('click',e=>{const b=e.target.closest('[data-fan-set]');if(!b)return;const row=b.closest('.fan-row'),value=Number(row.querySelector('input').value);b.hidden=true;
 const all=row.dataset.fan==='__all';askControl(all?'Set all fans?':'Set fan speed?',(all?'all manual fans':row.querySelector('.fan-name span').textContent.replace('🌀 ',''))+' → '+value+'%',all?'fanall':'fan_'+row.dataset.fan,value);});

function jogSteps(p){return axisCtrl(p.data||{})?{xy:[1,10],z:[1]}:{xy:[1,10],z:[0.1,1]};}
function renderJog(p){const steps=jogSteps(p),box=$('jogSteps'),key=JSON.stringify(steps);
 if(box.dataset.steps!==key){box.dataset.steps=key;if(!steps.xy.includes(jogStep))jogStep=steps.xy[0];
  box.innerHTML=steps.xy.map(s=>`<button type="button" data-step="${s}">${s} mm</button>`).join('');}
 box.querySelectorAll('button').forEach(b=>b.classList.toggle('active',Number(b.dataset.step)===jogStep));
 const zStep=steps.z.includes(jogStep)?jogStep:steps.z[steps.z.length-1];if(Date.now()>=jogReadyAt)$('jogCenter').textContent=jogStep+' mm';$('jogZStep').textContent='Z '+zStep+' mm';
 const idle=['IDLE','FINISH'].includes(p.state)&&p.connected&&!p.error,armed=$('jogArm').checked;
 const cooling=Date.now()<jogReadyAt;document.querySelectorAll('#jogPad button,#jogZ button').forEach(b=>b.disabled=!armed||!idle||jogBusy||cooling);
 if(!idle)$('jogStatus').textContent='Movement is only available while the printer is idle, connected and error-free.';
 else if(!armed)$('jogStatus').textContent='Tick the box above to unlock the movement buttons. Directions follow printer coordinates.';}
$('jogSteps').addEventListener('click',e=>{const b=e.target.closest('[data-step]');if(!b)return;jogStep=Number(b.dataset.step);renderJog(devPrinter());});
$('jogArm').onchange=()=>{clearTimeout(jogArmTimer);if($('jogArm').checked){jogArmTimer=setTimeout(()=>{$('jogArm').checked=false;renderJog(devPrinter());},5*60*1000);$('jogStatus').textContent='Unlocked for 5 minutes or until this panel closes.';}renderJog(devPrinter());};
async function jog(axis,dir){const p=devPrinter(),steps=jogSteps(p),step=axis==='Z'?(steps.z.includes(jogStep)?jogStep:steps.z[steps.z.length-1]):jogStep,value=dir*step;
 jogBusy=true;renderJog(p);$('jogStatus').textContent=`Moving ${axis} ${value>0?'+':''}${value} mm…`;
 try{const r=await api('printers/'+encodeURIComponent(selectedPrinter)+'/move',{value,axis,confirmed:true,homed:true});$('jogStatus').textContent=r.message;
  // The server allows one move every 3 seconds; wait it out instead of showing an error.
  jogReadyAt=Date.now()+3100;const tick=()=>{const left=Math.ceil((jogReadyAt-Date.now())/1000);if(left>0){$('jogCenter').textContent=left+' s';setTimeout(tick,250);}else renderJog(devPrinter());};tick();}
 catch(e){$('jogStatus').textContent=e.message;}finally{jogBusy=false;renderJog(devPrinter());}}
document.querySelectorAll('#jogPad [data-axis],#jogZ [data-axis]').forEach(b=>b.onclick=()=>jog(b.dataset.axis,Number(b.dataset.dir)));

function swatch(t,active){const color=/^[0-9a-f]{6}/i.test(t?.tray_color||'')?'#'+t.tray_color.slice(0,6):null,empty=!t||!t.tray_type,remain=num(t?.remain);
 return `<div class="slot${empty?' is-empty':''}${active?' active':''}" title="${esc(empty?'Empty':t.tray_type+(remain!==null&&remain>=0?' · '+remain+'%':''))}"><span class="swatch"${color&&!empty?` data-color="${color}"`:''}></span><strong>${empty?'Empty':esc(t.tray_type)}</strong><small>${empty?'—':remain!==null&&remain>=0?remain+'%':'?'}</small>${!empty&&remain!==null&&remain>=0?`<i class="remain" data-width="${Math.max(0,Math.min(100,remain))}"></i>`:''}</div>`;}
function exists(bits,unit,slot){try{return bits==null||Number(unit)>=32||((BigInt('0x'+bits)>>BigInt(Number(unit)*4+Number(slot)))&1n)===1n;}catch{return true;}}
function renderAms(p){const d=p.data||{},ams=Array.isArray(d.ams)?{ams:d.ams}:(d.ams||{}),units=ams.ams||[],now=num(ams.tray_now);
 let html=units.map(u=>`<div class="ams-unit"><div class="ams-head"><strong>AMS ${esc(Number(u.id)+1||u.id)}</strong><small>💧 ${esc(u.humidity??'—')} · ${esc(u.temp??'—')}°C</small></div><div class="slots">${(u.tray||[]).map(t=>swatch(exists(ams.tray_exist_bits,u.id,t.id)?t:null,now===Number(u.id)*4+Number(t.id))).join('')}</div></div>`).join('');
 if(d.vt_tray)html+=`<div class="ams-unit external"><div class="ams-head"><strong>External spool</strong></div><div class="slots">${swatch(d.vt_tray,now===254||now===255)}</div></div>`;
 $('amsView').innerHTML=html||'<p class="muted">No filament data reported yet.</p>';applyStyles($('amsView'));}

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
 const light=(d.lights_report||[]).find(l=>l.node==='chamber_light');$('devLight').setAttribute('aria-pressed',String(light?.mode==='on'));
 renderTiles(p);renderSpeed(p);renderFans(p);renderJog(p);renderAms(p);}
document.querySelector('#detailDialog .now-actions').addEventListener('click',async e=>{const b=e.target.closest('[data-dev]');if(!b)return;const name=selectedPrinter,action=b.dataset.dev;
 if(action==='stop'){confirmAction('Stop this print?',printerLabel(name)+' — this cancels the current job.','I want to cancel this print.',()=>api('printers/'+encodeURIComponent(name)+'/stop',{confirmed:true}));return;}
 const target=action==='light'?(b.getAttribute('aria-pressed')==='true'?'lightoff':'lighton'):action;
 b.disabled=true;try{await api('printers/'+encodeURIComponent(name)+'/'+target,{});notice('Command submitted. Waiting for printer telemetry.');await refresh();}catch(err){notice(err.message);}finally{b.disabled=false;}});
$('devFiles').onclick=()=>downloadFileListing(selectedPrinter,$('devFiles'));
$('detailDialog').addEventListener('close',()=>{$('jogArm').checked=false;clearTimeout(jogArmTimer);editingTile=null;$('tempTiles').dataset.printer='';});
// The play button shows whenever live view isn't running, however it stopped.
setInterval(()=>{$('cameraPlaceholder').hidden=!!livePrinter;$('stopLive').hidden=!livePrinter;},300);

function loadSwapSettings(){const cfg=state?.printers.find(p=>p.name===selectedPrinter)?.plate_swap||{};$('swapEnabled').checked=!!cfg.enabled;$('swapModel').value=cfg.model||'A1 mini';$('swapSpares').value=cfg.spares||0;renderSwapStatus();}
function renderSwapStatus(){const cfg=state?.printers.find(p=>p.name===selectedPrinter)?.plate_swap;$('swapStatus').textContent=cfg?.enabled?`${cfg.model} kit enabled · ${cfg.spares} estimated magazine plates · ${cfg.verified?'Starting setup checked':'Starting setup check needed'}`:'Disabled for this printer — manual queue workflow.';}
setInterval(()=>{if($('detailDialog').open&&state)renderSwapStatus();},1000);
$('swapForm').onsubmit=e=>{e.preventDefault();const name=selectedPrinter,data={enabled:$('swapEnabled').checked,model:$('swapModel').value,spares:Number($('swapSpares').value),confirmed:true};confirmAction('Save plate-swap settings?',printerLabel(name)+' — '+(data.enabled?'enable '+data.model+' kit':'disable kit')+'; '+data.spares+' magazine plates. Existing plate checks and file approvals will be reset.','I verified the installed hardware and actual spare count.',async()=>{await api('plateswap/'+encodeURIComponent(name)+'/configure',data);notice('Saved for this printer. Approve the prepared Swaplist batch and check its starting setup before starting.');});};
$('swapChecked').onclick=()=>{const name=selectedPrinter;confirmAction('Swapmod starting setup checked?',printerLabel(name)+' — this records your inspection; no swap movement is sent.','I checked the starting setup against Swaplist instructions, loaded the required magazine plates and cleared the ejection path.',async()=>{await api('plateswap/'+encodeURIComponent(name)+'/check',{confirmed:true});notice('Setup checked. Start the approved Swaplist batch when ready.');});};

let swapApprovalJob=null;
function openSwapApproval(id){const j=state.jobs.find(x=>x.id===id);swapApprovalJob=id;$('swapApproveText').textContent=j.label+' — '+printerLabel(j.printer);$('swapBatchPlates').value=j.options.swap_plates||1;$('swapBatchConfirm').checked=false;$('swapApproveDialog').showModal();}
$('swapApproveForm').onsubmit=async e=>{e.preventDefault();try{await api('jobs/'+swapApprovalJob+'/swapapprove',{confirmed:$('swapBatchConfirm').checked,plates:Number($('swapBatchPlates').value)});$('swapApproveDialog').close();notice('Swaplist batch approved.');await refresh();}catch(e){notice(e.message);}};
