#!/usr/bin/env python3
import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REACTIONS_DIR = ROOT / "reactions"
SOURCES_DIR = ROOT / "sources"
OUTPUT_DIR = ROOT / "output"
DATA_DIR = ROOT / "data"
REACTIONS_FILE = DATA_DIR / "reactions.json"
QUEUE_FILE = DATA_DIR / "queue.json"

for d in (REACTIONS_DIR, SOURCES_DIR, OUTPUT_DIR, DATA_DIR):
    d.mkdir(parents=True, exist_ok=True)


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2))
    tmp.replace(path)


def run(cmd):
    print("$", " ".join(str(x) for x in cmd))
    subprocess.run(cmd, check=True)


def ffprobe_duration(path):
    p = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ], capture_output=True, text=True, check=True)
    return float(p.stdout.strip())


def ingest_source(source):
    source_path = Path(source)
    if source_path.exists():
        target = SOURCES_DIR / f"{uuid.uuid4().hex[:10]}{source_path.suffix.lower() or '.mp4'}"
        shutil.copy2(source_path, target)
        return target

    if source.startswith("http://") or source.startswith("https://"):
        ytdlp = shutil.which("yt-dlp")
        if not ytdlp:
            raise RuntimeError(
                "URL ingest needs yt-dlp. Install it first with: python3 -m pip install yt-dlp"
            )
        target_tpl = str(SOURCES_DIR / f"{uuid.uuid4().hex[:10]}.%(ext)s")
        run([ytdlp, "--no-playlist", "-f", "bv*+ba/b", "--merge-output-format", "mp4", "-o", target_tpl, source])
        candidates = sorted(SOURCES_DIR.glob(Path(target_tpl).name.replace(".%(ext)s", ".*")), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            raise RuntimeError("Download completed but no source file was found.")
        return candidates[0]

    raise FileNotFoundError(f"Source not found: {source}")


def add_reaction(path, label, notes=""):
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(path)
    rid = uuid.uuid4().hex[:10]
    target = REACTIONS_DIR / f"{rid}{src.suffix.lower() or '.mp4'}"
    shutil.copy2(src, target)
    items = load_json(REACTIONS_FILE, [])
    items.append({"id": rid, "label": label.lower().strip(), "path": str(target), "notes": notes})
    save_json(REACTIONS_FILE, items)
    print(f"Added reaction {rid}: {label}")


def choose_reaction(caption="", preferred="auto"):
    items = load_json(REACTIONS_FILE, [])
    if not items:
        raise RuntimeError("No reactions yet. Add 5-10 reaction clips first.")
    if preferred != "auto":
        matches = [x for x in items if x["label"] == preferred.lower() or x["id"] == preferred]
        if matches:
            return random.choice(matches)
    text = caption.lower()
    buckets = [
        (("fail", "fall", "funny", "lol", "prank", "oops"), ("laugh", "funny", "lol")),
        (("wow", "crazy", "unbelievable", "shock"), ("shock", "wow", "surprised")),
        (("cute", "baby", "dog", "cat", "sweet"), ("smile", "cute", "happy")),
        (("awkward", "cringe", "weird"), ("cringe", "confused")),
    ]
    for keywords, labels in buckets:
        if any(k in text for k in keywords):
            matches = [x for x in items if x["label"] in labels]
            if matches:
                return random.choice(matches)
    return random.choice(items)


def compose(source, reaction, output, max_seconds=60):
    source = Path(source)
    reaction = Path(reaction)
    source_duration = ffprobe_duration(source)
    if source_duration < 4:
        raise RuntimeError("Source must be at least 4 seconds for a Reel.")
    duration = float(max_seconds)

    # Both inputs loop when shorter than the target so every output is a full 60s Reel.
    # 1080x1920: top 30% reaction (576px), bottom 70% source (1344px).
    filter_complex = (
        "[0:v]scale=1080:576:force_original_aspect_ratio=increase,"
        "crop=1080:576,setsar=1,fps=30[reaction];"
        "[1:v]scale=1080:1344:force_original_aspect_ratio=increase,"
        "crop=1080:1344,setsar=1,fps=30[source];"
        "[reaction][source]vstack=inputs=2[v]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", str(reaction),
        "-stream_loop", "-1", "-i", str(source),
        "-t", f"{duration:.3f}",
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "1:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(output),
    ]
    run(cmd)
    return output


def make_reel(source, caption="", reaction="auto", rights_ok=False):
    if not rights_ok:
        raise RuntimeError("Rights approval is required before processing a third-party clip.")
    local_source = ingest_source(source)
    reaction_item = choose_reaction(caption, reaction)
    out = OUTPUT_DIR / f"reel_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.mp4"
    compose(local_source, reaction_item["path"], out)
    print(json.dumps({
        "output": str(out),
        "source": str(local_source),
        "reaction": reaction_item,
        "caption": caption,
    }, indent=2))
    return out, reaction_item


def next_publish_slot():
    tz_name = os.getenv("TIMEZONE", "America/Chicago")
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    raw_times = os.getenv("POST_TIMES", "11:00,14:00,17:00,20:00")
    slots = []
    for value in raw_times.split(","):
        value = value.strip()
        if not value:
            continue
        hour, minute = [int(x) for x in value.split(":", 1)]
        slots.append((hour, minute))
    if not slots:
        slots = [(11, 0), (14, 0), (17, 0), (20, 0)]

    jobs = load_json(QUEUE_FILE, [])
    reserved = {j.get("publish_at") for j in jobs if j.get("status") in {"queued", "processing", "ready"}}
    for day_offset in range(0, 14):
        day = now.date() + timedelta(days=day_offset)
        for hour, minute in sorted(slots):
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
            candidate_iso = candidate.isoformat(timespec="minutes")
            if candidate > now and candidate_iso not in reserved:
                return candidate_iso
    raise RuntimeError("No open posting slot found in the next 14 days.")


def queue_job(source, caption="", reaction="auto", rights_ok=False, publish_at=None):
    if not rights_ok:
        raise RuntimeError("Rights approval is required before queueing a third-party clip.")
    jobs = load_json(QUEUE_FILE, [])
    job = {
        "id": uuid.uuid4().hex[:12],
        "source": source,
        "caption": caption,
        "reaction": reaction,
        "rights_ok": True,
        "publish_at": publish_at or next_publish_slot(),
        "status": "queued",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "output": None,
        "metadata": None,
        "error": None,
    }
    jobs.append(job)
    save_json(QUEUE_FILE, jobs)
    print(json.dumps(job, indent=2))


def list_jobs():
    print(json.dumps(load_json(QUEUE_FILE, []), indent=2))


def main():
    ap = argparse.ArgumentParser(description="Facebook Reaction Reel Factory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add-reaction")
    p.add_argument("path")
    p.add_argument("--label", required=True, help="laugh, shock, smile, cringe, confused, etc.")
    p.add_argument("--notes", default="")

    p = sub.add_parser("make")
    p.add_argument("--source", required=True, help="Local video path or URL")
    p.add_argument("--caption", default="")
    p.add_argument("--reaction", default="auto")
    p.add_argument("--rights-ok", action="store_true", required=True)

    p = sub.add_parser("queue")
    p.add_argument("--source", required=True)
    p.add_argument("--caption", default="")
    p.add_argument("--reaction", default="auto")
    p.add_argument("--rights-ok", action="store_true", required=True)
    p.add_argument("--publish-at", default=None, help="ISO local datetime, optional")

    sub.add_parser("list")

    args = ap.parse_args()
    if args.cmd == "add-reaction":
        add_reaction(args.path, args.label, args.notes)
    elif args.cmd == "make":
        make_reel(args.source, args.caption, args.reaction, args.rights_ok)
    elif args.cmd == "queue":
        queue_job(args.source, args.caption, args.reaction, args.rights_ok, args.publish_at)
    elif args.cmd == "list":
        list_jobs()


if __name__ == "__main__":
    main()
