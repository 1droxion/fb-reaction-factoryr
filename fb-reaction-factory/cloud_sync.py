#!/usr/bin/env python3
import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
REACTIONS = ROOT / "reactions"
CACHE = DATA / "cloud_upload_cache"
CLOUD_URL = "https://zlnhaqzawbzagraxhmlb.supabase.co/functions/v1/reaction-factory-cloud"
STATE_FILES = (
    "source_feeds.txt",
    "approved_urls.txt",
    "reactions.json",
    "discovery_state.json",
    "autopilot_state.json",
    "auto_pipeline_state.json",
)
MAX_CLOUD_BYTES = 6 * 1024 * 1024
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

DATA.mkdir(parents=True, exist_ok=True)
REACTIONS.mkdir(parents=True, exist_ok=True)
CACHE.mkdir(parents=True, exist_ok=True)


def load_env():
    env_file = ROOT / ".env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()


def token():
    value = os.getenv("META_PAGE_ACCESS_TOKEN", "").strip()
    if not value:
        raise RuntimeError("META_PAGE_ACCESS_TOKEN is missing. Run python3 meta_connect.py first.")
    return value


def headers(content_type=None):
    # Use a custom header for the internal worker gateway. This avoids any
    # platform-level handling of the normal Authorization header while the
    # gateway still validates the token against the configured Meta Page.
    h = {
        "X-Meta-Access-Token": token(),
        "X-Meta-Version": os.getenv("META_GRAPH_VERSION", "v26.0"),
    }
    if content_type:
        h["Content-Type"] = content_type
    return h


def cloud_url(op, **params):
    query = "&".join(f"{quote(str(k))}={quote(str(v))}" for k, v in params.items())
    return f"{CLOUD_URL}?op={quote(op)}" + (f"&{query}" if query else "")


def safe_name(name):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._")
    if not cleaned:
        cleaned = "reaction.mp4"
    if not cleaned.lower().endswith(".mp4"):
        cleaned += ".mp4"
    return cleaned


def ffmpeg_path():
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg is required for cloud compression.")
    return exe


def compact_reaction(path: Path, remote_name: str) -> Path:
    if path.stat().st_size <= MAX_CLOUD_BYTES and path.name == remote_name:
        return path

    target = CACHE / remote_name
    if target.exists() and 0 < target.stat().st_size <= MAX_CLOUD_BYTES:
        print(f"Using cached cloud copy: {remote_name}")
        return target

    print(f"Preparing small safe cloud copy: {path.name} -> {remote_name}")
    cmd = [
        ffmpeg_path(), "-y", "-i", str(path),
        "-vf", "scale='min(720,iw)':-2,fps=24",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "29",
        "-maxrate", "900k", "-bufsize", "1800k",
        "-c:a", "aac", "-b:a", "64k",
        "-movflags", "+faststart",
        str(target),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not target.exists() or target.stat().st_size <= 0:
        raise RuntimeError(f"Cloud copy creation failed for {path.name}")
    if target.stat().st_size > MAX_CLOUD_BYTES:
        raise RuntimeError(
            f"Cloud copy is still too large ({target.stat().st_size / 1024 / 1024:.1f} MB): {path.name}"
        )
    print(f"Cloud copy ready: {target.stat().st_size / 1024 / 1024:.1f} MB")
    return target


def upload_file(local_path: Path, remote_path: str):
    content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
    last_detail = "unknown error"
    for attempt in range(1, 4):
        try:
            with local_path.open("rb") as f:
                r = requests.post(
                    cloud_url("upload", path=remote_path),
                    headers=headers(content_type),
                    data=f,
                    timeout=180,
                )
            if r.ok:
                print(f"Uploaded: {remote_path}")
                return
            last_detail = f"{r.status_code} {r.text[:300]}"
            if r.status_code not in RETRYABLE_STATUS:
                break
        except requests.RequestException as exc:
            last_detail = str(exc)
        if attempt < 3:
            print(f"Upload retry {attempt}/2 for {remote_path}...")
            time.sleep(attempt * 3)
    raise RuntimeError(f"Cloud upload failed for {remote_path}: {last_detail}")


def download_file(remote_path: str, local_path: Path, required=False):
    r = requests.get(cloud_url("download", path=remote_path), headers=headers(), timeout=180)
    if r.status_code == 404 and not required:
        return False
    if not r.ok:
        raise RuntimeError(f"Cloud download failed for {remote_path}: {r.status_code} {r.text[:300]}")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(r.content)
    print(f"Downloaded: {remote_path}")
    return True


def list_remote(prefix):
    r = requests.get(cloud_url("list", prefix=prefix), headers=headers(), timeout=60)
    if not r.ok:
        raise RuntimeError(f"Cloud list failed: {r.status_code} {r.text[:300]}")
    return (r.json() or {}).get("files") or []


def reaction_items():
    path = DATA / "reactions.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def referenced_reaction_files():
    files = []
    seen = set()
    for item in reaction_items():
        if not isinstance(item, dict):
            continue
        raw = str(item.get("path") or "").strip()
        if not raw:
            continue
        name = Path(raw).name
        local = REACTIONS / name
        if local.exists() and name not in seen:
            files.append(local)
            seen.add(name)
    if files:
        return files
    return sorted(REACTIONS.glob("*.mp4"))


def push_reactions():
    files = referenced_reaction_files()
    if not files:
        raise RuntimeError("No reaction MP4 files found in reactions/.")

    remote = set(list_remote("reactions"))
    uploaded = 0
    skipped = 0
    for path in files:
        remote_name = safe_name(path.name)
        if remote_name in remote:
            print(f"Already in cloud, skipping: reactions/{remote_name}")
            skipped += 1
            continue
        cloud_copy = compact_reaction(path, remote_name)
        upload_file(cloud_copy, f"reactions/{remote_name}")
        remote.add(remote_name)
        uploaded += 1
    print(f"Referenced reaction clips synced: {len(files)} total ({uploaded} uploaded, {skipped} already present)")


def pull_reactions():
    names = [x for x in list_remote("reactions") if x.lower().endswith(".mp4")]
    if not names:
        raise RuntimeError("No reaction clips are stored in cloud yet. Run: python3 cloud_sync.py bootstrap")
    for name in names:
        download_file(f"reactions/{name}", REACTIONS / name, required=True)
    print(f"Reaction clips restored: {len(names)}")


def portable_reactions_json():
    items = reaction_items()
    if not items:
        return None
    portable = []
    for item in items:
        if not isinstance(item, dict):
            continue
        copy = dict(item)
        raw = str(copy.get("path") or "").strip()
        if raw:
            copy["path"] = f"reactions/{safe_name(Path(raw).name)}"
        portable.append(copy)
    target = CACHE / "reactions.json"
    target.write_text(json.dumps(portable, indent=2), encoding="utf-8")
    return target


def push_state():
    count = 0
    for name in STATE_FILES:
        path = DATA / name
        if not path.exists():
            continue
        upload_path = path
        if name == "reactions.json":
            upload_path = portable_reactions_json() or path
        upload_file(upload_path, f"state/{name}")
        count += 1
    print(f"State files synced: {count}")


def pull_state():
    count = 0
    for name in STATE_FILES:
        if download_file(f"state/{name}", DATA / name, required=False):
            count += 1
    print(f"State files restored: {count}")


def health():
    r = requests.get(cloud_url("health"), headers=headers(), timeout=30)
    if not r.ok:
        raise RuntimeError(f"Cloud gateway failed: {r.status_code} {r.text[:300]}")
    print(json.dumps(r.json(), indent=2))


def main():
    load_env()
    ap = argparse.ArgumentParser(description="Sync Reaction Factory private assets/state with cloud storage")
    ap.add_argument("command", choices=("bootstrap", "pull", "push-state", "health"))
    args = ap.parse_args()

    if args.command == "bootstrap":
        health()
        push_reactions()
        push_state()
        print("\nCLOUD BOOTSTRAP SUCCESS")
    elif args.command == "pull":
        health()
        pull_reactions()
        pull_state()
    elif args.command == "push-state":
        push_state()
    elif args.command == "health":
        health()


if __name__ == "__main__":
    main()
