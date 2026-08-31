import os
from pathlib import Path

import requests


def _config():
    page_id = os.getenv("META_PAGE_ID", "").strip()
    token = (
        os.getenv("META_SYSTEM_USER_ACCESS_TOKEN", "").strip()
        or os.getenv("META_PAGE_ACCESS_TOKEN", "").strip()
    )
    version = os.getenv("META_GRAPH_VERSION", "v26.0").strip()
    if not page_id or not token:
        raise RuntimeError(
            "Set META_PAGE_ID and META_SYSTEM_USER_ACCESS_TOKEN (or META_PAGE_ACCESS_TOKEN) first."
        )
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


def _resolve_page_token(page_id, token, version):
    """Return a Page access token when given either a Page token or System User token."""
    endpoint = f"https://graph.facebook.com/{version}/{page_id}"

    # Preferred path for a permanent Business System User token.
    response = requests.get(
        endpoint,
        params={
            "fields": "id,name,access_token",
            "access_token": token,
        },
        timeout=60,
    )
    if response.ok:
        try:
            data = response.json()
        except Exception:
            data = {}
        page_token = str(data.get("access_token") or "").strip() if isinstance(data, dict) else ""
        if page_token:
            return page_token

    # Compatibility fallback: System User /me/accounts can expose assigned Pages
    # and their Page-scoped access tokens.
    accounts = requests.get(
        f"https://graph.facebook.com/{version}/me/accounts",
        params={
            "fields": "id,name,access_token,tasks",
            "limit": 100,
            "access_token": token,
        },
        timeout=60,
    )
    if accounts.ok:
        try:
            payload = accounts.json()
        except Exception:
            payload = {}
        for page in payload.get("data", []) if isinstance(payload, dict) else []:
            if str(page.get("id") or "") == str(page_id):
                page_token = str(page.get("access_token") or "").strip()
                if page_token:
                    return page_token

    # If the supplied credential already is a Page token, use it directly.
    # If it is a misconfigured System User token, Meta's publish error below will
    # still provide the authoritative permission error.
    return token


def publish_reel(video_path, description, state="PUBLISHED"):
    page_id, supplied_token, version = _config()
    token = _resolve_page_token(page_id, supplied_token, version)

    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    if not video_path.is_file():
        raise RuntimeError(f"Not a video file: {video_path}")

    start_url = f"https://graph.facebook.com/{version}/{page_id}/video_reels"
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
