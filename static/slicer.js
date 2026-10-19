// The Slicer tab (slicer/web.py, docs/slicer.md): build a plate in the browser, slice it with OrcaSlicer on a worker computer.
// Everything here is millimetres, Z up, in the printer's bed coordinates, like OrcaSlicer. Moving objects never slices:
// only "Slice" sends the plate (each object's model and 4x4 transform, the presets and the changed settings).
const S={ready:false,catalog:null,schema:[],objects:[],selected:null,changes:{},jobs:[],workers:[],events:null};
let THREE,OrbitControls,TransformControls,STLLoader,renderer,scene,camera,orbit,gizmo,plateGroup,raf=0;
const el=id=>document.getElementById(id);
const h=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const COLORS={ok:0x8dc6ff,selected:0xbaf778,bad:0xff6b6b};

window.addEventListener('tab',e=>{if(e.detail==='slicer')openSlicer();else closeEvents();});

async function openSlicer(){
 if(!S.ready){
  try{[THREE,{OrbitControls},{TransformControls},{STLLoader}]=await Promise.all([import('./three.module.min.js'),import('./OrbitControls.js'),import('./TransformControls.js'),import('./STLLoader.js')]);}
  catch(e){el('slicerMessage').textContent='The 3D view could not load: '+e.message;return;}
  setupView();setupControls();S.ready=true;
 }
 await loadCatalog();openEvents();resize();
}

// ---- 3D view ----
function setupView(){
 const box=el('slicerView');
 renderer=new THREE.WebGLRenderer({antialias:true});renderer.setPixelRatio(Math.min(2,window.devicePixelRatio||1));box.appendChild(renderer.domElement);
 scene=new THREE.Scene();scene.background=new THREE.Color(0x111815);
 camera=new THREE.PerspectiveCamera(45,1,1,5000);camera.up.set(0,0,1);
 scene.add(new THREE.HemisphereLight(0xffffff,0x334433,1.6));const sun=new THREE.DirectionalLight(0xffffff,1.4);sun.position.set(200,-300,500);scene.add(sun);
 orbit=new OrbitControls(camera,renderer.domElement);orbit.enableDamping=true;
 gizmo=new TransformControls(camera,renderer.domElement);gizmo.setSize(0.8);
 gizmo.addEventListener('dragging-changed',e=>{orbit.enabled=!e.value;if(!e.value){dropToBed(S.selected);refreshObjects();}});
 gizmo.addEventListener('objectChange',()=>{updateTransformInputs();checkPlate();});
 scene.add(gizmo.getHelper?gizmo.getHelper():gizmo);
 renderer.domElement.addEventListener('pointerdown',pick);
 new ResizeObserver(resize).observe(box);
 const loop=()=>{raf=requestAnimationFrame(loop);orbit.update();renderer.render(scene,camera);};loop();
}
function resize(){if(!renderer)return;const box=el('slicerView'),w=box.clientWidth||600,ht=box.clientHeight||420;renderer.setSize(w,ht,false);camera.aspect=w/ht;camera.updateProjectionMatrix();}

function machine(){return S.catalog?.machines.find(m=>m.name===el('slicerMachine').value);}
function bedBox(){const m=machine();if(!m||!m.bed.length)return {x0:0,y0:0,x1:256,y1:256,z:250};
 const xs=m.bed.map(p=>p[0]),ys=m.bed.map(p=>p[1]);return {x0:Math.min(...xs),y0:Math.min(...ys),x1:Math.max(...xs),y1:Math.max(...ys),z:m.height||250};}
function drawPlate(){
 if(plateGroup)scene.remove(plateGroup);plateGroup=new THREE.Group();const b=bedBox(),w=b.x1-b.x0,d=b.y1-b.y0;
 const plate=new THREE.Mesh(new THREE.PlaneGeometry(w,d),new THREE.MeshStandardMaterial({color:0x202925,roughness:0.9}));plate.position.set(b.x0+w/2,b.y0+d/2,-0.05);plateGroup.add(plate);
 const grid=new THREE.GridHelper(Math.max(w,d),Math.round(Math.max(w,d)/10),0x3d4b44,0x2a332f);grid.rotation.x=Math.PI/2;grid.position.set(b.x0+w/2,b.y0+d/2,0);plateGroup.add(grid);
 const volume=new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(w,d,b.z)),new THREE.LineBasicMaterial({color:0x576b48}));volume.position.set(b.x0+w/2,b.y0+d/2,b.z/2);plateGroup.add(volume);
 scene.add(plateGroup);
 const size=Math.max(w,d);camera.position.set(b.x0+w/2,b.y0-size*0.75,size*0.85);orbit.target.set(b.x0+w/2,b.y0+d*0.45,b.z*0.1);orbit.update();
 checkPlate();
}

// ---- objects ----
function setupControls(){
 el('slicerFiles').onchange=e=>addFiles([...e.target.files]).finally(()=>e.target.value='');
 const view=el('slicerView');view.addEventListener('dragover',e=>{e.preventDefault();view.classList.add('drop');});
 view.addEventListener('dragleave',()=>view.classList.remove('drop'));
 view.addEventListener('drop',e=>{e.preventDefault();view.classList.remove('drop');addFiles([...e.dataTransfer.files]);});
 el('slicerMachine').onchange=()=>{fillPresets(true);drawPlate();};
 el('slicerProcess').onchange=el('slicerFilament').onchange=()=>{};
 document.querySelectorAll('[data-gizmo]').forEach(b=>b.onclick=()=>{gizmo.setMode(b.dataset.gizmo);document.querySelectorAll('[data-gizmo]').forEach(x=>x.classList.toggle('on',x===b));});
 el('slicerDuplicate').onclick=duplicate;el('slicerDelete').onclick=removeSelected;el('slicerReset').onclick=resetSelected;
 el('slicerDrop').onclick=()=>{S.objects.forEach(o=>dropToBed(o));refreshObjects();};el('slicerArrange').onclick=arrange;
 el('slicerSlice').onclick=slice;el('slicerAddWorker').onclick=addWorker;
 el('slicerTransform').addEventListener('change',applyTransformInputs);el('slicerTransform').addEventListener('submit',e=>e.preventDefault());
 document.addEventListener('keydown',e=>{if(el('slicer').hidden||/INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName))return;
  const k=e.key.toLowerCase();if(k==='g'||k==='m')gizmo.setMode('translate');else if(k==='r')gizmo.setMode('rotate');else if(k==='s')gizmo.setMode('scale');
  else if(k==='delete'||k==='backspace')removeSelected();else if(k==='d'&&(e.ctrlKey||e.metaKey)){e.preventDefault();duplicate();}else if(k==='escape')select(null);});
}

async function addFiles(files){
 for(const file of files){
  if(!/\.stl$/i.test(file.name)){notice(file.name+': only .stl files can be added for now.');continue;}
  el('slicerMessage').textContent='Uploading '+file.name+'…';
  try{
   const form=new FormData();form.append('file',file,file.name);
   const info=await api('slicer/models',form);
   const geometry=new STLLoader().parse(await file.arrayBuffer());
   addObject(info,geometry);el('slicerMessage').textContent='';
  }catch(e){el('slicerMessage').textContent=file.name+': '+e.message;}
 }
}

function addObject(info,geometry,from){
 geometry.computeBoundingBox();const bb=geometry.boundingBox,center=new THREE.Vector3();bb.getCenter(center);
 const mesh=new THREE.Mesh(geometry,new THREE.MeshStandardMaterial({color:COLORS.ok,roughness:0.6,metalness:0.05}));
 mesh.position.set(-center.x,-center.y,-bb.min.z);   // the object's origin: the middle of its bottom
 const group=new THREE.Group();group.add(mesh);
 const obj={info,group,mesh,name:info.name,id:crypto.randomUUID?crypto.randomUUID():String(Math.random())};
 if(from){group.position.copy(from.group.position).add(new THREE.Vector3(10,10,0));group.rotation.copy(from.group.rotation);group.scale.copy(from.group.scale);}
 else{const b=bedBox();group.position.set((b.x0+b.x1)/2,(b.y0+b.y1)/2,0);}
 obj.home={position:group.position.clone(),rotation:group.rotation.clone(),scale:group.scale.clone()};
 scene.add(group);S.objects.push(obj);dropToBed(obj);select(obj);refreshObjects();
}

function worldBox(obj){obj.group.updateMatrixWorld(true);return new THREE.Box3().setFromObject(obj.mesh,true);}
function dropToBed(obj){if(!obj)return;const box=worldBox(obj);obj.group.position.z-=box.min.z;obj.group.updateMatrixWorld(true);}
function pick(e){if(gizmo.dragging)return;const r=renderer.domElement.getBoundingClientRect(),p=new THREE.Vector2(((e.clientX-r.left)/r.width)*2-1,-((e.clientY-r.top)/r.height)*2+1);
 const ray=new THREE.Raycaster();ray.setFromCamera(p,camera);const hit=ray.intersectObjects(S.objects.map(o=>o.mesh))[0];
 if(hit)select(S.objects.find(o=>o.mesh===hit.object));else if(!gizmo.axis)select(null);}
function select(obj){S.selected=obj;if(obj)gizmo.attach(obj.group);else gizmo.detach();refreshObjects();updateTransformInputs();}
function duplicate(){const o=S.selected;if(o)addObject(o.info,o.mesh.geometry,o);}
function removeSelected(){const o=S.selected;if(!o)return;scene.remove(o.group);S.objects=S.objects.filter(x=>x!==o);select(null);}
function resetSelected(){const o=S.selected;if(!o)return;o.group.position.copy(o.home.position);o.group.rotation.copy(o.home.rotation);o.group.scale.copy(o.home.scale);dropToBed(o);refreshObjects();updateTransformInputs();}

// Simple arrange: rows of objects (largest first) with 5 mm gaps, inside the bed.
function arrange(){const b=bedBox(),gap=5;let x=b.x0+gap,y=b.y0+gap,row=0;
 const sized=S.objects.map(o=>({o,box:worldBox(o)})).sort((a,c)=>(c.box.max.y-c.box.min.y)-(a.box.max.y-a.box.min.y));
 for(const {o,box} of sized){const w=box.max.x-box.min.x,d=box.max.y-box.min.y;
  if(x+w>b.x1-gap&&x>b.x0+gap){x=b.x0+gap;y+=row+gap;row=0;}
  o.group.position.x+=x-box.min.x;o.group.position.y+=y-box.min.y;x+=w+gap;row=Math.max(row,d);}
 refreshObjects();updateTransformInputs();}

// Out of the build volume or overlapping another object: drawn red and listed, and slicing asks first.
function checkPlate(){const b=bedBox(),problems=[],boxes=S.objects.map(o=>worldBox(o));
 S.objects.forEach((o,i)=>{const box=boxes[i],bad=[];
  if(box.min.x<b.x0-0.01||box.min.y<b.y0-0.01||box.max.x>b.x1+0.01||box.max.y>b.y1+0.01)bad.push('outside the plate');
  if(box.max.z>b.z+0.01)bad.push('taller than the printer');
  boxes.forEach((other,j)=>{if(j!==i&&box.min.x<other.max.x&&box.max.x>other.min.x&&box.min.y<other.max.y&&box.max.y>other.min.y)bad.push('overlaps '+S.objects[j].name);});
  o.problems=bad;o.mesh.material.color.setHex(bad.length?COLORS.bad:o===S.selected?COLORS.selected:COLORS.ok);
  if(bad.length)problems.push(o.name+': '+[...new Set(bad)].join(', '));});
 el('slicerWarnings').innerHTML=problems.map(p=>`<li>${h(p)}</li>`).join('');el('slicerWarnings').hidden=!problems.length;return problems;}

function refreshObjects(){
 el('slicerObjects').innerHTML=S.objects.map((o,i)=>{const box=worldBox(o),size=box.getSize(new THREE.Vector3());
  return `<li class="${o===S.selected?'on':''}${o.problems?.length?' bad':''}" data-index="${i}"><strong>${h(o.name)}</strong><small>${size.x.toFixed(1)} × ${size.y.toFixed(1)} × ${size.z.toFixed(1)} mm · ${o.info.triangles.toLocaleString()} triangles</small></li>`;}).join('')||'<li class="muted">Add STL files with the button above, or drop them on the plate.</li>';
 el('slicerObjects').querySelectorAll('[data-index]').forEach(li=>li.onclick=()=>select(S.objects[+li.dataset.index]));
 ['slicerDuplicate','slicerDelete','slicerReset'].forEach(id=>el(id).disabled=!S.selected);el('slicerSlice').disabled=!S.objects.length;
 checkPlate();
}

function updateTransformInputs(){const o=S.selected,form=el('slicerTransform');form.hidden=!o;if(!o)return;
 const g=o.group,deg=r=>+(r*180/Math.PI).toFixed(1),box=worldBox(o),size=box.getSize(new THREE.Vector3());
 const values={px:g.position.x,py:g.position.y,pz:g.position.z,rx:deg(g.rotation.x),ry:deg(g.rotation.y),rz:deg(g.rotation.z),sx:g.scale.x*100,sy:g.scale.y*100,sz:g.scale.z*100};
 for(const [k,v] of Object.entries(values)){const input=form.elements[k];if(document.activeElement!==input)input.value=(+v).toFixed(k[0]==='s'?1:2).replace(/\.?0+$/,'');}
 el('slicerSize').textContent=`${size.x.toFixed(1)} × ${size.y.toFixed(1)} × ${size.z.toFixed(1)} mm`;}
function applyTransformInputs(e){const o=S.selected;if(!o)return;const f=el('slicerTransform').elements,n=k=>parseFloat(f[k].value)||0,rad=d=>d*Math.PI/180;
 if(e.target.name?.[0]==='s'&&f.uniform.checked){const v=n(e.target.name);['sx','sy','sz'].forEach(k=>f[k].value=v);}
 const scale=['sx','sy','sz'].map(k=>Math.max(0.1,n(k))/100);
 o.group.position.set(n('px'),n('py'),n('pz'));o.group.rotation.set(rad(n('rx')),rad(n('ry')),rad(n('rz')));o.group.scale.set(...scale);
 dropToBed(o);refreshObjects();updateTransformInputs();}

// ---- presets and settings ----
async function loadCatalog(){
 try{const r=await api('slicer/catalog',undefined,'GET');S.schema=r.schema;S.printers=r.printers;
  el('slicerWorkerCount').textContent=r.workers?`${r.workers} worker${r.workers>1?'s':''} ready`:'No worker online';
  if(!r.catalog){el('slicerPresetNote').textContent='No slicing worker has reported its presets yet. Add a worker below.';return;}
  const fresh=!S.catalog||S.catalogVersion!==r.version;S.catalog=r.catalog;S.catalogVersion=r.version;el('slicerPresetNote').textContent='OrcaSlicer '+(r.version||'')+' presets';
  if(fresh){const m=el('slicerMachine'),models=(r.printers||[]).map(p=>p.model).filter(Boolean),wanted=S.catalog.machines.find(x=>models.some(model=>x.model.endsWith(model))&&x.nozzle==='0.4')||S.catalog.machines[0];
   m.innerHTML=S.catalog.machines.map(x=>`<option${x===wanted?' selected':''}>${h(x.name)}</option>`).join('');fillPresets(true);drawPlate();buildSettings();}
 }catch(e){el('slicerPresetNote').textContent=e.message;}
}
function fillPresets(defaults){const m=machine();if(!m)return;
 for(const [id,list,def] of [['slicerProcess',S.catalog.processes,m.default_process],['slicerFilament',S.catalog.filaments,m.default_filament]]){
  const select=el(id),keep=select.value,fits=list.filter(p=>p.printers.includes(m.name));
  const chosen=(!defaults&&fits.some(p=>p.name===keep))?keep:(fits.find(p=>p.name===def)||fits[0])?.name;
  select.innerHTML=fits.map(p=>`<option${p.name===chosen?' selected':''}>${h(p.name)}</option>`).join('');}}
function buildSettings(){const groups={};S.schema.forEach(s=>(groups[s.group]=groups[s.group]||[]).push(s));
 el('slicerSettings').innerHTML=Object.entries(groups).map(([g,list])=>`<details${g==='Quality'?' open':''}><summary>${h(g)}</summary>${list.map(s=>{
  const input=s.type==='bool'?`<select data-key="${s.key}"><option value="">Preset</option><option value="true">On</option><option value="false">Off</option></select>`
   :s.type==='enum'?`<select data-key="${s.key}"><option value="">Preset</option>${s.options.map(o=>`<option>${h(o)}</option>`).join('')}</select>`
   :`<input data-key="${s.key}" type="number" inputmode="decimal" placeholder="Preset" min="${s.min}" max="${s.max}" step="${s.step||1}">`;
  return `<label>${h(s.label)}${s.unit?` <small>${h(s.unit)}</small>`:''}${input}</label>`;}).join('')}</details>`).join('');
 el('slicerSettings').onchange=e=>{const k=e.target.dataset.key;if(!k)return;const s=S.schema.find(x=>x.key===k),v=e.target.value;
  if(v==='')delete S.changes[k];else S.changes[k]=s.type==='bool'?v==='true':s.type==='enum'?v:Number(v);
  el('slicerChanged').textContent=Object.keys(S.changes).length?`${Object.keys(S.changes).length} changed from the presets`:'';};}

// ---- slicing ----
async function slice(){
 if(!S.objects.length)return;const problems=checkPlate();
 if(problems.length&&!confirm('Some objects have problems:\n'+problems.join('\n')+'\n\nSlice anyway? OrcaSlicer may refuse.'))return;
 const body={label:el('slicerLabel').value||S.objects[0].name.replace(/\.stl$/i,''),
  settings:{machine:el('slicerMachine').value,process:el('slicerProcess').value,filament:el('slicerFilament').value},changes:S.changes,
  objects:S.objects.map(o=>{o.group.updateMatrixWorld(true);return {model:o.info.id,name:o.name,matrix:Array.from(o.mesh.matrixWorld.elements)};})};
 el('slicerSlice').disabled=true;
 try{const job=await api('slicer/jobs',body);notice('Queued for slicing: '+job.label);}catch(e){notice(e.message);}finally{el('slicerSlice').disabled=!S.objects.length;}
}

// ---- live jobs and workers (Server-Sent Events) ----
function openEvents(){if(S.events)return;S.events=new EventSource('/api/slicer/events');
 S.events.onmessage=e=>{const d=JSON.parse(e.data);S.jobs=d.jobs;S.workers=d.workers;renderJobs();renderWorkers();};
 S.events.onerror=()=>{el('slicerLive').textContent='Reconnecting…';};S.events.onopen=()=>{el('slicerLive').textContent='Live';};}
function closeEvents(){if(S.events){S.events.close();S.events=null;}}
const STATUS={queued:'Waiting for a worker',assigned:'Sent to a worker',preparing:'Preparing',slicing:'Slicing',processing:'Checking the result',completed:'Sliced',failed:'Failed',cancelled:'Cancelled'};
function minutes(s){const m=Math.round((s||0)/60);return m>=60?`${Math.floor(m/60)} h ${m%60} min`:`${m} min`;}
function renderJobs(){
 el('slicerJobs').innerHTML=S.jobs.map(j=>{const st=j.stats||{},active=!['completed','failed','cancelled'].includes(j.status);
  const worker=S.workers.find(w=>w.id===j.worker);
  const facts=j.status==='completed'?[st.seconds!=null?'⏱ '+minutes(st.seconds):'',st.grams!=null?`⚖ ${st.grams.toFixed(1)} g`:'',(st.filaments||[]).map(f=>`${f.type} ${f.meters.toFixed(2)} m`).join(', ')].filter(Boolean).join(' · '):'';
  const printers=(S.printers||[]).map(p=>`<option value="${h(p.name)}">${h(p.name)}${p.model?` (${h(p.model)})`:''}</option>`).join('');
  return `<li class="slice-job ${h(j.status)}"><div><strong>${h(j.label)}</strong><small>${h(STATUS[j.status]||j.status)}${j.queue_position?` · #${j.queue_position} in line`:''}${worker?` · ${h(worker.name)}`:''}${j.stage_note?` · ${h(j.stage_note)}`:''}</small>
  <small>${h(j.settings.machine||'')} · ${h(j.settings.process||'')} · ${h(j.settings.filament||'')}</small>${facts?`<small>${h(facts)}</small>`:''}${(st.warnings||[]).map(w=>`<small class="warn">⚠ ${h(w)}</small>`).join('')}${j.error?`<small class="error">${h(j.error)}</small>`:''}</div>
  <div class="slice-actions">${j.has_result?`<a class="button" href="/api/slicer/jobs/${h(j.id)}/result">Download</a><select data-printer="${h(j.id)}" aria-label="Printer">${printers}</select><button data-queue="${h(j.id)}">Send to print queue</button>`:''}${active?`<button data-cancel="${h(j.id)}" class="danger">Cancel</button>`:''}</div></li>`;}).join('')||'<li class="muted">Sliced plates appear here. You can leave this page while a plate slices.</li>';
 el('slicerJobs').querySelectorAll('[data-cancel]').forEach(b=>b.onclick=async()=>{try{await api(`slicer/jobs/${b.dataset.cancel}/cancel`,{});}catch(e){notice(e.message);}});
 el('slicerJobs').querySelectorAll('[data-queue]').forEach(b=>b.onclick=async()=>{const printer=el('slicerJobs').querySelector(`[data-printer="${b.dataset.queue}"]`).value;
  try{const r=await api(`slicer/jobs/${b.dataset.queue}/queue`,{printer});notice(`In ${r.printer}'s print queue. Start it from the Print queue when the plate is clear.`);}catch(e){notice(e.message);}});
}
function bytes(n){return n?(n/1073741824).toFixed(1)+' GB':'—';}
function renderWorkers(){
 el('slicerWorkerCount').textContent=(()=>{const n=S.workers.filter(w=>w.online&&w.enabled).length;return n?`${n} worker${n>1?'s':''} ready`:'No worker online';})();
 el('slicerWorkers').innerHTML=S.workers.map(w=>{const i=w.info||{};return `<li><div><strong>${w.online?'🟢':'🔴'} ${h(w.name)}</strong>
  <small>${h(i.cpu||'')} · ${h(i.cores||'?')} cores · RAM ${bytes(i.ram_free)} free of ${bytes(i.ram_total)} · load ${h(i.load??'—')} · disk ${bytes(i.disk_free)} free</small>
  <small>${h(i.os||'')} · OrcaSlicer ${h(i.orca_version||'?')} · worker ${h(i.worker_version||'?')} · ${w.jobs.length?'slicing '+w.jobs.length:'idle'}${w.draining?' · draining':''}${w.enabled?'':' · turned off'}</small></div>
  <div class="slice-actions"><label class="checkbox"><input type="checkbox" data-enable="${h(w.id)}"${w.enabled?' checked':''}> On</label><label class="checkbox"><input type="checkbox" data-drain="${h(w.id)}"${w.draining?' checked':''}> Drain</label>
  <label>Jobs at once<select data-concurrency="${h(w.id)}">${[1,2,3,4].map(n=>`<option${n===w.concurrency?' selected':''}>${n}</option>`).join('')}</select></label><button data-remove="${h(w.id)}" class="danger small-btn">Remove</button></div></li>`;}).join('')||'<li class="muted">No slicing workers yet.</li>';
 const set=(id,body)=>api('slicer/workers/'+id,body).catch(e=>notice(e.message));
 el('slicerWorkers').querySelectorAll('[data-enable]').forEach(c=>c.onchange=()=>set(c.dataset.enable,{enabled:c.checked}));
 el('slicerWorkers').querySelectorAll('[data-drain]').forEach(c=>c.onchange=()=>set(c.dataset.drain,{draining:c.checked}));
 el('slicerWorkers').querySelectorAll('[data-concurrency]').forEach(c=>c.onchange=()=>set(c.dataset.concurrency,{concurrency:+c.value}));
 el('slicerWorkers').querySelectorAll('[data-remove]').forEach(b=>b.onclick=()=>{if(confirm('Remove this worker? It has to be set up again to come back.'))api(`slicer/workers/${b.dataset.remove}/remove`,{}).catch(e=>notice(e.message));});
}
async function addWorker(){try{const r=await api('slicer/workers/enroll',{});el('slicerEnroll').hidden=false;el('slicerEnrollCommand').textContent=r.command;}catch(e){notice(e.message);}}
