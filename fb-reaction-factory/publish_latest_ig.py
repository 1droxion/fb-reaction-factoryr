#!/usr/bin/env python3
import argparse
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
ENV_FILE = ROOT / ".env"
CLEAN_CAPTION = (
    "Wait for the ending 😂\n\n"
    "This reaction gets better at the end 😂 Follow for more daily funny reactions.\n\n"
    "#FunnyVideos #ReactionVideo #ComedyReels #WaitForIt #FunnyReels"
)


def load_env_file(path: Path):
    if not path.exists():
        raise RuntimeError(f"Missing {path}. Run meta_connect.py first.")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def latest_reel():
    reels = sorted(OUTPUT.glob("reel_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not reels:
        raise RuntimeError("No finished Reel found in output/.")
    return reels[0]


def dirty_caption(text: str):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if not lines:
        return True
    first = lines[0]
    compact = re.sub(r"[^A-Za-z0-9]", "", first)
    if re.fullmatch(r"[0-9a-fA-F]{16,}", compact):
        return True
    if first.lower().startswith(("approved_", "approved ", "auto_", "reel_")):
        return True
    if len(lines) > 1 and lines[0] == lines[1] and len(compact) >= 12:
        return True
    return False


def caption_for(video: Path):
    text_path = video.with_name(video.stem + "_facebook.txt")
    if text_path.exists():
        text = text_path.read_text(encoding="utf-8").strip()
        if text and not dirty_caption(text):
            return text, text_path
        print("Old/random caption detected. Replacing it with a clean caption.")
        text_path.write_text(CLEAN_CAPTION + "\n", encoding="utf-8")
        return CLEAN_CAPTION, text_path
    return CLEAN_CAPTION, None


def main():
    ap = argparse.ArgumentParser(description="Publish the newest finished Reaction Factory Reel to Instagram.")
    ap.add_argument("--publish", action="store_true", help="Actually publish. Without this flag the command is a dry run.")
    args = ap.parse_args()

    load_env_file(ENV_FILE)
    video = latest_reel()
    caption, text_path = caption_for(video)

    ig_id = os.getenv("META_IG_USER_ID", "").strip()
    ig_username = os.getenv("META_IG_USERNAME", "").strip()
    if not ig_id:
        raise RuntimeError("META_IG_USER_ID is missing from .env")

    print(f"Instagram: @{ig_username or '?'} ({ig_id})")
    print(f"Video: {video}")
    if text_path:
        print(f"Caption file: {text_path}")
    print("\nCaption preview:\n" + "-" * 50)
    print(caption)
    print("-" * 50)

    if not args.publish:
        print("\nDRY RUN ONLY - nothing was posted.")
        print("Run again with --publish to post this Reel to Instagram.")
        return

    from instagram import publish_reel

    print("\nUploading directly to Instagram...")
    result = publish_reel(video, caption)
    print("\nINSTAGRAM PUBLISH SUCCESS")
    print(f"Media ID: {result.get('media_id')}")
    if result.get("permalink"):
        print(f"Permalink: {result['permalink']}")


if __name__ == "__main__":
    main()
