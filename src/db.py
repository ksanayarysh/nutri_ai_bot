# src/db.py
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

import psycopg2

from src.config import DATABASE_URL, TZ


# -------------------------
# time helpers
# -------------------------

def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now(TZ).date().isoformat()


# -------------------------
# DB connection (Postgres only)
# -------------------------

@contextmanager
def db() -> Iterator:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required (Postgres only)")

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# -------------------------
# schema init
# -------------------------

def init_db() -> None:
    with db() as conn:
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TEXT,
            trial_until TEXT,
            subscribed_until TEXT
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            day TEXT,
            meal_type TEXT,
            text TEXT,
            calories REAL,
            protein REAL,
            fat REAL,
            carbs REAL,
            net_carbs REAL,
            created_at TEXT
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS payment_requests (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            status TEXT,
            created_at TEXT,
            proof_file_id TEXT,
            proof_text TEXT
        );
        """)
