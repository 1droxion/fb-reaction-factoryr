#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from facebook import publish_reel as publish_facebook
from instagram import publish_reel as publish_instagram
from instagram_download import download_instagram_reel, is_instagram_url
from metadata import generate_metadata
from prepare_reel import write_package
from reaction_factory import ffprobe_duration, make_reel

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUTPUT = ROOT / "output"
SOURCES = ROOT / "sources"
INBOX = SOURCES / "approved_inbox"
URLS_FILE = DATA / "approved_urls.txt"
STATE_FILE = DATA / "auto_pipeline_state.json"
DISCOVERY_STATE_FILE = DATA / "discovery_state.json"
ENV_FILE = ROOT / ".env"
MIN_SECONDS = 4.0
TARGET_SECONDS = 60

for p in (DATA, OUTPUT, SOURCES, INBOX):
    p.mkdir(parents=True, exist_ok=True)


def load_env_file():
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # Always overwrite the current process environment so a freshly
        # refreshed token from meta_connect.py is picked up without restart.
        os.environ[key.strip()] = value.strip()


def load_state():
    if not STATE_FILE.exists():
        return {"done": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"done": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def ensure_url_file():
    if not URLS_FILE.exists():
        URLS_FILE.write_text(
            "# Put one approved source video URL per line.\n"
            "# Supports public Instagram/Facebook/video URLs and Google Drive shared files.\n"
            "# Only add URLs you own or have permission/license to download and reuse.\n",
            encoding="utf-8",
        )


def approved_urls():
    ensure_url_file()
    return [
        line.strip()
        for line in URLS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def comparable_url(url):
    value = (url or "").strip()
    if not value:
        return ""
    if "instagram.com" in value.lower():
        return value.split("?", 1)[0].rstrip("/")
    return value


def require_explicit_approval(url):
    approved = {comparable_url(item) for item in approved_urls()}
    if comparable_url(url) not in approved:
        raise RuntimeError(
            "Source is not in the approved queue. Only owned, licensed, or explicitly permissioned clips may be processed."
        )


def source_credit_for_url(url):
    if not DISCOVERY_STATE_FILE.exists():
        return None
    try:
        state = json.loads(DISCOVERY_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    selected = state.get("last_selected") or {}
    if not isinstance(selected, dict):
        return None

    selected_urls = {
        comparable_url(str(selected.get("url") or "")),
        comparable_url(str(selected.get("permalink") or "")),
    }
    if comparable_url(url) not in selected_urls:
        return None

    account = str(selected.get("source_account") or "").strip().lstrip("@")
    return f"@{account}" if account else None


def add_source_disclosure(post_text, text_path, json_path, url):
    credit = source_credit_for_url(url)
    disclosure = "Reaction edit from an approved source."
    if credit:
        disclosure += f" Source credit: {credit}."

    final_text = "\n\n".join(x for x in (post_text.strip(), disclosure) if x).strip()
    text_path.write_text(final_text + "\n", encoding="utf-8")

    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    payload["facebook_post_text"] = final_text
    payload["source_credit"] = credit
    payload["source_rights_required"] = True
    payload["reaction_edit"] = True
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return final_text


def ytdlp():
    exe = shutil.which("yt-dlp")
    if not exe:
        raise RuntimeError("yt-dlp is not installed. Run: python3 -m pip install -r requirements.txt")
    return exe


def google_drive_direct_url(url):
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "drive.google.com" not in host and "docs.google.com" not in host:
        return None

    match = re.search(r"/file/d/([^/]+)", parsed.path)
    file_id = match.group(1) if match else None
    if not file_id:
        file_id = parse_qs(parsed.query).get("id", [None])[0]
    if not file_id:
        return None
    return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"


def direct_download(url, token):
    target = INBOX / f"approved_{token}.mp4"
    response = requests.get(url, stream=True, timeout=120, allow_redirects=True)
    response.raise_for_status()
    content_type = (response.headers.get("content-type") or "").lower()
    if "text/html" in content_type:
        raise RuntimeError("The link returned a web page instead of a downloadable video.")
    with target.open("wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
    if target.stat().st_size < 10000:
        target.unlink(missing_ok=True)
        raise RuntimeError("Downloaded file is too small to be a usable video.")
    return target


def download_url(url):
    # Instagram gets its dedicated public-page fallback path, matching dashboard.py.
    # It does not bypass private/login-only/age/region/audience restrictions.
    if is_instagram_url(url):
        print("Downloading approved Instagram Reel...")
        return download_instagram_reel(url)

    token = uuid.uuid4().hex[:10]
    drive_url = google_drive_direct_url(url)
    if drive_url:
        print("Downloading approved Google Drive video...")
        try:
            return direct_download(drive_url, token)
        except Exception as exc:
            print(f"Google Drive direct download failed, trying yt-dlp: {exc}")

    template = str(INBOX / f"approved_{token}.%(ext)s")
    cmd = [
        ytdlp(), "--no-playlist",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", template,
        url,
    ]
    print(f"Downloading approved URL: {url}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        if url.lower().endswith((".mp4", ".mov", ".m4v", ".webm")):
            return direct_download(url, token)
        raise

    matches = sorted(INBOX.glob(f"approved_{token}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        raise RuntimeError("Download finished but no file was found.")
    return matches[0]


def clean_caption_seed(source):
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


def process_url(url, publish_fb=False, publish_ig=False):
    # Safety/rights gate: process_url cannot silently turn an arbitrary public URL
    # into a publishable Reel. It must already be in the explicit approved queue.
    require_explicit_approval(url)

    source = download_url(url)
    duration = ffprobe_duration(source)
    if duration < MIN_SECONDS:
        raise RuntimeError(f"Source is only {duration:.1f}s. Need at least {MIN_SECONDS:.0f}s.")

    caption_seed = clean_caption_seed(source)
    video, reaction_used = make_reel(
        str(source), caption=caption_seed, reaction="auto", rights_ok=True
    )
    metadata = generate_metadata(caption_seed)
    text_path, json_path, post_text = write_package(
        Path(video), metadata,
        {
            "source_url": url,
            "source_duration_seconds": duration,
            "reaction_used": reaction_used,
            "target_reel_seconds": TARGET_SECONDS,
            "source_looped": False,
            "rights_gate": "approved_queue",
        },
    )
    post_text = add_source_disclosure(post_text, text_path, json_path, url)

    print("\nREADY")
    print(f"Video: {video}")
    print(f"Caption: {text_path}")
    print(f"Metadata: {json_path}")

    results = {"video": str(video), "facebook": None, "instagram": None}

    if publish_fb:
        print("\nPublishing to Facebook Page...")
        results["facebook"] = publish_facebook(video, post_text)
        print(f"Facebook success. Video ID: {results['facebook'].get('video_id')}")

    if publish_ig:
        print("\nPublishing to Instagram...")
        results["instagram"] = publish_instagram(video, post_text)
        print(f"Instagram success. Media ID: {results['instagram'].get('media_id')}")
        if results["instagram"].get("permalink"):
            print(f"Instagram permalink: {results['instagram']['permalink']}")

    return results


def run_once(publish_fb=False, publish_ig=False):
    load_env_file()
    state = load_state()
    done = state.setdefault("done", {})
    for url in approved_urls():
        if done.get(url) == "success":
            continue
        try:
            results = process_url(url, publish_fb=publish_fb, publish_ig=publish_ig)
            done[url] = "success"
            state["last"] = {"url": url, "results": results}
            save_state(state)
            return True
        except Exception as exc:
            done[url] = f"error: {exc}"
            state["last"] = {"url": url, "error": str(exc)}
            save_state(state)
            print(f"ERROR: {exc}")
            continue
    print(f"No new approved URLs. Add one per line to: {URLS_FILE}")
    return False


def main():
    ap = argparse.ArgumentParser(description="Approved URL -> reaction Reel -> metadata -> Facebook Page/Instagram publishing")
    ap.add_argument("--publish-facebook", action="store_true")
    ap.add_argument("--publish-instagram", action="store_true")
    ap.add_argument("--publish-both", action="store_true")
    args = ap.parse_args()
    fb = args.publish_facebook or args.publish_both
    ig = args.publish_instagram or args.publish_both
    run_once(publish_fb=fb, publish_ig=ig)


if __name__ == "__main__":
    main()
