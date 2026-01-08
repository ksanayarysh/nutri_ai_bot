# src/notifications.py
from __future__ import annotations

import re
from typing import List

from src.db import db

HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

DEFAULT_DAILY_TIME = "21:00"
DEFAULT_WEEKLY_TIME = "10:00"
DEFAULT_TZ = "America/Sao_Paulo"


def ensure_notify_tables() -> None:
    """Best-effort table creation. Safe to call on startup."""
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            '''
            CREATE TABLE IF NOT EXISTS notify_settings (
              user_id BIGINT PRIMARY KEY,
              daily_enabled BOOLEAN NOT NULL DEFAULT FALSE,
              daily_time TEXT NOT NULL DEFAULT '21:00',
              weekly_enabled BOOLEAN NOT NULL DEFAULT FALSE,
              weekly_time TEXT NOT NULL DEFAULT '10:00',
              timezone TEXT NOT NULL DEFAULT 'America/Sao_Paulo',
              created_at TIMESTAMP NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
            '''
        )
        cur.execute(
            '''
            CREATE TABLE IF NOT EXISTS jobs_log (
              job_name TEXT NOT NULL,
              user_id BIGINT NOT NULL,
              run_key TEXT NOT NULL,
              created_at TIMESTAMP NOT NULL DEFAULT NOW(),
              PRIMARY KEY (job_name, user_id, run_key)
            );
            '''
        )
        conn.commit()


def ensure_notify_settings(user_id: int) -> None:
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            '''
            INSERT INTO notify_settings (user_id, daily_enabled, daily_time, weekly_enabled, weekly_time, timezone, created_at, updated_at)
            VALUES (%s, FALSE, %s, FALSE, %s, %s, NOW(), NOW())
            ON CONFLICT (user_id) DO UPDATE SET updated_at = EXCLUDED.updated_at
            ''',
            (user_id, DEFAULT_DAILY_TIME, DEFAULT_WEEKLY_TIME, DEFAULT_TZ),
        )
        conn.commit()


def set_daily_enabled(user_id: int, enabled: bool) -> None:
    ensure_notify_settings(user_id)
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE notify_settings SET daily_enabled=%s, updated_at=NOW() WHERE user_id=%s",
            (enabled, user_id),
        )
        conn.commit()


def set_weekly_enabled(user_id: int, enabled: bool) -> None:
    ensure_notify_settings(user_id)
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE notify_settings SET weekly_enabled=%s, updated_at=NOW() WHERE user_id=%s",
            (enabled, user_id),
        )
        conn.commit()


def set_daily_time_hhmm(user_id: int, hhmm: str) -> None:
    hhmm = hhmm.strip()
    if not HHMM_RE.match(hhmm):
        raise ValueError("bad time")
    ensure_notify_settings(user_id)
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE notify_settings SET daily_time=%s, updated_at=NOW() WHERE user_id=%s",
            (hhmm, user_id),
        )
        conn.commit()


def set_weekly_time_hhmm(user_id: int, hhmm: str) -> None:
    hhmm = hhmm.strip()
    if not HHMM_RE.match(hhmm):
        raise ValueError("bad time")
    ensure_notify_settings(user_id)
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE notify_settings SET weekly_time=%s, updated_at=NOW() WHERE user_id=%s",
            (hhmm, user_id),
        )
        conn.commit()


def _claim_job(job_name: str, user_id: int, run_key: str) -> bool:
    """Returns True if this run_key wasn't sent before."""
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            '''
            INSERT INTO jobs_log (job_name, user_id, run_key, created_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT DO NOTHING
            ''',
            (job_name, user_id, run_key),
        )
        conn.commit()
        return cur.rowcount == 1


def list_daily_recipients(hhmm: str) -> List[int]:
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            '''
            SELECT user_id
            FROM notify_settings
            WHERE daily_enabled = TRUE AND daily_time = %s
            ''',
            (hhmm,),
        )
        return [int(r[0]) for r in cur.fetchall()]


def list_weekly_recipients(hhmm: str) -> List[int]:
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            '''
            SELECT user_id
            FROM notify_settings
            WHERE weekly_enabled = TRUE AND weekly_time = %s
            ''',
            (hhmm,),
        )
        return [int(r[0]) for r in cur.fetchall()]


def should_send_daily(job_name: str, user_id: int, day_iso: str) -> bool:
    return _claim_job(job_name, user_id, day_iso)


def should_send_weekly(job_name: str, user_id: int, week_key: str) -> bool:
    return _claim_job(job_name, user_id, week_key)
