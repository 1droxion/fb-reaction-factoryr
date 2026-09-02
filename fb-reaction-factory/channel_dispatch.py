#!/usr/bin/env python3
from auto_pipeline import approved_urls
from autopilot import load_state
from channel_queue import is_channel_job, lane_job_ready, run_channel_cycle


def is_instant_old_dashboard_job(raw):
    raw = raw if isinstance(raw, dict) else {}
    mode = str(raw.get("mode") or "").strip().lower()
    lane = str(raw.get("lane") or "").strip().lower()
    youtube = bool(raw.get("youtube", False))
    instagram = bool(raw.get("instagram", False))
    facebook = bool(raw.get("facebook", False))
    if mode == "instant":
        return True
    if youtube:
        return False
    if lane == "personal" and instagram and not facebook:
        return True
    if lane == "tvmind" and facebook and not instagram:
        return True
    return False


def run_channel_dispatch(progress_sync=None):
    state = load_state()
    processed = state.setdefault("processed", {})
    failed = state.setdefault("failed", {})
    job_options = state.get("job_options", {}) or {}
    preferred = str(state.get("last_url") or "").strip()

    # The latest instant dashboard request must always win over older
    # scheduled channel jobs. Returning no-channel here lets cloud_daemon
    # immediately hand the preferred URL to the old instant post pipeline.
    if preferred:
        preferred_raw = job_options.get(preferred) or {}
        if (
            is_instant_old_dashboard_job(preferred_raw)
            and processed.get(preferred) != "success"
            and not failed.get(preferred, {}).get("skip")
        ):
            return "no-channel"

    channel_urls = []
    for url in approved_urls():
        raw = job_options.get(url) or {}
        if is_instant_old_dashboard_job(raw):
            continue
        if not is_channel_job(raw):
            continue
        if processed.get(url) == "success" or failed.get(url, {}).get("skip"):
            continue
        channel_urls.append(url)

    if not channel_urls:
        return "no-channel"

    ordered = ([preferred] if preferred in channel_urls else []) + [u for u in channel_urls if u != preferred]
    for url in ordered:
        raw = job_options.get(url) or {}
        if lane_job_ready(state, url, raw):
            return run_channel_cycle(url, raw, progress_sync)

    return "waiting"
