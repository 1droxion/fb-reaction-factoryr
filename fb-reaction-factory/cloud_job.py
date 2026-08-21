#!/usr/bin/env python3
from datetime import datetime
from pathlib import Path

from autopilot import load_state, parse_dt, run_cycle
from cloud_sync import load_env, pull_reactions, pull_state, push_state

ROOT = Path(__file__).resolve().parent
URLS_FILE = ROOT / "data" / "approved_urls.txt"
SEED_URLS = [
    "https://www.instagram.com/reel/DEUj3fkshcA/",
]


def seed_approved_urls():
    URLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not URLS_FILE.exists():
        URLS_FILE.write_text("# AutoPilot queue. One approved source URL per line.\n", encoding="utf-8")

    existing = {
        line.strip()
        for line in URLS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    additions = [url for url in SEED_URLS if url not in existing]
    if not additions:
        return

    current = URLS_FILE.read_text(encoding="utf-8")
    with URLS_FILE.open("a", encoding="utf-8") as f:
        if current and not current.endswith("\n"):
            f.write("\n")
        for url in additions:
            f.write(url + "\n")
            print(f"Queued approved source: {url}")


def main():
    load_env()
    pull_reactions()
    pull_state()
    seed_approved_urls()

    state = load_state()
    target = parse_dt(state.get("next_run"))
    now = datetime.now().astimezone()
    if target and target > now:
        print(f"Not due yet. Next post window: {target.isoformat(timespec='minutes')}")
        return

    status = None
    try:
        status = run_cycle(3.0, publish_instagram=True, publish_facebook=False)
    finally:
        push_state()

    if status in {"failed", "token_error"}:
        raise SystemExit(f"AutoPilot post failed with status: {status}")


if __name__ == "__main__":
    main()
