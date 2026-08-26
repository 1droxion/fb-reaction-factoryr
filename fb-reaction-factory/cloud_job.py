#!/usr/bin/env python3
import os
from datetime import datetime
from pathlib import Path

from autopilot import load_state, parse_dt, run_cycle
from cloud_sync import load_env, pull_reactions, pull_state, push_state

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
URLS_FILE = DATA / "approved_urls.txt"
FEEDS_FILE = DATA / "source_feeds.txt"

# Keep direct approved URLs for one-off processing.
SEED_URLS = [
    "https://www.instagram.com/reel/DEUj3fkshcA/",
]

# Approved discovery accounts. Use profile URLs (not /reels/) so
# auto_discover.py can resolve the username through Meta business discovery.
SEED_SOURCE_FEEDS = [
    "https://www.instagram.com/kapilsharmafp55/",
]


def append_missing(path, header, values, label):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(header, encoding="utf-8")

    existing = {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    additions = [value for value in values if value not in existing]
    if not additions:
        return

    current = path.read_text(encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        if current and not current.endswith("\n"):
            handle.write("\n")
        for value in additions:
            handle.write(value + "\n")
            print(f"Added {label}: {value}")


def seed_sources():
    append_missing(
        URLS_FILE,
        "# AutoPilot queue. One approved source URL per line.\n",
        SEED_URLS,
        "approved source URL",
    )
    append_missing(
        FEEDS_FILE,
        (
            "# Approved discovery feeds. Only include accounts/content you own "
            "or have permission/license to reuse.\n"
        ),
        SEED_SOURCE_FEEDS,
        "approved discovery source",
    )


def main():
    load_env()
    pull_reactions()
    pull_state()
    seed_sources()

    force_now = os.getenv("REACTION_FACTORY_FORCE_NOW", "").strip() == "1"
    state = load_state()
    target = parse_dt(state.get("next_run"))
    now = datetime.now().astimezone()
    if target and target > now and not force_now:
        print(f"Not due yet. Next post window: {target.isoformat(timespec='minutes')}")
        push_state()
        return

    if force_now:
        print("One-time POST NOW trigger received; bypassing the current wait window.")

    status = None
    try:
        status = run_cycle(3.0, publish_instagram=True, publish_facebook=False)
    finally:
        push_state()

    if status in {"failed", "token_error"}:
        raise SystemExit(f"AutoPilot post failed with status: {status}")


if __name__ == "__main__":
    main()
