#!/usr/bin/env python3
import base64
import os
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, render_template_string, request

from auto_pipeline import download_url, source_context_from_url
from facebook import publish_reel as publish_facebook
from instagram_download import download_instagram_reel, is_instagram_url
from reaction_factory import ffmpeg_exe, ffprobe_duration
from youtube_short import publish_short

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
GAMING_DIR = ROOT / "data" / "gaming"
ASSET_B64 = ROOT / "assets" / "gaming_reaction_default.b64"
USER_REACTION = ROOT / "assets" / "gaming_reaction_user.mp4"
REACTION_FILE = GAMING_DIR / "saved_gaming_reaction.mp4"
MIN_SOURCE_SECONDS = 4.0
MAX_SOURCE_SECONDS = 60.0
GAMING_TAGS = ["Gaming", "Gameplay", "GamingClips", "Gamer", "Shorts"]

GAMING_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT.mkdir(parents=True, exist_ok=True)

gaming_bp = Blueprint("gaming_saved", __name__, url_prefix="/gaming")
JOB_LOCK = threading.Lock()
JOB = {
    "state": "idle",
    "stage": "idle",
    "progress": 0,
    "message": "Saved gaming reaction ready.",
    "steps": {"download": "waiting", "edit": "waiting", "metadata": "waiting", "publish": "waiting"},
    "result": None,
    "error": None,
}


def ensure_saved_reaction():
    # Prefer the user's dedicated D6x8 gaming reaction. The cloud workflow restores
    # this file before the worker starts so Personal/funny reactions are never used.
    if USER_REACTION.exists() and USER_REACTION.stat().st_size > 1000:
        return USER_REACTION
    if REACTION_FILE.exists() and REACTION_FILE.stat().st_size > 1000:
        return REACTION_FILE
    if not ASSET_B64.exists():
        raise RuntimeError("Saved gaming reaction asset is missing.")
    raw = ASSET_B64.read_text(encoding="utf-8").strip()
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise RuntimeError(f"Saved gaming reaction asset is invalid: {exc}") from exc
    if len(data) < 1000:
        raise RuntimeError("Saved gaming reaction asset is empty.")
    REACTION_FILE.write_bytes(data)
    return REACTION_FILE


def get_source(url):
    return Path(download_instagram_reel(url) if is_instagram_url(url) else download_url(url))


def run(cmd):
    print("$", " ".join(str(x) for x in cmd))
    subprocess.run(list(cmd), check=True)


def _has_audio(path):
    path = Path(path)
    probe = shutil.which("ffprobe")
    if probe:
        result = subprocess.run(
            [probe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    # Fallback for environments that only expose ffmpeg.
    result = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return "Audio:" in (result.stderr or "")


def compose_gaming(source, reaction):
    source = Path(source)
    reaction = Path(reaction)
    source_duration = ffprobe_duration(source)
    if source_duration < MIN_SOURCE_SECONDS:
        raise RuntimeError(f"Source is only {source_duration:.1f}s. Need at least 4s.")
    duration = min(MAX_SOURCE_SECONDS, float(source_duration))
    output = OUTPUT / f"gaming_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.mp4"

    reaction_has_audio = _has_audio(reaction)
    source_has_audio = _has_audio(source)

    # 1080x1920 exactly: 672px top = 35%, 1248px bottom = 65%.
    # Lanczos + a light unsharp pass keeps the dedicated gaming reaction as clear
    # as the source allows. Reaction voice is primary; gameplay sound stays lower.
    filters = [
        f"[0:v]scale=1080:672:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop=1080:672,setsar=1,fps=30,unsharp=5:5:0.55:5:5:0.0,"
        f"trim=duration={duration:.3f},setpts=PTS-STARTPTS[top]",
        f"[1:v]scale=1080:1248:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop=1080:1248,setsar=1,fps=30,trim=duration={duration:.3f},setpts=PTS-STARTPTS[game]",
        "[top][game]vstack=inputs=2[v]",
    ]

    audio_map = []
    if reaction_has_audio and source_has_audio:
        filters.extend([
            f"[0:a]atrim=duration={duration:.3f},asetpts=PTS-STARTPTS,volume=1.25[voice]",
            f"[1:a]atrim=duration={duration:.3f},asetpts=PTS-STARTPTS,volume=0.30[gamea]",
            "[voice][gamea]amix=inputs=2:duration=longest:dropout_transition=2:normalize=0,alimiter=limit=0.95[a]",
        ])
        audio_map = ["-map", "[a]"]
    elif reaction_has_audio:
        filters.append(
            f"[0:a]atrim=duration={duration:.3f},asetpts=PTS-STARTPTS,volume=1.20,alimiter=limit=0.95[a]"
        )
        audio_map = ["-map", "[a]"]
    elif source_has_audio:
        filters.append(
            f"[1:a]atrim=duration={duration:.3f},asetpts=PTS-STARTPTS,volume=0.75,alimiter=limit=0.95[a]"
        )
        audio_map = ["-map", "[a]"]

    cmd = [
        ffmpeg_exe(), "-y",
        "-stream_loop", "-1", "-i", str(reaction),
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-filter_complex", ";".join(filters),
        "-map", "[v]",
    ]
    cmd.extend(audio_map)
    cmd.extend([
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-profile:v", "high", "-level:v", "4.1",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(output),
    ])
    run(cmd)
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


def snapshot():
    with JOB_LOCK:
        return {**JOB, "steps": dict(JOB["steps"])}


def set_job(**kwargs):
    with JOB_LOCK:
        step = kwargs.pop("step", None)
        step_state = kwargs.pop("step_state", None)
        for key, value in kwargs.items():
            if value is not None:
                JOB[key] = value
        if step and step_state:
            JOB["steps"][step] = step_state


def reset_job():
    with JOB_LOCK:
        JOB.update({
            "state": "running",
            "stage": "download",
            "progress": 5,
            "message": "Starting gaming pipeline...",
            "steps": {"download": "running", "edit": "waiting", "metadata": "waiting", "publish": "waiting"},
            "result": None,
            "error": None,
        })


def process_job(url, post_facebook, post_youtube, youtube_privacy):
    try:
        reaction = ensure_saved_reaction()
        set_job(stage="download", progress=10, message="Downloading gameplay once...", step="download", step_state="running")
        source = get_source(url)
        set_job(progress=28, message="Gameplay downloaded.", step="download", step_state="done")

        set_job(stage="edit", progress=38, message="Building clear 35% gaming reaction + 65% gameplay with reaction voice...", step="edit", step_state="running")
        final_video, duration = compose_gaming(source, reaction)
        set_job(progress=72, message="Gaming video ready.", step="edit", step_state="done")

        set_job(stage="metadata", progress=76, message="Creating gaming title + exactly 5 tags...", step="metadata", step_state="running")
        metadata = gaming_metadata(url)
        post_text = metadata["description"] + "\n\n" + " ".join(metadata["hashtags"])
        set_job(progress=84, message="Gaming metadata ready.", step="metadata", step_state="done")

        set_job(stage="publish", progress=88, message="Publishing to gaming accounts only...", step="publish", step_state="running")
        result = {
            "facebook_video_id": None,
            "youtube_url": None,
            "youtube_channel": None,
            "gaming_video_url": f"/output/{final_video.name}",
            "duration_seconds": duration,
            "tags": metadata["hashtags"],
        }
        completed = []

        if post_facebook:
            fb = publish_facebook(final_video, post_text, profile="gaming")
            result["facebook_video_id"] = fb.get("video_id") if fb else None
            completed.append("Gaming Facebook")

        if post_youtube:
            yt = publish_short(
                final_video,
                title=metadata["title"],
                description=post_text,
                tags=metadata["tags"],
                privacy="public",
                profile="gaming",
            )
            result["youtube_url"] = yt.get("url")
            result["youtube_channel"] = yt.get("channel")
            completed.append("Gaming YouTube")

        result["summary"] = "Published to: " + ", ".join(completed)
        set_job(step="publish", step_state="done")
        set_job(state="done", stage="done", progress=100, message=result["summary"], result=result)
    except Exception as exc:
        with JOB_LOCK:
            stage = JOB.get("stage")
            if stage in JOB["steps"]:
                JOB["steps"][stage] = "error"
        set_job(state="error", stage="error", message="Gaming pipeline stopped.", error=str(exc))


PAGE = r'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Gaming Auto Post</title>
<style>body{margin:0;background:#070a0f;color:#f6f8fb;font-family:Arial,sans-serif}.wrap{max-width:900px;margin:auto;padding:30px 16px}.card{background:#101722;border:1px solid #263142;border-radius:18px;padding:18px}.saved{padding:13px;border:1px solid #2dd881;border-radius:12px;background:#0a0f17;margin-bottom:14px}.row{display:flex;gap:9px}.url{flex:1;background:#070b11;color:#fff;border:1px solid #263142;border-radius:12px;padding:14px}button{background:#6d5dfc;color:white;border:0;border-radius:12px;padding:0 20px;font-weight:800}.dest{display:flex;gap:20px;margin:14px 0}.small{color:#9aa6b5;font-size:13px}.status{margin-top:15px;padding:13px;border:1px solid #263142;border-radius:12px;background:#090e15}.error{color:#ff9aaa}.result{margin-top:10px}.result a{color:white}@media(max-width:700px){.row{flex-direction:column}.row button{min-height:48px}}</style></head><body><div class="wrap"><h1>Gaming 35/65</h1><div class="card"><div class="saved"><b>✓ Dedicated D6x8 gaming reaction</b><div class="small">Top 35% · clearer render · reaction voice kept · gameplay audio lower.</div></div><div class="row"><input id="url" class="url" placeholder="Paste gaming video URL"><button onclick="startJob()">Download · Edit · Post</button></div><div class="dest"><label><input id="fb" type="checkbox" checked> Gaming Facebook</label><label><input id="yt" type="checkbox" checked> Gaming YouTube</label></div><div class="small"><label><input id="rights" type="checkbox"> I own this source or have permission/license to reuse it.</label></div><div class="small" style="margin-top:10px"><b>5 tags:</b> #Gaming #Gameplay #GamingClips #Gamer #Shorts</div><div class="status"><b id="msg">Ready</b><div id="err" class="error"></div><div id="result" class="result"></div></div></div></div><script>
let timer=null;async function startJob(){const url=document.getElementById('url').value.trim();if(!url)return alert('Paste a gaming video URL first.');if(!document.getElementById('rights').checked)return alert('Confirm reuse rights first.');const fb=document.getElementById('fb').checked,yt=document.getElementById('yt').checked;if(!fb&&!yt)return alert('Choose Facebook, YouTube, or both.');const r=await fetch('/gaming/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,rights_ok:true,facebook:fb,youtube:yt,youtube_privacy:'public'})}),d=await r.json();if(!r.ok)return alert(d.error||'Could not start');poll();if(timer)clearInterval(timer);timer=setInterval(poll,1000)}
async function poll(){const r=await fetch('/gaming/api/status'),d=await r.json();document.getElementById('msg').textContent=(d.message||'')+' '+(d.progress||0)+'%';document.getElementById('err').textContent=d.error||'';if(d.state==='done'){if(timer)clearInterval(timer);const x=d.result||{};document.getElementById('result').innerHTML=(x.youtube_url?'<a target="_blank" href="'+x.youtube_url+'">Open public YouTube video</a>':'')+(x.summary?'<div>'+x.summary+'</div>':'')}if(d.state==='error'&&timer)clearInterval(timer)}poll();
</script></body></html>'''


@gaming_bp.get("/")
def home():
    return render_template_string(PAGE)


@gaming_bp.get("/api/status")
def status():
    try:
        ensure_saved_reaction()
    except Exception as exc:
        if JOB["state"] != "running":
            return jsonify({**snapshot(), "error": str(exc)})
    return jsonify(snapshot())


@gaming_bp.post("/api/start")
def start():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Paste a valid http/https gaming video URL."}), 400
    if not bool(payload.get("rights_ok")):
        return jsonify({"error": "Confirm reuse rights/permission first."}), 400
    post_facebook = bool(payload.get("facebook"))
    post_youtube = bool(payload.get("youtube"))
    if not (post_facebook or post_youtube):
        return jsonify({"error": "Choose Facebook, YouTube, or both."}), 400
    try:
        ensure_saved_reaction()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    with JOB_LOCK:
        if JOB["state"] == "running":
            return jsonify({"error": "A gaming video is already processing."}), 409
    reset_job()
    threading.Thread(target=process_job, args=(url, post_facebook, post_youtube, "public"), daemon=True).start()
    return jsonify({"ok": True})
