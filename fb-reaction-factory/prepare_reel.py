#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

from metadata import generate_metadata
from reaction_factory import make_reel


def build_post_text(metadata):
    title = (metadata.get("title") or "").strip()
    description = (metadata.get("description") or "").strip()
    hashtags = metadata.get("hashtags") or []
    if isinstance(hashtags, str):
        hashtags = hashtags.split()
    hashtag_text = " ".join(str(x).strip() for x in hashtags if str(x).strip())
    return "\n\n".join(x for x in (title, description, hashtag_text) if x).strip()


def write_package(video_path: Path, metadata, extra=None):
    stem = video_path.with_suffix("")
    text_path = Path(str(stem) + "_facebook.txt")
    json_path = Path(str(stem) + "_metadata.json")
    post_text = build_post_text(metadata)

    text_path.write_text(post_text + "\n", encoding="utf-8")
    payload = {
        "video": str(video_path),
        "metadata": metadata,
        "facebook_post_text": post_text,
    }
    if extra:
        payload.update(extra)
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return text_path, json_path, post_text


def main():
    ap = argparse.ArgumentParser(
        description="Prepare a Facebook reaction Reel and ready-to-copy post text."
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--source", help="Source video path or approved URL to render")
    mode.add_argument("--existing", help="Existing rendered Reel; metadata only, no re-render")
    ap.add_argument("--caption", default="funny reaction", help="Short context used to create metadata")
    ap.add_argument("--reaction", default="auto", help="Reaction label/id, or auto")
    ap.add_argument("--rights-ok", action="store_true", help="Confirm you have rights/permission to reuse the source")
    args = ap.parse_args()

    extra = {}
    if args.existing:
        video = Path(args.existing)
        if not video.exists():
            raise FileNotFoundError(video)
    else:
        if not args.rights_ok:
            raise RuntimeError("Use --rights-ok only after confirming you have rights/permission to reuse the source clip.")
        video, reaction = make_reel(
            args.source,
            caption=args.caption,
            reaction=args.reaction,
            rights_ok=True,
        )
        video = Path(video)
        extra["reaction_used"] = reaction

    metadata = generate_metadata(args.caption)
    text_path, json_path, post_text = write_package(video, metadata, extra)

    print("\nREADY FOR FACEBOOK")
    print("=" * 60)
    print(post_text)
    print("=" * 60)
    print(f"Video: {video}")
    print(f"Copy/paste text: {text_path}")
    print(f"Metadata: {json_path}")
    print("Personal Facebook profiles still require the final upload/post action manually.")


if __name__ == "__main__":
    main()
