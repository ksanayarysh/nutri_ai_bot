from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from src.db import db

from datetime import datetime, timezone
from typing import Any, Optional


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        # убедимся, что timezone-aware
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        # поддержка ISO вида ...Z
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def is_subscribed(sub: dict | None) -> bool:
    """
    sub ожидается примерно такой:
    {"status": "active", "expires_at": datetime|str|None}
    """
    if not sub:
        return False

    status = (sub.get("status") or "").lower()
    if status != "active":
        return False

    expires_at = _parse_dt(sub.get("expires_at"))

    if expires_at is None:
        # NULL = бессрочно
        return True

    now = datetime.now(timezone.utc)
    return expires_at > now



def get_subscription(user_id: int) -> Optional[Dict[str, Any]]:
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT status, plan, expires_at, created_at, updated_at
            FROM subscriptions
            WHERE user_id = %s
            ORDER BY updated_at DESC
            LIMIT 1
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
    sub = get_subscription(user_id)
    if not sub:
        return {"exists": False, "subscribed": False}

    return {
        "exists": True,
        "subscribed": is_subscribed(sub),
        "status": sub["status"],
        "plan": sub.get("plan"),
        "expires_at": sub.get("expires_at"),
        "created_at": sub.get("created_at"),
        "updated_at": sub.get("updated_at"),
    }

def is_subscribed_user(user_id: int) -> bool:
    sub = get_subscription(user_id)
    return is_subscribed(sub)

