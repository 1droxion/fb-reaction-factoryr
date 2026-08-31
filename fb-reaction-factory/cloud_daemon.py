#!/usr/bin/env python3
import os
import time

import auto_pipeline
import autopilot
from autopilot import load_state, run_cycle, save_state
from cloud_job import clear_old_length_skips, prefer_latest_dashboard_url
from cloud_sync import load_env, pull_reactions, pull_state, push_state

POLL_SECONDS = max(5, int(os.getenv("REACTION_FACTORY_POLL_SECONDS", "10")))
MAX_RUNTIME_SECONDS = max(300, int(os.getenv("REACTION_FACTORY_MAX_RUNTIME_SECONDS", str(5 * 60 * 60 + 30 * 60))))


def main():
    load_env()
    pull_reactions()

    auto_pipeline.MIN_SECONDS = 4.0
    auto_pipeline.MAX_SECONDS = 60.0
    autopilot.next_url = prefer_latest_dashboard_url

    started = time.monotonic()
    print("Reaction Factory LIVE worker is online.")
    print(f"Polling dashboard queue every {POLL_SECONDS} seconds.")

    while time.monotonic() - started < MAX_RUNTIME_SECONDS:
        try:
            pull_state()
            state = clear_old_length_skips()
            waiting_url = prefer_latest_dashboard_url(state)

            if not waiting_url:
                time.sleep(POLL_SECONDS)
                continue

            # Manual dashboard jobs are immediate. Ignore any old 3-hour fallback window.
            state["next_run"] = None
            save_state(state)
            push_state()

            print(f"Immediate dashboard job found: {waiting_url}")
            status = run_cycle(
                3.0,
                publish_instagram=True,
                publish_facebook=False,
                progress_sync=push_state,
            )
            print(f"Job finished with status: {status}")
            time.sleep(2)
        except Exception as exc:
            print(f"Live worker cycle error: {exc}")
            try:
                push_state()
            except Exception as sync_exc:
                print(f"State sync after error failed: {sync_exc}")
            time.sleep(POLL_SECONDS)

    print("Live worker runtime window complete; scheduled restart will take over.")


if __name__ == "__main__":
    main()
