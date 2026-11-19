#!/usr/bin/env python3
"""Meta-specific guardrails for Instagram sales messaging.

This module intentionally keeps the first comment-to-DM private reply separate
from normal conversation replies. It also prevents ordinary outbound replies
when the lead has not messaged within the standard 24-hour response window.
"""
from datetime import datetime, timezone

import requests


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def install(agent):
    original_send = agent._send_instagram_message

    def send_private_reply(comment_id, text):
        ig_id = agent._ig_user_id()
        token, source = agent._messaging_token()
        if not ig_id:
            raise RuntimeError("INSTAGRAM_BUSINESS_ID or META_IG_USER_ID is not configured.")
        if not token:
            raise RuntimeError("No Instagram messaging access token is configured.")

        host = agent._graph_host(source)
        url = f"https://{host}/{agent._graph_version()}/{ig_id}/messages"
        body = {
            "recipient": {"comment_id": str(comment_id)},
            "message": {"text": str(text)[:1000]},
        }
        headers = {"Content-Type": "application/json"}
        params = {}
        if host == "graph.instagram.com":
            headers["Authorization"] = f"Bearer {token}"
        else:
            params["access_token"] = token

        response = requests.post(url, params=params, headers=headers, json=body, timeout=45)
        return agent._meta_error(response, "private reply")

    def guarded_send(recipient_id, text):
        # Normal outbound messages are only allowed after the Instagram user has
        # engaged. For leads already known to the store, additionally enforce a
        # conservative 24-hour standard reply window.
        lead = None
        with agent.STORE_LOCK:
            lead = agent._find_lead(agent._load_store(), scoped_id=recipient_id)
        if lead:
            last_in = _parse_time(lead.get("last_inbound_at"))
            if not last_in:
                raise RuntimeError("Cannot send a normal DM before this Instagram user replies.")
            now = datetime.now(timezone.utc)
            if last_in.tzinfo is None:
                last_in = last_in.replace(tzinfo=timezone.utc)
            if (now - last_in).total_seconds() > 24 * 60 * 60:
                raise RuntimeError(
                    "Instagram standard reply window has expired. Wait for the user to message again before sending."
                )
        return original_send(recipient_id, text)

    def corrected_comment_change(value):
        if not isinstance(value, dict):
            return
        comment_id = str(value.get("id") or value.get("comment_id") or "").strip()
        text = str(value.get("text") or "").strip()
        author = value.get("from") or value.get("user") or {}
        scoped_id = str(author.get("id") or value.get("from_id") or "").strip()
        username = str(author.get("username") or value.get("username") or "").strip()
        if not comment_id or not scoped_id or not agent._comment_matches(text):
            return

        lead = agent._upsert_lead(scoped_id, username=username, source="comment")
        if not agent._record_private_reply(lead["id"], comment_id):
            return

        reply = (
            "Thanks for commenting! We help businesses turn existing phone videos into edited Reels and can handle "
            "captions, titles, and Instagram/Facebook posting. The first month is $299 with no long-term contract. "
            "Reply here and tell me what kind of business you run."
        )
        try:
            send_private_reply(comment_id, reply)
            agent._append_message(
                lead["id"],
                "out",
                reply,
                {"automated": True, "private_reply": True, "comment_id": comment_id},
            )
        except Exception:
            # Undo the idempotency marker so a temporary Meta/API failure can be
            # retried on a later delivery without producing duplicate successes.
            with agent.STORE_LOCK:
                data = agent._load_store()
                stored = agent._find_lead(data, lead_id=lead["id"])
                if stored and comment_id in stored.get("private_reply_comment_ids", []):
                    stored["private_reply_comment_ids"].remove(comment_id)
                    agent._save_store(data)
            raise

    agent._send_instagram_message = guarded_send
    agent._process_comment_change = corrected_comment_change
    agent._send_instagram_private_reply = send_private_reply
