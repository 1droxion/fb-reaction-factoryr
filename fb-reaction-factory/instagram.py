import os
import time
from pathlib import Path

import requests


def _config():
    ig_user_id = os.getenv("META_IG_USER_ID", "").strip()
    token = os.getenv("META_PAGE_ACCESS_TOKEN", "").strip()
    version = os.getenv("META_GRAPH_VERSION", "v26.0").strip()
    if not ig_user_id or not token:
        raise RuntimeError("Set META_IG_USER_ID and META_PAGE_ACCESS_TOKEN first.")
    return ig_user_id, token, version


def _json(response):
    try:
        data = response.json()
    except Exception:
        data = {"error": {"message": response.text[:500]}}
    if not response.ok or "error" in data:
        error = data.get("error", {})
        message = error.get("message") or f"HTTP {response.status_code}"
        code = error.get("code")
        detail = f"Instagram API error: {message}"
        if code is not None:
            detail += f" (code {code})"
        raise RuntimeError(detail)
    return data


def publish_reel(video_url, caption, share_to_feed=True, timeout_seconds=300):
    ig_user_id, token, version = _config()
    if not video_url.startswith(("http://", "https://")):
        raise ValueError("Instagram requires a public http/https video URL.")

    create_url = f"https://graph.facebook.com/{version}/{ig_user_id}/media"
    create = requests.post(
        create_url,
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true" if share_to_feed else "false",
            "access_token": token,
        },
        timeout=60,
    )
    container_id = _json(create)["id"]

    status_url = f"https://graph.facebook.com/{version}/{container_id}"
    deadline = time.time() + timeout_seconds
    last_status = None
    while time.time() < deadline:
        status = requests.get(
            status_url,
            params={
                "fields": "status_code,status",
                "access_token": token,
            },
            timeout=60,
        )
        data = _json(status)
        code = str(data.get("status_code") or "").upper()
        last_status = data.get("status") or code
        if code == "FINISHED":
            break
        if code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Instagram container failed: {last_status}")
        time.sleep(5)
    else:
        raise TimeoutError(f"Instagram processing timed out. Last status: {last_status}")

    publish_url = f"https://graph.facebook.com/{version}/{ig_user_id}/media_publish"
    published = requests.post(
        publish_url,
        data={
            "creation_id": container_id,
            "access_token": token,
        },
        timeout=60,
    )
    result = _json(published)
    return {
        "container_id": container_id,
        "media_id": result.get("id"),
        "response": result,
    }
