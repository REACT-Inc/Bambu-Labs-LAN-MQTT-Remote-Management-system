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
function tuningFields(){const kind=$('controlKind').value;$('controlValue').hidden=kind==='speed';$('controlSpeed').hidden=kind!=='speed';$('controlAxisLabel').hidden=kind!=='move';$('controlValue').required=kind!=='speed';$('controlValue').step=kind==='move'?'0.1':'1';$('controlHint').textContent=kind==='move'?'X/Y: ±0.1–10 mm. Z: ±0.1–1 mm. Home on the printer first; idle only. Positive/negative directions are printer coordinates.':kind==='nozzle'?'Active nozzle only (no H2D tool switching). 0 turns heating off. Firmware may reject a setting.':kind==='bed'?'0 turns heating off. Model-specific limits are checked by the server.':kind==='fan'?'Part cooling fan: 0–100%.':'Choose a printer speed profile.';}
const originalTuningFields=tuningFields;
tuningFields=function(){originalTuningFields();const kind=$('controlKind').value;$('controlFanLabel').hidden=kind!=='fan';if(kind==='fan'||kind==='fanall')$('controlHint').textContent='0–100%. Automatic/off fans are controlled by firmware. Fanall affects this printer only.';if(kind==='chamber')$('controlHint').textContent='H2D only: 0 = off, 40–65 °C = heating target. Firmware controls heating and may reject a target for the loaded filament.';};
$('controlKind').onchange=tuningFields;tuningFields();
function refreshFanChoices(){const list=state?.printers.find(p=>p.name===selectedPrinter)?.limits?.fans||[];const select=$('controlFan'),value=select.value;select.replaceChildren(...list.map(f=>{const option=document.createElement('option');option.value=f.key;option.textContent=f.label+(f.manual?'':' (automatic / off)');option.disabled=!f.manual;return option;}));if(list.some(f=>f.key===value&&f.manual))select.value=value;}
setInterval(()=>{if($('detailDialog').open)refreshFanChoices();},1000);
$('controlForm').onsubmit=e=>{e.preventDefault();const selection=$('controlKind').value,kind=selection==='fan'?'fan_'+$('controlFan').value:selection,name=selectedPrinter;const value=kind==='speed'?$('controlSpeed').value:Number($('controlValue').value);const axis=$('controlAxis').value;confirmAction('Apply printer control?',printerLabel(name)+' — '+kind+': '+value+(kind==='move'?' mm on '+axis:''),kind==='move'?'I homed the printer, verified the travel path and bed are clear, and am watching the printer.':'I checked this setting and want to apply it.',async()=>{const r=await api('printers/'+encodeURIComponent(name)+'/'+kind,{value,axis,confirmed:true,homed:kind==='move'});notice(r.message);});};

function loadSwapSettings(){const cfg=state?.printers.find(p=>p.name===selectedPrinter)?.plate_swap||{};$('swapEnabled').checked=!!cfg.enabled;$('swapModel').value=cfg.model||'A1 mini';$('swapSpares').value=cfg.spares||0;renderSwapStatus();}
function renderSwapStatus(){const cfg=state?.printers.find(p=>p.name===selectedPrinter)?.plate_swap;$('swapStatus').textContent=cfg?.enabled?`${cfg.model} kit enabled · ${cfg.spares} estimated magazine plates · ${cfg.verified?'Starting setup checked':'Starting setup check needed'}`:'Disabled for this printer — manual queue workflow.';}
setInterval(()=>{if($('detailDialog').open&&state)renderSwapStatus();},1000);
$('swapForm').onsubmit=e=>{e.preventDefault();const name=selectedPrinter,data={enabled:$('swapEnabled').checked,model:$('swapModel').value,spares:Number($('swapSpares').value),confirmed:true};confirmAction('Save plate-swap settings?',printerLabel(name)+' — '+(data.enabled?'enable '+data.model+' kit':'disable kit')+'; '+data.spares+' magazine plates. Existing plate checks and file approvals will be reset.','I verified the installed hardware and actual spare count.',async()=>{await api('plateswap/'+encodeURIComponent(name)+'/configure',data);notice('Saved for this printer. Approve the prepared Swaplist batch and check its starting setup before starting.');});};
$('swapChecked').onclick=()=>{const name=selectedPrinter;confirmAction('Swapmod starting setup checked?',printerLabel(name)+' — this records your inspection; no swap movement is sent.','I checked the starting setup against Swaplist instructions, loaded the required magazine plates and cleared the ejection path.',async()=>{await api('plateswap/'+encodeURIComponent(name)+'/check',{confirmed:true});notice('Setup checked. Start the approved Swaplist batch when ready.');});};

let swapApprovalJob=null;
function openSwapApproval(id){const j=state.jobs.find(x=>x.id===id);swapApprovalJob=id;$('swapApproveText').textContent=j.label+' — '+printerLabel(j.printer);$('swapBatchPlates').value=j.options.swap_plates||1;$('swapBatchConfirm').checked=false;$('swapApproveDialog').showModal();}
$('swapApproveForm').onsubmit=async e=>{e.preventDefault();try{await api('jobs/'+swapApprovalJob+'/swapapprove',{confirmed:$('swapBatchConfirm').checked,plates:Number($('swapBatchPlates').value)});$('swapApproveDialog').close();notice('Swaplist batch approved.');await refresh();}catch(e){notice(e.message);}};
