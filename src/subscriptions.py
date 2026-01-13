from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from src.db import db


def get_subscription(user_id: int) -> Optional[Dict[str, Any]]:
    """Return subscription row for a user (or None).

    Expected semantics:
    - status == 'active' AND (expires_at is NULL OR expires_at > now) => subscribed
    - expires_at NULL means 'forever'
    """
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
    """True if the user currently has access."""
    sub = get_subscription(user_id)
    if not sub:
        return False
    if sub["status"] != "active":
        return False

    expires_at = sub["expires_at"]
    if expires_at is None:
        return True

    now = datetime.now(timezone.utc)
    # expires_at from Postgres is typically timezone-aware; comparing with UTC-aware now is OK
    return expires_at > now


def set_subscription(
    user_id: int,
    *,
    status: str,
    expires_at,
    plan: str | None = None,
) -> None:
    """Upsert a subscription row.

    Notes on "plan":
    - plan is informational (e.g. 'monthly', 'lifetime', 'trial', 'grant').
    - If plan is None, we keep the existing plan (so callers don't accidentally erase it).
    """
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


def grant_subscription(
    user_id: int,
    *,
    days: int | None = 30,
    plan: str = "monthly",
) -> None:
    """Grant access.

    - days=None => forever (expires_at NULL)
    - days=int => expires_at = now + days
    """
    if days is None:
        expires_at = None
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(days=int(days))

    set_subscription(user_id, status="active", plan=plan, expires_at=expires_at)


def revoke_subscription(user_id: int, *, plan: str | None = None) -> None:
    """Revoke access (sets status to 'inactive')."""
    # Keep expires_at as-is (history), but you can also force it to now if you prefer.
    sub = get_subscription(user_id)
    expires_at = sub["expires_at"] if sub else None
    set_subscription(user_id, status="inactive", plan=plan, expires_at=expires_at)


def subscription_status(user_id: int) -> Dict[str, Any]:
    """Convenience helper for /sub admin command outputs."""
    sub = get_subscription(user_id)
    if not sub:
        return {"exists": False, "subscribed": False}

    return {
        "exists": True,
        "subscribed": is_subscribed(user_id),
        "status": sub["status"],
        "plan": sub.get("plan"),
        "expires_at": sub.get("expires_at"),
        "created_at": sub.get("created_at"),
        "updated_at": sub.get("updated_at"),
    }
