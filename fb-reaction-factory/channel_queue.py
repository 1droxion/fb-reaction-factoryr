#!/usr/bin/env python3
import math
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from auto_pipeline import clean_caption_seed, download_url, require_explicit_approval, source_context_from_url
from autopilot import load_state, now_iso, save_state
from facebook import publish_reel as publish_facebook
from instagram import publish_reel as publish_instagram
from metadata import generate_metadata
from prepare_reel import write_package
from reaction_factory import ffprobe_duration, make_reel
from youtube_short import publish_short

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "data" / "channel_work"
WORK.mkdir(parents=True, exist_ok=True)

LANES = {
    "personal": {
        "label": "Personal",
        "clip_seconds": 60,
        "selection_ratio": 0.60,
        "instagram": True,
        "youtube": True,
        "facebook": False,
        "reaction": True,
        "youtube_profile": "personal",
        "facebook_profile": None,
        "made_for_kids": False,
    },
    "tvmind": {
        "label": "TV Mind",
        "clip_seconds": 27,
        "selection_ratio": 0.60,
        "instagram": False,
        "youtube": True,
        "facebook": True,
        "reaction": False,
        "youtube_profile": "tvmind",
        "facebook_profile": "tvmind",
        "made_for_kids": False,
    },
    "kids": {
        "label": "Kids",
        "clip_seconds": 27,
        "selection_ratio": 0.60,
        "instagram": False,
        "youtube": True,
        "facebook": True,
        "reaction": False,
        "youtube_profile": "kids",
        "facebook_profile": "kids",
        "made_for_kids": True,
    },
}

TIMEZONE = ZoneInfo(os.getenv("REACTION_FACTORY_TIMEZONE", "America/Chicago"))
DAILY_SLOTS = (9, 14, 20)


def is_channel_job(raw):
    lane = str((raw or {}).get("lane") or "").strip().lower()
    return lane in LANES


def normalize_lane_options(raw):
    raw = raw if isinstance(raw, dict) else {}
    lane = str(raw.get("lane") or "").strip().lower()
    if lane not in LANES:
        raise RuntimeError("Unknown channel lane.")
    cfg = dict(LANES[lane])
    privacy = str(raw.get("youtube_privacy") or "public").strip().lower()
    if privacy not in {"public", "unlisted", "private"}:
        privacy = "public"
    overlay_position = str(raw.get("overlay_position") or "none").strip().lower()
    if overlay_position not in {"none", "top", "middle", "bottom"}:
        overlay_position = "none"
    overlay_text = str(raw.get("overlay_text") or "").strip()[:80]
    cfg.update({
        "lane": lane,
        "youtube_privacy": privacy,
        "instagram": bool(raw.get("instagram", cfg["instagram"])),
        "youtube": bool(raw.get("youtube", cfg["youtube"])),
        "facebook": bool(raw.get("facebook", cfg["facebook"])),
        "overlay_position": overlay_position,
        "overlay_text": overlay_text,
    })
    return cfg


def _parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=TIMEZONE)
    except Exception:
        return None


def _now():
    return datetime.now(TIMEZONE)


def _next_slots(count, start=None):
    cursor = (start or _now()).astimezone(TIMEZONE)
    result = []
    day = cursor.date()
    while len(result) < count:
        for hour in DAILY_SLOTS:
            slot = datetime(day.year, day.month, day.day, hour, 0, tzinfo=TIMEZONE)
            if slot > cursor:
                result.append(slot)
                if len(result) >= count:
                    break
        day += timedelta(days=1)
    return result


def lane_job_ready(state, url, raw_options):
    if not is_channel_job(raw_options):
        return True
    plan = (state.get("clip_plans", {}) or {}).get(url)
    if not plan:
        return True
    clips = plan.get("clips") or []
    now = _now()
    for clip in clips:
        if clip.get("status") == "queued":
            when = _parse_time(clip.get("publish_at"))
            if when is None or when.astimezone(TIMEZONE) <= now:
                return True
    return False


def _save_progress(url, lane, stage, detail, progress_sync=None, status="active", **extra):
    state = load_state()
    payload = {
        "url": url,
        "lane": lane,
        "stage": stage,
        "status": status,
        "detail": detail,
        "updated_at": now_iso(),
    }
    payload.update({k: v for k, v in extra.items() if v is not None})
    state["last_url"] = url
    state["current_progress"] = payload
    save_state(state)
    if progress_sync:
        progress_sync()


def _ffmpeg():
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg is required for long-video clipping.")
    return exe


def _activity_score(source, start, duration, index):
    sample = max(3.0, min(12.0, float(duration)))
    cmd = [
        _ffmpeg(), "-hide_banner", "-nostats", "-ss", f"{float(start):.3f}",
        "-t", f"{sample:.3f}", "-i", str(source), "-vn",
        "-af", "volumedetect", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=45)
        text = proc.stderr or ""
        match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", text)
        if match:
            mean_db = float(match.group(1))
            loudness = max(0.0, min(100.0, 100.0 + (mean_db * 2.4)))
        else:
            loudness = 50.0
    except Exception:
        loudness = 50.0
    tie_break = ((index * 17) % 11) / 10.0
    return round(min(100.0, loudness + tie_break), 1)


def _candidate_segments(duration, clip_seconds):
    segments = []
    cursor = 0.0
    index = 1
    while cursor < duration - 3.0:
        length = min(float(clip_seconds), duration - cursor)
        if length < min(8.0, clip_seconds * 0.35):
            break
        segments.append({"index": index, "start": round(cursor, 3), "duration": round(length, 3)})
        cursor += float(clip_seconds)
        index += 1
    return segments


def _build_plan(url, cfg, source, duration, progress_sync=None):
    candidates = _candidate_segments(duration, cfg["clip_seconds"])
    if not candidates:
        raise RuntimeError("The source video is too short to create clips.")
    _save_progress(url, cfg["lane"], "analyzing", f"Analyzing {len(candidates)} possible clips and ranking the strongest moments...", progress_sync, total_candidates=len(candidates))
    for item in candidates:
        item["score"] = _activity_score(source, item["start"], item["duration"], item["index"])
    candidates.sort(key=lambda x: (x["score"], -x["index"]), reverse=True)
    select_count = max(1, int(math.ceil(len(candidates) * float(cfg["selection_ratio"]))))
    selected = candidates[:select_count]
    slots = _next_slots(len(selected))
    clips = []
    for rank, (item, slot) in enumerate(zip(selected, slots), start=1):
        clips.append({
            **item,
            "rank": rank,
            "status": "queued",
            "publish_at": slot.isoformat(),
            "posted_at": None,
            "views": 0,
            "results": {},
            "error": None,
        })
    state = load_state()
    plans = state.setdefault("clip_plans", {})
    plans[url] = {
        "lane": cfg["lane"],
        "label": cfg["label"],
        "source_url": url,
        "source_duration_seconds": round(duration, 1),
        "clip_seconds": cfg["clip_seconds"],
        "candidate_count": len(candidates),
        "selected_count": len(clips),
        "created_at": now_iso(),
        "status": "scheduled",
        "overlay_position": cfg.get("overlay_position", "none"),
        "overlay_text": cfg.get("overlay_text", ""),
        "clips": clips,
    }
    state.setdefault("processed", {}).pop(url, None)
    state.setdefault("failed", {}).pop(url, None)
    state["last_error"] = None
    state["current_progress"] = {
        "url": url,
        "lane": cfg["lane"],
        "stage": "scheduled",
        "status": "done",
        "detail": f"{len(clips)} best clips selected from {len(candidates)}. First post: {clips[0]['publish_at']}",
        "updated_at": now_iso(),
        "selected_count": len(clips),
        "candidate_count": len(candidates),
        "next_publish_at": clips[0]["publish_at"],
    }
    save_state(state)
    if progress_sync:
        progress_sync()
    return plans[url]


def _extract_clip(source, start, duration, lane, clip_index, vertical=False):
    target = WORK / f"{lane}_{clip_index}_{int(float(start))}.mp4"
    vf = None
    if vertical:
        vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30"
    cmd = [
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{float(start):.3f}", "-i", str(source), "-t", f"{float(duration):.3f}",
    ]
    if vf:
        cmd += ["-vf", vf]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(target),
    ]
    subprocess.run(cmd, check=True, timeout=900)
    if not target.exists() or target.stat().st_size <= 0:
        raise RuntimeError("Clip extraction failed.")
    return target


def _overlay_label(cfg, clip):
    raw = str(cfg.get("overlay_text") or "").strip()
    if not raw:
        return ""
    if raw == "__AUTO_PART__":
        return f"Part {clip.get('rank', 1)}"
    return raw


def _ffmpeg_escape_text(value):
    return str(value).replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'").replace("%", r"\%")


def _apply_overlay(video, cfg, clip):
    label = _overlay_label(cfg, clip)
    position = str(cfg.get("overlay_position") or "none").lower()
    if not label or position == "none":
        return Path(video)
    y_expr = {
        "top": "h*0.10",
        "middle": "(h-text_h)/2",
        "bottom": "h-text_h-h*0.10",
    }.get(position, "h*0.10")
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    target = WORK / f"overlay_{cfg['lane']}_{clip.get('index', 0)}.mp4"
    escaped = _ffmpeg_escape_text(label)
    draw = (
        f"drawtext=fontfile='{font}':text='{escaped}':fontcolor=white:fontsize=h*0.055:"
        f"borderw=3:bordercolor=black:x=(w-text_w)/2:y={y_expr}:"
        "box=1:boxcolor=black@0.38:boxborderw=18"
    )
    cmd = [
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
        "-vf", draw,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "copy", "-movflags", "+faststart", str(target),
    ]
    subprocess.run(cmd, check=True, timeout=900)
    if not target.exists() or target.stat().st_size <= 0:
        raise RuntimeError("Text overlay rendering failed.")
    return target


def _caption_seed(source, url, cfg, clip):
    base = source_context_from_url(url) or clean_caption_seed(source)
    if cfg["lane"] == "kids":
        return f"kid friendly fun {base}".strip()
    if cfg["lane"] == "tvmind":
        return f"TV moment {base}".strip()
    return f"funny reaction {base}".strip()


def _publish_clip(url, cfg, plan, clip, source, progress_sync=None):
    lane = cfg["lane"]
    _save_progress(url, lane, "clipping", f"Preparing {cfg['label']} clip #{clip['rank']}...", progress_sync, clip_rank=clip["rank"], clip_index=clip["index"])
    raw_clip = _extract_clip(source, clip["start"], clip["duration"], lane, clip["index"], vertical=not cfg["reaction"])
    publish_video = raw_clip
    overlay_video = None
    metadata = None
    post_text = ""
    try:
        seed = _caption_seed(source, url, cfg, clip)
        if cfg["reaction"]:
            _save_progress(url, lane, "editing", f"Creating reaction edit for clip #{clip['rank']}...", progress_sync)
            out, reaction_used = make_reel(str(raw_clip), caption=seed, reaction="auto", rights_ok=True)
            publish_video = Path(out)
            metadata = generate_metadata(seed)
            _, _, post_text = write_package(
                publish_video,
                metadata,
                {
                    "source_url": url,
                    "source_duration_seconds": plan.get("source_duration_seconds"),
                    "source_clip_start_seconds": clip["start"],
                    "source_clip_duration_seconds": clip["duration"],
                    "reaction_used": reaction_used,
                    "target_reel_seconds": cfg["clip_seconds"],
                    "lane": lane,
                    "rights_gate": "approved_queue",
                },
            )
        else:
            metadata = generate_metadata(seed)
            hashtags = " ".join(metadata.get("hashtags", []))
            post_text = f"{metadata.get('title','')}\n\n{metadata.get('description','')}\n\n{hashtags}".strip()

        if cfg.get("overlay_text") and cfg.get("overlay_position") != "none":
            _save_progress(url, lane, "overlay", f"Adding text to clip #{clip['rank']}...", progress_sync)
            overlay_video = _apply_overlay(publish_video, cfg, clip)
            publish_video = overlay_video

        results = {}
        failures = {}
        if cfg["instagram"]:
            _save_progress(url, lane, "publishing_instagram", f"Posting Personal clip #{clip['rank']} to Instagram...", progress_sync)
            try:
                results["instagram"] = publish_instagram(publish_video, post_text)
            except Exception as exc:
                failures["Instagram"] = str(exc)
        if cfg["youtube"]:
            _save_progress(url, lane, "publishing_youtube", f"Posting {cfg['label']} clip #{clip['rank']} to YouTube...", progress_sync)
            try:
                hashtags = metadata.get("hashtags", []) if metadata else []
                results["youtube"] = publish_short(
                    publish_video,
                    title=(metadata or {}).get("title") or f"{cfg['label']} Short",
                    description=((metadata or {}).get("description") or "") + "\n\n" + " ".join(hashtags),
                    tags=hashtags,
                    privacy=cfg["youtube_privacy"],
                    profile=cfg["youtube_profile"],
                    made_for_kids=cfg["made_for_kids"],
                )
            except Exception as exc:
                failures["YouTube"] = str(exc)
        if cfg["facebook"]:
            _save_progress(url, lane, "publishing_facebook", f"Posting {cfg['label']} clip #{clip['rank']} to Facebook...", progress_sync)
            try:
                results["facebook"] = publish_facebook(publish_video, post_text, profile=cfg["facebook_profile"])
            except Exception as exc:
                failures["Facebook"] = str(exc)
        if failures:
            raise RuntimeError("; ".join(f"{k}: {v}" for k, v in failures.items()))
        return results
    finally:
        try:
            raw_clip.unlink(missing_ok=True)
        except Exception:
            pass
        if overlay_video is not None:
            try:
                Path(overlay_video).unlink(missing_ok=True)
            except Exception:
                pass
        if publish_video != raw_clip and overlay_video is None:
            try:
                Path(publish_video).unlink(missing_ok=True)
            except Exception:
                pass


def _due_clip(plan):
    now = _now()
    queued = []
    for clip in plan.get("clips") or []:
        if clip.get("status") != "queued":
            continue
        when = _parse_time(clip.get("publish_at"))
        if when is None or when.astimezone(TIMEZONE) <= now:
            queued.append((when or now, clip))
    if not queued:
        return None
    queued.sort(key=lambda x: x[0])
    return queued[0][1]


def _update_plan_after_post(url, clip_index, results, progress_sync=None):
    state = load_state()
    plan = state.setdefault("clip_plans", {}).get(url) or {}
    target = None
    for clip in plan.get("clips") or []:
        if clip.get("index") == clip_index:
            target = clip
            break
    if target is None:
        raise RuntimeError("Clip plan changed while publishing.")
    target["status"] = "posted"
    target["posted_at"] = now_iso()
    target["results"] = results or {}
    target["error"] = None
    clips = plan.get("clips") or []
    posted = sum(1 for c in clips if c.get("status") == "posted")
    remaining = sum(1 for c in clips if c.get("status") == "queued")
    errors = sum(1 for c in clips if c.get("status") == "error")
    next_times = [c.get("publish_at") for c in clips if c.get("status") == "queued" and c.get("publish_at")]
    next_publish_at = min(next_times) if next_times else None
    if remaining == 0:
        plan["status"] = "complete" if errors == 0 else "needs_attention"
        if errors == 0:
            state.setdefault("processed", {})[url] = "success"
            state.setdefault("failed", {}).pop(url, None)
            state["last_success"] = now_iso()
    else:
        plan["status"] = "scheduled"
    result_links = results or {}
    history_item = {
        "at": now_iso(),
        "status": "success",
        "url": url,
        "lane": plan.get("lane"),
        "clip_index": target.get("index"),
        "clip_rank": target.get("rank"),
        "remaining": remaining,
        "instagram_permalink": (result_links.get("instagram") or {}).get("permalink"),
        "youtube_url": (result_links.get("youtube") or {}).get("url"),
        "youtube_video_id": (result_links.get("youtube") or {}).get("video_id"),
        "youtube_channel": (result_links.get("youtube") or {}).get("channel"),
        "facebook_video_id": (result_links.get("facebook") or {}).get("video_id"),
    }
    history = state.setdefault("history", [])
    history.insert(0, history_item)
    del history[100:]
    state["last_error"] = None
    state["current_progress"] = {
        "url": url,
        "lane": plan.get("lane"),
        "stage": "posted",
        "status": "done",
        "detail": f"Posted clip #{target.get('rank')} · {remaining} remaining",
        "updated_at": now_iso(),
        "posted": posted,
        "remaining": remaining,
        "selected": len(clips),
        "next_publish_at": next_publish_at,
    }
    save_state(state)
    if progress_sync:
        progress_sync()


def _mark_clip_error(url, clip_index, exc, progress_sync=None):
    state = load_state()
    plan = state.setdefault("clip_plans", {}).get(url) or {}
    for clip in plan.get("clips") or []:
        if clip.get("index") == clip_index:
            clip["status"] = "error"
            clip["error"] = str(exc)
            break
    plan["status"] = "needs_attention"
    state["last_error"] = str(exc)
    state["current_progress"] = {
        "url": url,
        "lane": plan.get("lane"),
        "stage": "failed",
        "status": "error",
        "detail": str(exc),
        "updated_at": now_iso(),
        "clip_index": clip_index,
    }
    history = state.setdefault("history", [])
    history.insert(0, {"at": now_iso(), "status": "failed", "url": url, "lane": plan.get("lane"), "clip_index": clip_index, "error": str(exc)})
    del history[100:]
    save_state(state)
    if progress_sync:
        progress_sync()


def run_channel_cycle(url, raw_options, progress_sync=None):
    cfg = normalize_lane_options(raw_options)
    require_explicit_approval(url)
    state = load_state()
    plan = (state.get("clip_plans", {}) or {}).get(url)
    if not plan:
        try:
            _save_progress(url, cfg["lane"], "downloading", f"Downloading {cfg['label']} source for clip analysis...", progress_sync)
            source = Path(download_url(url))
            duration = ffprobe_duration(source)
            _build_plan(url, cfg, source, duration, progress_sync)
            return "planned"
        except Exception as exc:
            state = load_state()
            state["last_error"] = str(exc)
            state.setdefault("failed", {})[url] = {"error": str(exc), "at": now_iso(), "skip": True}
            state["current_progress"] = {"url": url, "lane": cfg["lane"], "stage": "failed", "status": "error", "detail": str(exc), "updated_at": now_iso()}
            save_state(state)
            if progress_sync:
                progress_sync()
            return "failed"
    clip = _due_clip(plan)
    if not clip:
        return "waiting"
    try:
        state = load_state()
        live_plan = (state.get("clip_plans", {}) or {}).get(url) or plan
        for live_clip in live_plan.get("clips") or []:
            if live_clip.get("index") == clip.get("index"):
                live_clip["status"] = "processing"
                break
        save_state(state)
        if progress_sync:
            progress_sync()
        _save_progress(url, cfg["lane"], "downloading", f"Downloading source for scheduled clip #{clip['rank']}...", progress_sync)
        source = Path(download_url(url))
        results = _publish_clip(url, cfg, live_plan, clip, source, progress_sync)
        _update_plan_after_post(url, clip["index"], results, progress_sync)
        return "success"
    except Exception as exc:
        _mark_clip_error(url, clip["index"], exc, progress_sync)
        return "failed"
