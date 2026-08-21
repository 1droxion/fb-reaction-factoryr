import os
from pathlib import Path

import requests


def _config():
    page_id = os.getenv("META_PAGE_ID", "").strip()
    token = os.getenv("META_PAGE_ACCESS_TOKEN", "").strip()
    version = os.getenv("META_GRAPH_VERSION", "v26.0").strip()
    if not page_id or not token:
        raise RuntimeError("Set META_PAGE_ID and META_PAGE_ACCESS_TOKEN first.")
    return page_id, token, version


def _meta_json(response, stage):
    try:
        data = response.json()
    except Exception:
        data = {}

    if response.ok and not (isinstance(data, dict) and data.get("error")):
        return data

    error = data.get("error", {}) if isinstance(data, dict) else {}
    message = error.get("message") or response.text[:500] or f"HTTP {response.status_code}"
    code = error.get("code")
    subcode = error.get("error_subcode")
    trace_id = error.get("fbtrace_id")

    detail = f"{stage} failed: {message}"
    if code is not None:
        detail += f" (code {code}"
        if subcode is not None:
            detail += f", subcode {subcode}"
        detail += ")"
    if trace_id:
        detail += f" [fbtrace_id {trace_id}]"
    raise RuntimeError(detail)


def publish_reel(video_path, description, state="PUBLISHED"):
    page_id, token, version = _config()
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    if not video_path.is_file():
        raise RuntimeError(f"Not a video file: {video_path}")

    # Meta Page Reels Publishing flow: start -> upload -> finish.
    # A Page access token makes /me resolve to the Page.
    start_url = f"https://graph.facebook.com/{version}/me/video_reels"
    start = requests.post(
        start_url,
        params={
            "upload_phase": "start",
            "access_token": token,
        },
        timeout=60,
    )
    start_data = _meta_json(start, "Facebook Reel start")
    video_id = str(start_data.get("video_id") or "").strip()
    if not video_id:
        raise RuntimeError(f"Facebook Reel start did not return video_id: {start_data}")

    upload_url = (
        start_data.get("upload_url")
        or f"https://rupload.facebook.com/video-upload/{version}/{video_id}"
    )
    size = video_path.stat().st_size
    with video_path.open("rb") as video_file:
        upload = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(size),
                "Content-Type": "application/octet-stream",
            },
            data=video_file,
            timeout=900,
        )
    _meta_json(upload, "Facebook Reel upload")

    # Keep the finish request to Meta's documented Reel parameters only.
    finish = requests.post(
        start_url,
        params={
            "upload_phase": "finish",
            "video_state": state,
            "video_id": video_id,
            "access_token": token,
            "description": description,
        },
        timeout=60,
    )
    finish_data = _meta_json(finish, "Facebook Reel finish")
    return {"video_id": video_id, "response": finish_data}
