#!/usr/bin/env python3
from auto_pipeline import approved_urls
from autopilot import load_state
from channel_queue import is_channel_job, lane_job_ready, run_channel_cycle


def run_channel_dispatch(progress_sync=None):
    state = load_state()
    processed = state.setdefault("processed", {})
    failed = state.setdefault("failed", {})
    job_options = state.get("job_options", {}) or {}

    channel_urls = []
    for url in approved_urls():
        raw = job_options.get(url) or {}
        if not is_channel_job(raw):
            continue
        if processed.get(url) == "success" or failed.get(url, {}).get("skip"):
            continue
        channel_urls.append(url)

    if not channel_urls:
        return "no-channel"

    preferred = str(state.get("last_url") or "").strip()
    ordered = ([preferred] if preferred in channel_urls else []) + [u for u in channel_urls if u != preferred]
    for url in ordered:
        raw = job_options.get(url) or {}
        if lane_job_ready(state, url, raw):
            return run_channel_cycle(url, raw, progress_sync)

    return "waiting"
