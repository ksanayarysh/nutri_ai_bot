# src/subscriptions.py
from __future__ import annotations

from datetime import datetime, timedelta

from src.config import TZ, TRIAL_DAYS
from src.db import db


# -------------------------
# helpers
# -------------------------

def _now() -> datetime:
    return datetime.now(TZ)


def _is_active(until: str | None) -> bool:
    if not until:
        return False
    if until == "forever":
        return True
    try:
        return datetime.fromisoformat(until) > _now()
    except Exception:
        return False


# -------------------------
# public API
# -------------------------

def ensure_trial_subscription(user_id: int) -> None:
    """
    Issues trial ONLY IF:
    - user exists
    - no active subscription
    - no existing trial

    Trial duration is taken from config.TRIAL_DAYS
    """
    with db() as conn:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT trial_until, subscribed_until
            FROM users
            WHERE user_id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return

        trial_until, subscribed_until = row

        # already subscribed -> no trial
        if _is_active(subscribed_until):
            return

        # already had a trial -> do not reissue
        if trial_until:
            return

        trial_end = _now() + timedelta(days=TRIAL_DAYS)

        cur.execute(
            """
            UPDATE users
            SET trial_until = %s
            WHERE user_id = %s
            """,
            (trial_end.isoformat(timespec="seconds"), user_id),
        )


def is_subscribed(user_id: int) -> bool:
    """
    True if user has:
    - active paid subscription
    OR
    - active trial
    """
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT trial_until, subscribed_until
            FROM users
            WHERE user_id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return False

        trial_until, subscribed_until = row

        return _is_active(subscribed_until) or _is_active(trial_until)


def grant_subscription(user_id: int, days: int | None) -> None:
    """
    Grants paid subscription.
    days = None -> forever
    """
    if days is None:
        until = "forever"
    else:
        until = (_now() + timedelta(days=days)).isoformat(timespec="seconds")

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET subscribed_until = %s
            WHERE user_id = %s
            """,
            (until, user_id),
        )


def revoke_subscription(user_id: int) -> None:
    """
    Explicitly removes paid subscription (trial is untouched).
    """
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET subscribed_until = NULL
            WHERE user_id = %s
            """,
            (user_id,),
        )
