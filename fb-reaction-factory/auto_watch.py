#!/usr/bin/env python3
import argparse
import json
import time
from pathlib import Path

from metadata import generate_metadata
from prepare_reel import write_package
from reaction_factory import make_reel

ROOT = Path(__file__).resolve().parent
INBOX = ROOT / "sources" / "approved_inbox"
STATE = ROOT / "data" / "auto_watch_state.json"
SUPPORTED = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}

INBOX.mkdir(parents=True, exist_ok=True)
STATE.parent.mkdir(parents=True, exist_ok=True)


def load_state():
    if not STATE.exists():
        return {"processed": {}}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"processed": {}}


def save_state(state):
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def caption_for(path: Path):
    text = path.stem.replace("_", " ").replace("-", " ").strip()
    return text or "funny reaction"


def stable_file(path: Path, wait_seconds=2):
    try:
        size1 = path.stat().st_size
        time.sleep(wait_seconds)
        size2 = path.stat().st_size
        return size1 > 0 and size1 == size2
    except FileNotFoundError:
        return False


def process_once(reaction="auto"):
    state = load_state()
    processed = state.setdefault("processed", {})
    files = sorted(
        (p for p in INBOX.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED),
        key=lambda p: p.stat().st_mtime,
    )

    for source in files:
        key = str(source.resolve())
        stamp = f"{source.stat().st_size}:{source.stat().st_mtime_ns}"
        if processed.get(key) == stamp:
            continue
        if not stable_file(source):
            continue

        caption = caption_for(source)
        print(f"Preparing: {source.name}")
        video, reaction_used = make_reel(
            str(source),
            caption=caption,
            reaction=reaction,
            rights_ok=True,
        )
        metadata = generate_metadata(caption)
        text_path, json_path, post_text = write_package(
            Path(video),
            metadata,
            {"reaction_used": reaction_used, "source_inbox_file": str(source)},
        )
        processed[key] = stamp
        save_state(state)
        print("\nREADY FOR FACEBOOK")
        print("=" * 60)
        print(post_text)
        print("=" * 60)
        print(f"Video: {video}")
        print(f"Copy/paste text: {text_path}")
        print(f"Metadata: {json_path}")
        return True
    return False


def main():
    ap = argparse.ArgumentParser(
        description="Watch sources/approved_inbox and auto-prepare reaction Reels. Only place clips here after confirming reuse rights/permission."
    )
    ap.add_argument("--reaction", default="auto")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=10)
    args = ap.parse_args()

    print(f"Approved inbox: {INBOX}")
    print("Only put clips here that you own or have permission/license to reuse.")

    if args.loop:
        while True:
            try:
                process_once(args.reaction)
            except Exception as exc:
                print(f"ERROR: {exc}")
            time.sleep(max(3, args.interval))
    else:
        if not process_once(args.reaction):
            print("No new approved clips found.")


if __name__ == "__main__":
    main()
