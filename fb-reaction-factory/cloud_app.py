#!/usr/bin/env python3
import hmac
import os
import threading

from flask import Response, jsonify, request

from dashboard import app, load_env_file
from gaming_dashboard_saved import gaming_bp
from instagram_sales_agent import sales_bp

load_env_file()
app.register_blueprint(gaming_bp)
app.register_blueprint(sales_bp)


def _authorized():
    expected_user = os.getenv("DASHBOARD_USER", "dhruv").strip() or "dhruv"
    expected_password = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if not expected_password:
        return False

    auth = request.authorization
    if not auth:
        return False

    return hmac.compare_digest(auth.username or "", expected_user) and hmac.compare_digest(
        auth.password or "", expected_password
    )


@app.before_request
def cloud_auth():
    # Meta must be able to reach the webhook verification and event endpoints
    # without the private Reaction Factory dashboard Basic Auth challenge.
    # POST authenticity is checked separately with X-Hub-Signature-256.
    if request.path in {"/healthz", "/instagram/webhook"}:
        return None

    if not os.getenv("DASHBOARD_PASSWORD", "").strip():
        return Response(
            "DASHBOARD_PASSWORD is not configured. Refusing public access.",
            status=503,
            content_type="text/plain; charset=utf-8",
        )

    if not _authorized():
        return Response(
            "Authentication required.",
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="Reaction Factory"'},
            content_type="text/plain; charset=utf-8",
        )
    return None


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "reaction-factory"})


def _restore_assets():
    if os.getenv("AUTO_RESTORE_REACTIONS", "1").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    try:
        from restore_reactions import main as restore_reactions

        restore_reactions()
    except Exception as exc:
        print(f"Cloud reaction restore skipped/failed: {exc}")


threading.Thread(target=_restore_assets, daemon=True).start()
