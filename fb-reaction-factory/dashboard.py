#!/usr/bin/env python3
import os, re, threading, uuid
from pathlib import Path
from flask import Flask, jsonify, render_template_string, request, send_from_directory
from auto_pipeline import download_url
from facebook import publish_reel as publish_facebook
from instagram import publish_reel as publish_instagram
from instagram_download import download_instagram_reel, is_instagram_url
from metadata import generate_metadata
from prepare_reel import write_package
from reaction_factory import ffprobe_duration, make_reel
from youtube_bridge import run_youtube_factory

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
INBOX = ROOT / "sources" / "approved_inbox"
ENV_FILE = ROOT / ".env"
MIN_SOURCE_SECONDS = 4.0
MODE_IG = "instagram_reaction"
MODE_FB = "facebook_direct"
MODE_YT = "youtube_factory"
VALID_MODES = {MODE_IG, MODE_FB, MODE_YT}

app = Flask(__name__)
JOB_LOCK = threading.Lock()
CURRENT_JOB = {
    "id": None, "mode": None, "state": "idle", "stage": "idle", "progress": 0,
    "message": "Paste a video URL to begin.",
    "steps": {"download": "waiting", "edit": "waiting", "caption": "waiting", "publish": "waiting"},
    "result": None, "error": None,
}

def load_env_file():
    if not ENV_FILE.exists(): return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line: continue
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
        step = kwargs.pop("step", None); step_state = kwargs.pop("step_state", None)
        for key, value in kwargs.items():
            if value is not None: CURRENT_JOB[key] = value
        if step and step_state: CURRENT_JOB["steps"][step] = step_state

def reset_job(job_id, mode):
    with JOB_LOCK:
        CURRENT_JOB.update({
            "id": job_id, "mode": mode, "state": "running", "stage": "download", "progress": 5,
            "message": "Starting...",
            "steps": {"download": "running", "edit": "waiting", "caption": "waiting", "publish": "waiting"},
            "result": None, "error": None,
        })

def get_source(url):
    return Path(download_instagram_reel(url) if is_instagram_url(url) else download_url(url))

def process_youtube(url, rights_ok, privacy, music_policy):
    set_job(stage="download", progress=10, message="YouTube Factory · downloading source...", step="download", step_state="running")

    def on_line(line):
        low = line.lower()
        if "download" in low or "yt-dlp" in low:
            set_job(stage="download", progress=25, message="YouTube Factory · downloading source...", step="download", step_state="running")
        if "recognized music" in low or "music_sample" in low or "audd" in low:
            set_job(stage="edit", progress=45, message="YouTube Factory · checking music...", step="download", step_state="done",)
            set_job(step="edit", step_state="running")
        if "thumbnail" in low or "metadata" in low or "openai" in low:
            set_job(stage="caption", progress=65, message="YouTube Factory · creating title, tags and thumbnail...", step="edit", step_state="done")
            set_job(step="caption", step_state="running")
        if "upload progress" in low or "youtube.com/watch" in low:
            set_job(stage="publish", progress=85, message="YouTube Factory · uploading to YouTube...", step="caption", step_state="done")
            set_job(step="publish", step_state="running")

    result = run_youtube_factory(url, rights_ok, privacy=privacy, music_policy=music_policy, on_line=on_line)
    set_job(step="download", step_state="done")
    set_job(step="edit", step_state="done")
    set_job(step="caption", step_state="done")
    set_job(step="publish", step_state="done")
    metadata = result.get("metadata") or {}
    final = {
        "mode": MODE_YT,
        "video_url": result.get("url"),
        "youtube_url": result.get("url"),
        "caption": metadata.get("description") or "",
        "summary": f"YouTube video uploaded · {metadata.get('title') or 'Upload complete'}",
    }
    set_job(state="done", stage="done", progress=100, message="Done · uploaded to YouTube.", result=final)

def process_job(job_id, url, mode, rights_ok, privacy="private", music_policy="stop"):
    try:
        if not rights_ok: raise RuntimeError("Confirm that you have rights or permission to reuse this source.")
        if mode not in VALID_MODES: raise RuntimeError("Choose Instagram Reaction, TV Mind USA Direct, or YouTube Factory.")

        if mode == MODE_YT:
            load_env_file()
            process_youtube(url, rights_ok, privacy, music_policy)
            return

        set_job(stage="download", progress=8, message="Downloading source video...", step="download", step_state="running")
        source = get_source(url)

        if mode == MODE_FB:
            set_job(progress=35, message="Download complete · original video ready", step="download", step_state="done")
            set_job(stage="edit", progress=50, message="Keeping original video — no reaction edit.", step="edit", step_state="skipped")
            set_job(stage="caption", progress=65, message="Using fixed TVMind USA caption and tags.", step="caption", step_state="skipped")
            load_env_file()
            set_job(stage="publish", progress=80, message="Posting original video to TV Mind USA...", step="publish", step_state="running")
            fb = publish_facebook(source, "")
            set_job(step="publish", step_state="done")
            result = {
                "mode": mode, "video_url": f"/source/{source.name}", "caption": "Follow for more TVMind USA\n\n#Explore #FacebookReel #ReelsViral #TVMindUSA",
                "published_facebook_page": bool(fb), "facebook_video_id": fb.get("video_id") if fb else None,
                "summary": "Original video posted to TV Mind USA. No reaction edit was added.",
            }
            set_job(state="done", stage="done", progress=100, message="Done · original video posted to TV Mind USA.", result=result)
            return

        try:
            duration = ffprobe_duration(source)
        except FileNotFoundError as exc:
            raise RuntimeError("Instagram Reaction needs FFmpeg/ffprobe installed on this computer.") from exc
        if duration < MIN_SOURCE_SECONDS: raise RuntimeError(f"Source is only {duration:.1f}s. Need at least 4s.")
        set_job(progress=30, message=f"Download complete · {duration:.1f}s", step="download", step_state="done")

        set_job(stage="edit", progress=38, message="Building reaction Reel...", step="edit", step_state="running")
        seed = clean_caption_seed(source)
        video, reaction_used = make_reel(str(source), caption=seed, reaction="auto", rights_ok=True)
        video = Path(video)
        set_job(progress=70, message="Reaction edit complete", step="edit", step_state="done")

        set_job(stage="caption", progress=75, message="Creating caption and hashtags...", step="caption", step_state="running")
        metadata = generate_metadata(seed)
        text_path, json_path, post_text = write_package(video, metadata, {
            "source_url": url, "source_duration_seconds": duration, "reaction_used": reaction_used,
            "target_reel_seconds": 60, "source_looped": duration < 60,
        })
        set_job(progress=84, message="Caption ready", step="caption", step_state="done")

        load_env_file()
        set_job(stage="publish", progress=88, message="Publishing reaction Reel to Instagram...", step="publish", step_state="running")
        ig = publish_instagram(video, post_text)
        set_job(step="publish", step_state="done")
        result = {
            "mode": mode, "video_url": f"/output/{video.name}", "caption": post_text,
            "instagram_permalink": ig.get("permalink") if ig else None,
            "published_instagram": bool(ig), "summary": "Reaction Reel created and posted to Instagram.",
        }
        set_job(state="done", stage="done", progress=100, message="Done · reaction Reel published to Instagram.", result=result)
    except Exception as exc:
        with JOB_LOCK:
            stage = CURRENT_JOB.get("stage")
            if stage in CURRENT_JOB["steps"]: CURRENT_JOB["steps"][stage] = "error"
        set_job(state="error", stage="error", message="Pipeline stopped.", error=str(exc))

DASHBOARD_HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reaction Factory</title><style>
:root{--bg:#070a0f;--panel:#101722;--line:#263142;--text:#f6f8fb;--muted:#8d99aa;--accent:#6d5dfc;--good:#2dd881;--bad:#ff5d73}*{box-sizing:border-box}body{margin:0;background:#070a0f;color:var(--text);font-family:Arial,sans-serif}.wrap{max-width:1050px;margin:auto;padding:36px 18px}.brand{font-size:28px;font-weight:900;margin-bottom:6px}.sub{color:var(--muted);margin-bottom:24px}.card{background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:20px}.modes{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}.mode{display:block;border:1px solid var(--line);border-radius:15px;padding:15px;background:#0a0f17;cursor:pointer}.mode.selected{border-color:var(--accent);box-shadow:inset 0 0 0 1px #6d5dfc55}.mode input{display:none}.mode strong{display:block}.mode span{display:block;color:var(--muted);font-size:12px;line-height:1.45;margin-top:5px}.badge{display:inline-block!important;width:auto;margin-top:9px!important;background:#171f2b;border-radius:999px;padding:5px 8px;font-weight:800}.row{display:flex;gap:10px}.url{flex:1;background:#070b11;border:1px solid var(--line);border-radius:13px;padding:15px;color:white;font-size:15px}button{border:0;border-radius:13px;background:var(--accent);color:white;padding:0 22px;font-weight:800;min-height:50px;cursor:pointer}.rights,.notice{margin-top:12px;font-size:13px;color:#c7cfdb}.notice{background:#0a0f17;border:1px solid var(--line);padding:11px;border-radius:12px;color:#aab4c2}.ytopts{display:none;gap:10px;margin-top:12px}.ytopts.show{display:flex}.ytopts select{background:#070b11;color:white;border:1px solid var(--line);border-radius:10px;padding:10px}.pipe{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin-top:18px}.step{border:1px solid var(--line);border-radius:13px;padding:12px;background:#0a0f17;min-height:78px}.step small{display:block;color:var(--muted);margin-top:4px}.step.running{border-color:var(--accent)}.step.done{border-color:var(--good)}.step.skipped{opacity:.55;border-style:dashed}.step.error{border-color:var(--bad)}.status,.result{margin-top:14px;border:1px solid var(--line);background:#090e15;border-radius:13px;padding:14px}.track{height:7px;background:#18202c;border-radius:99px;margin-top:10px;overflow:hidden}.bar{height:100%;background:var(--accent);width:0}.error{display:none;color:#ffd4da;margin-top:9px}.result{display:none}.actions{display:flex;gap:8px;flex-wrap:wrap}.link{display:inline-block;padding:9px 12px;border-radius:10px;background:#1a2331;color:white;text-decoration:none}.caption{white-space:pre-wrap;font-size:12px;color:#cbd2dc;margin-top:12px;background:#070b11;padding:10px;border-radius:10px}.summary{color:#aab4c2;font-size:12px;margin-top:10px}@media(max-width:800px){.modes{grid-template-columns:1fr}.pipe{grid-template-columns:1fr 1fr}.row,.ytopts{flex-direction:column}button{min-height:48px}}
</style></head><body><div class="wrap"><div class="brand">Reaction Factory</div><div class="sub">Paste one URL and choose where it goes.</div><div class="card">
<div class="modes">
<label id="m-ig" class="mode selected"><input type="radio" name="mode" value="instagram_reaction" checked><strong>Instagram Reaction</strong><span>Download → reaction edit → caption → Instagram.</span><span class="badge">REACTION ON</span></label>
<label id="m-fb" class="mode"><input type="radio" name="mode" value="facebook_direct"><strong>TV Mind USA Direct</strong><span>Download original → fixed caption/tags → Facebook Page.</span><span class="badge">NO REACTION · NO EDIT</span></label>
<label id="m-yt" class="mode"><input type="radio" name="mode" value="youtube_factory"><strong>YouTube Factory</strong><span>Download → music check → AI metadata + thumbnail → YouTube.</span><span class="badge">LONG VIDEO FACTORY</span></label>
</div>
<div class="row"><input id="url" class="url" placeholder="Paste Instagram / Facebook / YouTube / Google Drive URL"><button id="start" onclick="startJob()">Start Instagram Reaction</button></div>
<div id="ytopts" class="ytopts"><select id="privacy"><option value="private">YouTube: Private</option><option value="unlisted">YouTube: Unlisted</option><option value="public">YouTube: Public</option></select><select id="music"><option value="stop">Music: Stop if detected</option><option value="mute">Music: Mute detected music</option><option value="ignore">Music: Ignore (rights cleared)</option></select></div>
<div class="rights"><label><input id="rights" type="checkbox"> I confirm I own this source or have permission/license to reuse it.</label></div><div id="notice" class="notice"><b>Instagram Reaction:</b> creates your reaction version and posts it to Instagram.</div>
<div class="pipe"><div id="s-download" class="step"><b id="d1">1 Download</b><small id="d2">Get source</small></div><div id="s-edit" class="step"><b id="e1">2 Reaction Edit</b><small id="e2">Build reaction</small></div><div id="s-caption" class="step"><b id="c1">3 Caption</b><small id="c2">Title + tags</small></div><div id="s-publish" class="step"><b>4 Publish</b><small id="p2">Instagram</small></div></div>
<div class="status"><div><b id="msg">Ready</b> <span id="pct" style="float:right">0%</span></div><div class="track"><div id="bar" class="bar"></div></div><div id="error" class="error"></div></div>
<div id="result" class="result"><div class="actions"><a id="video" class="link" target="_blank">Open result</a><a id="iglink" class="link" target="_blank" style="display:none">Open Instagram post</a></div><div id="summary" class="summary"></div><div id="caption" class="caption" style="display:none"></div></div>
</div></div><script>
const steps=['download','edit','caption','publish'];let timer=null;function mode(){return document.querySelector('input[name="mode"]:checked').value}function paint(n,s){document.getElementById('s-'+n).className='step '+(s||'waiting')}function modeUI(){const m=mode(),ig=m==='instagram_reaction',fb=m==='facebook_direct',yt=m==='youtube_factory';document.getElementById('m-ig').classList.toggle('selected',ig);document.getElementById('m-fb').classList.toggle('selected',fb);document.getElementById('m-yt').classList.toggle('selected',yt);document.getElementById('ytopts').classList.toggle('show',yt);document.getElementById('start').textContent=ig?'Start Instagram Reaction':fb?'Post to TV Mind USA':'Start YouTube Factory';document.getElementById('notice').innerHTML=ig?'<b>Instagram Reaction:</b> creates your reaction version and posts it to Instagram.':fb?'<b>TV Mind USA Direct:</b> downloads the original and posts it with your fixed caption and tags.':'<b>YouTube Factory:</b> runs your existing YouTube automation with music check, AI title/description/tags/thumbnail, then uploads.';document.getElementById('d1').textContent='1 Download';document.getElementById('d2').textContent='Get source';document.getElementById('e1').textContent=ig?'2 Reaction Edit':fb?'2 No Edit':'2 Music Check';document.getElementById('e2').textContent=ig?'Build reaction':fb?'Keep original':'Scan / mute';document.getElementById('c1').textContent=ig?'3 Caption':fb?'3 Fixed Caption':'3 AI Package';document.getElementById('c2').textContent=ig?'Title + tags':fb?'TVMind USA':'Title + thumbnail';document.getElementById('p2').textContent=ig?'Instagram':fb?'TV Mind USA':'YouTube'}document.querySelectorAll('input[name="mode"]').forEach(x=>x.addEventListener('change',modeUI));modeUI();
async function startJob(){const url=document.getElementById('url').value.trim(),rights=document.getElementById('rights').checked;if(!url)return alert('Paste a URL first.');if(!rights)return alert('Confirm reuse rights first.');document.getElementById('start').disabled=true;document.getElementById('result').style.display='none';document.getElementById('error').style.display='none';const body={url,mode:mode(),rights_ok:rights,privacy:document.getElementById('privacy').value,music_policy:document.getElementById('music').value};const r=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),d=await r.json();if(!r.ok){document.getElementById('start').disabled=false;return alert(d.error||'Could not start')}poll();if(timer)clearInterval(timer);timer=setInterval(poll,1000)}
async function poll(){const r=await fetch('/api/status'),d=await r.json();document.getElementById('msg').textContent=d.message||'';document.getElementById('pct').textContent=(d.progress||0)+'%';document.getElementById('bar').style.width=(d.progress||0)+'%';steps.forEach(n=>paint(n,d.steps[n]));if(d.state==='error'){document.getElementById('error').style.display='block';document.getElementById('error').textContent=d.error||'Unknown error';document.getElementById('start').disabled=false;if(timer)clearInterval(timer)}if(d.state==='done'){document.getElementById('start').disabled=false;if(timer)clearInterval(timer);show(d.result)}}
function show(r){if(!r)return;document.getElementById('result').style.display='block';const v=document.getElementById('video');v.href=r.youtube_url||r.video_url||'#';v.textContent=r.youtube_url?'Open YouTube video':'Open result';const ig=document.getElementById('iglink');if(r.instagram_permalink){ig.href=r.instagram_permalink;ig.style.display='inline-block'}else ig.style.display='none';document.getElementById('summary').textContent=r.summary||'';const c=document.getElementById('caption');if(r.caption){c.textContent=r.caption;c.style.display='block'}else c.style.display='none'}poll();
</script></body></html>'''

@app.get("/")
def home(): return render_template_string(DASHBOARD_HTML)

@app.get("/api/status")
def api_status(): return jsonify(snapshot())

@app.post("/api/start")
def api_start():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url") or "").strip()
    if not url.startswith(("http://", "https://")): return jsonify({"error": "Paste a valid http/https video URL."}), 400
    rights_ok = bool(payload.get("rights_ok"))
    if not rights_ok: return jsonify({"error": "Confirm reuse rights/permission first."}), 400
    mode = str(payload.get("mode") or MODE_IG).strip()
    if mode not in VALID_MODES: return jsonify({"error": "Choose Instagram Reaction, TV Mind USA Direct, or YouTube Factory."}), 400
    privacy = str(payload.get("privacy") or "private").strip()
    music_policy = str(payload.get("music_policy") or "stop").strip()
    with JOB_LOCK:
        if CURRENT_JOB["state"] == "running": return jsonify({"error": "A video is already processing."}), 409
    job_id = uuid.uuid4().hex[:10]
    reset_job(job_id, mode)
    threading.Thread(target=process_job, args=(job_id, url, mode, rights_ok, privacy, music_policy), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id, "mode": mode})

@app.get("/output/<path:filename>")
def output_file(filename): return send_from_directory(OUTPUT, filename, as_attachment=False)

@app.get("/source/<path:filename>")
def source_file(filename): return send_from_directory(INBOX, filename, as_attachment=False)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f"Reaction Factory dashboard: http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
