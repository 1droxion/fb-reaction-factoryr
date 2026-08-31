#!/usr/bin/env python3
import os
from datetime import datetime

import auto_pipeline
import autopilot
from autopilot import load_state, next_url, parse_dt, run_cycle, save_state
from cloud_sync import load_env, pull_reactions, pull_state, push_state


OLD_LENGTH_ERROR = "AutoPilot requires 35-60s"


def clear_old_length_skips():
    state = load_state()
    failed = state.setdefault("failed", {})
    retry_urls = [
        url
        for url, item in failed.items()
        if OLD_LENGTH_ERROR in str((item or {}).get("error", ""))
    ]
    if not retry_urls:
        return state

    for url in retry_urls:
        failed.pop(url, None)
    state["next_run"] = None
    state["last_error"] = None
    state["current_progress"] = None
    save_state(state)
    print(f"Retry enabled for {len(retry_urls)} Reel(s) skipped by the old 35-second minimum.")
    return state


def prefer_latest_dashboard_url(state):
    preferred = str(state.get("last_url") or "").strip()
    if preferred:
        processed = state.setdefault("processed", {})
        failed = state.setdefault("failed", {})
        if processed.get(preferred) != "success" and not failed.get(preferred, {}).get("skip"):
            return preferred
    return next_url(state)


def main():
    load_env()
    pull_reactions()
    pull_state()

    # Instagram Reels shorter than 35 seconds are valid. Keep a 4-second
    # minimum for a usable reaction edit and retain the 60-second cap.
    auto_pipeline.MIN_SECONDS = 4.0
    auto_pipeline.MAX_SECONDS = 60.0

    state = clear_old_length_skips()
    force_now = os.getenv("REACTION_FACTORY_FORCE_NOW", "").strip() == "1"

    # POST NOW must process the exact dashboard URL that was just submitted.
    # The dashboard stores that URL in state.last_url, so temporarily make it
    # the first choice for this worker run. Older waiting items stay queued.
    if force_now:
        autopilot.next_url = prefer_latest_dashboard_url

    waiting_url = prefer_latest_dashboard_url(state) if force_now else next_url(state)
    target = parse_dt(state.get("next_run"))
    now = datetime.now().astimezone()

    # A URL pasted into the private dashboard is always an immediate job.
    # The old 3-hour next_run window is only a fallback when the queue is empty.
    if target and target > now and not force_now and not waiting_url:
        print(f"Not due yet. Next fallback check window: {target.isoformat(timespec='minutes')}")
        push_state()
        return

    if waiting_url and target and target > now and not force_now:
        print("Queued dashboard URL found; bypassing the 3-hour wait window.")

    if force_now:
        print("One-time POST NOW trigger received; prioritizing the latest dashboard URL.")
        if waiting_url:
            print(f"POST NOW source: {waiting_url}")

    status = None
    try:
        status = run_cycle(
            3.0,
            publish_instagram=True,
            publish_facebook=False,
            progress_sync=push_state,
        )
    finally:
        push_state()

    if status in {"failed", "token_error"}:
        raise SystemExit(f"AutoPilot post failed with status: {status}")


if __name__ == "__main__":
    main()
