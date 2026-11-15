"use strict";
const $ = id => document.getElementById(id);
const memory = {state:null, logs:new Map(), initialised:false, clearedAt:0, response:null,
  selected:"AMX-500", queryBusy:false, queryPending:false, updating:false, toastTimer:null, controls:new Set()};
const componentNames = {legacy:"LEGACY",broker:"QUEUE",worker:"SYNC WORKER",enquiry:"ENQUIRY API"};
const processNames = {legacy:"stock app",broker:"queue",worker:"sync worker",enquiry:"enquiry API"};
const processHints = {
  legacy:{stop:"Stop the stock application. Stock changes and source checkpoints stop; other processes stay running.",
    start:"Start the stock application and resume publishing its committed events."},
  broker:{stop:"Stop the queue. Stock writes continue; unpublished events wait in the legacy outbox.",
    start:"Start the queue and resume delivery of persisted events."},
  worker:{stop:"Stop synchronisation. Stock writes continue, but new events wait in the queue.",
    start:"Start synchronisation and apply queued events to the enquiry database."},
  enquiry:{stop:"Stop the enquiry API. Stock writes and event processing continue.",
    start:"Start the enquiry API and allow stock checks again."}
};
const escapeHTML = text => String(text ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const timeString = stamp => new Date(stamp * 1000).toLocaleTimeString([], {hour12:false});
const shortId = id => id ? id.slice(0,8) : "—";
const duration = age => age == null ? "—" : `${age.toFixed(1)}s`;

async function api(path, method="GET", body) {
  const response = await fetch(path, {method, headers:{"Content-Type":"application/json","X-Demo-Control":"local"},
    body:body === undefined ? undefined : JSON.stringify(body), signal:AbortSignal.timeout(10000)});
  let data;
  try { data = await response.json(); } catch { data = {detail:"The server did not return JSON."}; }
  return {ok:response.ok, code:response.status, data};
}
function toast(message) {
  $("toast").textContent=message; $("toast").hidden=false;
  clearTimeout(memory.toastTimer); memory.toastTimer=setTimeout(()=>{$("toast").hidden=true;},4500);
}
let hintTarget=null;
function positionHint(){
  if(!hintTarget)return;
  const hint=$("control-hint");
  const rect=hintTarget.getBoundingClientRect();
  if(rect.bottom<0 || rect.top>innerHeight){hideHint();return;}
  const box=hint.getBoundingClientRect();
  hint.style.left=`${Math.max(12,Math.min(rect.left+(rect.width-box.width)/2,innerWidth-box.width-12))}px`;
  hint.style.top=`${rect.bottom+box.height+12<innerHeight?rect.bottom+8:Math.max(8,rect.top-box.height-8)}px`;
}
function hideHint(){
  if(hintTarget){
    const ids=(hintTarget.getAttribute("aria-describedby")||"").split(" ").filter(id=>id && id!=="control-hint");
    if(ids.length)hintTarget.setAttribute("aria-describedby",ids.join(" "));
    else hintTarget.removeAttribute("aria-describedby");
  }
  hintTarget=null;
  $("control-hint").hidden=true;
}
function showHint(target){
  if(!target?.dataset.hint || target.disabled)return;
  hideHint();
  hintTarget=target;
  const hint=$("control-hint");
  hint.textContent=target.dataset.hint;
  // A modal dialog makes outside elements inert, so its tooltip must be inside it.
  (target.closest("dialog") || document.body).appendChild(hint);
  hint.hidden=false;
  const described=(target.getAttribute("aria-describedby")||"").split(" ").filter(Boolean);
  target.setAttribute("aria-describedby",[...described,"control-hint"].join(" "));
  positionHint();
}
document.addEventListener("pointerover",event=>{
  if(event.pointerType==="touch")return;
  const target=event.target.closest("[data-hint]");
  if(target && target!==hintTarget)showHint(target);
});
document.addEventListener("pointerout",event=>{
  if(hintTarget && !hintTarget.contains(event.relatedTarget) && document.activeElement!==hintTarget)hideHint();
});
document.addEventListener("focusin",event=>showHint(event.target.closest("[data-hint]")));
document.addEventListener("focusout",hideHint);
document.addEventListener("click",hideHint);
document.addEventListener("keydown",event=>{if(event.key==="Escape")hideHint();});
window.addEventListener("resize",positionHint);
window.addEventListener("scroll",positionHint,true);

function renderControls(){
  if(!memory.state)return;
  for(const [name,svc] of Object.entries(memory.state.services)){
    const action=svc.reachable?"stop":"start";
    const busy=memory.controls.has(name);
    const label=`${action==="stop"?"Stop":"Start"} ${processNames[name]}`;
    const button=document.querySelector(`[data-process="${name}"]`);
    if(button.dataset.action!==action){
      button.innerHTML=`<span class="button-icon" aria-hidden="true">${action==="stop"?"■":"▶"}</span> ${label}`;
    }
    button.dataset.action=action;
    button.dataset.hint=processHints[name][action];
    button.setAttribute("aria-busy",String(busy));
    button.disabled=busy || svc.status==="starting";
    if(name==="worker"){
      const experiment=$("worker-experiment");
      experiment.querySelector("strong").textContent=label;
      experiment.querySelector(".button-icon").textContent=action==="stop"?"■":"▶";
      experiment.dataset.action=action;
      experiment.dataset.hint=processHints[name][action];
      experiment.setAttribute("aria-busy",String(busy));
      experiment.disabled=button.disabled;
    }
  }
  $("restart-api").disabled=memory.controls.has("enquiry");
  $("restart-api").setAttribute("aria-busy",String(memory.controls.has("enquiry")));
}
function pulse(edge, service) {
  if (edge) document.querySelectorAll(`[data-edge="${edge}"]`).forEach(el=>{
    el.classList.remove("active-edge"); void el.offsetWidth; el.classList.add("active-edge");
    setTimeout(()=>el.classList.remove("active-edge"),1500);
  });
  const card=$(`card-${service}`);
  if(card){card.classList.remove("flash");void card.offsetWidth;card.classList.add("flash");}
}
function updateLegacy() {
  const legacy=memory.state?.services.legacy;
  const item=legacy?.items?.find(item=>item.sku===memory.selected);
  if(item && legacy.reachable){
    $("legacy-quantity").innerHTML=`${item.quantity.toLocaleString()} <small>${escapeHTML(item.unit)}</small>`;
    $("legacy-version").textContent=`Record version ${item.version} · source sequence ${legacy.sequence}`;
    $("input-unit").textContent=item.unit;
  } else {
    $("legacy-quantity").textContent="Offline";
    $("legacy-version").textContent="The authoritative stock process is not reachable.";
  }
  $("update-button").disabled=!legacy?.reachable || memory.updating;
}
function serviceRunning(service){
  return service.reachable && service.status==="running";
}
function renderBoundary(name,components){
  const running=components.filter(serviceRunning).length;
  const starting=components.some(component=>component.status==="starting");
  const reachable=components.some(component=>component.reachable);
  const state=running===components.length?"running":running?"partial":starting?"starting":reachable?"degraded":"stopped";
  const text={running:"Running",partial:"Partial",starting:"Starting",degraded:"Degraded",stopped:"Stopped"}[state];
  $(`${name}-boundary`).dataset.state=state;
  $(`${name}-runtime`).textContent=text+(components.length>1?` · ${running}/${components.length}`:"");
}
function renderState(state) {
  memory.state=state;
  const services=state.services;
  let running=0;
  Object.entries(services).forEach(([name,svc])=>{
    const healthy=serviceRunning(svc);
    if(healthy) running++;
    const impaired=svc.reachable && !healthy;
    $(`dot-${name}`).className=`status-dot ${healthy?"running":impaired?"warning":"stopped"}`;
    $(`meta-${name}`).textContent=svc.reachable?`:${svc.port} · PID ${svc.pid}${impaired?` · ${svc.status}`:""}`:
      svc.status==="starting"?`:${svc.port} · starting…`:`:${svc.port} · stopped`;
    const processState=healthy?"running":svc.reachable?svc.status:svc.status==="starting"?"starting":"stopped";
    $(`card-${name}`).dataset.state=processState;
    $(`card-${name}`).classList.toggle("stopped",processState==="stopped");
  });
  renderBoundary("legacy",[services.legacy]);
  renderBoundary("enquiry",[services.worker,services.enquiry]);
  renderControls();
  $("running-count").textContent=running.toString().padStart(2,"0");
  $("process-dot").style.background=running===4?"#56a477":"#c19351";
  const broker=services.broker;
  const queued=(broker.pending||0)+(broker.leased||0);
  $("queue-count").textContent=broker.reachable?queued.toString().padStart(2,"0"):"—";
  $("diagram-pending").textContent=broker.reachable?queued:"—";
  $("queue-note").textContent=broker.reachable?`${broker.acked} acknowledged · ${broker.dead} dead-letter`:"Queue process is stopped";
  const enquiry=services.enquiry;
  const verification=enquiry.data?.verification;
  const verified=enquiry.reachable && verification?.verified;
  $("data-state").textContent=verified?"Verified":!enquiry.reachable?"API offline":"Unavailable";
  $("data-state").className=`metric-state ${verified?"good":"warning"}`;
  $("feed-age").textContent=verification?`${duration(verification.feed_age)} / ${verification.threshold}s feed limit`:"No verified checkpoint available";
  $("checkpoint-summary").textContent=`Applied checkpoint: #${services.worker.checkpoint?.sequence ?? verification?.sequence ?? "—"} · ${services.worker.duplicates ?? "—"} duplicates ignored`;
  updateLegacy();
  for(const log of state.logs) {
    if(!memory.logs.has(log.id) && memory.initialised && log.at>memory.clearedAt) {
      if(log.details?.kind!=="SourceCheckpoint" || $("show-checkpoints").checked) pulse(log.edge,log.service);
    }
    memory.logs.set(log.id,log);
  }
  if(memory.logs.size>600){
    const ordered=[...memory.logs.values()].sort((a,b)=>b.at-a.at).slice(0,500);
    memory.logs=new Map(ordered.map(log=>[log.id,log]));
  }
  memory.initialised=true;
  renderTimeline();
}
function renderTimeline(){
  const include=$("show-checkpoints").checked;
  const visible=[...memory.logs.values()].filter(log=>log.at>memory.clearedAt &&
    (include || !(log.details?.kind==="SourceCheckpoint" || log.details?.event?.kind==="SourceCheckpoint" ||
      log.action==="checkpoint_created"))).sort((a,b)=>b.at-a.at).slice(0,75);
  $("event-count").textContent=visible.length;
  $("clear-timeline").disabled=visible.length===0;
  $("timeline").innerHTML=visible.length?visible.map(log=>`<button class="timeline-row" data-log-id="${escapeHTML(log.id)}" aria-label="View event: ${escapeHTML(log.message)}">
    <time>${timeString(log.at)}</time><span class="service-chip ${log.service}">${componentNames[log.service]}</span>
    <span class="event-message">${escapeHTML(log.message)}</span><span class="details-link">View event →</span></button>`).join(""):
    '<div class="empty-state">No visible events yet. Change stock, send an enquiry, or include checkpoints.</div>';
}
$("timeline").addEventListener("click",event=>{
  const row=event.target.closest("[data-log-id]"); if(!row)return;
  const log=memory.logs.get(row.dataset.logId); if(!log)return;
  showDetails(`${componentNames[log.service]} · ${log.action}`,
    `${timeString(log.at)} · ${log.message} · correlation ${log.correlation || "not applicable"}`,log);
});
$("show-checkpoints").addEventListener("change",renderTimeline);
$("clear-timeline").addEventListener("click",()=>{memory.clearedAt=Date.now()/1000;renderTimeline();});

async function refresh(){
  try{
    const result=await api("/api/state");
    if(!result.ok)throw new Error("State unavailable");
    renderState(result.data);
    $("connection").textContent=`Live · ${new Date().toLocaleTimeString([],{hour12:false})}`;
  } catch {
    $("connection").textContent="Console disconnected";
    $("data-state").textContent="Disconnected";
    $("data-state").className="metric-state warning";
    $("feed-age").textContent="Displayed values are last observed—not live";
    $("running-count").textContent="—";
    $("queue-count").textContent="—";
    $("update-button").disabled=true;
    for(const name of ["legacy","enquiry"]){
      $(`${name}-boundary`).dataset.state="unknown";
      $(`${name}-runtime`).textContent="Unknown";
    }
    for(const name of Object.keys(processNames)){
      $(`card-${name}`).dataset.state="unknown";
      $(`dot-${name}`).className="status-dot";
    }
  }
}
async function polling(){
  await refresh();
  setTimeout(polling,1000);
}
function renderResponse(result){
  memory.response=result;
  $("response-button").disabled=false;
  const data=result.data;
  const stock=data.stock;
  const valid=result.ok && data.status==="verified" && stock;
  $("result-badge").textContent=valid?"Verified snapshot":`Unavailable · HTTP ${result.code}`;
  $("result-badge").className=`pill ${valid?"good":"warning"}`;
  $("enquiry-result").className=`enquiry-result ${valid?"":"unavailable"}`;
  $("enquiry-result").querySelector(".result-icon").textContent=valid?"✓":"!";
  $("result-label").textContent=valid?stock.name:"Information cannot be verified";
  $("result-quantity").innerHTML=valid?`${stock.quantity.toLocaleString()}<small>${escapeHTML(stock.unit)}</small>`:"Unavailable";
  $("result-description").textContent=valid?(stock.quantity===0?"Valid zero stock. This is a verified business result—not a service failure.":
    "Read from the microservice’s own copy—not from the legacy database."):(data.message || data.detail || "The enquiry process is not reachable.");
  $("result-version").textContent=valid?`v${stock.version}`:"Withheld";
  $("result-feed-age").textContent=data.verification?`${duration(data.verification.feed_age)} / ${data.verification.threshold}s`:"—";
  $("result-latency").textContent=`${result.code}${data.duration_ms!=null?` / ${data.duration_ms}ms`:""}`;
  $("response-timestamp").textContent=`Last response: ${new Date().toLocaleTimeString([],{hour12:false})} · request ${shortId(data.request_id)} · ${$("auto-query").checked?"automatic enquiries enabled":"automatic enquiries paused; this is the last response"}`;
}
async function query(){
  if(memory.queryBusy){memory.queryPending=true;return;}
  memory.queryBusy=true;
  $("query-button").disabled=true;
  $("query-button").setAttribute("aria-busy","true");
  try{
    do{
      memory.queryPending=false;
      const sku=memory.selected;
      try{
        const result=await api(`/api/enquiry/${encodeURIComponent(sku)}`);
        if(memory.selected===sku)renderResponse(result);
      } catch(error){
        if(memory.selected===sku)renderResponse({ok:false,code:503,data:{message:"Console disconnected. No current stock response is available."}});
      }
    }while(memory.queryPending);
  } finally {memory.queryBusy=false;$("query-button").disabled=false;$("query-button").setAttribute("aria-busy","false");}
}
$("query-button").addEventListener("click",query);
$("auto-query").addEventListener("change",()=>{
  if($("auto-query").checked)query();
  else $("response-timestamp").textContent+=" · automatic enquiries PAUSED";
});
$("medicine").addEventListener("change",async()=>{
  memory.selected=$("medicine").value;
  $("request-path").textContent=`GET /inventory/${memory.selected}`;
  updateLegacy();memory.response=null;
  $("response-button").disabled=true;
  $("result-badge").textContent="Loading";
  $("result-badge").className="pill neutral";
  $("enquiry-result").className="enquiry-result";
  $("enquiry-result").querySelector(".result-icon").textContent="…";
  $("result-label").textContent="Fetching selected medicine…";
  $("result-quantity").textContent="—";
  $("result-description").textContent="Waiting for this medicine’s verified stock response.";
  for(const id of ["result-version","result-feed-age","result-latency"])$(id).textContent="—";
  $("response-timestamp").textContent="Waiting for the selected medicine’s response.";
  await query();
});
$("stock-form").addEventListener("submit",async event=>{
  event.preventDefault();
  const quantity=Number($("quantity").value);
  if(!Number.isInteger(quantity)||quantity<0||quantity>1000000){toast("Enter a whole quantity between 0 and 1,000,000.");return;}
  memory.updating=true;$("update-button").disabled=true;$("update-button").setAttribute("aria-busy","true");
  try{
    const result=await api(`/api/legacy/inventory/${encodeURIComponent(memory.selected)}`,"PUT",{quantity});
    if(!result.ok)throw new Error(result.data.detail || "The legacy update failed.");
    const event=result.data.event;
    $("write-feedback").textContent=`Committed · version ${event.version} · event ${shortId(event.event_id)} · sequence #${event.sequence}`;
    toast("Stock committed in the legacy system. Follow its event in the timeline.");
    await refresh();
    setTimeout(query,650);
  } catch(error){
    $("write-feedback").textContent=error.message;toast(error.message);
  } finally{memory.updating=false;$("update-button").setAttribute("aria-busy","false");updateLegacy();}
});
async function control(name,action){
  if(memory.controls.has(name))return;
  memory.controls.add(name);
  renderControls();
  try{
    const result=await api(`/api/control/${name}/${action}`,"POST");
    if(!result.ok)throw new Error(result.data.detail || "Control request failed.");
    const text=`${componentNames[name]}: ${action} requested. This controls its real process.`;
    $("experiment-feedback").textContent=text;toast(text);
    await refresh();setTimeout(refresh,900);
  } catch(error){toast(error.message);}
  finally{memory.controls.delete(name);renderControls();}
}
document.querySelectorAll("[data-process]").forEach(button=>button.addEventListener("click",()=>{
  const name=button.dataset.process;
  control(name,memory.state?.services[name]?.reachable?"stop":"start");
}));
$("worker-experiment").addEventListener("click",()=>control("worker",
  memory.state?.services.worker.reachable?"stop":"start"));
$("restart-api").addEventListener("click",()=>control("enquiry","restart"));
$("replay-event").addEventListener("click",async()=>{
  const button=$("replay-event");button.disabled=true;button.setAttribute("aria-busy","true");
  try{
    const result=await api("/api/demo/replay","POST");
    if(!result.ok)throw new Error(result.data.detail || "Replay failed.");
    const message=`Same stock event #${result.data.event.sequence} replayed. Look for “Duplicate ignored” in the worker log.`;
    $("experiment-feedback").textContent=message;toast(message);await refresh();
  }catch(error){toast(error.message);}finally{button.disabled=false;button.setAttribute("aria-busy","false");}
});
$("security-check").addEventListener("click",async()=>{
  const button=$("security-check");button.disabled=true;button.setAttribute("aria-busy","true");
  try{
    const result=await api("/api/demo/security-check");
    if(!result.ok)throw new Error(result.data.detail || "Security check failed.");
    $("experiment-feedback").textContent=`${result.data.passed?"PASS":"FAIL"} · Direct request without service key returned HTTP ${result.data.http_status}.`;
    showDetails("Access-control check",result.data.message,result.data);
  }catch(error){toast(error.message);}finally{button.disabled=false;button.setAttribute("aria-busy","false");}
});
function showDetails(title,description,payload){
  $("dialog-title").textContent=title;$("dialog-description").textContent=description;
  $("dialog-payload").textContent=JSON.stringify(payload,null,2);$("detail-dialog").showModal();
}
$("response-button").addEventListener("click",()=>{
  if(!memory.response){toast("Send an enquiry first.");return;}
  showDetails("Enquiry API response","Actual HTTP response. Unavailable information is never represented as zero.",memory.response);
});
$("close-dialog").addEventListener("click",()=>$("detail-dialog").close());
$("detail-dialog").addEventListener("click",event=>{if(event.target===$("detail-dialog"))$("detail-dialog").close();});
$("copy-payload").addEventListener("click",async()=>{
  try{await navigator.clipboard.writeText($("dialog-payload").textContent);toast("JSON copied.");}
  catch{toast("Clipboard unavailable. Select the JSON text to copy it.");}
});
polling();
setTimeout(query,1200);
setInterval(()=>{if($("auto-query").checked && !document.hidden)query();},3000);
