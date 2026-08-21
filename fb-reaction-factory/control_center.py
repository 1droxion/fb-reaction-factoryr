#!/usr/bin/env python3
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from flask import jsonify, render_template_string

import dashboard as core

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
HISTORY_FILE = DATA / "dashboard_history.json"
AUTOPILOT_STATE_FILE = DATA / "autopilot_state.json"
APPROVED_URLS_FILE = DATA / "approved_urls.txt"
HISTORY_LOCK = threading.Lock()
ORIGINAL_PROCESS_JOB = core.process_job

DATA.mkdir(parents=True, exist_ok=True)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_history(entry):
    with HISTORY_LOCK:
        history = load_json(HISTORY_FILE, [])
        if not isinstance(history, list):
            history = []
        history.insert(0, entry)
        save_json(HISTORY_FILE, history[:200])


def tracked_process_job(job_id, url, publish_ig, publish_fb, rights_ok):
    started_at = now_iso()
    ORIGINAL_PROCESS_JOB(job_id, url, publish_ig, publish_fb, rights_ok)
    state = core.snapshot()
    result = state.get("result") or {}
    success = state.get("state") == "done"
    append_history({
        "id": job_id,
        "status": "success" if success else "failed",
        "started_at": started_at,
        "finished_at": now_iso(),
        "source_url": url,
        "source_duration": result.get("source_duration"),
        "published_instagram": bool(result.get("published_instagram")),
        "published_facebook_page": bool(result.get("published_facebook_page")),
        "instagram_permalink": result.get("instagram_permalink"),
        "error": None if success else state.get("error"),
    })


core.process_job = tracked_process_job


def approved_urls():
    if not APPROVED_URLS_FILE.exists():
        return []
    return [
        line.strip()
        for line in APPROVED_URLS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def analytics():
    history = load_json(HISTORY_FILE, [])
    if not isinstance(history, list):
        history = []
    state = load_json(AUTOPILOT_STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}

    processed = state.get("processed") or {}
    failed = state.get("failed") or {}
    urls = approved_urls()
    auto_posted = sum(1 for value in processed.values() if value == "success")
    skipped = sum(
        1 for value in failed.values()
        if isinstance(value, dict) and value.get("skip")
    )
    waiting = max(0, len(urls) - auto_posted - skipped)

    ok = [item for item in history if item.get("status") == "success"]
    bad = [item for item in history if item.get("status") == "failed"]
    finished = len(ok) + len(bad)
    rate = round(100 * len(ok) / finished) if finished else 100
    published = sum(
        1 for item in ok
        if item.get("published_instagram") or item.get("published_facebook_page")
    )

    return {
        "automation_on": True,
        "cadence_hours": 3,
        "autopilot_posted": auto_posted,
        "queue_total": len(urls),
        "queue_waiting": waiting,
        "last_success": state.get("last_success"),
        "next_run": state.get("next_run"),
        "last_error": state.get("last_error"),
        "dashboard_success": len(ok),
        "dashboard_failed": len(bad),
        "dashboard_published": published,
        "success_rate": rate,
        "recent": history[:15],
    }


CONTROL_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reaction Factory Control Center</title>
<style>
:root{--bg:#07090f;--card:#0f151f;--card2:#0a0f17;--line:#202a38;--text:#f7f8fb;--muted:#8d9aac;--purple:#735cff;--purple2:#9a8cff;--green:#2dd881;--red:#ff6075;--yellow:#ffca5c}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 50% -10%,#1b1741 0,#0b0e16 35%,var(--bg) 72%);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh}
main{max-width:1160px;margin:auto;padding:28px 18px 60px}.top{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:18px}.brand{display:flex;gap:11px;align-items:center}.logo{width:44px;height:44px;border-radius:14px;background:linear-gradient(145deg,var(--purple2),#4637dc);display:grid;place-items:center;font-weight:900}.brand h1{margin:0;font-size:25px;letter-spacing:-.5px}.brand p{margin:4px 0 0;color:var(--muted);font-size:12px}.live{display:flex;gap:8px;align-items:center;border:1px solid #25513e;background:#0d2118;color:#80f2b2;border-radius:999px;padding:9px 12px;font-size:11px;font-weight:850}.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 5px #2dd8811f}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:13px}.stat,.card{background:linear-gradient(180deg,#111722ee,#0b1018ee);border:1px solid var(--line);border-radius:18px}.stat{padding:14px}.stat span{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.65px;font-weight:800}.stat strong{display:block;font-size:23px;margin-top:6px}.stat small{color:#6e7b8e;font-size:10px}
.layout{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(300px,.7fr);gap:13px}.card{padding:18px}.card h2{font-size:15px;margin:0 0 4px}.sub{font-size:11px;color:var(--muted);margin-bottom:14px}
.urlrow{display:flex;gap:9px}.url{flex:1;min-width:0;background:#070b11;color:white;border:1px solid #293446;border-radius:12px;padding:15px;font-size:15px;outline:none}.url:focus{border-color:var(--purple)}
button{min-height:49px;border:0;border-radius:12px;background:linear-gradient(135deg,var(--purple2),#5242ea);color:white;padding:0 18px;font-weight:850;cursor:pointer}button:disabled{opacity:.45;cursor:not-allowed}
.opts{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}.opt{display:flex;gap:8px;padding:11px;background:var(--card2);border:1px solid var(--line);border-radius:12px}.opt input,.rights input{accent-color:var(--purple)}.opt b{display:block;font-size:12px}.opt small{color:var(--muted);font-size:10px;line-height:1.4}.rights{margin-top:11px;font-size:11px;color:#c7ced8}.rights label{display:flex;gap:7px;align-items:flex-start}.notice{margin-top:10px;background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:9px 11px;color:#99a6b7;font-size:10px}
.steps{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin-top:13px}.step{background:var(--card2);border:1px solid var(--line);border-radius:12px;padding:11px;min-height:78px}.step i{display:grid;place-items:center;width:24px;height:24px;border-radius:50%;background:#192231;color:#8794a7;font-style:normal;font-size:10px;font-weight:900;margin-bottom:7px}.step b{font-size:11px}.step small{display:block;color:var(--muted);font-size:9px;margin-top:3px}.step.running{border-color:#735cff88}.step.running i{background:var(--purple);color:white}.step.done{border-color:#2dd88155}.step.done i{background:#173b2b;color:var(--green)}.step.ready{border-color:#ffca5c55}.step.ready i{background:#3d311b;color:var(--yellow)}.step.error{border-color:#ff607555}.step.error i{background:#3c1b24;color:var(--red)}
.status{margin-top:11px;background:#080d14;border:1px solid var(--line);border-radius:12px;padding:12px}.statushead{display:flex;justify-content:space-between;gap:10px;font-size:11px;font-weight:750}.pct{color:var(--muted)}.track{height:7px;background:#161f2c;border-radius:99px;overflow:hidden;margin-top:9px}.bar{height:100%;width:0;background:linear-gradient(90deg,#5140e9,#9b8cff);transition:width .3s}.err{display:none;margin-top:10px;background:#31131a;border:1px solid #622532;color:#ffd2da;padding:10px;border-radius:10px;font-size:11px;white-space:pre-wrap}
.result{display:none;margin-top:12px;background:var(--card2);border:1px solid #273244;border-radius:13px;padding:12px}.resultgrid{display:grid;grid-template-columns:190px 1fr;gap:12px}.result video{width:100%;border-radius:10px;background:#000}.actions{display:flex;gap:7px;flex-wrap:wrap}.a{display:inline-flex;padding:9px 11px;border-radius:9px;background:#1b2432;border:1px solid #2c384a;color:white;text-decoration:none;font-size:10px;font-weight:800}.a.primary{background:var(--purple);border-color:var(--purple)}.caption{margin-top:9px;background:#070b11;border-radius:9px;padding:10px;color:#c9d1dc;font-size:10px;white-space:pre-wrap;max-height:150px;overflow:auto}
.side{display:grid;gap:13px}.row{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid #1c2532}.row:last-child{border:0}.row span{font-size:11px;color:var(--muted)}.row strong{font-size:11px;text-align:right;max-width:65%;overflow-wrap:anywhere}.green{color:var(--green)}.red{color:var(--red)}
.history{margin-top:13px}.tablewrap{border:1px solid var(--line);border-radius:13px;overflow-x:auto}table{width:100%;border-collapse:collapse;min-width:760px;background:var(--card2)}th,td{padding:10px 11px;border-bottom:1px solid #1d2633;text-align:left;font-size:10px}th{color:#7d8a9c;text-transform:uppercase;letter-spacing:.5px;background:#0d131c}.badge{padding:5px 7px;border-radius:99px;font-size:9px;font-weight:900}.badge.ok{background:#123124;color:#71e7a3}.badge.bad{background:#351720;color:#ff9bab}.source{max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.empty{text-align:center;padding:20px;color:var(--muted);font-size:11px}.foot{text-align:center;color:#667386;font-size:9px;margin-top:16px}
@media(max-width:880px){.layout{grid-template-columns:1fr}.stats{grid-template-columns:1fr 1fr}.resultgrid{grid-template-columns:1fr}.result video{max-width:300px}}@media(max-width:600px){main{padding:20px 11px 45px}.top{align-items:flex-start;flex-direction:column}.urlrow{flex-direction:column}.opts{grid-template-columns:1fr}.steps{grid-template-columns:1fr 1fr}button{width:100%}}
</style>
</head>
<body><main>
<div class="top">
  <div class="brand"><div class="logo">RF</div><div><h1>Reaction Factory</h1><p>One dashboard for download, edit, publish and analysis.</p></div></div>
  <div class="live"><span class="dot"></span>AUTOPILOT ON · 3 HOURS</div>
</div>

<div class="stats">
  <div class="stat"><span>Auto posts</span><strong id="autoPosts">0</strong><small>Autopilot successes</small></div>
  <div class="stat"><span>Queue</span><strong id="queue">0</strong><small>Waiting URLs</small></div>
  <div class="stat"><span>Success</span><strong id="rate">100%</strong><small>Dashboard jobs</small></div>
  <div class="stat"><span>Next run</span><strong id="nextShort">—</strong><small>Cloud autopilot</small></div>
</div>

<div class="layout">
<section class="card">
  <h2>Make & post a Reel</h2><div class="sub">Paste an approved source URL. Instagram publishing is on by default.</div>
  <div class="urlrow"><input id="url" class="url" placeholder="Paste Instagram / Facebook / Google Drive / video URL"><button id="start">Download → Edit → Post</button></div>
  <div class="opts">
    <label class="opt"><input id="ig" type="checkbox" checked><div><b>Instagram auto-post</b><small>Publish the finished Reel automatically.</small></div></label>
    <label class="opt"><input id="fb" type="checkbox"><div><b>Facebook Page too</b><small>Optional second publish destination.</small></div></label>
  </div>
  <div class="rights"><label><input id="rights" type="checkbox"> I own this source or have permission/license to download, edit and republish it.</label></div>
  <div class="notice">1080×1920 · 30% reaction / 70% source · max 60 sec · reaction loops until the Reel ends.</div>
  <div class="steps">
    <div id="s-download" class="step"><i>1</i><b>Download</b><small>Get source</small></div>
    <div id="s-edit" class="step"><i>2</i><b>Edit</b><small>30/70 + loop</small></div>
    <div id="s-caption" class="step"><i>3</i><b>Caption</b><small>Title + tags</small></div>
    <div id="s-publish" class="step"><i>4</i><b>Publish</b><small>Instagram API</small></div>
  </div>
  <div class="status"><div class="statushead"><span id="msg">Ready.</span><span id="pct" class="pct">0%</span></div><div class="track"><div id="bar" class="bar"></div></div></div>
  <div id="error" class="err"></div>
  <div id="result" class="result"><div class="resultgrid"><video id="preview" controls playsinline></video><div><div class="actions"><a id="videoLink" class="a primary" target="_blank">Open video</a><a id="igLink" class="a" target="_blank" style="display:none">Open Instagram</a></div><div id="caption" class="caption"></div></div></div></div>
</section>

<aside class="side">
<section class="card"><h2>Automation</h2><div class="sub">Live status.</div>
  <div class="row"><span>Status</span><strong class="green">ON</strong></div>
  <div class="row"><span>Cadence</span><strong>Every 3 hours</strong></div>
  <div class="row"><span>Last success</span><strong id="lastSuccess">—</strong></div>
  <div class="row"><span>Next post</span><strong id="nextRun">—</strong></div>
  <div class="row"><span>Last error</span><strong id="lastError">None</strong></div>
</section>
<section class="card"><h2>Analysis</h2><div class="sub">Dashboard performance.</div>
  <div class="row"><span>Successful</span><strong id="dashOk">0</strong></div>
  <div class="row"><span>Failed</span><strong id="dashBad">0</strong></div>
  <div class="row"><span>Published</span><strong id="dashPublished">0</strong></div>
  <div class="row"><span>Success rate</span><strong id="dashRate">100%</strong></div>
</section>
</aside>
</div>

<section class="card history"><h2>Recent activity</h2><div class="sub">Your latest dashboard jobs.</div><div class="tablewrap">
<table><thead><tr><th>Status</th><th>Time</th><th>Source</th><th>Length</th><th>Instagram</th><th>Details</th></tr></thead><tbody id="history"></tbody></table>
<div id="empty" class="empty">No dashboard jobs yet.</div>
</div></section>
<div class="foot">Reaction Factory · approved/licensed sources only</div>
</main>
<script>
const names=["download","edit","caption","publish"];let timer=null;
const $=id=>document.getElementById(id);
function fmt(v){if(!v)return"—";const d=new Date(v);return Number.isNaN(d.getTime())?v:d.toLocaleString([],{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"})}
function short(v){if(!v)return"—";const d=new Date(v);return Number.isNaN(d.getTime())?"—":d.toLocaleTimeString([],{hour:"numeric",minute:"2-digit"})}
function paint(n,s){const e=$("s-"+n);e.className="step";if(["running","done","ready","error"].includes(s))e.classList.add(s)}
async function poll(){
 try{
  const r=await fetch("/api/status"),d=await r.json();$("msg").textContent=d.message||"";$("pct").textContent=(d.progress||0)+"%";$("bar").style.width=(d.progress||0)+"%";
  names.forEach(n=>paint(n,(d.steps||{})[n]));
  if(d.state==="error"){$("error").style.display="block";$("error").textContent=d.error||"Unknown error";$("start").disabled=false;if(timer)clearInterval(timer);refresh()}
  else $("error").style.display="none";
  if(d.state==="done"){$("start").disabled=false;if(timer)clearInterval(timer);show(d.result);refresh()}
 }catch(e){console.error(e)}
}
function show(r){if(!r)return;$("result").style.display="block";$("videoLink").href=r.video_url;$("preview").src=r.video_url;$("preview").load();$("caption").textContent=r.caption||"";if(r.instagram_permalink){$("igLink").href=r.instagram_permalink;$("igLink").style.display="inline-flex"}else $("igLink").style.display="none"}
async function start(){
 const url=$("url").value.trim();if(!url)return alert("Paste a source URL first.");if(!$("rights").checked)return alert("Confirm source rights/permission first.");
 $("start").disabled=true;$("result").style.display="none";$("error").style.display="none";
 try{
  const r=await fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url,publish_instagram:$("ig").checked,publish_facebook:$("fb").checked,rights_ok:true})});
  const d=await r.json();if(!r.ok)throw new Error(d.error||"Could not start");await poll();if(timer)clearInterval(timer);timer=setInterval(poll,1000)
 }catch(e){alert(e.message||String(e));$("start").disabled=false}
}
async function refresh(){
 try{
  const r=await fetch("/api/control-analytics"),d=await r.json();
  $("autoPosts").textContent=d.autopilot_posted||0;$("queue").textContent=d.queue_waiting||0;$("rate").textContent=(d.success_rate??100)+"%";$("nextShort").textContent=short(d.next_run);
  $("lastSuccess").textContent=fmt(d.last_success);$("nextRun").textContent=fmt(d.next_run);$("lastError").textContent=d.last_error||"None";$("lastError").className=d.last_error?"red":"green";
  $("dashOk").textContent=d.dashboard_success||0;$("dashBad").textContent=d.dashboard_failed||0;$("dashPublished").textContent=d.dashboard_published||0;$("dashRate").textContent=(d.success_rate??100)+"%";
  const body=$("history");body.innerHTML="";const recent=d.recent||[];$("empty").style.display=recent.length?"none":"block";
  recent.forEach(item=>{const tr=document.createElement("tr");const s=document.createElement("td"),b=document.createElement("span");b.className="badge "+(item.status==="success"?"ok":"bad");b.textContent=item.status==="success"?"SUCCESS":"FAILED";s.appendChild(b);
   const time=document.createElement("td");time.textContent=fmt(item.finished_at);const src=document.createElement("td");src.className="source";src.title=item.source_url||"";src.textContent=item.source_url||"—";
   const len=document.createElement("td");len.textContent=item.source_duration?item.source_duration+"s":"—";const ig=document.createElement("td");
   if(item.instagram_permalink){const a=document.createElement("a");a.className="a";a.textContent="Open";a.href=item.instagram_permalink;a.target="_blank";ig.appendChild(a)}else ig.textContent=item.published_instagram?"Published":"—";
   const detail=document.createElement("td");detail.textContent=item.error||"Completed";[s,time,src,len,ig,detail].forEach(x=>tr.appendChild(x));body.appendChild(tr)})
 }catch(e){console.error(e)}
}
$("start").addEventListener("click",start);$("url").addEventListener("keydown",e=>{if(e.key==="Enter")start()});poll();refresh();setInterval(refresh,15000);
</script>
</body></html>'''


def control_home():
    return render_template_string(CONTROL_HTML)


core.app.view_functions["home"] = control_home


@core.app.get("/api/control-analytics")
def api_control_analytics():
    return jsonify(analytics())


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f"Reaction Factory Control Center: http://127.0.0.1:{port}")
    core.app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
