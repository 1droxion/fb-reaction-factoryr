#!/usr/bin/env python3
import os
import time

from channel_dispatch import run_channel_dispatch
from cloud_multiplatform import run_cloud_cycle
from cloud_sync import load_env, pull_reactions, pull_state, push_state
from cloud_youtube_auth import pull_youtube_token

POLL_SECONDS = max(5, int(os.getenv("REACTION_FACTORY_POLL_SECONDS", "10")))
MAX_RUNTIME_SECONDS = max(300, int(os.getenv("REACTION_FACTORY_MAX_RUNTIME_SECONDS", str(5 * 60 * 60 + 30 * 60))))


def main():
    load_env()
    pull_reactions()

    try:
        if pull_youtube_token(required=False):
            print("YouTube cloud authorization is ready.")
        else:
            print("YouTube cloud authorization is not uploaded yet; Instagram/Facebook can still run.")
    except Exception as exc:
        print(f"YouTube cloud authorization restore failed: {exc}")

    started = time.monotonic()
    print("Reaction Factory multi-platform LIVE worker is online.")
    print(f"Polling Supabase dashboard queue every {POLL_SECONDS} seconds.")

    while time.monotonic() - started < MAX_RUNTIME_SECONDS:
        try:
            pull_state()
            channel_status = run_channel_dispatch(progress_sync=push_state)
            status = run_cloud_cycle(progress_sync=push_state) if channel_status == "no-channel" else channel_status
            if status in {"idle", "waiting"}:
                time.sleep(POLL_SECONDS)
            else:
                print(f"Cloud job finished with status: {status}")
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
