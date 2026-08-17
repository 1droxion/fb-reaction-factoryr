#!/usr/bin/env python3
import argparse
import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
REACTIONS = ROOT / "reactions"
CLOUD_URL = "https://zlnhaqzawbzagraxhmlb.supabase.co/functions/v1/reaction-factory-cloud"
STATE_FILES = (
    "source_feeds.txt",
    "approved_urls.txt",
    "reactions.json",
    "discovery_state.json",
    "autopilot_state.json",
    "auto_pipeline_state.json",
)

DATA.mkdir(parents=True, exist_ok=True)
REACTIONS.mkdir(parents=True, exist_ok=True)


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
    h = {
        "Authorization": f"Bearer {token()}",
        "X-Meta-Version": os.getenv("META_GRAPH_VERSION", "v26.0"),
    }
    if content_type:
        h["Content-Type"] = content_type
    return h


def cloud_url(op, **params):
    query = "&".join(f"{quote(str(k))}={quote(str(v))}" for k, v in params.items())
    return f"{CLOUD_URL}?op={quote(op)}" + (f"&{query}" if query else "")


def upload_file(local_path: Path, remote_path: str):
    content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
    with local_path.open("rb") as f:
        r = requests.post(
            cloud_url("upload", path=remote_path),
            headers=headers(content_type),
            data=f,
            timeout=180,
        )
    if not r.ok:
        raise RuntimeError(f"Cloud upload failed for {remote_path}: {r.status_code} {r.text[:300]}")
    print(f"Uploaded: {remote_path}")


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


def push_reactions():
    files = sorted(REACTIONS.glob("*.mp4"))
    if not files:
        raise RuntimeError("No reaction MP4 files found in reactions/.")
    for path in files:
        upload_file(path, f"reactions/{path.name}")
    print(f"Reaction clips synced: {len(files)}")


def pull_reactions():
    names = [x for x in list_remote("reactions") if x.lower().endswith(".mp4")]
    if not names:
        raise RuntimeError("No reaction clips are stored in cloud yet. Run: python3 cloud_sync.py bootstrap")
    for name in names:
        download_file(f"reactions/{name}", REACTIONS / name, required=True)
    print(f"Reaction clips restored: {len(names)}")


def push_state():
    count = 0
    for name in STATE_FILES:
        path = DATA / name
        if path.exists():
            upload_file(path, f"state/{name}")
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
