#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import signal
import time
from datetime import datetime, timedelta
from pathlib import Path

from auto_pipeline import approved_urls, load_env_file, process_url

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE_FILE = DATA / "autopilot_state.json"
LOCK_FILE = DATA / "autopilot.lock"
DEFAULT_INTERVAL_HOURS = 3.0
TOKEN_RETRY_MINUTES = 5
IDLE_POLL_SECONDS = 60
HISTORY_LIMIT = 100

DATA.mkdir(parents=True, exist_ok=True)
STOP = False


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def blank_state(last_error=None):
    return {
        "processed": {},
        "failed": {},
        "history": [],
        "last_success": None,
        "last_error": last_error,
        "next_run": None,
        "last_url": None,
        "last_results": None,
    }


def load_state():
    if not STATE_FILE.exists():
        return blank_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        data.setdefault("processed", {})
        data.setdefault("failed", {})
        data.setdefault("history", [])
        return data
    except Exception:
        return blank_state("State file could not be read; starting with a clean state.")


def save_state(state):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def add_history(state, status, url=None, *, error=None, results=None):
    item = {
        "at": now_iso(),
        "status": status,
        "url": url,
    }
    if error:
        item["error"] = str(error)
    if isinstance(results, dict):
        instagram = results.get("instagram") or {}
        if isinstance(instagram, dict):
            item["instagram_media_id"] = instagram.get("media_id")
            item["instagram_permalink"] = instagram.get("permalink")
        video = results.get("video")
        if video:
            item["video"] = str(video)
    history = state.setdefault("history", [])
    history.insert(0, item)
    del history[HISTORY_LIMIT:]


def next_url(state):
    processed = state.setdefault("processed", {})
    failed = state.setdefault("failed", {})
    for url in approved_urls():
        if processed.get(url) == "success":
            continue
        if failed.get(url, {}).get("skip"):
            continue
        return url
    return None


def looks_like_token_error(exc):
    text = str(exc).lower()
    signals = (
        "access token",
        "code 190",
        "subcode 463",
        "session has expired",
        "session is invalid",
        "oauth",
    )
    return any(s in text for s in signals)


def acquire_lock():
    handle = LOCK_FILE.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("Reaction Factory AutoPilot is already running in another terminal.")
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def wait_until(target):
    while not STOP:
        now = datetime.now().astimezone()
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(60, max(1, remaining)))


def print_status(state):
    urls = approved_urls()
    done = sum(1 for u in urls if state.get("processed", {}).get(u) == "success")
    skipped = sum(1 for u in urls if state.get("failed", {}).get(u, {}).get("skip"))
    waiting = max(0, len(urls) - done - skipped)
    print("\nREACTION FACTORY AUTOPILOT STATUS")
    print(f"Queue: {len(urls)} total · {waiting} waiting · {done} posted · {skipped} skipped")
    print("Source mode: dashboard/manual URLs only")
    print(f"Last success: {state.get('last_success') or 'none'}")
    print(f"Next run: {state.get('next_run') or 'as soon as a dashboard URL is available'}")
    if state.get("last_error"):
        print(f"Last error: {state['last_error']}")


def find_dashboard_url(state):
    url = next_url(state)
    if url:
        return url
    print("Queue empty. Waiting for a URL from the private dashboard.")
    return None


def run_cycle(interval_hours, publish_instagram=True, publish_facebook=True):
    load_env_file()
    state = load_state()
    url = find_dashboard_url(state)
    if not url:
        state["last_error"] = None
        next_run = datetime.now().astimezone() + timedelta(hours=interval_hours)
        state["next_run"] = next_run.isoformat(timespec="seconds")
        save_state(state)
        print("No waiting dashboard URLs. Auto-discovery is OFF.")
        print(f"Next scheduled check window: {state['next_run']}")
        return "idle"

    state["last_url"] = url
    state["last_error"] = None
    save_state(state)

    print("\n" + "=" * 68)
    print(f"AUTOPILOT START · {now_iso()}")
    print(f"Dashboard source: {url}")
    print("Download -> Edit 30/70 -> Caption/Tags -> Publish Instagram")
    print("=" * 68)

    try:
        results = process_url(
            url,
            publish_fb=publish_facebook,
            publish_ig=publish_instagram,
        )
    except Exception as exc:
        state = load_state()
        state["last_url"] = url
        state["last_error"] = str(exc)
        if looks_like_token_error(exc):
            retry_at = datetime.now().astimezone() + timedelta(minutes=TOKEN_RETRY_MINUTES)
            state["next_run"] = retry_at.isoformat(timespec="seconds")
            add_history(state, "token_error", url, error=exc)
            save_state(state)
            print("\nMETA TOKEN NEEDS REFRESH")
            print("Run: python3 meta_connect.py")
            print(f"AutoPilot will retry this same URL after {TOKEN_RETRY_MINUTES} minutes.")
            return "token_error"

        state.setdefault("failed", {})[url] = {
            "error": str(exc),
            "at": now_iso(),
            "skip": True,
        }
        state["next_run"] = datetime.now().astimezone().isoformat(timespec="seconds")
        add_history(state, "failed", url, error=exc)
        save_state(state)
        print(f"\nSKIPPING FAILED URL: {exc}")
        return "failed"

    state = load_state()
    state.setdefault("processed", {})[url] = "success"
    state.setdefault("failed", {}).pop(url, None)
    state["last_success"] = now_iso()
    state["last_error"] = None
    state["last_results"] = results
    next_run = datetime.now().astimezone() + timedelta(hours=interval_hours)
    state["next_run"] = next_run.isoformat(timespec="seconds")
    add_history(state, "success", url, results=results)
    save_state(state)

    print("\nAUTOPILOT POST SUCCESS")
    print(f"Next normal post window: {state['next_run']}")
    return "success"


def loop(interval_hours, publish_instagram=True, publish_facebook=True):
    lock = acquire_lock()
    print("Reaction Factory AutoPilot is ON")
    print("Source mode: private dashboard URLs only")
    print(f"Normal cadence: one Reel every {interval_hours:g} hours")
    print("Queue file: data/approved_urls.txt")
    print("Auto-discovery: OFF")
    print("Press Ctrl+C to stop AutoPilot.")

    try:
        while not STOP:
            state = load_state()
            target = parse_dt(state.get("next_run"))
            if target and target > datetime.now().astimezone():
                print(f"Waiting until {target.isoformat(timespec='minutes')} for the next post...")
                wait_until(target)
                if STOP:
                    break

            status = run_cycle(
                interval_hours,
                publish_instagram=publish_instagram,
                publish_facebook=publish_facebook,
            )
            if status == "idle":
                time.sleep(IDLE_POLL_SECONDS)
            elif status in {"failed", "token_error"}:
                time.sleep(2)
            else:
                time.sleep(2)
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        except Exception:
            pass


def handle_stop(signum, frame):
    global STOP
    STOP = True


def main():
    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    ap = argparse.ArgumentParser(description="Reaction Factory dashboard URL -> edit -> Instagram publishing autopilot")
    ap.add_argument("--interval-hours", type=float, default=DEFAULT_INTERVAL_HOURS)
    ap.add_argument("--instagram-only", action="store_true", help="Publish to Instagram only")
    ap.add_argument("--facebook-only", action="store_true", help="Publish to TVMind USA Page only")
    ap.add_argument("--once", action="store_true", help="Process at most one dashboard URL and exit")
    ap.add_argument("--status", action="store_true", help="Show queue/autopilot status and exit")
    args = ap.parse_args()

    if args.interval_hours <= 0:
        raise SystemExit("--interval-hours must be greater than 0")
    if args.instagram_only and args.facebook_only:
        raise SystemExit("Choose only one of --instagram-only or --facebook-only")

    state = load_state()
    if args.status:
        print_status(state)
        return

    publish_ig = not args.facebook_only
    publish_fb = not args.instagram_only

    if args.once:
        run_cycle(args.interval_hours, publish_instagram=publish_ig, publish_facebook=publish_fb)
        return

    loop(args.interval_hours, publish_instagram=publish_ig, publish_facebook=publish_fb)


if __name__ == "__main__":
    main()
