#!/usr/bin/env python3
import json
import os
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
DATA = ROOT / "data"
REACTIONS = ROOT / "reactions"
REACTIONS_JSON = DATA / "reactions.json"


def load_env_file():
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()


def ensure_page_token():
    existing = os.getenv("META_PAGE_ACCESS_TOKEN", "").strip()
    if existing:
        return existing

    system_token = os.getenv("META_SYSTEM_USER_ACCESS_TOKEN", "").strip()
    page_id = os.getenv("META_PAGE_ID", "").strip()
    version = os.getenv("META_GRAPH_VERSION", "v26.0").strip() or "v26.0"
    if not system_token or not page_id:
        raise RuntimeError("META_SYSTEM_USER_ACCESS_TOKEN and META_PAGE_ID are required in .env")

    response = requests.get(
        f"https://graph.facebook.com/{version}/{page_id}",
        params={"fields": "id,name,access_token", "access_token": system_token},
        timeout=60,
    )
    data = response.json() if response.content else {}
    if not response.ok or data.get("error"):
        message = data.get("error", {}).get("message") or response.text[:300]
        raise RuntimeError(f"Could not resolve Page access token: {message}")

    page_token = str(data.get("access_token") or "").strip()
    if not page_token:
        raise RuntimeError("Meta did not return a Page access token for TVMind USA.")
    os.environ["META_PAGE_ACCESS_TOKEN"] = page_token
    return page_token


def ensure_library_file():
    DATA.mkdir(parents=True, exist_ok=True)
    REACTIONS.mkdir(parents=True, exist_ok=True)

    try:
        existing = json.loads(REACTIONS_JSON.read_text(encoding="utf-8")) if REACTIONS_JSON.exists() else []
    except Exception:
        existing = []

    existing = existing if isinstance(existing, list) else []
    usable = []
    for item in existing:
        if not isinstance(item, dict):
            continue
        name = Path(str(item.get("path") or "")).name
        if name and (REACTIONS / name).exists():
            copy = dict(item)
            copy["path"] = f"reactions/{name}"
            usable.append(copy)

    if usable:
        REACTIONS_JSON.write_text(json.dumps(usable, indent=2), encoding="utf-8")
        return len(usable)

    clips = sorted(REACTIONS.glob("*.mp4"))
    rebuilt = []
    for clip in clips:
        rebuilt.append({
            "id": uuid.uuid4().hex[:10],
            "label": "funny",
            "path": f"reactions/{clip.name}",
            "notes": "Restored from Reaction Factory private cloud",
        })
    REACTIONS_JSON.write_text(json.dumps(rebuilt, indent=2), encoding="utf-8")
    return len(rebuilt)


def main():
    load_env_file()
    ensure_page_token()

    from cloud_sync import pull_reactions, pull_state

    print("Restoring saved Reaction Factory assets...")
    pull_reactions()
    pull_state()
    count = ensure_library_file()
    if count < 1:
        raise RuntimeError("Cloud restore completed but no reaction clips were found.")

    print(f"\nREACTION RESTORE SUCCESS: {count} clip(s) ready")
    print("Start the dashboard with: python dashboard.py")


if __name__ == "__main__":
    main()
