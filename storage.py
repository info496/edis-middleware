import sqlite3
from pathlib import Path
from typing import Iterable, List, Dict

DB_PATH = Path("data.sqlite")

def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            pod TEXT,
            ts TEXT,
            value_kwh REAL,
            quality TEXT,
            PRIMARY KEY (pod, ts)
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            pod TEXT,
            date_from TEXT,
            date_to TEXT,
            created_at TEXT,
            PRIMARY KEY (pod, date_from, date_to)
        )
        """)

def upsert_readings(rows: Iterable[tuple]):
    with sqlite3.connect(DB_PATH) as con:
        con.executemany(
            "INSERT OR REPLACE INTO readings(pod, ts, value_kwh, quality) VALUES (?,?,?,?)",
            rows
        )

def select_readings(pod: str, date_from: str, date_to: str) -> List[Dict]:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("""
            SELECT ts, value_kwh, quality
            FROM readings
            WHERE pod=? AND ts >= ? AND ts < ?
            ORDER BY ts ASC
        """, (pod, date_from, date_to))
        return [{"ts": r[0], "kWh": r[1], "quality": r[2]} for r in cur.fetchall()]
