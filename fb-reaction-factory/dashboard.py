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
from youtube_short import publish_short

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
INBOX = ROOT / "sources" / "approved_inbox"
ENV_FILE = ROOT / ".env"
MIN_SOURCE_SECONDS = 4.0

app = Flask(__name__)
JOB_LOCK = threading.Lock()
CURRENT_JOB = {
    "id": None,
    "state": "idle",
    "stage": "idle",
    "progress": 0,
    "message": "Paste one URL and choose destinations.",
    "steps": {"download": "waiting", "edit": "waiting", "caption": "waiting", "publish": "waiting"},
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


def clean_caption_seed(source):
    raw = source.stem.replace("_", " ").replace("-", " ").strip()
    compact = re.sub(r"[^A-Za-z0-9]", "", raw)
    if re.fullmatch(r"[0-9a-fA-F]{16,}", compact) or raw.lower().startswith(("approved ", "instagram ", "reel ")):
        return "funny reaction"
    return raw or "funny reaction"


def snapshot():
    with JOB_LOCK:
        return {**CURRENT_JOB, "steps": dict(CURRENT_JOB["steps"])}


def set_job(**kwargs):
    with JOB_LOCK:
        step = kwargs.pop("step", None)
        step_state = kwargs.pop("step_state", None)
        for key, value in kwargs.items():
            if value is not None:
                CURRENT_JOB[key] = value
        if step and step_state:
            CURRENT_JOB["steps"][step] = step_state


def reset_job(job_id):
    with JOB_LOCK:
        CURRENT_JOB.update({
            "id": job_id,
            "state": "running",
            "stage": "download",
            "progress": 5,
            "message": "Starting...",
            "steps": {"download": "running", "edit": "waiting", "caption": "waiting", "publish": "waiting"},
            "result": None,
            "error": None,
        })


def get_source(url):
    return Path(download_instagram_reel(url) if is_instagram_url(url) else download_url(url))


def process_job(job_id, url, rights_ok, post_instagram, post_youtube, post_facebook, youtube_privacy):
    try:
        if not rights_ok:
            raise RuntimeError("Confirm that you have rights or permission/license to reuse this source.")
        if not any((post_instagram, post_youtube, post_facebook)):
            raise RuntimeError("Choose at least one destination.")

        set_job(stage="download", progress=10, message="Downloading source once...", step="download", step_state="running")
        source = get_source(url)
        set_job(progress=28, message="Download complete.", step="download", step_state="done")

        reaction_video = None
        metadata = None
        post_text = ""

        if post_instagram or post_youtube:
            try:
                duration = ffprobe_duration(source)
            except FileNotFoundError as exc:
                raise RuntimeError("Reaction editing needs FFmpeg/ffprobe installed on this computer.") from exc
            if duration < MIN_SOURCE_SECONDS:
                raise RuntimeError(f"Source is only {duration:.1f}s. Need at least 4s.")

            set_job(stage="edit", progress=38, message="Creating one reaction short for Instagram/YouTube...", step="edit", step_state="running")
            seed = clean_caption_seed(source)
            reaction_video, reaction_used = make_reel(str(source), caption=seed, reaction="auto", rights_ok=True)
            reaction_video = Path(reaction_video)
            set_job(progress=68, message="Reaction short ready.", step="edit", step_state="done")

            set_job(stage="caption", progress=73, message="Creating title, caption and tags...", step="caption", step_state="running")
            metadata = generate_metadata(seed)
            _, _, post_text = write_package(reaction_video, metadata, {
                "source_url": url,
                "source_duration_seconds": duration,
                "reaction_used": reaction_used,
                "target_reel_seconds": 60,
                "source_looped": duration < 60,
            })
            set_job(progress=82, message="Metadata ready.", step="caption", step_state="done")
        else:
            set_job(stage="edit", progress=45, message="No reaction edit needed for TV Mind USA Direct.", step="edit", step_state="skipped")
            set_job(stage="caption", progress=65, message="Using fixed TVMind USA caption/tags.", step="caption", step_state="skipped")

        load_env_file()
        set_job(stage="publish", progress=86, message="Publishing selected destinations...", step="publish", step_state="running")

        result = {
            "instagram_permalink": None,
            "youtube_url": None,
            "youtube_channel": None,
            "facebook_video_id": None,
            "reaction_video_url": f"/output/{reaction_video.name}" if reaction_video else None,
            "source_video_url": f"/source/{source.name}",
            "caption": post_text,
        }
        completed = []

        if post_instagram:
            ig = publish_instagram(reaction_video, post_text)
            result["instagram_permalink"] = ig.get("permalink") if ig else None
            completed.append("Instagram")

        if post_youtube:
            hashtags = metadata.get("hashtags", []) if metadata else []
            yt = publish_short(
                reaction_video,
                title=(metadata or {}).get("title") or "Reaction Short 😂",
                description=((metadata or {}).get("description") or "") + "\n\n" + " ".join(hashtags),
                tags=hashtags,
                privacy=youtube_privacy,
            )
            result["youtube_url"] = yt.get("url")
            result["youtube_channel"] = yt.get("channel")
            completed.append("YouTube Shorts")

        if post_facebook:
            fb = publish_facebook(source, "")
            result["facebook_video_id"] = fb.get("video_id") if fb else None
            completed.append("TV Mind USA")

        result["summary"] = "Published to: " + ", ".join(completed)
        set_job(step="publish", step_state="done")
        set_job(state="done", stage="done", progress=100, message=result["summary"], result=result)

    except Exception as exc:
        with JOB_LOCK:
            stage = CURRENT_JOB.get("stage")
            if stage in CURRENT_JOB["steps"]:
                CURRENT_JOB["steps"][stage] = "error"
        set_job(state="error", stage="error", message="Pipeline stopped.", error=str(exc))


DASHBOARD_HTML = r'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reaction Factory</title>
<style>
:root{--bg:#070a0f;--panel:#101722;--line:#263142;--text:#f6f8fb;--muted:#8d99aa;--accent:#6d5dfc;--good:#2dd881;--bad:#ff5d73}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Arial,sans-serif}.wrap{max-width:1050px;margin:auto;padding:34px 18px}.brand{font-size:30px;font-weight:900}.sub{color:var(--muted);margin:6px 0 22px}.card{background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:20px}.destinations{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.dest{border:1px solid var(--line);border-radius:15px;padding:16px;background:#0a0f17}.dest label{display:flex;gap:10px;align-items:flex-start;cursor:pointer}.dest input{margin-top:4px;transform:scale(1.2)}.dest strong{display:block;font-size:18px}.dest span{display:block;color:var(--muted);font-size:12px;line-height:1.5;margin-top:5px}.badge{display:inline-block!important;background:#171f2b;border-radius:999px;padding:5px 8px;font-weight:800;margin-top:9px!important}.row{display:flex;gap:10px;margin-top:16px}.url{flex:1;background:#070b11;border:1px solid var(--line);border-radius:13px;padding:15px;color:#fff;font-size:15px}button{border:0;border-radius:13px;background:var(--accent);color:#fff;padding:0 25px;font-weight:800;min-height:50px;cursor:pointer}.opts{display:flex;gap:10px;margin-top:12px;align-items:center}.opts select{background:#070b11;color:white;border:1px solid var(--line);border-radius:10px;padding:10px}.rights,.notice{margin-top:12px;font-size:13px;color:#c7cfdb}.notice{background:#0a0f17;border:1px solid var(--line);padding:11px;border-radius:12px;color:#aab4c2}.pipe{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin-top:18px}.step{border:1px solid var(--line);border-radius:13px;padding:12px;background:#0a0f17;min-height:78px}.step small{display:block;color:var(--muted);margin-top:4px}.step.running{border-color:var(--accent)}.step.done{border-color:var(--good)}.step.skipped{opacity:.55;border-style:dashed}.step.error{border-color:var(--bad)}.status,.result{margin-top:14px;border:1px solid var(--line);background:#090e15;border-radius:13px;padding:14px}.track{height:7px;background:#18202c;border-radius:99px;margin-top:10px;overflow:hidden}.bar{height:100%;background:var(--accent);width:0}.error{display:none;color:#ffd4da;margin-top:9px}.result{display:none}.actions{display:flex;gap:8px;flex-wrap:wrap}.link{display:inline-block;padding:9px 12px;border-radius:10px;background:#1a2331;color:#fff;text-decoration:none}.summary{color:#cbd2dc;font-size:13px;margin-top:10px}.channel{margin-top:8px;color:#aab4c2;font-size:12px}@media(max-width:800px){.destinations{grid-template-columns:1fr}.pipe{grid-template-columns:1fr 1fr}.row,.opts{flex-direction:column}.url,button,.opts select{width:100%}}
</style></head><body><div class="wrap"><div class="brand">Reaction Factory</div><div class="sub">Paste one URL. Edit once. Publish where you choose.</div><div class="card">
<div class="destinations">
<div class="dest"><label><input id="ig" type="checkbox" checked><div><strong>Instagram Reaction</strong><span>Uses the reaction short.</span><span class="badge">REACTION ON</span></div></label></div>
<div class="dest"><label><input id="yt" type="checkbox"><div><strong>YouTube Shorts</strong><span>Posts the exact same reaction short used for Instagram.</span><span class="badge">SAME EDIT</span></div></label></div>
<div class="dest"><label><input id="fb" type="checkbox"><div><strong>TV Mind USA Direct</strong><span>Posts the original source to Facebook with no reaction edit.</span><span class="badge">ORIGINAL · NO EDIT</span></div></label></div>
</div>
<div class="row"><input id="url" class="url" placeholder="Paste Instagram / Facebook / YouTube / Google Drive URL"><button id="start" onclick="startJob()">Start</button></div>
<div class="opts"><select id="privacy"><option value="public">YouTube: Public</option><option value="unlisted">YouTube: Unlisted</option><option value="private">YouTube: Private</option></select></div>
<div class="rights"><label><input id="rights" type="checkbox"> I confirm I own this source or have permission/license to reuse it.</label></div>
<div class="notice"><b>Instagram + YouTube:</b> one download, one reaction edit, then the same finished short is uploaded to both. <b>TV Mind USA:</b> original video only.</div>
<div class="pipe"><div id="s-download" class="step"><b>1 Download</b><small>Once</small></div><div id="s-edit" class="step"><b>2 Reaction Edit</b><small>Once for IG + YT</small></div><div id="s-caption" class="step"><b>3 Metadata</b><small>Title + tags</small></div><div id="s-publish" class="step"><b>4 Publish</b><small>Selected destinations</small></div></div>
<div class="status"><div><b id="msg">Ready</b><span id="pct" style="float:right">0%</span></div><div class="track"><div id="bar" class="bar"></div></div><div id="error" class="error"></div></div>
<div id="result" class="result"><div class="actions"><a id="iglink" class="link" target="_blank" style="display:none">Open Instagram</a><a id="ytlink" class="link" target="_blank" style="display:none">Open YouTube</a><a id="video" class="link" target="_blank" style="display:none">Open Reaction Video</a></div><div id="summary" class="summary"></div><div id="channel" class="channel"></div></div>
</div></div>
<script>
const steps=['download','edit','caption','publish'];let timer=null;function paint(n,s){document.getElementById('s-'+n).className='step '+(s||'waiting')}
async function startJob(){const url=document.getElementById('url').value.trim();if(!url)return alert('Paste a URL first.');if(!document.getElementById('rights').checked)return alert('Confirm reuse rights first.');const payload={url,rights_ok:true,instagram:document.getElementById('ig').checked,youtube:document.getElementById('yt').checked,facebook:document.getElementById('fb').checked,youtube_privacy:document.getElementById('privacy').value};if(!payload.instagram&&!payload.youtube&&!payload.facebook)return alert('Choose at least one destination.');document.getElementById('start').disabled=true;document.getElementById('result').style.display='none';document.getElementById('error').style.display='none';const r=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),d=await r.json();if(!r.ok){document.getElementById('start').disabled=false;return alert(d.error||'Could not start')}poll();if(timer)clearInterval(timer);timer=setInterval(poll,1000)}
async function poll(){const r=await fetch('/api/status'),d=await r.json();document.getElementById('msg').textContent=d.message||'';document.getElementById('pct').textContent=(d.progress||0)+'%';document.getElementById('bar').style.width=(d.progress||0)+'%';steps.forEach(n=>paint(n,d.steps[n]));if(d.state==='error'){document.getElementById('error').style.display='block';document.getElementById('error').textContent=d.error||'Unknown error';document.getElementById('start').disabled=false;if(timer)clearInterval(timer)}if(d.state==='done'){document.getElementById('start').disabled=false;if(timer)clearInterval(timer);show(d.result)}}
function show(r){if(!r)return;document.getElementById('result').style.display='block';document.getElementById('summary').textContent=r.summary||'';const ig=document.getElementById('iglink'),yt=document.getElementById('ytlink'),v=document.getElementById('video');if(r.instagram_permalink){ig.href=r.instagram_permalink;ig.style.display='inline-block'}else ig.style.display='none';if(r.youtube_url){yt.href=r.youtube_url;yt.style.display='inline-block'}else yt.style.display='none';if(r.reaction_video_url){v.href=r.reaction_video_url;v.style.display='inline-block'}else v.style.display='none';document.getElementById('channel').textContent=r.youtube_channel?'YouTube channel: '+r.youtube_channel:''}poll();
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

    post_instagram = bool(payload.get("instagram"))
    post_youtube = bool(payload.get("youtube"))
    post_facebook = bool(payload.get("facebook"))
    youtube_privacy = str(payload.get("youtube_privacy") or "public").strip()
    if youtube_privacy not in {"public", "unlisted", "private"}:
        return jsonify({"error": "Invalid YouTube privacy setting."}), 400
    if not any((post_instagram, post_youtube, post_facebook)):
        return jsonify({"error": "Choose at least one destination."}), 400

    with JOB_LOCK:
        if CURRENT_JOB["state"] == "running":
            return jsonify({"error": "A video is already processing."}), 409

    job_id = uuid.uuid4().hex[:10]
    reset_job(job_id)
    threading.Thread(
        target=process_job,
        args=(job_id, url, rights_ok, post_instagram, post_youtube, post_facebook, youtube_privacy),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/output/<path:filename>")
def output_file(filename):
    return send_from_directory(OUTPUT, filename, as_attachment=False)


@app.get("/source/<path:filename>")
def source_file(filename):
    return send_from_directory(INBOX, filename, as_attachment=False)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f"Reaction Factory dashboard: http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
