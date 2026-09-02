import os
import time
import uuid
from pathlib import Path

import requests

from cloud_sync import cloud_url, headers as cloud_headers, upload_file


def _json(response, step="request"):
    try:
        data = response.json()
    except Exception:
        body = (response.text or "").strip()
        data = {"error": {"message": body[:500] or f"HTTP {response.status_code}"}}
    if not response.ok or "error" in data:
        error = data.get("error", {}) if isinstance(data, dict) else {}
        message = error.get("message") or f"HTTP {response.status_code}"
        code = error.get("code")
        subcode = error.get("error_subcode")
        detail = f"Instagram {step} error: {message}"
        if code is not None:
            detail += f" (code {code}"
            if subcode is not None:
                detail += f", subcode {subcode}"
            detail += ")"
        raise RuntimeError(detail)
    return data


def _resolve_page_token(page_id, token, version):
    if not page_id or not token:
        return ""
    response = requests.get(
        f"https://graph.facebook.com/{version}/{page_id}",
        params={"fields": "id,name,access_token", "access_token": token},
        timeout=60,
    )
    if not response.ok:
        return ""
    try:
        data = response.json()
    except Exception:
        return ""
    return str(data.get("access_token") or "").strip()


def _discover_instagram_account(page_id, tokens, version):
    if not page_id:
        return "", ""

    fields = "instagram_business_account{id,username},connected_instagram_account{id,username}"
    errors = []
    for label, token in tokens:
        if not token:
            continue
        try:
            response = requests.get(
                f"https://graph.facebook.com/{version}/{page_id}",
                params={"fields": fields, "access_token": token},
                timeout=60,
            )
            data = _json(response, f"account discovery ({label} token)")
            account = data.get("instagram_business_account") or data.get("connected_instagram_account") or {}
            ig_id = str(account.get("id") or "").strip()
            username = str(account.get("username") or "").strip()
            if ig_id:
                return ig_id, username
        except Exception as exc:
            errors.append(str(exc))

    detail = " | ".join(errors[-3:]) if errors else "No connected Instagram professional account was returned by Meta."
    raise RuntimeError(
        "Could not find the Instagram account connected to this Facebook Page. "
        "Make sure the Instagram professional account is linked to the Page. " + detail
    )


def _config():
    version = os.getenv("META_GRAPH_VERSION", "v26.0").strip() or "v26.0"
    page_id = os.getenv("META_PAGE_ID", "").strip()
    ig_user_id = os.getenv("META_IG_USER_ID", "").strip()
    page_token = os.getenv("META_PAGE_ACCESS_TOKEN", "").strip()
    user_token = os.getenv("META_USER_ACCESS_TOKEN", "").strip()
    system_token = os.getenv("META_SYSTEM_USER_ACCESS_TOKEN", "").strip()

    if not page_token and system_token:
        page_token = _resolve_page_token(page_id, system_token, version)

    # Prefer stable automation credentials. Keep the old user token only as a
    # last-resort fallback so a logged-out Facebook session cannot break posting.
    tokens = []
    seen = set()
    for label, token in (("page", page_token), ("system", system_token), ("user", user_token)):
        if token and token not in seen:
            tokens.append((label, token))
            seen.add(token)

    if not tokens:
        raise RuntimeError(
            "No Meta access token is available. Set META_SYSTEM_USER_ACCESS_TOKEN, "
            "META_PAGE_ACCESS_TOKEN, or META_USER_ACCESS_TOKEN first."
        )

    username = ""
    if not ig_user_id:
        ig_user_id, username = _discover_instagram_account(page_id, tokens, version)
        print(
            "Instagram account discovered automatically: "
            + (f"@{username} ({ig_user_id})" if username else ig_user_id)
        )

    return ig_user_id, tokens, version


def _wait_for_container(container_id, token, version, timeout_seconds):
    status_url = f"https://graph.facebook.com/{version}/{container_id}"
    deadline = time.time() + timeout_seconds
    last_status = None

    while time.time() < deadline:
        # Meta's Reels Publishing flow documents polling the container with
        # fields=status_code. Asking for unsupported fields such as status or
        # video_status can make Graph return code 100 / subcode 2207065.
        response = requests.get(
            status_url,
            params={
                "fields": "status_code",
                "access_token": token,
            },
            timeout=60,
        )
        data = _json(response, "container status")
        code = str(data.get("status_code") or "").upper()
        last_status = code or "UNKNOWN"
        if code == "FINISHED":
            return data
        if code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Instagram container failed: {last_status}")
        time.sleep(5)

    raise TimeoutError(f"Instagram processing timed out. Last status: {last_status}")


def _signed_cloud_video_url(video_path):
    remote_path = f"uploads/instagram_{int(time.time())}_{uuid.uuid4().hex[:8]}.mp4"
    upload_file(Path(video_path), remote_path)
    endpoint = cloud_url("signed-download", path=remote_path)
    response = requests.get(endpoint, headers=cloud_headers(), timeout=60)
    if response.status_code == 401:
        response = requests.get(endpoint, headers=cloud_headers(force_refresh=True), timeout=60)
    if not response.ok:
        raise RuntimeError(f"Could not create Instagram source URL: HTTP {response.status_code} {response.text[:300]}")
    data = response.json() or {}
    video_url = str(data.get("url") or "").strip()
    if not video_url:
        raise RuntimeError("Cloud gateway did not return a signed Instagram source URL.")
    return video_url


def _create_from_url(video_url, caption, ig_user_id, token, version, token_label):
    create = requests.post(
        f"https://graph.facebook.com/{version}/{ig_user_id}/media",
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "access_token": token,
        },
        timeout=60,
    )
    data = _json(create, f"container create ({token_label} token)")
    container_id = str(data.get("id") or "").strip()
    if not container_id:
        raise RuntimeError("Instagram did not return a media container ID.")
    return container_id


def _create_and_upload(video_path, caption, ig_user_id, token, version, token_label):
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
    create_data = _json(create, f"container create ({token_label} token)")
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
            },
            data=video_file,
            timeout=900,
        )
    _json(upload, f"video upload ({token_label} token)")
    return container_id


def publish_reel(video_path, caption, timeout_seconds=300):
    ig_user_id, candidates, version = _config()
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    if not video_path.is_file():
        raise RuntimeError(f"Not a video file: {video_path}")

    # Meta can fetch a public/signed video URL directly. This avoids the binary
    # resumable-upload HTTP 400 that the GitHub runner was hitting.
    signed_video_url = None
    try:
        signed_video_url = _signed_cloud_video_url(video_path)
        print("Instagram source staged to a temporary signed cloud URL.")
    except Exception as exc:
        print(f"Instagram cloud staging warning: {exc}")

    container_id = None
    active_token = None
    errors = []
    for token_label, token in candidates:
        try:
            if signed_video_url:
                container_id = _create_from_url(
                    signed_video_url, caption, ig_user_id, token, version, token_label
                )
            else:
                container_id = _create_and_upload(
                    video_path, caption, ig_user_id, token, version, token_label
                )
            active_token = token
            print(f"Instagram upload accepted using {token_label} token.")
            break
        except Exception as exc:
            errors.append(f"{token_label}: {exc}")
            print(f"Instagram attempt with {token_label} token failed: {exc}")

    if not container_id or not active_token:
        raise RuntimeError("Instagram upload failed. " + " | ".join(errors))

    _wait_for_container(container_id, active_token, version, timeout_seconds)

    publish_url = f"https://graph.facebook.com/{version}/{ig_user_id}/media_publish"
    published = requests.post(
        publish_url,
        data={
            "creation_id": container_id,
            "access_token": active_token,
        },
        timeout=60,
    )
    result = _json(published, "publish")
    media_id = result.get("id")

    permalink = None
    if media_id:
        try:
            link_response = requests.get(
                f"https://graph.facebook.com/{version}/{media_id}",
                params={"fields": "permalink", "access_token": active_token},
                timeout=60,
            )
            permalink = _json(link_response, "permalink").get("permalink")
        except Exception:
            permalink = None

    return {
        "container_id": container_id,
        "media_id": media_id,
        "permalink": permalink,
        "response": result,
    }
