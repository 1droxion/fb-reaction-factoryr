#!/usr/bin/env python3
from datetime import datetime

from autopilot import load_state, parse_dt, run_cycle
from cloud_sync import load_env, pull_reactions, pull_state, push_state


def main():
    load_env()
    pull_reactions()
    pull_state()

    state = load_state()
    target = parse_dt(state.get("next_run"))
    now = datetime.now().astimezone()
    if target and target > now:
        print(f"Not due yet. Next post window: {target.isoformat(timespec='minutes')}")
        return

    status = None
    try:
        status = run_cycle(5.0, publish_instagram=True, publish_facebook=True)
    finally:
        push_state()

    if status in {"failed", "token_error"}:
        raise SystemExit(f"AutoPilot post failed with status: {status}")


if __name__ == "__main__":
    main()
