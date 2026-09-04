#!/usr/bin/env python3
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, render_template_string, request, send_file
from werkzeug.utils import secure_filename

from auto_pipeline import download_url, source_context_from_url
from facebook import publish_reel as publish_facebook
from instagram_download import download_instagram_reel, is_instagram_url
from reaction_factory import ffmpeg_exe, ffprobe_duration
from youtube_short import publish_short

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
ENV_FILE = ROOT / ".env"
GAMING_DIR = ROOT / "data" / "gaming"
GAMING_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
MIN_SOURCE_SECONDS = 4.0
MAX_SOURCE_SECONDS = 60.0
GAMING_TAGS = ["Gaming", "Gameplay", "GamingClips", "Gamer", "Shorts"]

gaming_bp = Blueprint("gaming", __name__, url_prefix="/gaming")
GAMING_JOB_LOCK = threading.Lock()
GAMING_JOB = {
    "id": None,
    "state": "idle",
    "stage": "idle",
    "progress": 0,
    "message": "Upload your gaming image, paste a URL, then start.",
    "steps": {"download": "waiting", "edit": "waiting", "metadata": "waiting", "publish": "waiting"},
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


def current_gaming_image():
    matches = sorted(GAMING_DIR.glob("top_image.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def get_source(url):
    return Path(download_instagram_reel(url) if is_instagram_url(url) else download_url(url))


def snapshot():
    with GAMING_JOB_LOCK:
        return {**GAMING_JOB, "steps": dict(GAMING_JOB["steps"])}


def set_job(**kwargs):
    with GAMING_JOB_LOCK:
        step = kwargs.pop("step", None)
        step_state = kwargs.pop("step_state", None)
        for key, value in kwargs.items():
            if value is not None:
                GAMING_JOB[key] = value
        if step and step_state:
            GAMING_JOB["steps"][step] = step_state


def reset_job(job_id):
    with GAMING_JOB_LOCK:
        GAMING_JOB.update({
            "id": job_id,
            "state": "running",
            "stage": "download",
            "progress": 5,
            "message": "Starting gaming pipeline...",
            "steps": {"download": "running", "edit": "waiting", "metadata": "waiting", "publish": "waiting"},
            "result": None,
            "error": None,
        })


def run(cmd):
    print("$", " ".join(str(x) for x in cmd))
    subprocess.run(list(cmd), check=True)


def compose_gaming(source, top_image):
    source = Path(source)
    top_image = Path(top_image)
    source_duration = ffprobe_duration(source)
    if source_duration < MIN_SOURCE_SECONDS:
        raise RuntimeError(f"Source is only {source_duration:.1f}s. Need at least 4s.")
    duration = min(MAX_SOURCE_SECONDS, float(source_duration))
    output = OUTPUT / f"gaming_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.mp4"

    filter_complex = (
        f"[0:v]scale=1080:672:force_original_aspect_ratio=increase,"
        f"crop=1080:672,setsar=1,fps=30,trim=duration={duration:.3f},setpts=PTS-STARTPTS[top];"
        f"[1:v]scale=1080:1248:force_original_aspect_ratio=increase,"
        f"crop=1080:1248,setsar=1,fps=30,trim=duration={duration:.3f},setpts=PTS-STARTPTS[game];"
        "[top][game]vstack=inputs=2[v]"
    )

    run([
        ffmpeg_exe(), "-y",
        "-loop", "1", "-framerate", "30", "-i", str(top_image),
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "1:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-profile:v", "high", "-level:v", "4.1",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(output),
    ])
    return output, duration


def gaming_metadata(url):
    context = source_context_from_url(url) or ""
    context = re.sub(r"https?://\S+", " ", context)
    context = re.sub(r"\s+", " ", context).strip(" -|:.,")
    if context:
        first = re.split(r"[.!?]|\s[-|]\s", context, maxsplit=1)[0].strip()
        title = (first or context)[:82].rstrip(" ,.-")
        if title and "🎮" not in title:
            title += " 🎮"
    else:
        title = "Gaming Moment 🎮"
    description = "Gaming clip 🎮 Follow for more gameplay."
    hashtags = [f"#{tag}" for tag in GAMING_TAGS]
    return {
        "title": title[:100],
        "description": description,
        "hashtags": hashtags,
        "tags": list(GAMING_TAGS),
    }


def process_job(job_id, url, rights_ok, post_facebook, post_youtube, youtube_privacy):
    try:
        if not rights_ok:
            raise RuntimeError("Confirm that you own this source or have permission/license to reuse it.")
        if not (post_facebook or post_youtube):
            raise RuntimeError("Choose Facebook, YouTube, or both.")

        top_image = current_gaming_image()
        if not top_image:
            raise RuntimeError("Upload your gaming top image first.")

        set_job(stage="download", progress=10, message="Downloading gaming source once...", step="download", step_state="running")
        source = get_source(url)
        set_job(progress=28, message="Download complete.", step="download", step_state="done")

        set_job(stage="edit", progress=36, message="Building exact 35% image / 65% gameplay layout...", step="edit", step_state="running")
        gaming_video, duration = compose_gaming(source, top_image)
        set_job(progress=70, message="Gaming edit ready.", step="edit", step_state="done")

        set_job(stage="metadata", progress=74, message="Creating gaming title and 5 gaming tags...", step="metadata", step_state="running")
        metadata = gaming_metadata(url)
        post_text = metadata["description"] + "\n\n" + " ".join(metadata["hashtags"])
        set_job(progress=82, message="Gaming metadata ready.", step="metadata", step_state="done")

        load_env_file()
        set_job(stage="publish", progress=86, message="Publishing gaming clip...", step="publish", step_state="running")
        result = {
            "facebook_video_id": None,
            "youtube_url": None,
            "youtube_channel": None,
            "gaming_video_url": f"/output/{gaming_video.name}",
            "title": metadata["title"],
            "tags": metadata["hashtags"],
            "duration_seconds": duration,
        }
        completed = []

        if post_facebook:
            fb = publish_facebook(gaming_video, post_text, profile="gaming")
            result["facebook_video_id"] = fb.get("video_id") if fb else None
            completed.append("Gaming Facebook")

        if post_youtube:
            yt = publish_short(
                gaming_video,
                title=metadata["title"],
                description=post_text,
                tags=metadata["tags"],
                privacy=youtube_privacy,
                profile="gaming",
            )
            result["youtube_url"] = yt.get("url")
            result["youtube_channel"] = yt.get("channel")
            completed.append("Gaming YouTube")

        result["summary"] = "Published to: " + ", ".join(completed)
        set_job(step="publish", step_state="done")
        set_job(state="done", stage="done", progress=100, message=result["summary"], result=result)
    except Exception as exc:
        with GAMING_JOB_LOCK:
            stage = GAMING_JOB.get("stage")
            if stage in GAMING_JOB["steps"]:
                GAMING_JOB["steps"][stage] = "error"
        set_job(state="error", stage="error", message="Gaming pipeline stopped.", error=str(exc))


GAMING_HTML = r'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Gaming 35/65</title>
<style>
:root{--bg:#070a0f;--panel:#101722;--line:#263142;--text:#f6f8fb;--muted:#8d99aa;--accent:#6d5dfc;--good:#2dd881;--bad:#ff5d73}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Arial,sans-serif}.wrap{max-width:950px;margin:auto;padding:34px 18px}.brand{font-size:30px;font-weight:900}.sub{color:var(--muted);margin:6px 0 22px}.card{background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:20px}.layout{display:grid;grid-template-columns:35fr 65fr;height:180px;border:1px solid var(--line);border-radius:16px;overflow:hidden;margin-bottom:16px}.top,.bottom{display:flex;align-items:center;justify-content:center;font-weight:900}.top{background:#18102d}.bottom{background:#0a0f17}.top small,.bottom small{display:block;color:var(--muted);font-size:11px;text-align:center}.upload{display:flex;gap:12px;align-items:center;justify-content:space-between;border:1px solid var(--line);background:#0a0f17;padding:14px;border-radius:13px}.upload span{color:var(--muted);font-size:12px}.row{display:flex;gap:10px;margin-top:16px}.url{flex:1;background:#070b11;border:1px solid var(--line);border-radius:13px;padding:15px;color:#fff;font-size:15px}button{border:0;border-radius:13px;background:var(--accent);color:#fff;padding:0 22px;font-weight:800;min-height:48px;cursor:pointer}.destinations{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}.dest{border:1px solid var(--line);border-radius:13px;background:#0a0f17;padding:13px}.dest label{display:flex;gap:9px;align-items:center}.opts,.rights{margin-top:12px}.opts select{background:#070b11;color:white;border:1px solid var(--line);border-radius:10px;padding:10px}.tags{margin-top:12px;color:#c7cfdb;font-size:13px}.pipe{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin-top:18px}.step{border:1px solid var(--line);border-radius:13px;padding:12px;background:#0a0f17;min-height:72px}.step small{display:block;color:var(--muted);margin-top:4px}.step.running{border-color:var(--accent)}.step.done{border-color:var(--good)}.step.error{border-color:var(--bad)}.status,.result{margin-top:14px;border:1px solid var(--line);background:#090e15;border-radius:13px;padding:14px}.track{height:7px;background:#18202c;border-radius:99px;margin-top:10px;overflow:hidden}.bar{height:100%;background:var(--accent);width:0}.error{display:none;color:#ffd4da;margin-top:9px}.result{display:none}.link{display:inline-block;padding:9px 12px;border-radius:10px;background:#1a2331;color:#fff;text-decoration:none;margin-right:8px}.summary{color:#cbd2dc;font-size:13px;margin-top:10px}@media(max-width:700px){.row,.upload{flex-direction:column;align-items:stretch}.destinations,.pipe{grid-template-columns:1fr}.url,button{width:100%}}
</style></head><body><div class="wrap"><div class="brand">Gaming 35/65</div><div class="sub">Separate gaming pipeline. Existing auto-posting stays untouched.</div><div class="card">
<div class="layout"><div class="top">35%<small>&nbsp;Gaming Image</small></div><div class="bottom">65%<small>&nbsp;Gameplay URL Video</small></div></div>
<div class="upload"><div><b>Gaming Top Image</b><span id="imageStatus">Checking image...</span></div><div><input id="imageFile" type="file" accept="image/png,image/jpeg,image/webp" style="display:none"><button type="button" onclick="document.getElementById('imageFile').click()">Upload / Replace Image</button></div></div>
<div class="row"><input id="url" class="url" placeholder="Paste gaming video URL"><button id="start" onclick="startJob()">Download · Edit · Post</button></div>
<div class="destinations"><div class="dest"><label><input id="fb" type="checkbox" checked><b>Gaming Facebook Page</b></label></div><div class="dest"><label><input id="yt" type="checkbox" checked><b>Gaming YouTube</b></label></div></div>
<div class="opts"><select id="privacy"><option value="public">YouTube: Public</option><option value="unlisted">YouTube: Unlisted</option><option value="private">YouTube: Private</option></select></div>
<div class="rights"><label><input id="rights" type="checkbox"> I confirm I own this source or have permission/license to reuse it.</label></div>
<div class="tags"><b>5 tags:</b> #Gaming #Gameplay #GamingClips #Gamer #Shorts</div>
<div class="pipe"><div id="s-download" class="step"><b>1 Download</b><small>Once</small></div><div id="s-edit" class="step"><b>2 Edit</b><small>35% / 65%</small></div><div id="s-metadata" class="step"><b>3 Gaming Tags</b><small>Exactly 5</small></div><div id="s-publish" class="step"><b>4 Publish</b><small>FB + YT</small></div></div>
<div class="status"><div><b id="msg">Ready</b><span id="pct" style="float:right">0%</span></div><div class="track"><div id="bar" class="bar"></div></div><div id="error" class="error"></div></div>
<div id="result" class="result"><a id="ytlink" class="link" target="_blank" style="display:none">Open YouTube</a><a id="video" class="link" target="_blank" style="display:none">Open Gaming Video</a><div id="summary" class="summary"></div></div>
</div></div><script>
const steps=['download','edit','metadata','publish'];let timer=null;function paint(n,s){document.getElementById('s-'+n).className='step '+(s||'waiting')}
async function refreshImage(){try{const r=await fetch('/gaming/api/image'),d=await r.json();document.getElementById('imageStatus').textContent=d.ready?'Gaming image ready.':'No gaming image yet.'}catch(e){document.getElementById('imageStatus').textContent='Could not check image.'}}
async function uploadImage(){const input=document.getElementById('imageFile');if(!input.files.length)return;const fd=new FormData();fd.append('file',input.files[0]);document.getElementById('imageStatus').textContent='Uploading...';const r=await fetch('/gaming/api/image/upload',{method:'POST',body:fd}),d=await r.json();if(!r.ok){alert(d.error||'Could not upload image')}else{document.getElementById('imageStatus').textContent='Gaming image ready.'}input.value=''}
document.getElementById('imageFile').addEventListener('change',uploadImage);
async function startJob(){const url=document.getElementById('url').value.trim();if(!url)return alert('Paste a gaming video URL first.');if(!document.getElementById('rights').checked)return alert('Confirm reuse rights first.');const payload={url,rights_ok:true,facebook:document.getElementById('fb').checked,youtube:document.getElementById('yt').checked,youtube_privacy:document.getElementById('privacy').value};if(!payload.facebook&&!payload.youtube)return alert('Choose Facebook, YouTube, or both.');document.getElementById('start').disabled=true;document.getElementById('result').style.display='none';document.getElementById('error').style.display='none';const r=await fetch('/gaming/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),d=await r.json();if(!r.ok){document.getElementById('start').disabled=false;return alert(d.error||'Could not start')}poll();if(timer)clearInterval(timer);timer=setInterval(poll,1000)}
async function poll(){const r=await fetch('/gaming/api/status'),d=await r.json();document.getElementById('msg').textContent=d.message||'';document.getElementById('pct').textContent=(d.progress||0)+'%';document.getElementById('bar').style.width=(d.progress||0)+'%';steps.forEach(n=>paint(n,d.steps[n]));if(d.state==='error'){document.getElementById('error').style.display='block';document.getElementById('error').textContent=d.error||'Unknown error';document.getElementById('start').disabled=false;if(timer)clearInterval(timer)}if(d.state==='done'){document.getElementById('start').disabled=false;if(timer)clearInterval(timer);show(d.result)}}
function show(r){if(!r)return;document.getElementById('result').style.display='block';document.getElementById('summary').textContent=r.summary||'';const yt=document.getElementById('ytlink'),v=document.getElementById('video');if(r.youtube_url){yt.href=r.youtube_url;yt.style.display='inline-block'}else yt.style.display='none';if(r.gaming_video_url){v.href=r.gaming_video_url;v.style.display='inline-block'}else v.style.display='none'}
refreshImage();poll();
</script></body></html>'''


@gaming_bp.get("/")
def gaming_home():
    return render_template_string(GAMING_HTML)


@gaming_bp.get("/api/status")
def gaming_status():
    return jsonify(snapshot())


@gaming_bp.get("/api/image")
def gaming_image_status():
    image = current_gaming_image()
    return jsonify({"ready": bool(image), "filename": image.name if image else None})


@gaming_bp.get("/image")
def gaming_image_preview():
    image = current_gaming_image()
    if not image:
        return jsonify({"error": "No gaming image uploaded."}), 404
    return send_file(image)


@gaming_bp.post("/api/image/upload")
def gaming_image_upload():
    uploaded = request.files.get("file")
    filename = secure_filename((uploaded.filename if uploaded else "") or "")
    suffix = Path(filename).suffix.lower()
    if not uploaded or not filename or suffix not in ALLOWED_IMAGE_EXTS:
        return jsonify({"error": "Upload a JPG, JPEG, PNG, or WEBP image."}), 400

    for old in GAMING_DIR.glob("top_image.*"):
        old.unlink(missing_ok=True)
    target = GAMING_DIR / f"top_image{suffix}"
    uploaded.save(target)
    if target.stat().st_size < 100:
        target.unlink(missing_ok=True)
        return jsonify({"error": "Uploaded image is empty or invalid."}), 400
    return jsonify({"ok": True, "filename": target.name})


@gaming_bp.post("/api/start")
def gaming_start():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Paste a valid http/https gaming video URL."}), 400
    rights_ok = bool(payload.get("rights_ok"))
    if not rights_ok:
        return jsonify({"error": "Confirm reuse rights/permission first."}), 400

    post_facebook = bool(payload.get("facebook"))
    post_youtube = bool(payload.get("youtube"))
    youtube_privacy = str(payload.get("youtube_privacy") or "public").strip()
    if youtube_privacy not in {"public", "unlisted", "private"}:
        return jsonify({"error": "Invalid YouTube privacy setting."}), 400
    if not (post_facebook or post_youtube):
        return jsonify({"error": "Choose Facebook, YouTube, or both."}), 400
    if not current_gaming_image():
        return jsonify({"error": "Upload your gaming top image first."}), 400

    with GAMING_JOB_LOCK:
        if GAMING_JOB["state"] == "running":
            return jsonify({"error": "A gaming video is already processing."}), 409

    job_id = uuid.uuid4().hex[:10]
    reset_job(job_id)
    threading.Thread(
        target=process_job,
        args=(job_id, url, rights_ok, post_facebook, post_youtube, youtube_privacy),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id})
