#!/usr/bin/env python3
import os
from datetime import datetime

from autopilot import load_state, parse_dt, run_cycle
from cloud_sync import load_env, pull_reactions, pull_state, push_state


def main():
    load_env()
    pull_reactions()
    pull_state()

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
