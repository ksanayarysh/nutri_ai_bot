from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from src.db import db


def get_subscription(user_id: int) -> Optional[Dict[str, Any]]:
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT status, plan, expires_at, created_at, updated_at
            FROM subscriptions
            WHERE user_id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        status, plan, expires_at, created_at, updated_at = row
        return {
            "status": status,
            "plan": plan,
            "expires_at": expires_at,
            "created_at": created_at,
            "updated_at": updated_at,
        }


def is_subscribed(user_id: int) -> bool:
    sub = get_subscription(user_id)
    if not sub:
        return False
    if sub["status"] != "active":
        return False

    expires_at = sub["expires_at"]
    if expires_at is None:
        return True

    now = datetime.now(timezone.utc)
    # expires_at из Postgres обычно timezone-aware, это ок
    return expires_at > now


def set_subscription(user_id: int, *, status: str, plan: str | None, expires_at) -> None:
    now = datetime.now(timezone.utc)
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO subscriptions (user_id, status, plan, expires_at, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
              status = EXCLUDED.status,
              plan = COALESCE(EXCLUDED.plan, subscriptions.plan),
              expires_at = EXCLUDED.expires_at,
              updated_at = EXCLUDED.updated_at
            """,
            (user_id, status, plan, expires_at, now, now),
        )
        conn.commit()
