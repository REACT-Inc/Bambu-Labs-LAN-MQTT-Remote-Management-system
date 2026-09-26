'use strict';
let reviewedUpdate=null,updatePollBusy=false;
async function pollUpdate(){
 if(updatePollBusy||$('app').hidden)return;
 updatePollBusy=true;
 try{
  const r=await api('update/status',undefined,'GET');
  $('updateStatus').textContent=r.enabled ? `${r.state}: ${r.message}` : 'Run this release’s update.sh once on the Pi to enable web updates.';
  $('uploadUpdate').disabled=r.busy||!r.enabled;
  $('installUpdate').disabled=r.busy||!r.enabled;
 }catch(e){$('updateStatus').textContent='Reconnecting… Sign in again after the restart to see the update result.';}
 finally{updatePollBusy=false;}
}
$('updateForm').addEventListener('submit',async e=>{
 e.preventDefault();reviewedUpdate=null;$('installUpdate').hidden=true;
 const file=$('updateZip').files[0];if(!file)return;
 if(file.size>32*1024*1024){notice('Maximum update ZIP size is 32 MiB.');return;}
 $('uploadUpdate').disabled=true;$('updatePreview').textContent='Uploading and validating…';
 try{
  const form=new FormData();form.append('file',file);
  reviewedUpdate=await api('update/upload',form);
  $('updatePreview').textContent=`Version ${reviewedUpdate.version} · ${reviewedUpdate.files} application files · ${(reviewedUpdate.expanded_bytes/1048576).toFixed(1)} MiB expanded. SHA-256: ${reviewedUpdate.sha256}`;
  $('installUpdate').hidden=false;
 }catch(e){$('updatePreview').textContent=e.message;}
 finally{$('uploadUpdate').disabled=false;}
});
$('installUpdate').addEventListener('click',()=>{
 if(!reviewedUpdate)return;
 const reviewed=reviewedUpdate;
 confirmAction('Install software update?',`Install ${reviewed.version}? Management commands pause while dependencies are prepared, then the dashboard and Discord service restart. Settings and queue data are backed up.`, 'I trust this release and want to install it.',async()=>{
  const result=await api('update/install',{token:reviewed.token,confirmed:true});
  reviewedUpdate=null;$('installUpdate').hidden=true;$('updateStatus').textContent=result.message;notice(result.message);await pollUpdate();
 });
});
setInterval(pollUpdate,4000);pollUpdate();

let githubState=null,githubLoaded=false,githubPollBusy=false;
async function pollGitHub(force=false){
 if(githubPollBusy||$('app').hidden){if($('app').hidden)githubLoaded=false;return;}
 githubPollBusy=true;
 try{
  const r=await api('github/status',undefined,'GET');githubState=r;
  if(!githubLoaded||force){$('githubRepo').value=r.repository;$('githubAuto').checked=r.automatic;$('githubChannel').value=r.channel;$('githubToken').placeholder=r.has_token?'Token saved; blank keeps it':'Optional for public repositories';githubLoaded=true;}
  $('githubStatus').textContent=`Installed ${r.installed} (${r.installed_channel}) · ${r.channel} channel latest: ${r.latest||'not checked'} · ${r.message}`;
  $('githubInstall').hidden=!r.available;
 }catch(e){$('githubStatus').textContent=e.message;}finally{githubPollBusy=false;}
}
$('githubUpdateForm').onsubmit=e=>{
 e.preventDefault();
 const data={repository:$('githubRepo').value.trim(),automatic:$('githubAuto').checked,channel:$('githubChannel').value,token:$('githubToken').value,clear_token:$('githubClearToken').checked,confirmed:true};
 const save=async()=>{await api('github/settings',data);$('githubToken').value='';$('githubClearToken').checked=false;await pollGitHub(true);notice('GitHub settings saved.');};
 if(data.automatic)confirmAction('Enable automatic software updates?',`New ${data.channel}-channel releases from ${data.repository} will be installed when printers are idle. The service will restart. Only enable this for a repository you trust.`,'I authorize automatic installation from this repository.',save);
 else save().catch(e=>notice(e.message));
};
$('githubCheck').onclick=async()=>{
 $('githubCheck').disabled=true;
 try{await api('github/check',{});await pollGitHub();}catch(e){notice(e.message);}finally{$('githubCheck').disabled=false;}
};
$('githubInstall').onclick=()=>{
 if(!githubState?.available)return;
 const version=githubState.latest;
 confirmAction('Install GitHub release?',`${githubState.repository} — ${version}. Download, verify, back up, install and restart management.`,'I trust this release and want to install it.',async()=>{await api('github/install',{version,confirmed:true});await pollGitHub();await pollUpdate();});
};
setInterval(pollGitHub,5000);pollGitHub();
