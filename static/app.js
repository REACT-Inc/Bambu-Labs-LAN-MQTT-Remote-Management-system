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
function actionButton(action,label,extra='',cls=''){return `<button class="${cls}" data-action="${action}" ${extra}>${label}</button>`;}
function render(){
 const p=state.printers,j=state.jobs;
 $('printerSubtitle').textContent=state.demo?'Demo data. No real printers are controlled.':'Live telemetry. Shared control with Discord.';
 $('mode').textContent=state.demo?'DEMO MODE':'LIVE';$('mode').classList.toggle('demo',state.demo);
 $('discordState').textContent=state.discord?'Discord connected':'Discord offline · Dashboard available';
 $('stats').innerHTML=[['Printers online',p.filter(x=>x.connected).length+' / '+p.length],['Printing now',p.filter(x=>x.state==='RUNNING').length],['Waiting in queue',j.filter(x=>x.status==='queued').length],['Need attention',p.filter(x=>x.error||!x.connected).length+j.filter(x=>x.status==='needs_review').length]].map(([label,n],i)=>`<div class="stat"><small>${label}</small><strong class="${i===1?'green':''}">${n}</strong></div>`).join('');
 $('printers').innerHTML=p.map(x=>{
  const d=x.data||{},n=`data-printer="${esc(x.name)}"`,progress=Math.max(0,Math.min(100,Number(d.mc_percent)||0));
  const waiting=j.filter(v=>v.printer===x.name&&v.status==='queued').length;
  return `<article class="printer-card"><div class="printer-top"><h3>${esc(x.display_name||x.name)}</h3><span class="state ${statusClass(x.state,x.connected)}">${esc(x.connected?x.state:'OFFLINE')}</span></div><div class="printer-body"><div class="printer-symbol">▤</div><div class="filename">${esc(d.subtask_name||'Ready for the next job')}</div><div class="progress-label"><span>${progress}% complete</span><span>${esc(d.mc_remaining_time??'—')} min left</span></div><div class="progress"><progress value="${progress}" max="100"></progress></div><div class="temps"><div><small>NOZZLE</small><strong>${esc(d.nozzle_temper??'—')}°C</strong></div><div><small>BED</small><strong>${esc(d.bed_temper??'—')}°C</strong></div></div>${x.error_text?`<p class="printer-error">${esc(x.error_text)}</p>`:''}<div class="card-actions">${actionButton('details','Details & camera',n)}${actionButton('files','Printer files',n)}${actionButton('pause','Pause',n)}${actionButton('resume','Resume',n)}${actionButton('lighton','Light on',n)}${actionButton('lightoff','Light off',n)}${actionButton('stop','Stop',n,'danger')}${actionButton('printerQueue',`${waiting} queued →`,n)}</div></div></article>`;
 }).join('')||'<div class="empty">No printers configured.</div>';
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
  return `<div class="job-row"><div class="job-info"><strong>${j.demo?'🧪 DEMO · ':''}${esc(j.label)}</strong><small>${esc(printerLabel(j.printer))} · Plate ${j.options.plate} · ${j.options.use_ams?'AMS '+esc(j.options.ams_mapping.join(', ')):'External spool'}</small><small>${esc(j.id)} · Added by ${esc(j.author)}</small>${j.note?`<div class="job-note">${esc(j.note)}</div>`:''}</div><span class="state ${statusClass(j.status)}">${esc(j.status.replaceAll('_',' '))}</span><div class="job-actions">${buttons}</div></div>`;};
 $('jobs').innerHTML=jobs.filter(j=>!terminal.has(j.status)).map(row).join('')||'<div class="empty">The queue is clear. Add a print here or use /queueadd in Discord.</div>';
 $('history').innerHTML=jobs.filter(j=>terminal.has(j.status)).sort((a,b)=>b.updated-a.updated).slice(0,30).map(row).join('')||'<p class="muted">Completed and removed jobs will appear here.</p>';
}
async function refresh(){if(polling)return;polling=true;try{state=await api('state',undefined,'GET');csrf=state.csrf;$('loginScreen').hidden=true;$('app').hidden=false;$('connectionBanner').hidden=true;render();}catch(e){if(!$('app').hidden){$('connectionBanner').textContent='Connection interrupted. Displayed readings may be stale. '+e.message;$('connectionBanner').hidden=false;}}finally{polling=false;}}
function confirmAction(title,text,label,callback){$('confirmTitle').textContent=title;$('confirmText').textContent=text;$('confirmLabel').textContent=label;$('confirmCheck').checked=false;pendingConfirmation=callback;$('confirmDialog').showModal();}
function renderDetails(){const p=state.printers.find(x=>x.name===selectedPrinter);if(!p)return;const d=p.data||{};$('detailTitle').textContent=p.display_name||p.name;let units=Array.isArray(d.ams)?d.ams:(d.ams?.ams||[]);let trays=units.map(u=>`<h3>AMS ${esc(u.id)}</h3><p class="muted">Humidity: ${esc(u.humidity??'—')} · ${esc(u.temp??'—')}°C</p>`+(u.tray||[]).map(t=>`<p>Slot ${esc(t.id)} · ${esc(t.tray_type||'Empty')} · ${esc(t.remain??'?')}% · #${esc(t.tray_color||'?')}</p>`).join('')).join('');if(d.vt_tray)trays+=`<h3>External spool</h3><p>${esc(d.vt_tray.tray_type||'Unknown')} · ${esc(d.vt_tray.remain??'?')}% remaining</p>`;$('detailContent').innerHTML=`<p>${esc(d.subtask_name||'No file reported')}</p><div class="detail-grid"><div><small>STATE</small>${esc(p.state)}</div><div><small>PROGRESS</small>${esc(d.mc_percent??0)}%</div><div><small>LAYERS</small>${esc(d.layer_num??'?')} / ${esc(d.total_layer_num??'?')}</div><div><small>LAST TELEMETRY</small>${p.last_seen?esc(new Date(p.last_seen*1000).toLocaleTimeString()):'—'}</div></div><h2>Temperature & speed</h2><p>Nozzle: ${esc(d.nozzle_temper??"—")} °C → target ${esc(d.nozzle_target_temper??"—")} °C<br>Bed: ${esc(d.bed_temper??"—")} °C → target ${esc(d.bed_target_temper??"—")} °C<br>Speed profile: ${esc(({1:"Silent",2:"Standard",3:"Sport",4:"Ludicrous"})[d.spd_lvl]||"Not reported")}</p><h2>Filaments</h2>${trays||'<p class="muted">No filament data reported.</p>'}`;}
$('loginForm').addEventListener('submit',async e=>{e.preventDefault();$('loginError').textContent='';try{const r=await api('login',{password:$('loginPassword').value});csrf=r.csrf;$('loginPassword').value='';await refresh();}catch(e){$('loginError').textContent=e.message;}});
$('logout').onclick=async()=>{try{await api('logout',{});showLogin();}catch(e){notice(e.message);}};
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$(b.dataset.close).close());
$('queueFilter').onchange=renderJobs;
$('addJob').onclick=()=>{$('jobError').textContent='';$('jobDialog').showModal();};
$('source').onchange=()=>{$('uploadLabel').hidden=$('source').value!=='upload';$('remoteLabel').hidden=$('source').value!=='remote';};
$('jobFile').onchange=()=>{if(!$('jobLabel').value&&$('jobFile').files[0])$('jobLabel').value=$('jobFile').files[0].name.slice(0,120);};
$('jobForm').onsubmit=async e=>{e.preventDefault();$('jobError').textContent='';$('submitJob').disabled=true;try{const data={printer:$('jobPrinter').value,label:$('jobLabel').value,plate:Number($('jobPlate').value),use_ams:$('jobAms').checked,mapping:$('jobMapping').value,bed:$('jobBed').value};if($('source').value==='upload'){const f=$('jobFile').files[0];if(!f)throw new Error('Select a sliced .3mf file.');if(f.size>256*1024*1024)throw new Error('Maximum upload is 256 MiB.');$('submitJob').textContent='Uploading…';const form=new FormData();form.append('file',f);const uploaded=await api('upload',form);data.asset=uploaded.asset;}else data.remote=$('jobRemote').value;await api('jobs',data);$('jobDialog').close();$('jobForm').reset();$('source').onchange();notice('Added to the shared queue.');tab('queue');await refresh();}catch(e){$('jobError').textContent=e.message;}finally{$('submitJob').disabled=false;$('submitJob').textContent='Add to shared queue';}};
$('confirmForm').onsubmit=async e=>{e.preventDefault();const callback=pendingConfirmation;pendingConfirmation=null;$('confirmDialog').close();if(callback){try{await callback();await refresh();}catch(e){notice(e.message);}}};
document.addEventListener('click',async e=>{const b=e.target.closest('[data-action]');if(!b)return;const {action,printer,job,outcome}=b.dataset;try{
 if(action==='files'){b.disabled=true;notice('Reading printer storage…');try{const r=await api('files/'+encodeURIComponent(printer),undefined,'GET');const blob=new Blob([r.text],{type:'text/plain'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='printer-files.txt';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);notice('Printer file listing downloaded.');}finally{b.disabled=false;}return;}
 if(action==='details'){selectedPrinter=printer;loadSwapSettings();$('cameraImage').hidden=true;$('cameraMessage').textContent='Request a snapshot below.';renderDetails();$('detailDialog').showModal();return;}
 if(action==='printerQueue'){$('queueFilter').value=printer;tab('queue');renderJobs();return;}
 if(['pause','resume','lighton','lightoff'].includes(action)){await api('printers/'+encodeURIComponent(printer)+'/'+action,{});notice('Command submitted. Waiting for printer telemetry.');}
 if(action==='stop'){confirmAction('Stop this print?',printer+' — this cancels the current job.','I want to cancel this print.',()=>api('printers/'+encodeURIComponent(printer)+'/stop',{confirmed:true}));return;}
 if(action==='swapapprove'){openSwapApproval(job);return;}
 if(action==='startOverride'){const j=state.jobs.find(x=>x.id===job);confirmAction('Start ignoring reported error?',`${j.label} on ${printerLabel(j.printer)}. This bypasses the management error check and allows FAILED state. It does not clear the printer error or override firmware protections.`,'I inspected the printer, cleared the plate, and verified the material and sliced file. Start despite the reported error.',()=>api('jobs/'+job+'/start',{confirmed:true,override_error:true}));return;}
 if(action==='start'){const j=state.jobs.find(x=>x.id===job);confirmAction('Start next print?',`${j.label} on ${printerLabel(j.printer)}. Plate ${j.options.plate}. ${j.options.use_ams?'AMS mapping: '+j.options.ams_mapping.join(', '):'External spool'}.`,'The plate is clear, the correct material is loaded, and this file was sliced for this printer.',()=>api('jobs/'+job+'/start',{confirmed:true}));return;}
 if(action==='resolve'){confirmAction('Record the verified outcome?',`Mark ${job} as ${outcome}. This records a result; it does not send any command to the printer.`,'I inspected the physical printer and verified this outcome.',()=>api('jobs/'+job+'/resolve',{confirmed:true,outcome}));return;}
 if(action==='remove'){confirmAction('Remove this queued job?',job,'Remove this waiting job from the queue.',()=>api('jobs/'+job+'/remove',{}));return;}
 if(['up','down','reprint'].includes(action)){await api('jobs/'+job+'/'+action,{});if(action==='reprint')notice('A new copy was added to the queue.');}
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
