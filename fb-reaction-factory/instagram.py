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


def _wait_for_container(container_id, token, version, timeout_seconds):
    status_url = f"https://graph.facebook.com/{version}/{container_id}"
    deadline = time.time() + timeout_seconds
    last_status = None

    while time.time() < deadline:
        response = requests.get(
            status_url,
            params={
                "fields": "id,status,status_code,video_status",
                "access_token": token,
            },
            timeout=60,
        )
        data = _json(response)
        code = str(data.get("status_code") or "").upper()
        last_status = data.get("status") or data.get("video_status") or code
        if code == "FINISHED":
            return data
        if code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Instagram container failed: {last_status}")
        time.sleep(5)

    raise TimeoutError(f"Instagram processing timed out. Last status: {last_status}")


def publish_reel(video_path, caption, timeout_seconds=300):
    """Upload a local Reel file to Instagram and publish it."""
    ig_user_id, token, version = _config()
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    if not video_path.is_file():
        raise RuntimeError(f"Not a video file: {video_path}")

    create_url = f"https://graph.facebook.com/{version}/{ig_user_id}/media"
    create = requests.post(
        create_url,
        data={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "access_token": token,
        },
        timeout=60,
    )
    create_data = _json(create)
    container_id = create_data.get("id")
    upload_uri = create_data.get("uri")
    if not container_id or not upload_uri:
        raise RuntimeError("Instagram did not return a resumable upload container/URI.")

    size = video_path.stat().st_size
    with video_path.open("rb") as video_file:
        upload = requests.post(
            upload_uri,
            headers={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(size),
                "Content-Type": "application/octet-stream",
            },
            data=video_file,
            timeout=900,
        )
    _json(upload)

    _wait_for_container(container_id, token, version, timeout_seconds)

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
    media_id = result.get("id")

    permalink = None
    if media_id:
        try:
            link_response = requests.get(
                f"https://graph.facebook.com/{version}/{media_id}",
                params={"fields": "permalink", "access_token": token},
                timeout=60,
            )
            permalink = _json(link_response).get("permalink")
        except Exception:
            permalink = None

    return {
        "container_id": container_id,
        "media_id": media_id,
        "permalink": permalink,
        "response": result,
    }
