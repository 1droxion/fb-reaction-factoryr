#!/usr/bin/env python3
from pathlib import Path

from auto_pipeline import (
    add_source_disclosure,
    approved_urls,
    clean_caption_seed,
    download_url,
    require_explicit_approval,
    source_context_from_url,
)
from autopilot import load_state, now_iso, save_state
from facebook import publish_reel as publish_facebook
from instagram import publish_reel as publish_instagram
from metadata import generate_metadata
from prepare_reel import write_package
from reaction_factory import ffprobe_duration, make_reel, make_tvmind_reel
from youtube_short import publish_short

MIN_SOURCE_SECONDS = 4.0
MAX_SOURCE_SECONDS = 60.0


def normalize_options(raw):
    raw = raw if isinstance(raw, dict) else {}
    privacy = str(raw.get("youtube_privacy") or "public").strip().lower()
    if privacy not in {"public", "unlisted", "private"}:
        privacy = "public"
    return {
        "lane": str(raw.get("lane") or "").strip().lower(),
        "instagram": bool(raw.get("instagram", True)),
        "youtube": bool(raw.get("youtube", False)),
        "facebook": bool(raw.get("facebook", False)),
        "youtube_privacy": privacy,
    }


def is_personal_reaction_job(options):
    return (
        options.get("lane") == "personal"
        and not options.get("facebook")
        and bool(options.get("instagram") or options.get("youtube"))
    )


def next_cloud_url(state):
    preferred = str(state.get("last_url") or "").strip()
    processed = state.setdefault("processed", {})
    failed = state.setdefault("failed", {})
    if preferred and processed.get(preferred) != "success" and not failed.get(preferred, {}).get("skip"):
        return preferred
    for url in approved_urls():
        if processed.get(url) == "success":
            continue
        if failed.get(url, {}).get("skip"):
            continue
        return url
    return None


def _sync(progress_sync):
    if progress_sync:
        progress_sync()


def _progress(url, stage, detail, progress_sync=None, status="active", **extra):
    state = load_state()
    state["last_url"] = url
    payload = {"url": url, "stage": stage, "status": status, "detail": detail, "updated_at": now_iso()}
    payload.update({k: v for k, v in extra.items() if v is not None})
    state["current_progress"] = payload
    save_state(state)
    _sync(progress_sync)


def _destination_success(url, name, result, progress_sync=None):
    state = load_state()
    per_url = state.setdefault("destination_status", {}).setdefault(url, {})
    per_url[name] = {"status": "success", "at": now_iso(), "result": result or {}}
    save_state(state)
    _sync(progress_sync)


def _destination_failure(url, name, exc, progress_sync=None):
    state = load_state()
    per_url = state.setdefault("destination_status", {}).setdefault(url, {})
    per_url[name] = {"status": "failed", "at": now_iso(), "error": str(exc)}
    save_state(state)
    _sync(progress_sync)


def _already_posted(url, name):
    state = load_state()
    item = (state.get("destination_status", {}).get(url, {}) or {}).get(name, {}) or {}
    return item.get("status") == "success", item.get("result") or {}


def _history(state, status, url, options, results=None, error=None):
    item = {"at": now_iso(), "status": status, "url": url, "destinations": options}
    results = results or {}
    if results.get("instagram"):
        item["instagram_permalink"] = results["instagram"].get("permalink")
    if results.get("youtube"):
        item["youtube_url"] = results["youtube"].get("url")
        item["youtube_channel"] = results["youtube"].get("channel")
    if results.get("facebook"):
        item["facebook_video_id"] = results["facebook"].get("video_id")
    if error:
        item["error"] = str(error)
    history = state.setdefault("history", [])
    history.insert(0, item)
    del history[100:]


def process_cloud_job(url, options, progress_sync=None):
    options = normalize_options(options)
    if not any((options["instagram"], options["youtube"], options["facebook"])):
        raise RuntimeError("Choose at least one destination.")

    require_explicit_approval(url)
    personal_reaction = is_personal_reaction_job(options)
    reaction_needed = bool(options["instagram"] or options["youtube"])
    tvmind_edit_needed = bool(options["facebook"] and options.get("lane") in {"tvmind", "tvmind_direct"})

    _progress(url, "downloading", "Downloading source now...", progress_sync)
    source_context = source_context_from_url(url)
    source = Path(download_url(url))
    duration = ffprobe_duration(source)
    _progress(url, "downloaded", f"Download complete · {duration:.1f}s", progress_sync, status="done", duration_seconds=round(duration, 1))

    reaction_video = None
    tvmind_video = None
    metadata = None
    post_text = ""

    if reaction_needed:
        if duration < MIN_SOURCE_SECONDS:
            raise RuntimeError(f"Source is {duration:.1f}s. Instant reaction mode needs at least {MIN_SOURCE_SECONDS:.0f}s.")
        caption_seed = source_context or clean_caption_seed(source)
        edit_detail = "Creating 30/70 reaction edit with Droxion banner..."
        if duration > MAX_SOURCE_SECONDS:
            edit_detail = f"Creating 30/70 reaction edit from the first {MAX_SOURCE_SECONDS:.0f}s with Droxion banner..."
        _progress(url, "editing", edit_detail, progress_sync)
        reaction_video, reaction_used = make_reel(
            str(source), caption=caption_seed, reaction="auto", rights_ok=True,
            middle_banner=personal_reaction,
        )
        reaction_video = Path(reaction_video)
        _progress(url, "edited", "30/70 reaction edit ready.", progress_sync, status="done")
        _progress(url, "metadata", "Creating title, caption and tags...", progress_sync)
        metadata = generate_metadata(caption_seed)
        text_path, json_path, post_text = write_package(
            reaction_video,
            metadata,
            {
                "source_url": url,
                "source_duration_seconds": duration,
                "reaction_used": reaction_used,
                "target_reel_seconds": 60,
                "source_looped": False,
                "rights_gate": "approved_queue",
                "cloud_multi_platform": True,
                "personal_reaction": personal_reaction,
            },
        )
        post_text = add_source_disclosure(post_text, text_path, json_path, url)
        _progress(url, "metadata_done", "Title, caption and tags ready.", progress_sync, status="done", title=metadata.get("title"), hashtags=metadata.get("hashtags"))
    elif tvmind_edit_needed:
        _progress(url, "editing_tvmind", "Creating TV Mind edit: 33% Droxion promo + 67% original video...", progress_sync)
        tvmind_video = Path(make_tvmind_reel(str(source), rights_ok=True))
        _progress(url, "edited_tvmind", "TV Mind 33/67 edit ready.", progress_sync, status="done")
    else:
        _progress(url, "metadata_done", "Direct post uses the original video.", progress_sync, status="done")

    results = {"instagram": None, "youtube": None, "facebook": None}
    failures = {}

    if options["instagram"]:
        done, prior = _already_posted(url, "instagram")
        if done:
            results["instagram"] = prior
        else:
            _progress(url, "publishing_instagram", "Posting edited Reel to Instagram now...", progress_sync)
            try:
                result = publish_instagram(reaction_video, post_text)
                results["instagram"] = result
                _destination_success(url, "instagram", result, progress_sync)
            except Exception as exc:
                failures["Instagram"] = str(exc)
                _destination_failure(url, "instagram", exc, progress_sync)

    if options["youtube"]:
        done, prior = _already_posted(url, "youtube")
        if done:
            results["youtube"] = prior
        else:
            _progress(url, "publishing_youtube", "Publishing the same reaction short to YouTube Shorts...", progress_sync)
            try:
                hashtags = metadata.get("hashtags", []) if metadata else []
                result = publish_short(
                    reaction_video,
                    title=(metadata or {}).get("title") or "Reaction Short 😂",
                    description=((metadata or {}).get("description") or "") + "\n\n" + " ".join(hashtags),
                    tags=hashtags,
                    privacy=options["youtube_privacy"],
                    profile="personal",
                )
                results["youtube"] = result
                _destination_success(url, "youtube", result, progress_sync)
            except Exception as exc:
                failures["YouTube"] = str(exc)
                _destination_failure(url, "youtube", exc, progress_sync)

    if options["facebook"]:
        done, prior = _already_posted(url, "facebook")
        if done:
            results["facebook"] = prior
        else:
            _progress(url, "publishing_facebook", "Publishing TV Mind USA video to Facebook...", progress_sync)
            try:
                facebook_video = tvmind_video or source
                result = publish_facebook(facebook_video, "", profile="tvmind" if options.get("lane") in {"tvmind", "tvmind_direct"} else None)
                results["facebook"] = result
                _destination_success(url, "facebook", result, progress_sync)
            except Exception as exc:
                failures["Facebook"] = str(exc)
                _destination_failure(url, "facebook", exc, progress_sync)

    return results, failures


def run_cloud_cycle(progress_sync=None):
    state = load_state()
    url = next_cloud_url(state)
    if not url:
        return "idle"

    options = normalize_options((state.get("job_options", {}) or {}).get(url))
    state["last_url"] = url
    state["last_error"] = None
    state["next_run"] = None
    state["current_progress"] = {
        "url": url, "stage": "queued", "status": "done", "detail": "Queued for immediate cloud processing",
        "updated_at": now_iso(), "destinations": options,
    }
    save_state(state)
    _sync(progress_sync)

    try:
        results, failures = process_cloud_job(url, options, progress_sync)
    except Exception as exc:
        state = load_state()
        state["last_url"] = url
        state["last_error"] = str(exc)
        state.setdefault("failed", {})[url] = {"error": str(exc), "at": now_iso(), "skip": True}
        state["current_progress"] = {"url": url, "stage": "failed", "status": "error", "detail": str(exc), "updated_at": now_iso(), "destinations": options}
        _history(state, "failed", url, options, error=exc)
        save_state(state)
        _sync(progress_sync)
        return "failed"

    if failures:
        detail = "; ".join(f"{name}: {message}" for name, message in failures.items())
        state = load_state()
        state["last_url"] = url
        state["last_error"] = detail
        state.setdefault("failed", {})[url] = {"error": detail, "at": now_iso(), "skip": True}
        state["current_progress"] = {"url": url, "stage": "failed", "status": "error", "detail": detail, "updated_at": now_iso(), "destinations": options}
        _history(state, "failed", url, options, results=results, error=detail)
        save_state(state)
        _sync(progress_sync)
        return "failed"

    completed = []
    if options["instagram"]:
        completed.append("Instagram")
    if options["youtube"]:
        completed.append("YouTube Shorts")
    if options["facebook"]:
        completed.append("TV Mind USA")

    state = load_state()
    state.setdefault("processed", {})[url] = "success"
    state.setdefault("failed", {}).pop(url, None)
    state["last_success"] = now_iso()
    state["last_error"] = None
    state["last_results"] = results
    state["current_progress"] = {
        "url": url, "stage": "posted", "status": "done", "detail": "Posted now to: " + ", ".join(completed),
        "updated_at": now_iso(), "destinations": options,
        "instagram_permalink": (results.get("instagram") or {}).get("permalink"),
        "youtube_url": (results.get("youtube") or {}).get("url"),
        "youtube_channel": (results.get("youtube") or {}).get("channel"),
        "facebook_video_id": (results.get("facebook") or {}).get("video_id"),
    }
    _history(state, "success", url, options, results=results)
    save_state(state)
    _sync(progress_sync)
    return "success"
