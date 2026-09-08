const $ = id => document.getElementById(id);
let state, rfb, lastScreen = 0, busy = false, timer, screenURL, jobsSignature = "";
const labels = {queued:"Queued",running:"Running",verified:"Checks passed",needs_review:"Needs review",failed:"Failed",interrupted:"Interrupted"};
const controlLabels = {agent:"Talos has control",human:"You have control",paused:"Computer paused",stopped:"Computer stopped"};
function show(id, visible) { $(id).classList.toggle("hidden", !visible); }
function notice(message) { $("notice").textContent=message; show("notice",true); clearTimeout(timer); timer=setTimeout(()=>show("notice",false),7000); }

const zoomValues = new Set(["fit","100","125","150","200"]);
let expanded = false;
function preference(key, value) {
 try { if(value===undefined)return localStorage.getItem(key);localStorage.setItem(key,value); } catch {}
}
function applyZoom() {
 const zoom=$("screen-zoom").value, fixed=zoom!=="fit"&&state?.desktop!==false;
 $("screen").classList.toggle("zoomed",fixed);
 const factor=fixed?Number(zoom)/100:1;
 $("screen-content").style.setProperty("--desktop-width",Math.round(($("capture").naturalWidth||1440)*factor)+"px");
 $("screen-content").style.setProperty("--desktop-height",Math.round(($("capture").naturalHeight||900)*factor)+"px");
 $("zoom-note").textContent=fixed?zoom+"% · scroll to explore":"Fit to window";
 // noVNC scales to the same surface as the screenshot, preserving pointer coordinates.
 if(rfb)rfb.scaleViewport=true;
}
function setExpanded(value) {
 expanded=value;document.body.classList.toggle("computer-expanded",value);
 $("expand").textContent=value?"↙ Restore":"↗ Expand";
 $("expand").setAttribute("aria-pressed",String(value));
 $("expand").title=value?"Restore the workspace layout":"Use the whole window";
 preference("talos.computer.expanded",String(value));applyZoom();
}
$("screen-zoom").value=zoomValues.has(preference("talos.computer.zoom"))?preference("talos.computer.zoom"):"fit";
$("screen-zoom").onchange=()=>{preference("talos.computer.zoom",$("screen-zoom").value);applyZoom();};
$("capture").addEventListener("load",applyZoom);
$("expand").onclick=()=>setExpanded(!expanded);
$("fullscreen").onclick=async()=>{
 try {
  if(document.fullscreenElement)await document.exitFullscreen();
  else if($("workspace").requestFullscreen)await $("workspace").requestFullscreen();
  else {setExpanded(true);notice("Expanded to your window. Fullscreen is unavailable in this browser.");}
 } catch {setExpanded(true);notice("Expanded to your window. Use your browser’s fullscreen control for more space.");}
};
document.addEventListener("fullscreenchange",()=>{
 const full=document.fullscreenElement===$("workspace");
 $("fullscreen").textContent=full?"⛶ Exit fullscreen":"⛶ Fullscreen";
 $("fullscreen").setAttribute("aria-pressed",String(full));applyZoom();
});
document.addEventListener("keydown",event=>{
 if(event.key==="Escape"&&!document.fullscreenElement&&expanded){setExpanded(false);$("expand").focus();}
});
setExpanded(preference("talos.computer.expanded")==="true");

async function api(path, body) {
 const response=await fetch(path,{method:body?"POST":"GET",headers:body?{"Content-Type":"application/json"}:{},body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(18000)});
 if(response.status===401){show("login",true);show("workspace",false);throw new Error("Open your personal /computer link.");}
 const value=await response.json();
 if(!response.ok)throw new Error(value.error||"The computer is not responding right now.");
 return value;
}
function node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;}
function renderJobs(jobs) {
 const signature=JSON.stringify([jobs,state.routines]);
 if(signature===jobsSignature)return;
 jobsSignature=signature;$("jobs").replaceChildren();$("job-count").textContent=jobs.length;
 if(!jobs.length)$("jobs").append(node("p","No jobs yet. Ask Talos to use its computer.","empty"));
 for(const job of jobs){
  const card=node("article",undefined,"job"),top=node("div",undefined,"job-top");
  top.append(node("span",job.project),node("time",new Date(job.created*1000).toLocaleTimeString("en-GB",{hour:"2-digit",minute:"2-digit"})));
  card.append(top,node("h3",job.title),node("span",labels[job.state]||job.state,"badge "+job.state));
  const checks=job.result?.checks||[];
  card.append(node("p",checks.length?checks.filter(c=>c.passed).length+" / "+checks.length+" file checks passed":"No independent result check yet."));
  if(job.state==="verified"&&job.operation==="exec"){const save=node("button","↻ Save as routine");save.onclick=()=>saveRoutine(job);card.append(save);}
  if(job.result?.stdout||job.result?.stderr){const details=node("details");details.append(node("summary","View output"),node("pre",(job.result.stdout||"")+"\n"+(job.result.stderr||"")));card.append(details);}
  $("jobs").append(card);
 }
 const selected=$("project").value, names=[...new Set(jobs.map(j=>j.project).filter(Boolean))];
 $("project").replaceChildren(node("option","Select project"));$("project").firstChild.value="";
 for(const name of names){const option=node("option",name);option.value=name;$("project").append(option);}
 if(names.includes(selected))$("project").value=selected;
 $("routines").replaceChildren();
 for(const routine of state.routines||[]){const row=node("div",undefined,"file");row.append(node("span",routine.name),node("span","Source job checked"));$("routines").append(row);}
 if(!state.routines?.length)$("routines").append(node("p","Save a checked job as a private routine.","empty"));
}
async function updateScreen(){
 if(state.desktop===false||state.control==="human"||state.vm!=="running")return;
 if(Date.now()-lastScreen<3500||document.hidden)return;
 lastScreen=Date.now();
 const response=await fetch("/api/screen",{signal:AbortSignal.timeout(16000)});
 if(!response.ok)return;
 const blob=await response.blob(), next=URL.createObjectURL(blob);
 $("capture").src=next;if(screenURL)URL.revokeObjectURL(screenURL);screenURL=next;
 show("capture",true);show("empty-screen",false);
 $("screen-time").textContent=state.vm==="running"?"Live · "+new Date().toLocaleTimeString("en-GB"):"Last frame · paused";
}
async function connectDesktop(){
 if(rfb)return;
 const {default:RFB}=await import("/novnc/core/rfb.js");
 rfb=new RFB($("vnc"),"wss://"+location.host+"/websockify");
 rfb.scaleViewport=true;rfb.resizeSession=false;rfb.viewOnly=false;
 rfb.addEventListener("disconnect",()=>{rfb=null;show("vnc",false);show("capture",true);});
 show("vnc",true);show("capture",false);show("empty-screen",false);
}
async function refresh(){
 if(busy||document.hidden)return;
 busy=true;
 try{
  state=await api("/api/status");
  show("login",false);show("workspace",true);
  $("connection").textContent="Connected";
  $("control-state").textContent=controlLabels[state.control]||state.control;
  $("screen-note").textContent=state.vm==="running"?"":"· paused";
  if(state.desktop!==false&&state.vm!=="running")$("screen-time").textContent="Last frame · paused";
  show("takeover",state.desktop!==false&&state.control!=="human");show("release",state.control==="human");
  show("resume",["paused","stopped"].includes(state.control));
  $("view-mode").textContent=state.control==="human"?"You control the desktop · mouse and keyboard enabled":"Watching · refreshes automatically";
  renderJobs(state.jobs);
  const headless=state.desktop===false;
  $("screen-zoom").disabled=headless;
  applyZoom();
  show("headless",headless);
  $("screen-heading").textContent=headless?"Terminal output":"Live desktop";
  $("screen").classList.toggle("is-headless",headless);
  if(headless){
   show("capture",false);show("empty-screen",false);show("vnc",false);
   $("screen-time").textContent="Headless";
   $("view-mode").textContent="Code, scripts and persistent files";
   const last=state.jobs.find(job=>job.result?.stdout||job.result?.stderr);
   $("terminal-output").textContent=last?(last.result.stdout||"")+"\n"+(last.result.stderr||""):"Output will appear after a job produces it. Files are listed below.";
  }
  if(state.desktop!==false&&state.control==="human")await connectDesktop();else{if(rfb){rfb.disconnect();rfb=null;}await updateScreen();}
 }catch(error){$("connection").textContent="Connection interrupted";if(error.name!=="TimeoutError")$("screen-note").textContent="Your jobs remain saved";}
 finally{busy=false;}
}
async function control(op){
 for(const b of document.querySelectorAll("nav button"))b.disabled=true;
 try{await api("/api/control",{op});await refresh();}catch(error){notice(error.message);}
 finally{for(const b of document.querySelectorAll("nav button"))b.disabled=false;}
}
async function saveRoutine(job){
 const name=prompt("Name this private routine (lowercase, hyphens allowed):",job.project+"-routine");
 if(!name)return;
 try{await api("/api/control",{op:"save_routine",job_id:job.id,name});jobsSignature="";await refresh();notice("Routine saved privately. Each new run still needs its normal approvals.");}catch(error){notice(error.message);}
}
for(const [id,op] of Object.entries({takeover:"takeover",release:"release",pause:"pause",resume:"release",stop:"stop"}))$(id).onclick=()=>control(op);
$("project").onchange=async()=>{const project=$("project").value;if(!project)return;try{const data=await api("/api/files?project="+encodeURIComponent(project));$("files").replaceChildren();for(const file of data.files){const row=node("div",undefined,"file");row.append(node("span",file.name),node("span",Intl.NumberFormat("en-GB").format(file.bytes)+" B"));$("files").append(row);}if(!data.files.length)$("files").append(node("p","This project has no files yet.","empty"));}catch(error){notice(error.message);}};
async function login(token){await api("/api/login",{token});$("access").value="";await refresh();}
$("login-form").onsubmit=async event=>{event.preventDefault();try{await login($("access").value);}catch(error){notice(error.message);}};
const fragment=new URLSearchParams(location.hash.slice(1)), token=fragment.get("token");
if(location.hash)history.replaceState(null,"",location.pathname);
if(token){try{await login(token);}catch(error){notice(error.message);show("login",true);}}else await refresh();
setInterval(refresh,4000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden)refresh();});
