'use strict';
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state=null, csrf='', selectedPrinter=null, cameraUrl=null, polling=false, settingsLoaded=false;
let pendingConfirmation=null;
const terminal=new Set(['finished','failed','cancelled']);
async function api(path,body,method='POST'){
 const headers={'X-PM':'1'};
 if(csrf)headers['X-CSRF']=csrf;
 let payload=body;
 if(body!==undefined&&!(body instanceof FormData)){headers['Content-Type']='application/json';payload=JSON.stringify(body);}
 const r=await fetch('/api/'+path,{method,headers,body:payload});
 let result;try{result=await r.json();}catch{throw new Error('The server returned an unexpected response.');}
 if(!r.ok){if(r.status===401)showLogin();throw new Error(result.error||'Request failed');}
 return result;
}
function showLogin(){ $('app').hidden=true;$('loginScreen').hidden=false;state=null;settingsLoaded=false; }
function notice(text){$('notice').textContent=text;$('notice').hidden=false;setTimeout(()=>$('notice').hidden=true,7000);}
function tab(name){document.querySelectorAll('.tab').forEach(n=>n.hidden=n.id!==name);document.querySelectorAll('nav button').forEach(n=>n.classList.toggle('active',n.dataset.tab===name));$('crumb').textContent=name.toUpperCase();$('pageTitle').textContent={overview:'Your print room.',queue:'Make room for the next idea.',activity:'Every update, together.',settings:'Set up your workspace.',team:'Keep the team in sync.',devices:'Your laptops, connected.',server:'The system behind it all.'}[name];}
function printerLabel(name){return state?.printers.find(p=>p.name===name)?.display_name||name;}
function statusClass(s,online=true){return !online||['FAILED','failed','needs_review'].includes(s)?'bad':['PAUSE','paused','PREPARE','staging','awaiting_start'].includes(s)?'warn':['RUNNING','printing'].includes(s)?'blue':'';}
// Requested but not yet confirmed: the button pulses purple, a colour no result state uses, until the
// printer reports the result (then it shows the real state, e.g. amber for light on) or the time runs out.
const pending={};
function setPending(key,ms,done,label,extra={}){pending[key]={until:Date.now()+ms,done,label,...extra};}
function isPending(key,p){const x=pending[key];if(!x)return false;
 if(p&&x.done(p)){delete pending[key];return false;}
 if(Date.now()>x.until){delete pending[key];if(x.label)notice(x.label+': the printer hasn\'t reported the change. Check the printer.');return false;}
 return true;}
function pollSoon(){for(const ms of [1500,4000,7000,10500,15000])setTimeout(refresh,ms);}
function reportedLight(p){return ((p.data||{}).lights_report||[]).find(l=>l.node==='chamber_light')?.mode==='on';}
function lightShown(p){const key='light|'+p.name;return isPending(key,p)?{on:pending[key].on,busy:true}:{on:reportedLight(p),busy:false};}
async function toggleLight(name){const p=state.printers.find(x=>x.name===name),on=!lightShown(p).on;
 setPending('light|'+name,10000,q=>reportedLight(q)===on,printerLabel(name)+' light',{on});render();
 try{await api('printers/'+encodeURIComponent(name)+'/'+(on?'lighton':'lightoff'),{});}catch(e){delete pending['light|'+name];notice(e.message);render();return;}
 pollSoon();}
// Pause / resume / stop pulse until the printer's state changes.
const ACTIVE=['RUNNING','PAUSE','PREPARE'];
const PRINT_DONE={pause:p=>p.state==='PAUSE'||!ACTIVE.includes(p.state),resume:p=>p.state==='RUNNING'||p.state==='PREPARE'||!ACTIVE.includes(p.state),stop:p=>!ACTIVE.includes(p.state)};
async function printAction(name,action,body={}){setPending(action+'|'+name,20000,PRINT_DONE[action],printerLabel(name)+' '+action);render();
 try{await api('printers/'+encodeURIComponent(name)+'/'+action,body);}catch(e){delete pending[action+'|'+name];render();throw e;}
 pollSoon();}
function pendingClass(action,p){return isPending(action+'|'+p.name,p)?' pending':'';}
function confirmPause(name){confirmAction('Pause this print?',printerLabel(name)+' — the print pauses until you resume it.','I want to pause this print.',()=>printAction(name,'pause'));}
function confirmStop(name){confirmAction('Stop this print?',printerLabel(name)+' — this cancels the current job.','I want to cancel this print.',()=>printAction(name,'stop',{confirmed:true}));}
function actionButton(action,label,extra='',cls=''){return `<button class="${cls}" data-action="${action}" ${extra}>${label}</button>`;}
function render(){
 const p=state.printers,j=state.jobs;
 $('printerSubtitle').textContent=state.demo?'Demo data. No real printers are controlled.':'Live telemetry. Shared control with Discord.';
 $('mode').textContent=state.demo?'DEMO MODE':'LIVE';$('mode').classList.toggle('demo',state.demo);
 $('discordState').textContent=state.discord?'Discord connected':'Discord offline · Dashboard available';
 $('stats').innerHTML=[['Printers online',p.filter(x=>x.connected).length+' / '+p.length],['Printing now',p.filter(x=>x.state==='RUNNING').length],['Waiting in queue',j.filter(x=>x.status==='queued').length],['Need attention',p.filter(x=>x.error||!x.connected).length+j.filter(x=>x.status==='needs_review').length]].map(([label,n],i)=>`<div class="stat"><small>${label}</small><strong class="${i===1?'green':''}">${n}</strong></div>`).join('');
 $('printers').innerHTML=p.map(printerCard).join('')||'<div class="empty">No printers configured.</div>'
 applyStyles($('printers'));
 const names=p.map(x=>x.name);
 for(const id of ['jobPrinter','queueFilter']){
  const element=$(id),value=element.value;
  if(element.dataset.names!==JSON.stringify(names.map(n=>[n,printerLabel(n)]))){
   element.innerHTML=(id==='queueFilter'?'<option value="">All printers</option>':'')+names.map(n=>`<option value="${esc(n)}">${esc(printerLabel(n))}</option>`).join('');
   if(names.includes(value)||id==='queueFilter')element.value=value;
   element.dataset.names=JSON.stringify(names.map(n=>[n,printerLabel(n)]));
  }
 }
 renderJobs();
 $('events').innerHTML=state.events.map(e=>`<div class="event"><small>${new Date(e.time*1000).toLocaleString()}</small><div><strong>${esc(e.title)} · ${esc(printerLabel(e.printer)||'System')}</strong><p>${esc(e.detail)}</p></div></div>`).join('')||'<div class="empty">Events will appear here as printers report and jobs change.</div>';
 if(!settingsLoaded){$('notificationChannel').value=state.settings.notification_channel_id||'';$('commandsChannel').value=state.settings.commands_channel_id||'';$('adminIds').value=(state.settings.admin_user_ids||[]).join('\n');settingsLoaded=true;}
 $('refreshed').textContent='Updated '+new Date().toLocaleTimeString();
 if(selectedPrinter&&$('detailDialog').open)renderDetails();
}
function renderJobs(){
 if(!state)return;const filter=$('queueFilter').value,jobs=state.jobs.filter(j=>!filter||j.printer===filter);
 const row=j=>{const q=`data-job="${esc(j.id)}"`;let buttons='';
  if(j.status==='queued'){if(state.printers.find(p=>p.name===j.printer)?.plate_swap?.enabled)buttons+=actionButton('swapapprove','Approve Swaplist batch…',q);const first=state.jobs.find(x=>x.printer===j.printer&&x.status==='queued');if(first?.id===j.id)buttons+=actionButton('start','Start next',q,'primary')+actionButton('startOverride','Start ignoring error',q,'danger');buttons+=actionButton('up','↑',q)+actionButton('down','↓',q)+actionButton('remove','Remove',q);}
  if(j.status==='needs_review')buttons+=['finished','failed','cancelled'].map(o=>actionButton('resolve','Mark '+o,`${q} data-outcome="${o}"`)).join('');
  if(terminal.has(j.status))buttons+=actionButton('reprint','Queue again',q);
  if(terminal.has(j.status)&&state.printers.length>1)buttons+=actionButton('sendto','Print on another printer…',q);
  return `<div class="job-row"><div class="job-info"><strong>${j.demo?'🧪 DEMO · ':''}${esc(j.label)}</strong><small>${esc(printerLabel(j.printer))} · Plate ${j.options.plate} · ${j.options.use_ams?'AMS '+esc(j.options.ams_mapping.join(', ')):'External spool'}</small><small>${esc(j.id)} · Added by ${esc(j.author)}</small>${j.note?`<div class="job-note">${esc(j.note)}</div>`:''}</div><span class="state ${statusClass(j.status)}">${esc(j.status.replaceAll('_',' '))}</span><div class="job-actions">${buttons}</div></div>`;};
 $('jobs').innerHTML=jobs.filter(j=>!terminal.has(j.status)).map(row).join('')||'<div class="empty">The queue is clear. Add a print here or use /queueadd in Discord.</div>';
 $('history').innerHTML=jobs.filter(j=>terminal.has(j.status)).sort((a,b)=>b.updated-a.updated).slice(0,30).map(row).join('')||'<p class="muted">Completed and removed jobs will appear here.</p>';
}
async function refresh(){if(polling)return;polling=true;try{state=await api('state',undefined,'GET');csrf=state.csrf;$('loginScreen').hidden=true;$('app').hidden=false;$('connectionBanner').hidden=true;render();}catch(e){if(!$('app').hidden){$('connectionBanner').textContent='Connection interrupted. Displayed readings may be stale. '+e.message;$('connectionBanner').hidden=false;}}finally{polling=false;}}
function confirmAction(title,text,label,callback){$('confirmTitle').textContent=title;$('confirmText').textContent=text;$('confirmLabel').textContent=label;$('confirmCheck').checked=false;pendingConfirmation=callback;$('confirmDialog').showModal();}
function renderDetails(){if(typeof renderDevice==='function')renderDevice();}
function fmtMinutes(v){const n=Number(v);if(v===null||v===undefined||v===''||!Number.isFinite(n))return '';return n>=60?Math.floor(n/60)+' h '+n%60+' min':n+' min';}
function miniSwatches(d){const ams=Array.isArray(d.ams)?{ams:d.ams}:(d.ams||{}),trays=(ams.ams||[]).flatMap(u=>(u.tray||[]).map(t=>t));if(d.vt_tray)trays.push(d.vt_tray);
 return trays.slice(0,17).map(t=>{const c=/^[0-9a-f]{6}/i.test(t.tray_color||'')&&t.tray_type?'#'+t.tray_color.slice(0,6):'';return `<i class="mini-swatch${c?'':' is-empty'}"${c?` data-color="${c}"`:''} title="${esc(t.tray_type||'Empty')}"></i>`;}).join('');}
// The Content-Security-Policy blocks inline style attributes, so colours and widths are applied through the DOM.
function applyStyles(root){root.querySelectorAll('[data-color]').forEach(e=>e.style.background=e.dataset.color);root.querySelectorAll('[data-width]').forEach(e=>e.style.width=e.dataset.width+'%');}
// AI failure watch (#70): only shown on a card while it's watching a print or has something to say.
const AI_LABELS={watching:'🤖 AI watching',suspect:'🤖 AI: suspect frames',failure:'🤖 AI: print may be failing',paused:'🤖 AI paused this print',unavailable:'🤖 AI HAT unavailable'};
function aiLine(ai){if(!ai?.enabled||!ai.watching||!AI_LABELS[ai.status])return '';const bad=['failure','paused'].includes(ai.status);
 return `<p class="ai-line ai-${esc(ai.status)}" title="${esc(ai.message||'')}">${AI_LABELS[ai.status]}${ai.status==='suspect'||bad?` · ${esc(ai.failing??0)}/${esc(ai.frames??0)} frames`:''}</p>`;}
function printerCard(x){
 const d=x.data||{},n=`data-printer="${esc(x.name)}"`,progress=Math.max(0,Math.min(100,Number(d.mc_percent)||0)),active=['RUNNING','PAUSE','PREPARE'].includes(x.state);
 const waiting=state.jobs.filter(v=>v.printer===x.name&&v.status==='queued').length,left=fmtMinutes(d.mc_remaining_time);
 const round=v=>v===null||v===undefined||v===''||!Number.isFinite(Number(v))?'—':Math.round(Number(v));
 const temp=(now,target)=>`<b>${round(now)}°</b>${Number(target)?`<em>→ ${round(target)}°</em>`:''}`;
 const lt=lightShown(x),light=lt.on;
 let actions='';
 if(x.state==='RUNNING'||x.state==='PREPARE')actions+=actionButton('pause','⏸ Pause',n,pendingClass('pause',x));
 if(x.state==='PAUSE')actions+=actionButton('resume','▶ Resume',n,'primary'+pendingClass('resume',x));
 if(active)actions+=actionButton('stop','■ Stop',n,'danger'+pendingClass('stop',x));
 actions+=actionButton(light?'lightoff':'lighton','💡',`${n} aria-label="Turn light ${light?'off':'on'}" aria-pressed="${light}" title="Chamber light"`,'icon-btn'+(light?' on':'')+(lt.busy?' pending':''));
 return `<article class="printer-card${x.connected?'':' offline'}" ${n} tabindex="0" role="button" aria-label="Open ${esc(x.display_name||x.name)}">
<div class="pc-head"><h3>${esc(x.display_name||x.name)}</h3><span class="state ${statusClass(x.state,x.connected)}">${esc(x.connected?x.state:'OFFLINE')}</span></div>
${x.has_camera?`<div class="pc-cam">${x.snapshot?.time?`<img src="/api/snapshot/${encodeURIComponent(x.name)}?t=${x.snapshot.time}" alt="${esc(x.display_name||x.name)} camera"><small class="pc-cam-time">📷 ${new Date(x.snapshot.time*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}</small>`:`<small class="pc-cam-msg">${esc(x.snapshot?.error||'Waiting for the first snapshot…')}</small>`}</div>`:''}
<div class="pc-file">${esc(d.subtask_name||(active?'Unknown file':'Ready for the next job'))}</div>
${active?`<div class="progress"><progress value="${progress}" max="100"></progress></div><div class="pc-meta"><span>${progress}%</span><span>${d.layer_num!=null?`Layer ${esc(d.layer_num)}/${esc(d.total_layer_num??'?')}`:''}</span><span>${left?esc(left)+' left':''}</span></div>`:''}
<div class="pc-stats"><span><small>Nozzle</small>${temp(d.nozzle_temper,d.nozzle_target_temper)}</span><span><small>Bed</small>${temp(d.bed_temper,d.bed_target_temper)}</span><span class="pc-ams">${miniSwatches(d)}</span></div>
${x.error_text?`<p class="printer-error">${esc(x.error_text)}</p>`:''}${aiLine(x.ai)}
<div class="pc-actions">${actions}<span class="spacer"></span>${actionButton('printerQueue',waiting?`${waiting} queued →`:'Queue →',n,'ghost')}</div></article>`;}
async function downloadFileListing(printer,button){if(button)button.disabled=true;notice('Reading printer storage…');
 try{const r=await api('files/'+encodeURIComponent(printer),undefined,'GET'),url=URL.createObjectURL(new Blob([r.text],{type:'text/plain'})),a=document.createElement('a');
  a.href=url;a.download='printer-files.txt';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);notice('Printer file listing downloaded.');}
 catch(e){notice(e.message);}finally{if(button)button.disabled=false;}}
function openPrinter(printer){selectedPrinter=printer;loadSwapSettings();$('cameraImage').hidden=true;$('cameraMessage').textContent='';
 $('detailDialog').querySelector('.drawer-body').scrollTop=0;renderDetails();if(!$('detailDialog').open)$('detailDialog').showModal();
 if(typeof showCamera==='function')showCamera();}
// Popups: click outside or press Esc to close, with a short animation.
function closeDialog(d){if(!d.open||d.dataset.closing)return;d.dataset.closing='1';
 const done=()=>{if(!d.dataset.closing)return;delete d.dataset.closing;d.classList.remove('closing');d.close();};
 if(matchMedia('(prefers-reduced-motion: reduce)').matches){done();return;}
 d.classList.add('closing');d.addEventListener('animationend',done,{once:true});setTimeout(done,320);}
function outsideDialog(d,e){const r=d.getBoundingClientRect();return e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom;}
document.querySelectorAll('dialog').forEach(d=>{let downOutside=false;
 d.addEventListener('pointerdown',e=>{downOutside=e.target===d&&outsideDialog(d,e);});
 d.addEventListener('click',e=>{if(e.target===d&&downOutside&&outsideDialog(d,e))closeDialog(d);downOutside=false;});
 d.addEventListener('cancel',e=>{e.preventDefault();closeDialog(d);});});
$('loginForm').addEventListener('submit',async e=>{e.preventDefault();$('loginError').textContent='';try{const r=await api('login',{password:$('loginPassword').value});csrf=r.csrf;$('loginPassword').value='';await refresh();}catch(e){$('loginError').textContent=e.message;}});
$('logout').onclick=async()=>{try{await api('logout',{});showLogin();}catch(e){notice(e.message);}};
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>closeDialog($(b.dataset.close)));
$('queueFilter').onchange=renderJobs;
// ---- Queue a print (#7): the file is uploaded and checked as soon as it's picked; plates are chosen from cards,
// the AMS mapping is suggested from the printer's loaded trays, and Print now skips the queue.
let jobUpload=null,jobPlate=null;
function resetJobUpload(){jobUpload=null;jobPlate=null;$('jobPlates').hidden=true;$('jobPlates').innerHTML='';$('jobFileNote').textContent='';$('jobAmsHint').textContent='';}
$('addJob').onclick=()=>{$('jobError').textContent='';$('jobDialog').showModal();};
$('source').onchange=()=>{const upload=$('source').value==='upload';$('uploadLabel').hidden=!upload;$('remoteLabel').hidden=upload;$('jobPlateLabel').hidden=upload;
 $('jobPlates').hidden=!upload||!jobUpload;$('jobFileNote').hidden=!upload;$('jobAmsHint').hidden=!upload;};
$('jobFile').onchange=async()=>{const f=$('jobFile').files[0];resetJobUpload();$('jobError').textContent='';if(!f)return;
 if(!$('jobLabel').value)$('jobLabel').value=f.name.replace(/\.gcode\.3mf$|\.3mf$/i,'').slice(0,120);
 if(f.size>256*1024*1024){$('jobError').textContent='Maximum upload is 256 MiB.';$('jobFile').value='';return;}
 $('jobFileNote').textContent='Uploading and checking the file…';
 try{const form=new FormData();form.append('file',f);jobUpload=await api('upload',form);renderPlates();}
 catch(e){$('jobFileNote').textContent='';$('jobError').textContent=e.message;$('jobFile').value='';}};
function renderPlates(){const sliced=jobUpload.plates.filter(p=>p.sliced);if(!sliced.some(p=>p.index===jobPlate))jobPlate=sliced[0]?.index;
 $('jobFileNote').textContent=(jobUpload.model?`Sliced for the ${jobUpload.model}. `:'')+(sliced.length===1?'One sliced plate.':`${sliced.length} sliced plates: choose one.`);
 $('jobPlates').innerHTML=jobUpload.plates.map(p=>`<button type="button" class="plate-card${p.index===jobPlate?' active':''}" data-plate="${p.index}"${p.sliced?'':' disabled'}>`+
  (p.thumbnail?`<img alt="" src="/api/uploads/${encodeURIComponent(jobUpload.asset)}/plate/${p.index}">`:'<span class="plate-noimg">No preview</span>')+
  `<strong>${esc(p.name)}</strong><small>${p.sliced?[fmtMinutes(p.prediction!=null?Math.round(p.prediction/60):null),p.weight!=null?Math.round(p.weight)+' g':''].filter(Boolean).join(' · ')||'Sliced':'Not sliced'}</small>`+
  `<span class="plate-fils">${(p.filaments||[]).map(f=>`<i class="fil"${f.colour?` data-color="${esc(f.colour)}"`:''} title="${esc(f.type)} ${esc(f.colour)}"></i>`).join('')}</span></button>`).join('');
 $('jobPlates').hidden=false;applyStyles($('jobPlates'));suggestAms();}
$('jobPlates').addEventListener('click',e=>{const b=e.target.closest('[data-plate]');if(!b||b.disabled)return;jobPlate=Number(b.dataset.plate);renderPlates();});
$('jobPrinter').addEventListener('change',()=>{if(jobUpload)suggestAms();});
async function suggestAms(){if(!jobUpload||!jobPlate)return;const asset=jobUpload.asset,printer=$('jobPrinter').value;$('jobAmsHint').textContent='Checking the AMS on that printer…';
 try{const r=await api(`uploads/${encodeURIComponent(asset)}/suggest?printer=${encodeURIComponent(printer)}&plate=${jobPlate}`,undefined,'GET');
  if(jobUpload?.asset!==asset||$('jobPrinter').value!==printer)return;
  $('jobError').textContent=r.model_warning||'';
  if(r.complete){$('jobAms').checked=true;$('jobMapping').value=r.mapping;}
  $('jobAmsHint').textContent=r.message+(r.rows.length?' '+r.rows.map(x=>`Filament ${x.filament} (${x.type||'?'}) → ${x.tray_label||'no matching slot'}`).join(' · '):'');}
 catch(e){$('jobAmsHint').textContent=e.message;}}
function jobData(){const data={printer:$('jobPrinter').value,label:$('jobLabel').value,use_ams:$('jobAms').checked,mapping:$('jobAms').checked?$('jobMapping').value:'',bed:$('jobBed').value};
 if($('source').value==='upload'){if(!jobUpload)throw new Error($('jobFile').files[0]?'Wait for the file check to finish.':'Select a sliced .3mf file.');if(!jobPlate)throw new Error('Choose a plate.');data.asset=jobUpload.asset;data.plate=jobPlate;}
 else{data.remote=$('jobRemote').value;data.plate=Number($('jobPlate').value);}
 return data;}
function jobDone(message){$('jobDialog').close();$('jobForm').reset();resetJobUpload();$('source').onchange();notice(message);tab('queue');}
$('jobForm').onsubmit=async e=>{e.preventDefault();$('jobError').textContent='';$('submitJob').disabled=true;
 try{await api('jobs',jobData());jobDone('Added to the shared queue.');await refresh();}catch(e){$('jobError').textContent=e.message;}finally{$('submitJob').disabled=false;}};
$('printNow').onclick=()=>{$('jobError').textContent='';if(!$('jobForm').reportValidity())return;let data;try{data=jobData();}catch(e){$('jobError').textContent=e.message;return;}
 confirmAction('Print now?',`${data.label} on ${printerLabel(data.printer)}, plate ${data.plate}, straight away. ${data.use_ams?'AMS mapping: '+data.mapping+'.':'External spool.'}`,
  'The plate is clear, the correct material is loaded, and this file was sliced for this printer.',
  async()=>{await api('jobs',{...data,print_now:true,confirmed:true});jobDone(`Starting on ${printerLabel(data.printer)}.`);});};
$('confirmForm').onsubmit=async e=>{e.preventDefault();const callback=pendingConfirmation;pendingConfirmation=null;$('confirmDialog').close();if(callback){try{await callback();await refresh();}catch(e){notice(e.message);}}};
document.addEventListener('click',async e=>{const b=e.target.closest('[data-action]');if(!b){const card=e.target.closest('.printer-card[data-printer]');if(card)openPrinter(card.dataset.printer);return}const {action,printer,job,outcome}=b.dataset;try{
 if(action==='files'){await downloadFileListing(printer,b);return}
 if(action==='details'){openPrinter(printer);return}
 if(action==='printerQueue'){$('queueFilter').value=printer;tab('queue');renderJobs();return;}
 if(action==='pause'){confirmPause(printer);return;}
 if(action==='lighton'||action==='lightoff'){await toggleLight(printer);return;}
 if(action==='resume'){await printAction(printer,'resume');return;}
 if(action==='stop'){confirmStop(printer);return;}
 if(action==='swapapprove'){openSwapApproval(job);return;}
 if(action==='sendto'){openSendTo(job);return;}
 if(action==='startOverride'){const j=state.jobs.find(x=>x.id===job);confirmAction('Start ignoring reported error?',`${j.label} on ${printerLabel(j.printer)}. This bypasses the management error check and allows FAILED state. It does not clear the printer error or override firmware protections.`,'I inspected the printer, cleared the plate, and verified the material and sliced file. Start despite the reported error.',()=>api('jobs/'+job+'/start',{confirmed:true,override_error:true}));return;}
 if(action==='start'){const j=state.jobs.find(x=>x.id===job);confirmAction('Start next print?',`${j.label} on ${printerLabel(j.printer)}. Plate ${j.options.plate}. ${j.options.use_ams?'AMS mapping: '+j.options.ams_mapping.join(', '):'External spool'}.`,'The plate is clear, the correct material is loaded, and this file was sliced for this printer.',()=>api('jobs/'+job+'/start',{confirmed:true}));return;}
 if(action==='resolve'){confirmAction('Record the verified outcome?',`Mark ${job} as ${outcome}. This records a result; it does not send any command to the printer.`,'I inspected the physical printer and verified this outcome.',()=>api('jobs/'+job+'/resolve',{confirmed:true,outcome}));return;}
 if(action==='remove'){confirmAction('Remove this queued job?',job,'Remove this waiting job from the queue.',()=>api('jobs/'+job+'/remove',{}));return;}
 if(['up','down','reprint'].includes(action)){b.classList.add('pending');await api('jobs/'+job+'/'+action,{});if(action==='reprint')notice('A new copy was added to the queue.');}
 await refresh();
 }catch(e){notice(e.message);}});
$('takeSnapshot').onclick=async()=>{$('takeSnapshot').disabled=true;$('cameraMessage').textContent='Requesting snapshot…';try{const r=await fetch('/api/camera/'+encodeURIComponent(selectedPrinter));if(!r.ok){const data=await r.json();throw new Error(data.error||'Camera unavailable.');}if(cameraUrl)URL.revokeObjectURL(cameraUrl);cameraUrl=URL.createObjectURL(await r.blob());$('cameraImage').src=cameraUrl;$('cameraImage').hidden=false;$('cameraMessage').textContent='Snapshot captured '+new Date().toLocaleTimeString();}catch(e){$('cameraMessage').textContent=e.message;}finally{$('takeSnapshot').disabled=false;}};
$('settingsForm').onsubmit=async e=>{e.preventDefault();try{await api('settings',{notification_channel_id:$('notificationChannel').value,commands_channel_id:$('commandsChannel').value,admin_user_ids:$('adminIds').value});notice('Settings saved.');}catch(e){notice(e.message);}};
$('passwordForm').onsubmit=async e=>{e.preventDefault();try{await api('password',{password:$('newPassword').value});$('newPassword').value='';showLogin();}catch(e){notice(e.message);}};
$('testNotification').onclick=async()=>{try{await api('testnotification',{});notice('Test requested. Check Discord and the activity feed.');}catch(e){notice(e.message);}};
refresh();setInterval(()=>{if(state&&!document.hidden)refresh();},5000);
let permissionState=null;
function renderPermissions(){const r=permissionState;$('memberRoleIds').value=r.member_role_ids.join('\n');$('permissionsRows').innerHTML=r.commands.map(c=>{const options=Object.entries(r.levels).filter(([k])=>!c.locked||k==='admin'||k==='disabled').map(([k,v])=>`<option value="${esc(k)}"${k===c.level?' selected':''}>${esc(v)}${k===c.default?' (default)':''}</option>`).join('');const names=c.subcommands.map(n=>'/'+n).join(', ');return `<tr class="${c.level!==c.default?'changed':''}"><td><strong>${c.locked?'🔒 ':''}/${esc(c.command)}</strong>${names!=='/'+c.command?`<small>${esc(names)}</small>`:''}<small>${esc(c.description)}</small></td><td><select data-command="${esc(c.command)}" aria-label="Who can use /${esc(c.command)}">${options}</select></td></tr>`;}).join('');}
async function loadPermissions(){try{permissionState=await api('permissions',undefined,'GET');renderPermissions();}catch(e){$('permissionsRows').innerHTML='<tr><td colspan="2" class="error">'+esc(e.message)+'</td></tr>';}}
function permissionPayload(reset){const levels={};document.querySelectorAll('#permissionsRows select').forEach(s=>{const c=permissionState.commands.find(x=>x.command===s.dataset.command);levels[s.dataset.command]=reset?c.default:s.value;});return {levels,member_role_ids:$('memberRoleIds').value};}
async function savePermissions(reset){try{const r=await api('permissions',permissionPayload(reset));permissionState=r;renderPermissions();$('permissionsStatus').textContent=r.changed.length?'Saved: '+r.changed.join('; '):'No changes.';}catch(e){$('permissionsStatus').textContent=e.message;}}
$('permissionsForm').onsubmit=e=>{e.preventDefault();const opened=[...document.querySelectorAll('#permissionsRows select')].filter(s=>s.value==='everyone'&&permissionState.commands.find(c=>c.command===s.dataset.command).default!=='everyone').map(s=>'/'+s.dataset.command);if(opened.length)confirmAction('Open commands to everyone?','These admin commands will be usable by every member: '+opened.join(', '),'I understand every server member will be able to use these commands.',()=>savePermissions(false));else savePermissions(false);};
$('resetPermissions').onclick=()=>confirmAction('Reset Discord permissions?','Every command goes back to its default permission.','Reset all commands to their defaults.',()=>savePermissions(true));
const tabWithPermissions=tab;tab=function(name){tabWithPermissions(name);if(name==='settings')loadPermissions();};
async function loadErrors(){const box=$('recentErrors');try{const r=await api('errors',undefined,'GET');box.innerHTML=r.errors.length?r.errors.map(e=>`<div class="event"><strong>${esc(new Date(e.time*1000).toLocaleString())} · ${esc(e.level)}${e.error_id?' · ID '+esc(e.error_id):''}</strong><pre>${esc(e.text)}</pre></div>`).join(''):'<p class="muted">No errors since the service started.</p>';}catch(e){box.innerHTML='<p class="error">'+esc(e.message)+'</p>';}}
$('downloadDiagnostics').onclick=()=>{location.href='/api/diagnostics';};
$('refreshErrors').onclick=loadErrors;
const originalTab=tab;tab=function(name){originalTab(name);if(name==='settings')loadErrors();};
async function loadIssueSettings(){try{const r=await api('report/settings',undefined,'GET');$('issueDestination').innerHTML=Object.entries(r.destinations).map(([k,v])=>`<option value="${esc(k)}"${k===r.destination?' selected':''}>${esc(v)}</option>`).join('');$('issueRepository').value=r.repository;$('issueReportTarget').textContent=`Reports become ${r.destinations[r.destination]||r.destination}s in ${r.repository}.`+(r.has_token?'':' Add a GitHub token under “Report destination” first.');}catch(e){$('issueReportTarget').textContent=e.message;}}
$('issueReportForm').onsubmit=e=>{e.preventDefault();confirmAction('Send problem report?','Title: '+$('issueTitle').value,'I checked the description and understand the report may be public.',async()=>{$('issueReportStatus').textContent='Sending…';try{const r=await api('report',{title:$('issueTitle').value,description:$('issueDescription').value});$('issueReportStatus').innerHTML='Sent: <a href="'+esc(r.url)+'" target="_blank" rel="noopener">'+esc(r.url)+'</a>';$('issueTitle').value='';$('issueDescription').value='';}catch(err){$('issueReportStatus').textContent=err.message;}});};
$('saveIssueSettings').onclick=async()=>{try{await api('report/settings',{destination:$('issueDestination').value,repository:$('issueRepository').value,token:$('issueToken').value,clear_token:$('issueClearToken').checked});$('issueToken').value='';$('issueClearToken').checked=false;notice('Report destination saved.');loadIssueSettings();}catch(e){notice(e.message);}};
const tabWithErrors=tab;tab=function(name){tabWithErrors(name);if(name==='settings')loadIssueSettings();};

// Keyboard: Enter or Space on a focused printer card opens it.
$('printers').addEventListener('keydown',e=>{const card=e.target.closest?.('.printer-card[data-printer]');if(card&&e.target===card&&(e.key==='Enter'||e.key===' ')){e.preventDefault();openPrinter(card.dataset.printer);}});

// ---- Send a finished print to another printer's queue (#57) -----------------------------------
// The server decides which printers suit the file (same model it was sliced for); unknown models need a tick.
let sendJob=null,sendTargets=[],sendFrom=null;
async function openSendTo(id){const j=state.jobs.find(x=>x.id===id);if(!j)return;sendJob=id;sendTargets=[];sendFrom=null;
 $('sendToText').textContent=`${j.label} — printed on ${printerLabel(j.printer)}.`;$('sendToSliced').textContent='Checking the file…';
 $('sendToTarget').innerHTML='';$('sendToAms').checked=!!j.options.use_ams;$('sendToMapping').value=(j.options.ams_mapping||[]).join(',');
 $('sendToChecked').checked=false;updateSendTo();$('sendToDialog').showModal();
 try{const r=await api('jobs/'+encodeURIComponent(id)+'/targets',undefined,'GET');if(sendJob!==id)return;sendTargets=r.targets;
  sendFrom=r.remote?r.from:null;
  $('sendToSliced').textContent=(r.sliced_for?r.how+': '+r.sliced_for+'.':"The file doesn't say which printer it was sliced for.")+(r.remote?` The file is on ${printerLabel(r.from)}; it's copied over when you send it, so that printer must be on.`:'');
  $('sendToTarget').innerHTML=r.targets.map(t=>`<option value="${esc(t.name)}"${t.compatible===false?' disabled':''}>${esc(t.display)}${t.model?' ('+esc(t.model)+')':''}${t.compatible===false?' — not compatible':t.compatible===null?' — check first':''}</option>`).join('');
  const first=r.targets.find(t=>t.compatible===true)||r.targets.find(t=>t.compatible===null);if(first)$('sendToTarget').value=first.name;updateSendTo();}
 catch(e){$('sendToSliced').textContent='';$('sendToReason').textContent=e.message;}}
function updateSendTo(){const t=sendTargets.find(x=>x.name===$('sendToTarget').value&&x.compatible!==false);
 $('sendToReason').textContent=t?t.reason:(sendTargets.length?'No printer can take this file: '+(sendTargets[0]?.reason||''):'');
 $('sendToCheckRow').hidden=!(t&&t.compatible===null);$('sendToCheckLabel').textContent=t?`I checked this file was sliced for ${t.display}.`:'';
 $('sendToSubmit').disabled=!t;$('sendToMapping').disabled=!$('sendToAms').checked;}
$('sendToTarget').onchange=updateSendTo;$('sendToAms').onchange=updateSendTo;
$('sendToForm').onsubmit=async e=>{e.preventDefault();const target=$('sendToTarget').value;$('sendToSubmit').classList.add('pending');
 if(sendFrom)$('sendToReason').textContent=`Copying the file from ${printerLabel(sendFrom)}…`;
 try{await api('jobs/'+encodeURIComponent(sendJob)+'/sendto',{printer:target,use_ams:$('sendToAms').checked,mapping:$('sendToAms').checked?$('sendToMapping').value:'',checked:$('sendToChecked').checked});
  $('sendToDialog').close();notice(`Added to ${printerLabel(target)}'s queue. Start it from the queue when that printer is ready.`);await refresh();}
 catch(e){$('sendToReason').textContent=e.message;}finally{$('sendToSubmit').classList.remove('pending');}};
