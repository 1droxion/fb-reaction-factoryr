#!/usr/bin/env python3
import os
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"


def load_env_file():
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()


def get_json(url, token, params=None):
    query = dict(params or {})
    query["access_token"] = token
    response = requests.get(url, params=query, timeout=60)
    try:
        data = response.json()
    except Exception:
        data = {"raw": (response.text or "")[:300]}
    return response.status_code, data


def main():
    load_env_file()
    version = os.getenv("META_GRAPH_VERSION", "v26.0").strip() or "v26.0"
    page_id = os.getenv("META_PAGE_ID", "").strip()
    token = (
        os.getenv("META_SYSTEM_USER_ACCESS_TOKEN", "").strip()
        or os.getenv("META_PAGE_ACCESS_TOKEN", "").strip()
        or os.getenv("META_USER_ACCESS_TOKEN", "").strip()
    )

    print("META DOCTOR")
    print("===========")
    print(f"Graph version: {version}")
    print(f"Page ID set: {'YES' if page_id else 'NO'}")
    print(f"Token set: {'YES' if token else 'NO'}")
    if not page_id or not token:
        print("RESULT: Missing META_PAGE_ID or Meta token in .env")
        return

    base = f"https://graph.facebook.com/{version}"

    status, me = get_json(f"{base}/me", token, {"fields": "id,name"})
    if status == 200 and isinstance(me, dict) and not me.get("error"):
        print(f"Token identity works: YES ({me.get('name') or me.get('id')})")
    else:
        print("Token identity works: NO")
        print(f"Identity error: {me.get('error', {}).get('message') if isinstance(me, dict) else me}")

    status, perms = get_json(f"{base}/me/permissions", token)
    wanted = {"pages_manage_posts", "pages_read_engagement", "pages_show_list"}
    granted = set()
    declined = set()
    if status == 200 and isinstance(perms, dict):
        for item in perms.get("data", []) or []:
            name = str(item.get("permission") or "")
            state = str(item.get("status") or "")
            if state == "granted":
                granted.add(name)
            elif name:
                declined.add(name)
    print("Required scopes:")
    for scope in sorted(wanted):
        if scope in granted:
            state = "GRANTED"
        elif scope in declined:
            state = "NOT GRANTED"
        else:
            state = "NOT PRESENT"
        print(f"  {scope}: {state}")

    status, page = get_json(
        f"{base}/{page_id}",
        token,
        {"fields": "id,name,access_token"},
    )
    page_token = ""
    page_name = ""
    if status == 200 and isinstance(page, dict) and not page.get("error"):
        page_name = str(page.get("name") or "")
        page_token = str(page.get("access_token") or "").strip()
        print(f"Direct Page lookup: YES ({page_name or page_id})")
        print(f"Direct Page token returned: {'YES' if page_token else 'NO'}")
    else:
        print("Direct Page lookup: NO")
        if isinstance(page, dict):
            print(f"Page lookup error: {page.get('error', {}).get('message')}")

    status, accounts = get_json(
        f"{base}/me/accounts",
        token,
        {"fields": "id,name,tasks,access_token", "limit": 100},
    )
    matched = None
    if status == 200 and isinstance(accounts, dict):
        for item in accounts.get("data", []) or []:
            if str(item.get("id") or "") == page_id:
                matched = item
                break
    print(f"TVMind USA found in /me/accounts: {'YES' if matched else 'NO'}")
    if matched:
        tasks = [str(x) for x in (matched.get("tasks") or [])]
        print("Page tasks: " + (", ".join(tasks) if tasks else "NONE"))
        if not page_token:
            page_token = str(matched.get("access_token") or "").strip()
        print(f"Page token returned by /me/accounts: {'YES' if matched.get('access_token') else 'NO'}")

    print("-----------")
    if wanted.issubset(granted) and page_token:
        print("RESULT: AUTH READY")
        print("The token has all required scopes and Meta returned a Page access token.")
    elif not wanted.issubset(granted):
        missing = sorted(wanted - granted)
        print("RESULT: TOKEN SCOPE PROBLEM")
        print("Missing from the actual token: " + ", ".join(missing))
    elif not page_token:
        print("RESULT: PAGE TOKEN / ASSET ASSIGNMENT PROBLEM")
        print("The scopes are present, but Meta did not return a Page access token for this Page.")
    else:
        print("RESULT: META AUTH NEEDS REVIEW")


if __name__ == "__main__":
    main()
