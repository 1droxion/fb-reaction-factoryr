#!/usr/bin/env python3
import os
import re
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_from_directory

from auto_pipeline import download_url
from facebook import publish_reel as publish_facebook
from instagram import publish_reel as publish_instagram
from instagram_download import download_instagram_reel, is_instagram_url
from metadata import generate_metadata
from prepare_reel import write_package
from reaction_factory import ffprobe_duration, make_reel

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
ENV_FILE = ROOT / ".env"
MIN_SOURCE_SECONDS = 4.0
TARGET_REEL_SECONDS = 60

app = Flask(__name__)
JOB_LOCK = threading.Lock()
CURRENT_JOB = {
    "id": None,
    "state": "idle",
    "stage": "idle",
    "progress": 0,
    "message": "Paste a Reel URL to begin.",
    "steps": {
        "download": "waiting",
        "edit": "waiting",
        "caption": "waiting",
        "publish": "waiting",
    },
    "result": None,
    "error": None,
}


def load_env_file():
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()


def clean_caption_seed(source: Path):
    raw = source.stem.replace("_", " ").replace("-", " ").strip()
    compact = re.sub(r"[^A-Za-z0-9]", "", raw)
    lower = raw.lower()
    if (
        re.fullmatch(r"[0-9a-fA-F]{16,}", compact)
        or lower.startswith("approved ")
        or lower.startswith("instagram ")
        or lower.startswith("reel ")
    ):
        return "funny reaction"
    return raw or "funny reaction"


def snapshot():
    with JOB_LOCK:
        return {
            "id": CURRENT_JOB["id"],
            "state": CURRENT_JOB["state"],
            "stage": CURRENT_JOB["stage"],
            "progress": CURRENT_JOB["progress"],
            "message": CURRENT_JOB["message"],
            "steps": dict(CURRENT_JOB["steps"]),
            "result": CURRENT_JOB["result"],
            "error": CURRENT_JOB["error"],
        }


def set_job(*, state=None, stage=None, progress=None, message=None, step=None, step_state=None, result=None, error=None):
    with JOB_LOCK:
        if state is not None:
            CURRENT_JOB["state"] = state
        if stage is not None:
            CURRENT_JOB["stage"] = stage
        if progress is not None:
            CURRENT_JOB["progress"] = progress
        if message is not None:
            CURRENT_JOB["message"] = message
        if step is not None and step_state is not None:
            CURRENT_JOB["steps"][step] = step_state
        if result is not None:
            CURRENT_JOB["result"] = result
        if error is not None:
            CURRENT_JOB["error"] = error


def reset_job(job_id):
    with JOB_LOCK:
        CURRENT_JOB.update({
            "id": job_id,
            "state": "running",
            "stage": "download",
            "progress": 5,
            "message": "Starting download...",
            "steps": {
                "download": "running",
                "edit": "waiting",
                "caption": "waiting",
                "publish": "waiting",
            },
            "result": None,
            "error": None,
        })


def process_job(job_id, url, publish_ig, publish_fb, rights_ok):
    try:
        if not rights_ok:
            raise RuntimeError("Confirm that you have rights or permission to reuse this source.")

        set_job(stage="download", progress=8, message="Downloading source Reel...", step="download", step_state="running")
        if is_instagram_url(url):
            source = download_instagram_reel(url)
        else:
            source = download_url(url)
        source = Path(source)

        duration = ffprobe_duration(source)
        if duration < MIN_SOURCE_SECONDS:
            raise RuntimeError(f"Source is only {duration:.1f}s. Need at least {MIN_SOURCE_SECONDS:.0f}s.")

        set_job(progress=30, message=f"Download complete · {duration:.1f}s source", step="download", step_state="done")
        set_job(stage="edit", progress=38, message="Building 60-second reaction Reel...", step="edit", step_state="running")

        caption_seed = clean_caption_seed(source)
        video, reaction_used = make_reel(
            str(source),
            caption=caption_seed,
            reaction="auto",
            rights_ok=True,
        )
        video = Path(video)

        set_job(progress=70, message="Reaction edit complete", step="edit", step_state="done")
        set_job(stage="caption", progress=75, message="Creating clean title, description and hashtags...", step="caption", step_state="running")

        metadata = generate_metadata(caption_seed)
        text_path, json_path, post_text = write_package(
            video,
            metadata,
            {
                "source_url": url,
                "source_duration_seconds": duration,
                "reaction_used": reaction_used,
                "target_reel_seconds": TARGET_REEL_SECONDS,
                "source_looped": duration < TARGET_REEL_SECONDS,
            },
        )

        set_job(progress=84, message="Caption package ready", step="caption", step_state="done")

        publish_results = {}
        if publish_ig or publish_fb:
            load_env_file()
            set_job(stage="publish", progress=88, message="Publishing...", step="publish", step_state="running")

            if publish_ig:
                set_job(message="Publishing to Instagram...")
                publish_results["instagram"] = publish_instagram(video, post_text)

            if publish_fb:
                set_job(message="Publishing to TVMind USA Facebook Page...")
                publish_results["facebook"] = publish_facebook(video, post_text)

            set_job(step="publish", step_state="done")
        else:
            set_job(stage="publish", progress=90, message="Finished Reel is ready for Instagram app upload.", step="publish", step_state="ready")

        ig_permalink = None
        if publish_results.get("instagram"):
            ig_permalink = publish_results["instagram"].get("permalink")

        result = {
            "video_name": video.name,
            "video_url": f"/output/{video.name}",
            "caption": post_text,
            "caption_file": text_path.name,
            "metadata_file": json_path.name,
            "source_duration": round(duration, 1),
            "instagram_permalink": ig_permalink,
            "published_instagram": bool(publish_results.get("instagram")),
            "published_facebook_page": bool(publish_results.get("facebook")),
        }

        if publish_ig and publish_fb:
            message = "Done · published to Instagram and TVMind USA Page."
        elif publish_ig:
            message = "Done · published to Instagram."
        elif publish_fb:
            message = "Done · published to TVMind USA Page."
        else:
            message = "Done · Reel ready. Upload it in the Instagram app for Instagram + personal Facebook."

        set_job(
            state="done",
            stage="done",
            progress=100,
            message=message,
            result=result,
        )
    except Exception as exc:
        with JOB_LOCK:
            running_step = CURRENT_JOB.get("stage")
            if running_step in CURRENT_JOB["steps"]:
                CURRENT_JOB["steps"][running_step] = "error"
        set_job(
            state="error",
            stage="error",
            message="Pipeline stopped.",
            error=str(exc),
        )


DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reaction Factory</title>
<style>
:root{--bg:#070a0f;--panel:#0e131d;--panel2:#121925;--line:#222c3b;--text:#f6f8fb;--muted:#8d99aa;--accent:#6d5dfc;--accent2:#8b7cff;--good:#2dd881;--bad:#ff5d73;--warn:#ffca5c}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 50% -10%,#1b183e 0,#0a0d14 34%,var(--bg) 70%);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh}.wrap{max-width:920px;margin:0 auto;padding:42px 20px 70px}.brand{display:flex;align-items:center;gap:12px;margin-bottom:8px}.logo{width:40px;height:40px;border-radius:13px;background:linear-gradient(145deg,var(--accent2),#4436d8);display:grid;place-items:center;font-weight:900;box-shadow:0 10px 30px #6d5dfc33}.brand h1{font-size:27px;margin:0;letter-spacing:-.5px}.sub{color:var(--muted);margin:0 0 28px 52px}.card{background:linear-gradient(180deg,#111722ee,#0c1119ee);border:1px solid var(--line);border-radius:22px;padding:22px;box-shadow:0 24px 70px #0008}.urlrow{display:flex;gap:10px}.url{flex:1;background:#070b11;border:1px solid #293446;color:white;border-radius:14px;padding:16px 17px;font-size:15px;outline:none}.url:focus{border-color:var(--accent)}button{border:0;border-radius:14px;padding:0 22px;background:linear-gradient(135deg,var(--accent2),#5140e9);color:white;font-size:15px;font-weight:800;cursor:pointer;min-height:52px}button:disabled{opacity:.45;cursor:not-allowed}.options{display:grid;grid-template-columns:1fr 1fr;gap:11px;margin-top:15px}.option{background:#0a0f17;border:1px solid #202a38;border-radius:15px;padding:14px;display:flex;align-items:flex-start;gap:11px}.option input{margin-top:3px;accent-color:var(--accent)}.option strong{display:block;font-size:14px}.option span{display:block;color:var(--muted);font-size:12px;line-height:1.45;margin-top:3px}.rights{margin-top:12px}.rights label{display:flex;gap:9px;align-items:center;color:#cbd2dc;font-size:13px}.rights input{accent-color:var(--accent)}.notice{margin-top:14px;color:#aab4c2;font-size:12px;background:#0a0f17;border:1px solid #202a38;border-radius:12px;padding:11px 13px}.pipeline{margin-top:20px;display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.step{background:#0a0f17;border:1px solid #202a38;border-radius:15px;padding:14px;min-height:92px;transition:.2s}.step .num{width:27px;height:27px;border-radius:50%;display:grid;place-items:center;background:#182130;color:#8491a4;font-weight:800;font-size:12px;margin-bottom:9px}.step b{font-size:13px}.step small{display:block;color:var(--muted);margin-top:4px}.step.running{border-color:#6d5dfc88;box-shadow:inset 0 0 0 1px #6d5dfc22}.step.running .num{background:var(--accent);color:white;animation:pulse 1s infinite}.step.done{border-color:#2dd88155}.step.done .num{background:#173b2b;color:var(--good)}.step.ready{border-color:#ffca5c55}.step.ready .num{background:#3c311b;color:var(--warn)}.step.error{border-color:#ff5d7355}.step.error .num{background:#3d1c25;color:var(--bad)}@keyframes pulse{50%{transform:scale(.9);opacity:.7}}.status{margin-top:16px;background:#090e15;border:1px solid #202a38;border-radius:16px;padding:16px}.statusline{display:flex;justify-content:space-between;gap:10px;align-items:center}.statusmsg{font-size:14px;font-weight:700}.pct{color:#9aa6b7;font-size:13px}.track{height:8px;background:#161f2c;border-radius:999px;overflow:hidden;margin-top:12px}.bar{height:100%;width:0;background:linear-gradient(90deg,#5140e9,#8b7cff);transition:width .35s}.error{display:none;margin-top:12px;color:#ffd4da;background:#31131a;border:1px solid #622532;border-radius:12px;padding:12px;font-size:13px;white-space:pre-wrap}.result{display:none;margin-top:18px;background:#0a0f17;border:1px solid #263142;border-radius:18px;padding:18px}.result h3{margin:0 0 12px;font-size:17px}.actions{display:flex;gap:9px;flex-wrap:wrap}.linkbtn{display:inline-flex;align-items:center;text-decoration:none;background:#1a2331;color:white;border:1px solid #2b3749;padding:11px 14px;border-radius:11px;font-size:13px;font-weight:700}.linkbtn.primary{background:var(--accent);border-color:var(--accent)}.caption{margin-top:13px;padding:13px;background:#070b11;border-radius:12px;color:#c8d0db;font-size:12px;white-space:pre-wrap;max-height:170px;overflow:auto}.personal{margin-top:12px;color:#9faaba;font-size:12px;line-height:1.5}.footer{color:#687487;font-size:11px;text-align:center;margin-top:20px}@media(max-width:700px){.wrap{padding-top:25px}.sub{margin-left:0}.urlrow{flex-direction:column}.options{grid-template-columns:1fr}.pipeline{grid-template-columns:1fr 1fr}button{width:100%}} 
</style>
</head>
<body><div class="wrap">
<div class="brand"><div class="logo">RF</div><h1>Reaction Factory</h1></div>
<p class="sub">Paste one source URL. The factory handles the rest.</p>
<div class="card">
<div class="urlrow"><input id="url" class="url" placeholder="Paste Instagram / Facebook / Google Drive video URL"><button id="start" onclick="startJob()">Start Reaction</button></div>
<div class="options">
<label class="option"><input id="ig" type="checkbox"><div><strong>Auto-publish to Instagram</strong><span>Posts directly through the Instagram API. This does not auto-share to your personal Facebook profile.</span></div></label>
<label class="option"><input id="fb" type="checkbox"><div><strong>Also publish TVMind USA Page</strong><span>Optional. Leave off if you only want the finished Reel for your personal workflow.</span></div></label>
</div>
<div class="rights"><label><input id="rights" type="checkbox"> I confirm I own this source or have permission/license to reuse it.</label></div>
<div class="notice">For <b>Instagram + your personal Facebook profile</b>: leave Auto-publish to Instagram off. When the Reel is ready, open/save the finished MP4 and post it once in the Instagram app with “Also share on… Dhruv Patel” enabled.</div>
<div class="pipeline">
<div id="s-download" class="step"><div class="num">1</div><b>Download</b><small>Get source video</small></div>
<div id="s-edit" class="step"><div class="num">2</div><b>Edit</b><small>Add reaction + 60s</small></div>
<div id="s-caption" class="step"><div class="num">3</div><b>Caption</b><small>Clean title + tags</small></div>
<div id="s-publish" class="step"><div class="num">4</div><b>Publish</b><small>Ready or auto-post</small></div>
</div>
<div class="status"><div class="statusline"><div id="msg" class="statusmsg">Ready</div><div id="pct" class="pct">0%</div></div><div class="track"><div id="bar" class="bar"></div></div><div id="error" class="error"></div></div>
<div id="result" class="result"><h3>Finished Reel</h3><div class="actions"><a id="video" class="linkbtn primary" target="_blank">Open finished video</a><a id="iglink" class="linkbtn" target="_blank" style="display:none">Open Instagram post</a></div><div id="personal" class="personal"></div><div id="caption" class="caption"></div></div>
</div><div class="footer">Reaction Factory · sources require reuse rights/permission</div></div>
<script>
const stepIds=['download','edit','caption','publish'];let timer=null;
function paintStep(name,state){const el=document.getElementById('s-'+name);el.className='step '+(state||'waiting')}
async function startJob(){const url=document.getElementById('url').value.trim();const rights=document.getElementById('rights').checked;if(!url){alert('Paste a source URL first.');return}if(!rights){alert('Confirm reuse rights/permission first.');return}document.getElementById('start').disabled=true;document.getElementById('result').style.display='none';document.getElementById('error').style.display='none';const r=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,publish_instagram:document.getElementById('ig').checked,publish_facebook:document.getElementById('fb').checked,rights_ok:rights})});const data=await r.json();if(!r.ok){alert(data.error||'Could not start');document.getElementById('start').disabled=false;return}poll();if(timer)clearInterval(timer);timer=setInterval(poll,1000)}
async function poll(){const r=await fetch('/api/status');const d=await r.json();document.getElementById('msg').textContent=d.message||'';document.getElementById('pct').textContent=(d.progress||0)+'%';document.getElementById('bar').style.width=(d.progress||0)+'%';for(const n of stepIds)paintStep(n,d.steps[n]);const err=document.getElementById('error');if(d.state==='error'){err.style.display='block';err.textContent=d.error||'Unknown error';document.getElementById('start').disabled=false;if(timer)clearInterval(timer)}if(d.state==='done'){document.getElementById('start').disabled=false;if(timer)clearInterval(timer);showResult(d.result)} }
function showResult(r){if(!r)return;const box=document.getElementById('result');box.style.display='block';document.getElementById('video').href=r.video_url;document.getElementById('caption').textContent=r.caption||'';const ig=document.getElementById('iglink');if(r.instagram_permalink){ig.href=r.instagram_permalink;ig.style.display='inline-flex'}else{ig.style.display='none'}const personal=document.getElementById('personal');if(r.published_instagram){personal.textContent='Published automatically to Instagram. API-published Reels do not trigger your personal Facebook auto-share.'}else{personal.textContent='Personal workflow: post this finished Reel once in the Instagram app. Your “Also share on… Dhruv Patel” setting can then share it to your personal Facebook profile.'}}
poll();
</script></body></html>'''


@app.get("/")
def home():
    return render_template_string(DASHBOARD_HTML)


@app.get("/api/status")
def api_status():
    return jsonify(snapshot())


@app.post("/api/start")
def api_start():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Paste a valid http/https video URL."}), 400

    rights_ok = bool(payload.get("rights_ok"))
    if not rights_ok:
        return jsonify({"error": "Confirm reuse rights/permission first."}), 400

    with JOB_LOCK:
        if CURRENT_JOB["state"] == "running":
            return jsonify({"error": "A Reel is already processing."}), 409

    job_id = uuid.uuid4().hex[:10]
    reset_job(job_id)
    worker = threading.Thread(
        target=process_job,
        args=(
            job_id,
            url,
            bool(payload.get("publish_instagram")),
            bool(payload.get("publish_facebook")),
            rights_ok,
        ),
        daemon=True,
    )
    worker.start()
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/output/<path:filename>")
def output_file(filename):
    return send_from_directory(OUTPUT, filename, as_attachment=False)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f"Reaction Factory dashboard: http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
