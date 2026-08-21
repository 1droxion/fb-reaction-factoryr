#!/usr/bin/env python3
import os
import sys

import requests

PAGE_ID = os.getenv("META_PAGE_ID", "").strip()
VERSION = os.getenv("META_GRAPH_VERSION", "v26.0").strip()
USER_TOKEN = os.getenv("META_USER_ACCESS_TOKEN", "").strip()
GITHUB_ENV = os.getenv("GITHUB_ENV", "").strip()

REQUIRED_USER_PERMISSIONS = {
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "instagram_basic",
    "instagram_content_publish",
}
CONTENT_TASKS = {
    "CREATE_CONTENT",
    "MANAGE",
    "PROFILE_PLUS_CREATE_CONTENT",
    "PROFILE_PLUS_MANAGE",
    "PROFILE_PLUS_FULL_CONTROL",
}


def graph_get_path(path, token, **params):
    if not token:
        return None, "missing token"
    request_params = dict(params)
    request_params["access_token"] = token
    try:
        response = requests.get(
            f"https://graph.facebook.com/{VERSION}/{path.lstrip('/')}",
            params=request_params,
            timeout=30,
        )
    except requests.RequestException as exc:
        return None, f"network error: {exc}"

    try:
        data = response.json()
    except Exception:
        data = {}

    if not response.ok or (isinstance(data, dict) and "error" in data):
        error = data.get("error", {}) if isinstance(data, dict) else {}
        message = str(error.get("message") or response.text[:500] or f"HTTP {response.status_code}")
        code = error.get("code")
        subcode = error.get("error_subcode")
        detail = message
        if code is not None:
            detail += f" (code {code}"
            if subcode is not None:
                detail += f", subcode {subcode}"
            detail += ")"
        return None, detail

    return data, None


def write_github_env(values):
    if not GITHUB_ENV:
        raise RuntimeError("GITHUB_ENV is unavailable; cannot pass refreshed credentials to later steps.")
    with open(GITHUB_ENV, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            if value:
                # Prevent dynamically-derived Meta credentials from being echoed
                # by later GitHub Actions step environment summaries.
                print(f"::add-mask::{value}")
                handle.write(f"{key}={value}\n")


def granted_user_permissions():
    data, error = graph_get_path("me/permissions", USER_TOKEN)
    if not data:
        return set(), error
    granted = {
        str(item.get("permission") or "")
        for item in data.get("data", [])
        if item.get("status") == "granted"
    }
    return granted, None


def find_managed_page():
    data, error = graph_get_path(
        "me/accounts",
        USER_TOKEN,
        fields="id,name,access_token,tasks,instagram_business_account{id,username}",
        limit="100",
    )
    if not data:
        return None, error
    for page in data.get("data", []):
        if str(page.get("id") or "") == PAGE_ID:
            return page, None
    return None, "configured Page was not returned by /me/accounts"


def main():
    if not PAGE_ID:
        raise SystemExit("META_PAGE_ID is missing.")

    if not USER_TOKEN:
        raise SystemExit(
            "META_USER_ACCESS_TOKEN is missing. Generate a fresh Meta User access token with "
            "pages_show_list, pages_read_engagement, pages_manage_posts, instagram_basic and "
            "instagram_content_publish, then save it in GitHub Actions."
        )

    granted, permission_error = granted_user_permissions()
    if permission_error:
        raise SystemExit("Could not validate META_USER_ACCESS_TOKEN permissions: " + permission_error)

    missing = sorted(REQUIRED_USER_PERMISSIONS - granted)
    if missing:
        raise SystemExit(
            "META_USER_ACCESS_TOKEN is missing required permission(s): "
            + ", ".join(missing)
            + ". Generate a NEW User access token granting those permissions and replace the GitHub secret."
        )

    page, page_error = find_managed_page()
    if not page:
        raise SystemExit(
            "The Meta user token cannot manage the configured Facebook Page: "
            f"{page_error}. Make sure the Facebook account has Page access/full control and then generate a new token."
        )

    tasks = {str(task) for task in (page.get("tasks") or [])}
    if tasks and not (tasks & CONTENT_TASKS):
        raise SystemExit(
            "Your Facebook account can see the Page but does not have a content-publishing Page task. "
            "Give the account Facebook Page access/full control, then generate a new token. "
            f"Current Page tasks: {', '.join(sorted(tasks))}"
        )

    refreshed_page_token = str(page.get("access_token") or "").strip()
    if not refreshed_page_token:
        raise SystemExit("Meta returned the Page but no Page access token. Re-authorize the app.")

    page_check, page_check_error = graph_get_path(PAGE_ID, refreshed_page_token, fields="id,name")
    if not page_check or str(page_check.get("id") or "") != PAGE_ID:
        raise SystemExit(
            "The refreshed Page token could not access the configured Page: "
            f"{page_check_error or 'wrong Page returned'}"
        )

    ig = page.get("instagram_business_account") or {}
    ig_id = str(ig.get("id") or "").strip()
    ig_username = str(ig.get("username") or "").strip()
    if not ig_id:
        raise SystemExit(
            "No Instagram Professional account is linked to this Facebook Page. "
            "Connect the Instagram Business/Creator account to the Page first."
        )

    limit_data, limit_error = graph_get_path(
        f"{ig_id}/content_publishing_limit",
        USER_TOKEN,
        fields="config,quota_usage",
    )
    if not limit_data:
        raise SystemExit(
            "Instagram Content Publishing API access check failed: "
            + str(limit_error)
            + ". Confirm the Instagram account is Professional and the token has instagram_basic and instagram_content_publish."
        )

    values = {
        "META_PAGE_ACCESS_TOKEN": refreshed_page_token,
        "META_IG_USER_ID": ig_id,
    }
    if ig_username:
        values["META_IG_USERNAME"] = ig_username

    write_github_env(values)
    print("Meta preflight: Facebook publishing permission is valid.")
    print("Meta preflight: Instagram content publishing access is valid.")
    print("Meta preflight: refreshed runtime Page token from META_USER_ACCESS_TOKEN.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Meta preflight failed: {exc}", file=sys.stderr)
        raise
