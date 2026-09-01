#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path

from cloud_sync import ROOT, download_file, load_env, upload_file

REMOTE_TOKEN = "state/youtube_token.secure.json"
LOCAL_TOKEN = ROOT / "youtube_token.json"


def _candidate_tokens():
    configured = os.getenv("YOUTUBE_TOKEN_FILE", "").strip()
    if configured:
        yield Path(configured).expanduser()
    yield LOCAL_TOKEN
    yield Path.home() / "youtube-auto-publisher" / "youtube_token.json"
    yield ROOT.parent.parent / "youtube-auto-publisher" / "youtube_token.json"


def _validate(path: Path):
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"YouTube token file not found: {path}")
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"YouTube token JSON is invalid: {exc}") from exc

    required = ["client_id", "client_secret", "refresh_token"]
    missing = [name for name in required if not str(info.get(name) or "").strip()]
    if missing:
        raise RuntimeError("YouTube token is missing: " + ", ".join(missing))

    scopes = info.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    required_scopes = {
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
    }
    if not required_scopes.issubset(set(scopes)):
        raise RuntimeError("YouTube token does not contain upload + readonly scopes. Run connect_youtube.py again.")
    return info


def find_local_token():
    for path in _candidate_tokens():
        if path.exists():
            _validate(path)
            return path
    raise RuntimeError(
        "No YouTube token found. Expected youtube_token.json in the Reaction Factory folder "
        "or in ~/youtube-auto-publisher/."
    )


def _prepare_cloud_auth():
    """Reuse whichever working Meta credential the local Reaction Factory already has."""
    current = os.getenv("META_PAGE_ACCESS_TOKEN", "").strip()
    if current:
        return

    fallback = (
        os.getenv("META_SYSTEM_USER_ACCESS_TOKEN", "").strip()
        or os.getenv("META_USER_ACCESS_TOKEN", "").strip()
    )
    if fallback:
        os.environ["META_PAGE_ACCESS_TOKEN"] = fallback
        return

    raise RuntimeError(
        "No local Meta credential found for secure Supabase upload. "
        "Expected META_SYSTEM_USER_ACCESS_TOKEN, META_PAGE_ACCESS_TOKEN, or META_USER_ACCESS_TOKEN in .env."
    )


def push_youtube_token():
    source = find_local_token()
    _prepare_cloud_auth()
    upload_file(source, REMOTE_TOKEN)
    print("YouTube cloud authorization saved securely in private Supabase storage.")
    print("Token contents were not added to GitHub.")


def pull_youtube_token(required=False):
    _prepare_cloud_auth()
    ok = download_file(REMOTE_TOKEN, LOCAL_TOKEN, required=required)
    if not ok:
        return False
    _validate(LOCAL_TOKEN)
    try:
        os.chmod(LOCAL_TOKEN, 0o600)
    except OSError:
        pass
    print("YouTube cloud authorization restored from private Supabase storage.")
    return True


def main():
    load_env()
    parser = argparse.ArgumentParser(description="Securely sync YouTube OAuth for the Reaction Factory cloud worker")
    parser.add_argument("command", choices=("push", "pull", "check"))
    args = parser.parse_args()

    if args.command == "push":
        push_youtube_token()
    elif args.command == "pull":
        if not pull_youtube_token(required=True):
            raise RuntimeError("No YouTube cloud authorization found in Supabase.")
    else:
        path = find_local_token()
        _validate(path)
        print(f"YouTube token ready: {path}")


if __name__ == "__main__":
    main()
