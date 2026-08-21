#!/usr/bin/env python3
import os
import sys

import requests

PAGE_ID = os.getenv("META_PAGE_ID", "").strip()
VERSION = os.getenv("META_GRAPH_VERSION", "v26.0").strip()
PAGE_TOKEN = os.getenv("META_PAGE_ACCESS_TOKEN", "").strip()
USER_TOKEN = os.getenv("META_USER_ACCESS_TOKEN", "").strip()
GITHUB_ENV = os.getenv("GITHUB_ENV", "").strip()


def graph_get(token, fields):
    if not token:
        return None, "missing token"
    try:
        response = requests.get(
            f"https://graph.facebook.com/{VERSION}/{PAGE_ID}",
            params={"fields": fields, "access_token": token},
            timeout=30,
        )
    except requests.RequestException as exc:
        return None, f"network error: {exc}"

    try:
        data = response.json()
    except Exception:
        data = {}

    if not response.ok or "error" in data:
        error = data.get("error", {}) if isinstance(data, dict) else {}
        message = str(error.get("message") or f"HTTP {response.status_code}")
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
                handle.write(f"{key}={value}\n")


def main():
    if not PAGE_ID:
        raise SystemExit("META_PAGE_ID is missing.")

    if PAGE_TOKEN:
        page, page_error = graph_get(PAGE_TOKEN, "id,name")
        if page and str(page.get("id") or "") == PAGE_ID:
            print("Meta preflight: Page access token is valid.")
            return
        print(f"Meta preflight: stored Page token is not usable: {page_error or 'wrong Page'}")
    else:
        print("Meta preflight: META_PAGE_ACCESS_TOKEN is missing.")

    if not USER_TOKEN:
        raise SystemExit(
            "Meta credentials need refresh. Add a fresh GitHub Actions secret named "
            "META_USER_ACCESS_TOKEN, then run the workflow again."
        )

    page, user_error = graph_get(
        USER_TOKEN,
        "id,name,access_token,instagram_business_account{id,username}",
    )
    if not page or str(page.get("id") or "") != PAGE_ID:
        raise SystemExit(
            "Meta User token cannot access the configured Facebook Page: "
            f"{user_error or 'wrong Page returned'}. Refresh META_USER_ACCESS_TOKEN."
        )

    refreshed_page_token = str(page.get("access_token") or "").strip()
    if not refreshed_page_token:
        raise SystemExit(
            "Meta User token reached the Page, but Meta did not return a Page access token. "
            "Make sure it has pages_show_list, pages_read_engagement and pages_manage_posts."
        )

    values = {"META_PAGE_ACCESS_TOKEN": refreshed_page_token}
    ig = page.get("instagram_business_account") or {}
    ig_id = str(ig.get("id") or "").strip()
    ig_username = str(ig.get("username") or "").strip()
    if ig_id:
        values["META_IG_USER_ID"] = ig_id
    if ig_username:
        values["META_IG_USERNAME"] = ig_username

    write_github_env(values)
    print("Meta preflight: refreshed the runtime Page token from META_USER_ACCESS_TOKEN.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Meta preflight failed: {exc}", file=sys.stderr)
        raise
