#!/usr/bin/env python3
import hashlib
import hmac
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Blueprint, Response, jsonify, render_template_string, request

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LEADS_FILE = DATA_DIR / "instagram_sales_leads.json"
STORE_LOCK = threading.RLock()

sales_bp = Blueprint("instagram_sales", __name__)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _env(name, default=""):
    return str(os.getenv(name, default) or "").strip()


def _graph_version():
    return _env("INSTAGRAM_GRAPH_VERSION", _env("META_GRAPH_VERSION", "v26.0")) or "v26.0"


def _ig_user_id():
    return _env("INSTAGRAM_BUSINESS_ID", _env("META_IG_USER_ID"))


def _messaging_token():
    # Prefer a dedicated Instagram messaging token. The existing Facebook-login
    # credentials are fallbacks for installations that use graph.facebook.com.
    for key in (
        "INSTAGRAM_ACCESS_TOKEN",
        "META_PAGE_ACCESS_TOKEN",
        "META_SYSTEM_USER_ACCESS_TOKEN",
        "META_USER_ACCESS_TOKEN",
    ):
        value = _env(key)
        if value:
            return value, key
    return "", ""


def _graph_host(token_source=""):
    configured = _env("INSTAGRAM_GRAPH_HOST")
    if configured:
        return configured.replace("https://", "").strip("/")
    if token_source == "INSTAGRAM_ACCESS_TOKEN":
        return "graph.instagram.com"
    return "graph.facebook.com"


def _load_store():
    if not LEADS_FILE.exists():
        return {"leads": []}
    try:
        data = json.loads(LEADS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("leads"), list):
            return data
    except Exception:
        pass
    return {"leads": []}


def _save_store(data):
    tmp = LEADS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(LEADS_FILE)


def _lead_id(scoped_id):
    digest = hashlib.sha256(str(scoped_id).encode("utf-8")).hexdigest()[:16]
    return f"ig_{digest}"


def _find_lead(data, *, lead_id=None, scoped_id=None):
    for lead in data.get("leads", []):
        if lead_id and lead.get("id") == lead_id:
            return lead
        if scoped_id and str(lead.get("instagram_scoped_id")) == str(scoped_id):
            return lead
    return None


def _upsert_lead(scoped_id, username="", source="dm"):
    scoped_id = str(scoped_id or "").strip()
    if not scoped_id:
        raise ValueError("instagram_scoped_id is required")
    with STORE_LOCK:
        data = _load_store()
        lead = _find_lead(data, scoped_id=scoped_id)
        now = _now()
        if not lead:
            lead = {
                "id": _lead_id(scoped_id),
                "instagram_scoped_id": scoped_id,
                "username": str(username or "").strip(),
                "source": source,
                "status": "new",
                "human_takeover": False,
                "needs_human": False,
                "business_type": "",
                "first_seen_at": now,
                "last_seen_at": now,
                "last_inbound_at": "",
                "last_outbound_at": "",
                "messages": [],
                "private_reply_comment_ids": [],
            }
            data["leads"].append(lead)
        else:
            lead["last_seen_at"] = now
            if username and not lead.get("username"):
                lead["username"] = str(username).strip()
            if source and lead.get("source") == "dm":
                lead["source"] = source
        _save_store(data)
        return dict(lead)


def _append_message(lead_id, direction, text, metadata=None):
    text = str(text or "").strip()
    if not text:
        return None
    with STORE_LOCK:
        data = _load_store()
        lead = _find_lead(data, lead_id=lead_id)
        if not lead:
            return None
        now = _now()
        item = {
            "direction": direction,
            "text": text[:4000],
            "created_at": now,
            "metadata": metadata or {},
        }
        messages = lead.setdefault("messages", [])
        messages.append(item)
        # Keep enough context for sales handoff without letting a webhook grow
        # the local file without bound.
        if len(messages) > 80:
            del messages[:-80]
        lead["last_seen_at"] = now
        if direction == "in":
            lead["last_inbound_at"] = now
            if lead.get("status") == "new":
                lead["status"] = "engaged"
        else:
            lead["last_outbound_at"] = now
        _save_store(data)
        return dict(lead)


def _update_lead(lead_id, **changes):
    allowed = {"status", "human_takeover", "needs_human", "business_type", "username"}
    with STORE_LOCK:
        data = _load_store()
        lead = _find_lead(data, lead_id=lead_id)
        if not lead:
            return None
        for key, value in changes.items():
            if key in allowed:
                lead[key] = value
        lead["last_seen_at"] = _now()
        _save_store(data)
        return dict(lead)


def _all_leads():
    with STORE_LOCK:
        leads = list(_load_store().get("leads", []))
    return sorted(leads, key=lambda item: item.get("last_seen_at") or "", reverse=True)


def _record_private_reply(lead_id, comment_id):
    with STORE_LOCK:
        data = _load_store()
        lead = _find_lead(data, lead_id=lead_id)
        if not lead:
            return False
        ids = lead.setdefault("private_reply_comment_ids", [])
        if comment_id in ids:
            return False
        ids.append(comment_id)
        if len(ids) > 30:
            del ids[:-30]
        _save_store(data)
        return True


def _meta_error(response, action):
    try:
        data = response.json()
    except Exception:
        data = {}
    if response.ok and not (isinstance(data, dict) and data.get("error")):
        return data
    error = data.get("error", {}) if isinstance(data, dict) else {}
    message = error.get("message") or (response.text or "")[:500] or f"HTTP {response.status_code}"
    code = error.get("code")
    detail = f"Instagram {action} failed: {message}"
    if code is not None:
        detail += f" (code {code})"
    raise RuntimeError(detail)


def _send_instagram_message(recipient_id, text):
    ig_id = _ig_user_id()
    token, source = _messaging_token()
    if not ig_id:
        raise RuntimeError("INSTAGRAM_BUSINESS_ID or META_IG_USER_ID is not configured.")
    if not token:
        raise RuntimeError("No Instagram messaging access token is configured.")

    host = _graph_host(source)
    url = f"https://{host}/{_graph_version()}/{ig_id}/messages"
    body = {"recipient": {"id": str(recipient_id)}, "message": {"text": str(text)[:1000]}}
    headers = {"Content-Type": "application/json"}
    params = {}
    if host == "graph.instagram.com":
        headers["Authorization"] = f"Bearer {token}"
    else:
        params["access_token"] = token

    response = requests.post(url, params=params, headers=headers, json=body, timeout=45)
    return _meta_error(response, "send message")


def _keywords():
    raw = _env("INSTAGRAM_DM_KEYWORDS", "reels,grow,content,video")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _comment_matches(text):
    words = {part for part in re.findall(r"[a-z0-9]+", str(text or "").lower()) if part}
    return bool(words & _keywords())


def _hot_intent(text):
    lower = str(text or "").lower()
    phrases = (
        "ready to start",
        "how do i pay",
        "how can i pay",
        "send payment",
        "sign me up",
        "i want to start",
        "let's start",
        "lets start",
        "book a call",
        "call me",
        "i'm interested",
        "im interested",
    )
    return any(phrase in lower for phrase in phrases)


def _not_interested(text):
    lower = str(text or "").strip().lower()
    return lower in {"stop", "unsubscribe", "no thanks", "not interested", "leave me alone"}


def _fallback_reply(text):
    lower = str(text or "").lower()
    if any(word in lower for word in ("price", "cost", "how much", "$")):
        return (
            "The first month is $299 with no long-term contract. We can handle the editing, captions, "
            "titles, and Instagram/Facebook posting from your existing videos. What kind of business do you run?"
        )
    if _hot_intent(text):
        return (
            "Great — I can help get this set up. What kind of business do you run, and about how many videos "
            "do you already have each month?"
        )
    return (
        "We help businesses turn their existing phone videos into edited Reels and handle the posting to "
        "Instagram and Facebook. The first month is $299 with no long-term contract. What kind of business do you run?"
    )


def _extract_output_text(data):
    if isinstance(data, dict) and isinstance(data.get("output_text"), str):
        return data["output_text"].strip()
    chunks = []
    for item in (data.get("output") or []) if isinstance(data, dict) else []:
        for content in item.get("content", []) if isinstance(item, dict) else []:
            if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                text = content.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "\n".join(chunks).strip()


def _ai_reply(lead, inbound_text):
    key = _env("OPENAI_API_KEY")
    if not key:
        return _fallback_reply(inbound_text)

    model = _env("OPENAI_DM_MODEL", _env("OPENAI_MODEL", "gpt-5")) or "gpt-5"
    offer = _env("REACTION_FACTORY_OFFER_PRICE", "$299/month")
    history = []
    for msg in (lead.get("messages") or [])[-12:]:
        role = "Customer" if msg.get("direction") == "in" else "Assistant"
        history.append(f"{role}: {msg.get('text', '')}")
    transcript = "\n".join(history)

    system = f"""You are the automated Instagram sales assistant for Reaction Factory.
Reaction Factory helps small businesses turn their existing phone videos into professional short-form Reels and can handle captions, titles, editing, and Instagram/Facebook posting.
Current introductory offer: {offer}, no long-term contract.
Goal: understand the business, answer questions clearly, and move qualified prospects toward a human handoff.
Rules:
- Keep replies concise and conversational, normally 1-3 short sentences.
- Ask at most one useful question per reply.
- Never promise guaranteed followers, revenue, views, or business results.
- Never ask for passwords, access tokens, card numbers, bank details, SSNs, or other secrets in Instagram chat.
- Do not pressure someone who says no, stop, unsubscribe, or not interested.
- Do not claim to be human. If asked, say you are Reaction Factory's automated assistant and can bring in Dhruv.
- If the person is ready to buy, wants payment instructions, asks for a call, or has a custom deal question, tell them you will bring in Dhruv and keep the reply brief.
"""
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system}]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"Conversation so far:\n{transcript}\n\nLatest customer message: {inbound_text}\nWrite the next reply only.",
                    }
                ],
            },
        ],
        "max_output_tokens": 180,
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=45,
        )
        data = _meta_error(response, "AI reply") if response.status_code >= 400 else response.json()
        text = _extract_output_text(data)
        return text[:1000] if text else _fallback_reply(inbound_text)
    except Exception as exc:
        print(f"Instagram sales AI fallback: {exc}")
        return _fallback_reply(inbound_text)


def _verify_signature(raw_body):
    secret = _env("META_APP_SECRET")
    allow_unsigned = _env("INSTAGRAM_WEBHOOK_ALLOW_UNSIGNED", "0").lower() in {"1", "true", "yes", "on"}
    if not secret:
        return allow_unsigned
    received = request.headers.get("X-Hub-Signature-256", "")
    if not received.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)


def _process_dm_event(event):
    sender = event.get("sender") or {}
    message = event.get("message") or {}
    if message.get("is_echo"):
        return
    sender_id = str(sender.get("id") or "").strip()
    text = str(message.get("text") or "").strip()
    if not sender_id or not text:
        return

    lead = _upsert_lead(sender_id, username=sender.get("username") or "", source="dm")
    lead = _append_message(lead["id"], "in", text, {"mid": message.get("mid")}) or lead

    if _not_interested(text):
        _update_lead(lead["id"], status="not_interested", human_takeover=True, needs_human=False)
        return

    if _hot_intent(text):
        lead = _update_lead(lead["id"], status="hot", needs_human=True) or lead

    if lead.get("human_takeover"):
        return

    reply = _ai_reply(lead, text)
    if not reply:
        return
    _send_instagram_message(sender_id, reply)
    _append_message(lead["id"], "out", reply, {"automated": True})


def _process_comment_change(value):
    if not isinstance(value, dict):
        return
    comment_id = str(value.get("id") or value.get("comment_id") or "").strip()
    text = str(value.get("text") or "").strip()
    author = value.get("from") or value.get("user") or {}
    scoped_id = str(author.get("id") or value.get("from_id") or "").strip()
    username = str(author.get("username") or value.get("username") or "").strip()
    if not comment_id or not scoped_id or not _comment_matches(text):
        return

    lead = _upsert_lead(scoped_id, username=username, source="comment")
    if not _record_private_reply(lead["id"], comment_id):
        return

    reply = (
        "Thanks for commenting! We help businesses turn existing phone videos into edited Reels and can handle "
        "captions, titles, and Instagram/Facebook posting. The first month is $299 with no long-term contract. "
        "Reply here and tell me what kind of business you run."
    )
    try:
        # Meta private replies use the comment ID as recipient.id. Only one
        # private reply is sent for a given comment; normal follow-ups wait for
        # the recipient to respond.
        _send_instagram_message(comment_id, reply)
        _append_message(lead["id"], "out", reply, {"automated": True, "private_reply": True, "comment_id": comment_id})
    except Exception:
        # Let a future webhook retry if Meta rejected the private reply.
        with STORE_LOCK:
            data = _load_store()
            stored = _find_lead(data, lead_id=lead["id"])
            if stored and comment_id in stored.get("private_reply_comment_ids", []):
                stored["private_reply_comment_ids"].remove(comment_id)
                _save_store(data)
        raise


def _process_webhook(payload):
    if not isinstance(payload, dict):
        return
    for entry in payload.get("entry", []) or []:
        for event in entry.get("messaging", []) or []:
            try:
                _process_dm_event(event)
            except Exception as exc:
                print(f"Instagram DM event error: {exc}")
        for change in entry.get("changes", []) or []:
            field = str(change.get("field") or "").lower()
            if field in {"comments", "comment"}:
                try:
                    _process_comment_change(change.get("value") or {})
                except Exception as exc:
                    print(f"Instagram comment event error: {exc}")


@sales_bp.get("/instagram/webhook")
def instagram_webhook_verify():
    verify_token = _env("INSTAGRAM_WEBHOOK_VERIFY_TOKEN")
    mode = request.args.get("hub.mode", "")
    supplied = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    if verify_token and mode == "subscribe" and hmac.compare_digest(supplied, verify_token):
        return Response(challenge, status=200, content_type="text/plain; charset=utf-8")
    return Response("Webhook verification failed", status=403, content_type="text/plain; charset=utf-8")


@sales_bp.post("/instagram/webhook")
def instagram_webhook_receive():
    raw = request.get_data(cache=True)
    if not _verify_signature(raw):
        return jsonify({"error": "invalid webhook signature"}), 401
    payload = request.get_json(silent=True) or {}
    # Return quickly enough for Meta while processing only small text events in
    # this single-worker MVP. The handler never initiates bulk/cold messages.
    _process_webhook(payload)
    return jsonify({"ok": True})


SALES_HTML = r'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reaction Factory · Instagram Sales</title>
<style>
:root{--bg:#070a0f;--panel:#101722;--line:#263142;--text:#f6f8fb;--muted:#8d99aa;--accent:#6d5dfc;--good:#2dd881;--hot:#ffb020;--bad:#ff5d73}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Arial,sans-serif}.wrap{max-width:1180px;margin:auto;padding:30px 18px}.top{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}.brand{font-size:28px;font-weight:900}.sub{color:var(--muted);margin-top:5px}.link,button{border:0;border-radius:10px;background:#1a2331;color:#fff;padding:10px 13px;text-decoration:none;font-weight:800;cursor:pointer}.primary{background:var(--accent)}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0}.stat,.lead{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:15px}.stat b{font-size:24px;display:block}.stat span{color:var(--muted);font-size:12px}.leads{display:grid;gap:12px}.lead.hot{border-color:var(--hot)}.head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}.name{font-weight:900;font-size:17px}.meta{color:var(--muted);font-size:12px;margin-top:4px}.badge{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:5px 8px;font-size:11px;margin-left:5px}.badge.hot{border-color:var(--hot);color:#ffd58a}.badge.customer{border-color:var(--good);color:#92f0bd}.messages{margin-top:12px;background:#090e15;border:1px solid var(--line);border-radius:12px;padding:10px;max-height:260px;overflow:auto}.msg{padding:8px 10px;border-radius:10px;margin:6px 0;max-width:86%;font-size:13px;line-height:1.4}.in{background:#18202c}.out{background:#26205a;margin-left:auto}.controls{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}.send{display:flex;gap:7px;margin-top:10px}.send input{flex:1;background:#070b11;border:1px solid var(--line);border-radius:10px;color:white;padding:11px}.empty{color:var(--muted);padding:30px;text-align:center;border:1px dashed var(--line);border-radius:14px}@media(max-width:760px){.stats{grid-template-columns:1fr 1fr}.send{flex-direction:column}.send input,.send button{width:100%}}
</style></head><body><div class="wrap"><div class="top"><div><div class="brand">Instagram Sales Agent</div><div class="sub">Opt-in DMs · comment-to-DM · AI qualification · human takeover</div></div><div><a class="link" href="/">Reaction Factory</a> <button class="primary" onclick="load()">Refresh</button></div></div><div id="stats" class="stats"></div><div id="leads" class="leads"></div></div>
<script>
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
async function api(url,opts={}){const r=await fetch(url,{headers:{'Content-Type':'application/json'},...opts});const d=await r.json();if(!r.ok)throw new Error(d.error||'Request failed');return d}
async function action(id,name){try{await api('/api/sales/leads/'+id+'/'+name,{method:'POST',body:'{}'});await load()}catch(e){alert(e.message)}}
async function send(id){const el=document.getElementById('send-'+id);const text=el.value.trim();if(!text)return;try{await api('/api/sales/leads/'+id+'/send',{method:'POST',body:JSON.stringify({text})});el.value='';await load()}catch(e){alert(e.message)}}
function renderLead(l){const msgs=(l.messages||[]).slice(-14).map(m=>`<div class="msg ${m.direction==='in'?'in':'out'}"><b>${m.direction==='in'?'Lead':'Us'}:</b> ${esc(m.text)}</div>`).join('');const who=l.username?'@'+esc(l.username):esc(l.instagram_scoped_id);return `<div class="lead ${l.status==='hot'?'hot':''}"><div class="head"><div><div class="name">${who}${l.needs_human?'<span class="badge hot">HOT · NEEDS YOU</span>':''}<span class="badge ${esc(l.status)}">${esc(l.status)}</span></div><div class="meta">Source: ${esc(l.source)} · Last activity: ${esc(l.last_seen_at||'')}</div></div><div class="controls">${l.human_takeover?`<button onclick="action('${l.id}','release')">Resume AI</button>`:`<button onclick="action('${l.id}','takeover')">Take over</button>`}<button onclick="action('${l.id}','customer')">Mark customer</button></div></div><div class="messages">${msgs||'<div class="meta">No messages stored yet.</div>'}</div><div class="send"><input id="send-${l.id}" placeholder="Send a human reply…" onkeydown="if(event.key==='Enter')send('${l.id}')"><button class="primary" onclick="send('${l.id}')">Send</button></div></div>`}
async function load(){try{const d=await api('/api/sales/leads');const a=d.leads||[];const hot=a.filter(x=>x.status==='hot'||x.needs_human).length,customers=a.filter(x=>x.status==='customer').length,engaged=a.filter(x=>['engaged','hot','customer'].includes(x.status)).length;document.getElementById('stats').innerHTML=`<div class="stat"><b>${a.length}</b><span>Total leads</span></div><div class="stat"><b>${engaged}</b><span>Engaged</span></div><div class="stat"><b>${hot}</b><span>Hot / needs you</span></div><div class="stat"><b>${customers}</b><span>Customers</span></div>`;document.getElementById('leads').innerHTML=a.length?a.map(renderLead).join(''):'<div class="empty">No leads yet. Incoming DMs and matching Reel comments will appear here.</div>'}catch(e){document.getElementById('leads').innerHTML='<div class="empty">'+esc(e.message)+'</div>'}}
load();setInterval(load,15000);
</script></body></html>'''


@sales_bp.get("/sales/")
def sales_dashboard():
    return render_template_string(SALES_HTML)


@sales_bp.get("/api/sales/leads")
def api_sales_leads():
    return jsonify({"leads": _all_leads()})


@sales_bp.post("/api/sales/leads/<lead_id>/takeover")
def api_takeover(lead_id):
    lead = _update_lead(lead_id, human_takeover=True, needs_human=False)
    if not lead:
        return jsonify({"error": "Lead not found"}), 404
    return jsonify({"ok": True, "lead": lead})


@sales_bp.post("/api/sales/leads/<lead_id>/release")
def api_release(lead_id):
    lead = _update_lead(lead_id, human_takeover=False, needs_human=False)
    if not lead:
        return jsonify({"error": "Lead not found"}), 404
    return jsonify({"ok": True, "lead": lead})


@sales_bp.post("/api/sales/leads/<lead_id>/customer")
def api_customer(lead_id):
    lead = _update_lead(lead_id, status="customer", human_takeover=True, needs_human=False)
    if not lead:
        return jsonify({"error": "Lead not found"}), 404
    return jsonify({"ok": True, "lead": lead})


@sales_bp.post("/api/sales/leads/<lead_id>/send")
def api_manual_send(lead_id):
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Message is required"}), 400
    if len(text) > 1000:
        return jsonify({"error": "Message is too long"}), 400
    with STORE_LOCK:
        lead = _find_lead(_load_store(), lead_id=lead_id)
    if not lead:
        return jsonify({"error": "Lead not found"}), 404
    _send_instagram_message(lead["instagram_scoped_id"], text)
    _append_message(lead_id, "out", text, {"automated": False})
    _update_lead(lead_id, human_takeover=True, needs_human=False)
    return jsonify({"ok": True})


@sales_bp.get("/api/sales/config")
def api_sales_config():
    token, source = _messaging_token()
    return jsonify({
        "instagram_user_id_configured": bool(_ig_user_id()),
        "messaging_token_configured": bool(token),
        "token_source": source,
        "webhook_verify_token_configured": bool(_env("INSTAGRAM_WEBHOOK_VERIFY_TOKEN")),
        "app_secret_configured": bool(_env("META_APP_SECRET")),
        "graph_host": _graph_host(source),
        "graph_version": _graph_version(),
        "keywords": sorted(_keywords()),
    })
