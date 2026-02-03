from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, HTTPException

# IMPORTANT:
# This service must NOT depend on python-telegram-bot.
# src.payments is safe to import because Telegram deps are optional there.
from src.payments import handle_mercadopago_webhook  # type: ignore


app = FastAPI(title="nutri_webhooks")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/webhook/mercadopago")
async def mp_webhook(request: Request):
    """Mercado Pago webhook endpoint.

    MP can send notifications in different shapes:
    - Query params: ?data.id=123&type=payment
    - JSON body: {"type":"payment","data":{"id":"123"}}
    For safety, we accept both.

    We intentionally *don't* fail hard if the body is empty (Swagger "Try it out" sends empty body),
    because MP itself will also retry if we return non-2xx.
    """

    # Basic env check (so you don't forget it in Railway variables)
    if not os.getenv("MP_ACCESS_TOKEN"):
        raise HTTPException(status_code=500, detail="MP_ACCESS_TOKEN missing")

    # 1) Try read JSON body, but tolerate empty/invalid body
    body: Optional[Dict[str, Any]] = None
    try:
        raw = await request.body()
        if raw and raw.strip():
            body = await request.json()
    except Exception:
        body = None

    # 2) Collect query params (FastAPI gives them as strings)
    qp = dict(request.query_params)

    # Normalize into the shape expected by handle_mercadopago_webhook()
    payload: Dict[str, Any] = body or {}

    # MP uses data.id in query params
    data_id = qp.get("data.id") or qp.get("id") or (payload.get("data") or {}).get("id")
    notif_type = qp.get("type") or payload.get("type") or payload.get("topic")  # topic is older name

    if data_id and (not payload.get("data") or not isinstance(payload.get("data"), dict)):
        payload["data"] = {"id": data_id}
    elif data_id:
        payload["data"]["id"] = data_id  # type: ignore[index]

    if notif_type and not payload.get("type"):
        payload["type"] = notif_type

    # If we still have no id, accept but do nothing.
    if not data_id:
        return {"ok": True, "ignored": True, "reason": "missing data.id", "received": payload}

    # 3) Process: update payments + grant subscription if approved
    try:
        result = handle_mercadopago_webhook(payload)
    except Exception as e:
        # We return 200 to avoid endless retries while you debug.
        # Check Railway logs to see the exception.
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}", "received": payload}

    return {"ok": True, "result": result}
