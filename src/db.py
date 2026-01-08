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

        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name  TEXT;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_until  TEXT;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscribed_until  TEXT;")


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

        cur.execute("""
            CREATE TABLE IF NOT EXISTS targets (
              user_id BIGINT PRIMARY KEY,
              calories REAL,
              protein REAL,
              fat REAL,
              carbs REAL,
              net_carbs REAL,
              mode TEXT,
              updated_at TEXT
            );
        """)




def ensure_user(user_id: int, username: str | None, first_name: str | None) -> None:
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO users (user_id, created_at, username, first_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
              username = EXCLUDED.username,
              first_name = EXCLUDED.first_name
            """,
            (user_id, now_iso(), username, first_name),
        )

def get_targets(uid: int) -> dict | None:
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT kcal, protein, fat, carbs, net_carbs, diet, updated_at
            FROM targets
            WHERE user_id = %s
            """,
            (uid,),
        )
        row = cur.fetchone()
        if not row:
            return None

        calories, protein, fat, carbs, net_carbs, mode, updated_at = row
        return {
            "calories": float(calories) if calories is not None else None,
            "protein": float(protein) if protein is not None else None,
            "fat": float(fat) if fat is not None else None,
            "carbs": float(carbs) if carbs is not None else None,
            "net_carbs": float(net_carbs) if net_carbs is not None else None,
            "mode": mode,
            "updated_at": updated_at,
        }
