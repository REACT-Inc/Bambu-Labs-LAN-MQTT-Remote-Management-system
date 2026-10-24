// The Slicer tab (slicer/web.py, docs/slicer.md): build a plate in the browser, slice it with OrcaSlicer on a worker computer.
// Everything here is millimetres, Z up, in the printer's bed coordinates, like OrcaSlicer. Moving objects never slices:
// only "Slice" sends the plate (each object's model and 4x4 transform, the presets and the changed settings).
// A sliced plate can be previewed layer by layer from its G-code (in the same 3D view).
const S={ready:false,catalog:null,schema:[],objects:[],selected:null,changes:{},jobs:[],workers:[],events:null,mine:[],layFlat:false,preview:null,quick:[],values:{process:{},filament:{}},allPage:'process|Quality'};
let THREE,OrbitControls,TransformControls,STLLoader,renderer,scene,camera,orbit,gizmo,plateGroup,raf=0;
const el=id=>document.getElementById(id);
const h=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const COLORS={ok:0x8dc6ff,selected:0xbaf778,bad:0xff6b6b};

window.addEventListener('tab',e=>{if(e.detail==='slicer')openSlicer();else closeEvents();});

async function openSlicer(){
 if(!S.ready){
  try{[THREE,{OrbitControls},{TransformControls},{STLLoader}]=await Promise.all([import('./three.module.min.js'),import('./OrbitControls.js'),import('./TransformControls.js'),import('./STLLoader.js')]);}
  catch(e){el('slicerMessage').textContent='The 3D view could not load: '+e.message;return;}
  setupView();setupControls();drawPlate();S.ready=true;   // a default plate until a worker sends the presets
 }
 await loadCatalog();loadMine();openEvents();resize();
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

function machine(name){return S.catalog?.machines.find(m=>m.name===(name||el('slicerMachine').value));}
function bedBox(name){const m=machine(name);if(!m||!m.bed.length)return {x0:0,y0:0,x1:256,y1:256,z:250};
 const xs=m.bed.map(p=>p[0]),ys=m.bed.map(p=>p[1]);return {x0:Math.min(...xs),y0:Math.min(...ys),x1:Math.max(...xs),y1:Math.max(...ys),z:m.height||250};}
function drawPlate(name){   // name: another printer's plate (a preview of a plate sliced for it)
 if(plateGroup)scene.remove(plateGroup);plateGroup=new THREE.Group();const b=bedBox(name),w=b.x1-b.x0,d=b.y1-b.y0;
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
 el('slicerProcess').onchange=el('slicerFilament').onchange=()=>loadValues();
 document.querySelectorAll('[data-gizmo]').forEach(b=>b.onclick=()=>{gizmo.setMode(b.dataset.gizmo);document.querySelectorAll('[data-gizmo]').forEach(x=>x.classList.toggle('on',x===b));});
 el('slicerDuplicate').onclick=duplicate;el('slicerDelete').onclick=removeSelected;el('slicerReset').onclick=resetSelected;
 el('slicerLayFlat').onclick=()=>{S.layFlat=!S.layFlat;el('slicerLayFlat').classList.toggle('on',S.layFlat);el('slicerMessage').textContent=S.layFlat?'Click the face of an object that should lie on the plate.':'';};
 el('slicerMirrorX').onclick=()=>mirror('x');el('slicerMirrorY').onclick=()=>mirror('y');
 for(const box of [el('slicerSettings'),el('slicerAllList')]){box.addEventListener('change',changeSetting);box.addEventListener('click',resetSetting);}
 el('slicerAllOpen').onclick=openAll;el('slicerAllClose').onclick=()=>{el('slicerAll').close();renderAllNav();};
 el('slicerAllSearch').oninput=renderAll;el('slicerAllChanged').onchange=renderAll;
 el('slicerAllNav').onclick=e=>{const b=e.target.closest('[data-page]');if(!b)return;S.allPage=b.dataset.page;el('slicerAllSearch').value='';renderAllNav();renderAll();el('slicerAllList').scrollTop=0;};
 el('slicerMine').onchange=()=>{const p=S.mine.find(x=>x.name===el('slicerMine').value);if(p)applyMine(p);el('slicerDeleteMine').disabled=!p;};
 el('slicerSaveMine').onclick=saveMine;el('slicerDeleteMine').onclick=deleteMine;
 el('slicerLayer').oninput=()=>showLayers();el('slicerPreviewClose').onclick=closePreview;
 el('slicerLegend').onchange=()=>buildPreview();
 el('slicerDrop').onclick=()=>{S.objects.forEach(o=>dropToBed(o));refreshObjects();};el('slicerArrange').onclick=arrange;
 el('slicerSlice').onclick=slice;el('slicerAddWorker').onclick=addWorker;
 el('slicerTransform').addEventListener('change',applyTransformInputs);el('slicerTransform').addEventListener('submit',e=>e.preventDefault());
 document.addEventListener('keydown',e=>{if(el('slicer').hidden||/INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName))return;
  const k=e.key.toLowerCase();if(k==='g'||k==='m')gizmo.setMode('translate');else if(k==='r')gizmo.setMode('rotate');else if(k==='s')gizmo.setMode('scale');
  else if(k==='f')el('slicerLayFlat').click();
  else if(k==='delete'||k==='backspace')removeSelected();else if(k==='d'&&(e.ctrlKey||e.metaKey)){e.preventDefault();duplicate();}else if(k==='escape')select(null);});
}

// STL, OBJ and 3MF files: the Pi checks them and keeps each object as an STL (a 3MF can hold several); the view loads
// that STL back, so what's shown is what will be sliced.
async function addFiles(files){
 if(S.preview)closePreview();
 for(const file of files){
  if(!/\.(stl|obj|3mf)$/i.test(file.name)){notice(file.name+': add .stl, .obj or .3mf files.');continue;}
  el('slicerMessage').textContent='Uploading '+file.name+'…';
  try{
   const form=new FormData();form.append('file',file,file.name);
   const r=await api('slicer/models',form);
   for(const info of r.models){
    const response=await fetch('/api/slicer/models/'+info.id);if(!response.ok)throw new Error('The model could not be loaded back.');
    addObject(info,new STLLoader().parse(await response.arrayBuffer()));}
   if(r.models.length>1)arrange();
   el('slicerMessage').textContent=r.models.length>1?`${file.name}: ${r.models.length} objects added.`:'';
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
function pick(e){if(gizmo.dragging||S.preview)return;const r=renderer.domElement.getBoundingClientRect(),p=new THREE.Vector2(((e.clientX-r.left)/r.width)*2-1,-((e.clientY-r.top)/r.height)*2+1);
 const ray=new THREE.Raycaster();ray.setFromCamera(p,camera);const hit=ray.intersectObjects(S.objects.map(o=>o.mesh))[0];
 if(hit&&S.layFlat){layOnFace(S.objects.find(o=>o.mesh===hit.object),hit.face.normal);return;}
 if(hit)select(S.objects.find(o=>o.mesh===hit.object));else if(!gizmo.axis)select(null);}
// Lay on face: turn the object so the clicked face points down, then put it on the plate.
function layOnFace(obj,normal){const world=normal.clone().applyMatrix3(new THREE.Matrix3().getNormalMatrix(obj.mesh.matrixWorld)).normalize();
 obj.group.quaternion.premultiply(new THREE.Quaternion().setFromUnitVectors(world,new THREE.Vector3(0,0,-1)));
 dropToBed(obj);S.layFlat=false;el('slicerLayFlat').classList.remove('on');el('slicerMessage').textContent='';select(obj);}
function mirror(axis){const o=S.selected;if(!o)return;o.group.scale[axis]*=-1;dropToBed(o);refreshObjects();updateTransformInputs();}
function select(obj){S.selected=obj;if(obj)gizmo.attach(obj.group);else gizmo.detach();refreshObjects();updateTransformInputs();}
function duplicate(){const o=S.selected;if(!o)return;const copies=Math.max(1,Math.min(50,parseInt(el('slicerCopies').value,10)||1));
 for(let i=0;i<copies;i++)addObject(o.info,o.mesh.geometry,o);if(copies>1)arrange();}
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
 checkPlate();   // first: the list shows each object's current problems
 el('slicerObjects').innerHTML=S.objects.map((o,i)=>{const box=worldBox(o),size=box.getSize(new THREE.Vector3());
  return `<li class="${o===S.selected?'on':''}${o.problems?.length?' bad':''}" data-index="${i}"><strong>${h(o.name)}</strong><small>${size.x.toFixed(1)} × ${size.y.toFixed(1)} × ${size.z.toFixed(1)} mm · ${o.info.triangles.toLocaleString()} triangles</small></li>`;}).join('')||'<li class="muted">Add STL, OBJ or 3MF files with the button above, or drop them on the plate.</li>';
 el('slicerObjects').querySelectorAll('[data-index]').forEach(li=>li.onclick=()=>select(S.objects[+li.dataset.index]));
 ['slicerDuplicate','slicerDelete','slicerReset','slicerMirrorX','slicerMirrorY'].forEach(id=>el(id).disabled=!S.selected);el('slicerSlice').disabled=!S.objects.length;
}

function updateTransformInputs(){const o=S.selected,form=el('slicerTransform');form.hidden=!o;if(!o)return;
 const g=o.group,deg=r=>+(r*180/Math.PI).toFixed(1),box=worldBox(o),size=box.getSize(new THREE.Vector3());
 const values={px:g.position.x,py:g.position.y,pz:g.position.z,rx:deg(g.rotation.x),ry:deg(g.rotation.y),rz:deg(g.rotation.z),sx:g.scale.x*100,sy:g.scale.y*100,sz:g.scale.z*100};
 for(const [k,v] of Object.entries(values)){const input=form.elements[k];if(document.activeElement!==input)input.value=(+v).toFixed(k[0]==='s'?1:2).replace(/\.?0+$/,'');}
 el('slicerSize').textContent=`${size.x.toFixed(1)} × ${size.y.toFixed(1)} × ${size.z.toFixed(1)} mm`;}
function applyTransformInputs(e){const o=S.selected;if(!o)return;const f=el('slicerTransform').elements,n=k=>parseFloat(f[k].value)||0,rad=d=>d*Math.PI/180;
 if(e.target.name?.[0]==='s'&&f.uniform.checked){const v=n(e.target.name);['sx','sy','sz'].forEach(k=>f[k].value=v);}
 const scale=['sx','sy','sz'].map(k=>{const v=n(k);return (Math.abs(v)<0.1?(v<0?-0.1:0.1):v)/100;});   // negative: mirrored
 o.group.position.set(n('px'),n('py'),n('pz'));o.group.rotation.set(rad(n('rx')),rad(n('ry')),rad(n('rz')));o.group.scale.set(...scale);
 dropToBed(o);refreshObjects();updateTransformInputs();}

// ---- presets and settings ----
async function loadCatalog(){
 try{const r=await api('slicer/catalog',undefined,'GET');S.schema=r.schema;S.quick=r.quick||[];S.byKey=Object.fromEntries(S.schema.map(x=>[x.key,x]));S.printers=r.printers;
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
  select.innerHTML=fits.map(p=>`<option${p.name===chosen?' selected':''}>${h(p.name)}</option>`).join('');}
 loadValues();}
// ---- settings: the quick panel next to the plate, and All settings (every OrcaSlicer process and filament setting) ----
// Blank means "as in the preset": the placeholder shows the preset's value (or OrcaSlicer's default).
function presetValue(s){const v=S.values[s.preset]?.[s.key];return v!==undefined&&v!==null?v:s.default;}
function shown(s,v){if(v===undefined||v===null||v==='')return '';
 if(s.type==='bool')return [true,'1','true'].includes(v)?'On':'Off';
 if(s.type==='enum')return (s.options.find(o=>o[0]===String(v))||[0,String(v)])[1];
 if(s.type==='percent'&&!String(v).endsWith('%'))return v+'%';return String(v);}
function settingInput(s){const v=S.changes[s.key],now=v===undefined?'':String(v),pre=shown(s,presetValue(s));
 if(s.type==='bool')return `<select data-key="${s.key}"><option value="">${h(pre?'Preset: '+pre:'Preset')}</option><option value="true"${now==='true'?' selected':''}>On</option><option value="false"${now==='false'?' selected':''}>Off</option></select>`;
 if(s.type==='enum')return `<select data-key="${s.key}"><option value="">${h(pre?'Preset: '+pre:'Preset')}</option>${s.options.map(o=>`<option value="${h(o[0])}"${now===o[0]?' selected':''}>${h(o[1])}</option>`).join('')}</select>`;
 const number=s.type!=='float_or_percent',limits=number?`${s.min!==undefined?` min="${s.min}"`:''}${s.max!==undefined?` max="${s.max}"`:''} step="${s.type==='int'?1:'any'}"`:'';
 return `<input data-key="${s.key}" type="${number?'number':'text'}" inputmode="decimal"${limits} placeholder="${h(pre||'Preset')}" value="${h(now)}">`;}
function settingRow(s,label,help){const changed=s.key in S.changes;
 return `<div class="set-row${changed?' changed':''}" title="${h(s.tooltip||'')}"><label><span>${h(label||s.label)}${s.unit?` <small>${h(s.unit)}</small>`:''}</span>${settingInput(s)}</label>`+
  `<button type="button" class="set-reset" data-reset="${s.key}" title="Back to the preset"${changed?'':' hidden'}>↺</button>${help&&s.tooltip?`<small class="set-help">${h(s.tooltip)}</small>`:''}</div>`;}
function buildSettings(){
 el('slicerSettings').innerHTML=S.quick.map((g,i)=>`<details${i===0?' open':''}><summary>${h(g.group)}</summary>${g.keys.map(k=>S.byKey[k]?settingRow(S.byKey[k],S.byKey[k].quick_label):'').join('')}</details>`).join('');
 updateChanged();}
// A typed value as the Pi expects it, or undefined (blank: back to the preset); throws when it isn't valid.
function settingName(s){return s.quick_label||(s.group&&s.group!==s.label&&s.label.split(' ').length<3?`${s.group}: ${s.label}`:s.label);}   // "Line width: Default"
function parseSetting(s,raw){const v=String(raw).trim();if(v==='')return undefined;const name=settingName(s);
 if(s.type==='bool')return v==='true';if(s.type==='enum')return v;
 if(s.type==='float_or_percent'&&v.endsWith('%')){const n=Number(v.slice(0,-1));if(!Number.isFinite(n))throw new Error(`${name}: a number or a percentage like 50%`);return n+'%';}
 const n=Number(v);if(!Number.isFinite(n))throw new Error(`${name} must be a number.`);
 if(s.type==='int'&&!Number.isInteger(n))throw new Error(`${name} must be a whole number.`);
 if((s.min!==undefined&&n<s.min)||(s.max!==undefined&&n>s.max))throw new Error(`${name} must be between ${s.min??'…'} and ${s.max??'…'}${s.unit?' '+s.unit:''}.`);return n;}
function changeSetting(e){const input=e.target,k=input.dataset?.key;if(!k)return;const s=S.byKey[k];
 try{const v=parseSetting(s,input.value);if(v===undefined)delete S.changes[k];else S.changes[k]=v;input.classList.remove('bad');input.title='';}
 catch(err){input.classList.add('bad');input.title=err.message;el('slicerMessage').textContent=err.message;return;}
 document.querySelectorAll(`[data-key="${k}"]`).forEach(x=>{if(x!==input)x.value=input.value;});
 document.querySelectorAll(`[data-reset="${k}"]`).forEach(b=>{b.hidden=!(k in S.changes);b.closest('.set-row')?.classList.toggle('changed',k in S.changes);});
 updateChanged();}
function resetSetting(e){const b=e.target.closest('[data-reset]');if(!b)return;const k=b.dataset.reset;delete S.changes[k];
 document.querySelectorAll(`[data-key="${k}"]`).forEach(x=>{x.value='';x.classList.remove('bad');});
 document.querySelectorAll(`[data-reset="${k}"]`).forEach(x=>{x.hidden=true;x.closest('.set-row')?.classList.remove('changed');});updateChanged();}
function updateChanged(){const n=Object.keys(S.changes).length;el('slicerChanged').textContent=n?`${n} changed from the presets`:'';
 el('slicerAllOpen').textContent=`All settings (${S.schema.length})${n?` · ${n} changed`:''}`;}
// The selected presets' own values, for the placeholders (a worker sends them; without them, OrcaSlicer's defaults).
async function loadValues(){for(const [kind,id] of [['process','slicerProcess'],['filament','slicerFilament']]){const name=el(id).value;
  try{S.values[kind]=name?(await api(`slicer/values?kind=${kind}&name=${encodeURIComponent(name)}`,undefined,'GET')).values||{}:{};}catch(e){S.values[kind]={};}}
 buildSettings();if(el('slicerAll').open)renderAll();}
// All settings: OrcaSlicer's Process and Filament pages, a search over every setting, and "Changed only".
function openAll(){renderAllNav();renderAll();el('slicerAll').showModal();el('slicerAllSearch').focus();}
function pages(){const out=[];S.schema.forEach(s=>{const id=s.preset+'|'+s.page;if(!out.some(p=>p.id===id))out.push({id,preset:s.preset,page:s.page});});return out;}
function renderAllNav(){el('slicerAllNav').innerHTML=['process','filament'].map(kind=>`<strong>${kind==='process'?'Process':'Filament'}</strong>`+
 pages().filter(p=>p.preset===kind).map(p=>{const n=S.schema.filter(s=>s.preset===p.preset&&s.page===p.page&&s.key in S.changes).length;
  return `<button type="button" data-page="${h(p.id)}"${p.id===S.allPage?' class="on"':''}>${h(p.page)}${n?` <small>${n}</small>`:''}</button>`;}).join('')).join('');}
function renderAll(){const q=el('slicerAllSearch').value.trim().toLowerCase(),only=el('slicerAllChanged').checked;
 const list=S.schema.filter(s=>(q?(s.label+' '+s.key+' '+(s.tooltip||'')+' '+s.group).toLowerCase().includes(q):(only||s.preset+'|'+s.page===S.allPage))&&(!only||s.key in S.changes));
 const groups=[];list.forEach(s=>{const g=(q||only?(s.preset==='process'?'Process':'Filament')+' › '+s.page+' › ':'')+s.group;let x=groups.find(y=>y.name===g);if(!x)groups.push(x={name:g,items:[]});x.items.push(s);});
 el('slicerAllList').innerHTML=groups.map(g=>`<section><h3>${h(g.name)}</h3>${g.items.map(s=>settingRow(s,null,true)).join('')}</section>`).join('')||`<p class="muted">${only?'No settings changed from the presets.':'No setting matches.'}</p>`;
 el('slicerAllNav').hidden=!!q;}
// ---- slicing ----
async function slice(){
 if(!S.objects.length)return;const problems=checkPlate();
 if(problems.length&&!confirm('Some objects have problems:\n'+problems.join('\n')+'\n\nSlice anyway? OrcaSlicer may refuse.'))return;
 const body={label:el('slicerLabel').value||S.objects[0].name.replace(/\.(stl|obj|3mf)$/i,''),
  settings:{machine:el('slicerMachine').value,process:el('slicerProcess').value,filament:el('slicerFilament').value},changes:S.changes,options:sliceOptions(),
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
  <div class="slice-actions">${j.has_result?`<button data-preview="${h(j.id)}"${S.preview?.job===j.id?' class="on"':''}>Preview</button><a class="button" href="/api/slicer/jobs/${h(j.id)}/result">Download</a><select data-printer="${h(j.id)}" aria-label="Printer">${printers}</select><button data-queue="${h(j.id)}">Send to print queue</button>`:''}${active?`<button data-cancel="${h(j.id)}" class="danger">Cancel</button>`:''}</div></li>`;}).join('')||'<li class="muted">Sliced plates appear here. You can leave this page while a plate slices.</li>';
 el('slicerJobs').querySelectorAll('[data-preview]').forEach(b=>b.onclick=()=>openPreview(S.jobs.find(j=>j.id===b.dataset.preview)));
 el('slicerJobs').querySelectorAll('[data-cancel]').forEach(b=>b.onclick=async()=>{try{await api(`slicer/jobs/${b.dataset.cancel}/cancel`,{});}catch(e){notice(e.message);}});
 el('slicerJobs').querySelectorAll('[data-queue]').forEach(b=>b.onclick=async()=>{const printer=el('slicerJobs').querySelector(`[data-printer="${b.dataset.queue}"]`).value;
  try{const r=await api(`slicer/jobs/${b.dataset.queue}/queue`,{printer});notice(`In ${r.printer}'s print queue. Start it from the Print queue when the plate is clear.`);}catch(e){notice(e.message);}});
}
function bytes(n){return n?(n/1073741824).toFixed(1)+' GB':'—';}
function renderWorkers(){
 if(!S.catalog&&!S.loadingCatalog&&S.workers.some(w=>w.online)){S.loadingCatalog=true;loadCatalog().finally(()=>{S.loadingCatalog=false;});}   // a worker just came online
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

// ---- slicing options and saved settings ("My settings") ----
function sliceOptions(){return {arrange:el('slicerAutoArrange').checked,orient:el('slicerAutoOrient').checked};}
async function loadMine(){try{S.mine=(await api('slicer/presets',undefined,'GET')).presets;renderMine();}catch(e){}}
function renderMine(keep){const sel=el('slicerMine'),value=keep??sel.value;
 sel.innerHTML='<option value="">—</option>'+S.mine.map(p=>`<option${p.name===value?' selected':''}>${h(p.name)}</option>`).join('');el('slicerDeleteMine').disabled=!sel.value;}
function applyMine(p){
 if(S.catalog){const m=p.settings?.machine;if(m&&S.catalog.machines.some(x=>x.name===m)&&el('slicerMachine').value!==m){el('slicerMachine').value=m;fillPresets(true);drawPlate();}
  for(const [id,key] of [['slicerProcess','process'],['slicerFilament','filament']]){const v=p.settings?.[key];if(v&&[...el(id).options].some(o=>o.value===v))el(id).value=v;}}
 S.changes={...(p.changes||{})};
 buildSettings();if(el('slicerAll').open)renderAll();
 el('slicerAutoArrange').checked=!!p.options?.arrange;el('slicerAutoOrient').checked=!!p.options?.orient;
 updateChanged();}
async function saveMine(){const name=(prompt('Save these settings as:',el('slicerMine').value||'')||'').trim();if(!name)return;
 try{S.mine=(await api('slicer/presets',{name,settings:{machine:el('slicerMachine').value,process:el('slicerProcess').value,filament:el('slicerFilament').value},changes:S.changes,options:sliceOptions()})).presets;
  renderMine(name);notice(`Saved as "${name}".`);}catch(e){notice(e.message);}}
async function deleteMine(){const name=el('slicerMine').value;if(!name||!confirm(`Delete the saved settings "${name}"?`))return;
 try{S.mine=(await api('slicer/presets/delete',{name})).presets;renderMine('');}catch(e){notice(e.message);}}

// ---- G-code preview: the sliced plate's moves, layer by layer, coloured by feature like OrcaSlicer ----
const FEATURES=[['Outer wall',0xff8a00],['Inner wall',0xffd23f],['Overhang wall',0x3d7bff],['Sparse infill',0xc0392b],['Internal solid infill',0xa05ad8],
 ['Top surface',0xff4d6d],['Bottom surface',0x8c8cff],['Bridge',0x5fb7ff],['Internal Bridge',0x3d8bd9],['Gap infill',0xf5f5f5],['Ironing',0xff9fd0],
 ['Skirt',0x00c2a8],['Brim',0x00c2a8],['Support',0x3ddc84],['Support interface',0x1fa463],['Support transition',0x2e8b57],['Prime tower',0x9aa5b1],['Custom',0x6c7a89]];
const MAX_SEGMENTS=4000000;
// Reads G0-G3 moves (arcs split into short lines), absolute/relative positions and extrusion (G90/G91, M82/M83, G92),
// OrcaSlicer's "; FEATURE:" / ";TYPE:" and "; CHANGE_LAYER" / ";LAYER_CHANGE" comments (else a new layer when Z rises).
function parseGcode(text){
 let cap=1<<18,pos=new Float32Array(cap*6),feat=new Uint8Array(cap),n=0,truncated=false;
 const layers=[],names=FEATURES.map(f=>f[0]),index=name=>{let i=names.indexOf(name);if(i<0){names.push(name);i=names.length-1;}return i;};
 let f=index('Custom'),x=0,y=0,z=0,e=0,absXYZ=true,absE=false,tagged=false,layerZ=-1;
 const newLayer=()=>{if(!layers.length||n>layers[layers.length-1].start)layers.push({start:n,z});};
 const add=(x1,y1,z1,x2,y2,z2)=>{if(n>=MAX_SEGMENTS){truncated=true;return;}
  if(n>=cap){cap*=2;const p=new Float32Array(cap*6);p.set(pos);pos=p;const q=new Uint8Array(cap);q.set(feat);feat=q;}
  pos.set([x1,y1,z1,x2,y2,z2],n*6);feat[n++]=f;};
 for(const raw of text.split('\n')){
  let line=raw;const semi=line.indexOf(';');
  if(semi>=0){const c=line.slice(semi+1);
   if(c.startsWith(' FEATURE: '))f=index(c.slice(10).trim());else if(c.startsWith('TYPE:'))f=index(c.slice(5).trim());
   else if(c.startsWith(' CHANGE_LAYER')||c.startsWith('LAYER_CHANGE')){tagged=true;newLayer();}
   line=line.slice(0,semi);}
  line=line.trim();if(!line)continue;
  const words=line.split(/\s+/),cmd=words[0].toUpperCase();
  if(cmd==='M82'){absE=true;continue;}if(cmd==='M83'){absE=false;continue;}
  if(cmd==='G90'){absXYZ=true;continue;}if(cmd==='G91'){absXYZ=false;continue;}
  const v={};for(let i=1;i<words.length;i++){const k=words[i][0].toUpperCase(),num=parseFloat(words[i].slice(1));if(!Number.isNaN(num))v[k]=num;}
  if(cmd==='G92'){if('E' in v)e=v.E;if('X' in v)x=v.X;if('Y' in v)y=v.Y;if('Z' in v)z=v.Z;continue;}
  if(cmd!=='G0'&&cmd!=='G1'&&cmd!=='G2'&&cmd!=='G3')continue;
  const nx='X' in v?(absXYZ?v.X:x+v.X):x,ny='Y' in v?(absXYZ?v.Y:y+v.Y):y,nz='Z' in v?(absXYZ?v.Z:z+v.Z):z;
  let extruding=false;if('E' in v){extruding=absE?v.E>e+1e-6:v.E>1e-6;if(absE)e=v.E;}
  if(extruding&&(nx!==x||ny!==y)&&!truncated){
   if(!tagged&&nz>layerZ+1e-4){layerZ=nz;z=nz;newLayer();}else if(!layers.length)newLayer();
   if(cmd==='G2'||cmd==='G3'){const cx=x+(v.I||0),cy=y+(v.J||0),r=Math.hypot(x-cx,y-cy);let a0=Math.atan2(y-cy,x-cx),a1=Math.atan2(ny-cy,nx-cx);
    if(cmd==='G2'&&a1>=a0)a1-=2*Math.PI;if(cmd==='G3'&&a1<=a0)a1+=2*Math.PI;
    const steps=Math.max(2,Math.min(64,Math.ceil(Math.abs(a1-a0)*r)));let px=x,py=y,pz=z;
    for(let i=1;i<=steps;i++){const t=i/steps,a=a0+(a1-a0)*t,qx=i===steps?nx:cx+r*Math.cos(a),qy=i===steps?ny:cy+r*Math.sin(a),qz=z+(nz-z)*t;add(px,py,pz,qx,qy,qz);px=qx;py=qy;pz=qz;}}
   else add(x,y,z,nx,ny,nz);}
  x=nx;y=ny;z=nz;if(layers.length&&layers[layers.length-1].start===n)layers[layers.length-1].z=z;}
 layers.forEach((l,i)=>l.end=i+1<layers.length?layers[i+1].start:n);
 return {pos,feat,count:n,layers:layers.filter(l=>l.end>l.start),names,truncated};}
async function openPreview(job){if(!job)return;closePreview();
 el('slicerMessage').textContent='Loading the sliced plate…';
 try{const response=await fetch(`/api/slicer/jobs/${encodeURIComponent(job.id)}/gcode`);
  if(!response.ok)throw new Error((await response.json().catch(()=>({}))).error||'The G-code could not be loaded.');
  const parsed=parseGcode(await response.text());if(!parsed.layers.length)throw new Error('The G-code has no printing moves.');
  S.preview={job:job.id,...parsed,lines:null};
  select(null);S.objects.forEach(o=>o.group.visible=false);drawPlate(job.settings.machine);
  const present=[...new Set(parsed.feat.subarray(0,parsed.count))].sort((a,b)=>a-b);
  el('slicerLegend').innerHTML=present.map(i=>{const c=(FEATURES.find(x=>x[0]===parsed.names[i])||FEATURES[FEATURES.length-1])[1];
   return `<label class="checkbox"><input type="checkbox" data-feature="${i}" checked><i class="legend-chip" data-color="#${c.toString(16).padStart(6,'0')}"></i>${h(parsed.names[i])}</label>`;}).join('');
  el('slicerLegend').querySelectorAll('[data-color]').forEach(e=>e.style.background=e.dataset.color);
  el('slicerLayer').max=parsed.layers.length;el('slicerLayer').value=parsed.layers.length;
  el('slicerPreviewTitle').textContent=job.label;el('slicerPreview').hidden=false;el('slicerTools').hidden=true;
  el('slicerMessage').textContent=parsed.truncated?`The plate is very large: the preview shows its first ${MAX_SEGMENTS.toLocaleString()} moves.`:'';
  buildPreview();framePreview();renderJobs();}
 catch(e){el('slicerMessage').textContent=e.message;closePreview();}}
// Point the camera at what's printed (a small part on a big plate would be tiny otherwise).
function framePreview(){const P=S.preview,min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];
 for(let i=0;i<P.count*6;i++){const a=i%3,v=P.pos[i];if(v<min[a])min[a]=v;if(v>max[a])max[a]=v;}
 const c=min.map((v,i)=>(v+max[i])/2),size=Math.max(20,max[0]-min[0],max[1]-min[1],max[2]-min[2]);
 orbit.target.set(c[0],c[1],c[2]);camera.position.set(c[0],c[1]-size*1.6,c[2]+size*1.3);orbit.update();}
// The lines for the features that are ticked; the layer slider shows layers 1 to N of them.
function buildPreview(){const P=S.preview;if(!P)return;if(P.lines){scene.remove(P.lines);P.lines.geometry.dispose();}
 const on=new Set([...el('slicerLegend').querySelectorAll('[data-feature]:checked')].map(c=>+c.dataset.feature));
 const colours=P.names.map(name=>new THREE.Color((FEATURES.find(x=>x[0]===name)||FEATURES[FEATURES.length-1])[1]));
 let total=0;for(let i=0;i<P.count;i++)if(on.has(P.feat[i]))total++;
 const pos=new Float32Array(total*6),col=new Float32Array(total*6);let k=0;P.ends=[];
 for(const layer of P.layers){for(let i=layer.start;i<layer.end;i++){if(!on.has(P.feat[i]))continue;
   pos.set(P.pos.subarray(i*6,i*6+6),k*6);const c=colours[P.feat[i]];col.set([c.r,c.g,c.b,c.r,c.g,c.b],k*6);k++;}P.ends.push(k);}
 const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.BufferAttribute(pos,3));geometry.setAttribute('color',new THREE.BufferAttribute(col,3));
 P.lines=new THREE.LineSegments(geometry,new THREE.LineBasicMaterial({vertexColors:true}));scene.add(P.lines);showLayers();}
function showLayers(){const P=S.preview;if(!P?.lines)return;const layer=Math.max(1,Math.min(P.layers.length,+el('slicerLayer').value||1));
 P.lines.geometry.setDrawRange(0,(P.ends[layer-1]||0)*2);
 el('slicerLayerText').textContent=`Layer ${layer} of ${P.layers.length} · Z ${P.layers[layer-1].z.toFixed(2)} mm`;}
function closePreview(){const P=S.preview;if(!P)return;if(P.lines){scene.remove(P.lines);P.lines.geometry.dispose();}S.preview=null;
 el('slicerPreview').hidden=true;el('slicerTools').hidden=false;S.objects.forEach(o=>o.group.visible=true);drawPlate();renderJobs();}
