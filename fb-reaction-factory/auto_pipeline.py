#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from facebook import publish_reel as publish_facebook
from instagram import publish_reel as publish_instagram
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
ENV_FILE = ROOT / ".env"
MIN_SECONDS = 60.0
PUBLIC_PORT = int(os.getenv("REACTION_PUBLIC_PORT", "8765"))

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
        os.environ.setdefault(key.strip(), value.strip())


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
            "# Only add URLs you are allowed to download and reuse.\n",
            encoding="utf-8",
        )


def approved_urls():
    ensure_url_file()
    urls = []
    for raw in URLS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def ytdlp():
    exe = shutil.which("yt-dlp")
    if not exe:
        raise RuntimeError("yt-dlp is not installed. Run: python3 -m pip install -r requirements.txt")
    return exe


def download_url(url):
    token = uuid.uuid4().hex[:10]
    template = str(INBOX / f"approved_{token}.%(ext)s")
    cmd = [
        ytdlp(), "--no-playlist",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", template,
        url,
    ]
    print(f"Downloading approved URL: {url}")
    subprocess.run(cmd, check=True)
    matches = sorted(INBOX.glob(f"approved_{token}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        raise RuntimeError("Download finished but no file was found.")
    return matches[0]


def metadata_caption(metadata):
    title = (metadata.get("title") or "").strip()
    description = (metadata.get("description") or "").strip()
    tags = metadata.get("hashtags") or []
    if isinstance(tags, str):
        tags = tags.split()
    hashtag_text = " ".join(str(x).strip() for x in tags if str(x).strip())
    return "\n\n".join(x for x in (title, description, hashtag_text) if x).strip()


def codespaces_public_url(video_path):
    name = os.getenv("CODESPACE_NAME", "").strip()
    domain = os.getenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev").strip()
    if not name:
        raise RuntimeError("Instagram auto-upload currently expects GitHub Codespaces.")
    return f"https://{name}-{PUBLIC_PORT}.{domain}/{video_path.name}"


def start_video_server():
    cmd = [sys.executable, "-m", "http.server", str(PUBLIC_PORT), "--directory", str(OUTPUT)]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    if proc.poll() is not None:
        raise RuntimeError("Could not start temporary video server.")
    return proc


def make_port_public():
    gh = shutil.which("gh")
    if not gh:
        raise RuntimeError("GitHub CLI is required to make the temporary Codespaces port public.")
    subprocess.run(
        [gh, "codespace", "ports", "visibility", str(PUBLIC_PORT) + ":public", "-c", os.getenv("CODESPACE_NAME", "")],
        check=True,
    )


def make_port_private():
    gh = shutil.which("gh")
    if not gh or not os.getenv("CODESPACE_NAME"):
        return
    subprocess.run(
        [gh, "codespace", "ports", "visibility", str(PUBLIC_PORT) + ":private", "-c", os.getenv("CODESPACE_NAME", "")],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def process_url(url, publish_fb=False, publish_ig=False):
    source = download_url(url)
    duration = ffprobe_duration(source)
    if duration < MIN_SECONDS:
        raise RuntimeError(f"Source is only {duration:.1f}s. Need at least {MIN_SECONDS:.0f}s.")

    caption_seed = source.stem.replace("_", " ").replace("-", " ")
    video, reaction_used = make_reel(
        str(source),
        caption=caption_seed,
        reaction="auto",
        rights_ok=True,
    )
    metadata = generate_metadata(caption_seed)
    text_path, json_path, post_text = write_package(
        Path(video),
        metadata,
        {
            "source_url": url,
            "source_duration_seconds": duration,
            "reaction_used": reaction_used,
            "target_reel_seconds": 60,
        },
    )

    print("\nREADY")
    print(f"Video: {video}")
    print(f"Caption: {text_path}")
    print(f"Metadata: {json_path}")

    results = {"video": str(video), "facebook": None, "instagram": None}

    if publish_fb:
        print("\nPublishing to Facebook...")
        results["facebook"] = publish_facebook(video, post_text)
        print(f"Facebook success. Video ID: {results['facebook'].get('video_id')}")

    if publish_ig:
        server = None
        try:
            server = start_video_server()
            make_port_public()
            public_url = codespaces_public_url(Path(video))
            print(f"Temporary Instagram fetch URL: {public_url}")
            print("Publishing to Instagram...")
            results["instagram"] = publish_instagram(public_url, post_text)
            print(f"Instagram success. Media ID: {results['instagram'].get('media_id')}")
        finally:
            make_port_private()
            if server:
                server.terminate()

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
            return False
    print(f"No new approved URLs. Add one per line to: {URLS_FILE}")
    return False


def main():
    ap = argparse.ArgumentParser(description="Approved URL -> 60s reaction Reel -> metadata -> Facebook/Instagram publishing")
    ap.add_argument("--publish-facebook", action="store_true")
    ap.add_argument("--publish-instagram", action="store_true")
    ap.add_argument("--publish-both", action="store_true")
    args = ap.parse_args()
    fb = args.publish_facebook or args.publish_both
    ig = args.publish_instagram or args.publish_both
    run_once(publish_fb=fb, publish_ig=ig)


if __name__ == "__main__":
    main()
