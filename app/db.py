"""SQLite。单用户本机库，连接按请求打开，不在线程之间共享。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import DEFAULT_SETTINGS, SETTING_FIELDS
from app.timeutil import iso, now_local

SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (
  shop_id TEXT PRIMARY KEY,
  mall_id TEXT NOT NULL,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  shop_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  token TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  FOREIGN KEY (shop_id) REFERENCES shops(shop_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS login_tickets (
  ticket TEXT PRIMARY KEY,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
  campaign_id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  name TEXT NOT NULL,
  scene TEXT NOT NULL,
  status TEXT NOT NULL,
  daily_budget_fen INTEGER NOT NULL,
  bid_fen INTEGER,
  pace TEXT NOT NULL DEFAULT 'normal',
  updated_at TEXT NOT NULL,
  FOREIGN KEY (shop_id) REFERENCES shops(shop_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS campaign_daily (
  campaign_id TEXT NOT NULL,
  shop_id TEXT NOT NULL,
  stat_date TEXT NOT NULL,
  impressions INTEGER NOT NULL,
  clicks INTEGER NOT NULL,
  spend_fen INTEGER NOT NULL,
  orders INTEGER NOT NULL,
  gmv_fen INTEGER NOT NULL,
  add_to_cart INTEGER NOT NULL,
  synced_at TEXT NOT NULL,
  PRIMARY KEY (campaign_id, stat_date),
  FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS goods (
  goods_id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  campaign_id TEXT NOT NULL,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  price_fen INTEGER NOT NULL,
  stage TEXT NOT NULL,
  daily_budget_fen INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (shop_id) REFERENCES shops(shop_id) ON DELETE CASCADE,
  FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS goods_daily (
  goods_id TEXT NOT NULL,
  shop_id TEXT NOT NULL,
  stat_date TEXT NOT NULL,
  impressions INTEGER NOT NULL,
  clicks INTEGER NOT NULL,
  spend_fen INTEGER NOT NULL,
  orders INTEGER NOT NULL,
  gmv_fen INTEGER NOT NULL,
  add_to_cart INTEGER NOT NULL,
  synced_at TEXT NOT NULL,
  PRIMARY KEY (goods_id, stat_date),
  FOREIGN KEY (goods_id) REFERENCES goods(goods_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS image_tests (
  image_id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  goods_id TEXT NOT NULL,
  label TEXT NOT NULL,
  tone TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (shop_id) REFERENCES shops(shop_id) ON DELETE CASCADE,
  FOREIGN KEY (goods_id) REFERENCES goods(goods_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS image_daily (
  image_id TEXT NOT NULL,
  shop_id TEXT NOT NULL,
  stat_date TEXT NOT NULL,
  impressions INTEGER NOT NULL,
  clicks INTEGER NOT NULL,
  spend_fen INTEGER NOT NULL,
  orders INTEGER NOT NULL,
  gmv_fen INTEGER NOT NULL,
  synced_at TEXT NOT NULL,
  PRIMARY KEY (image_id, stat_date),
  FOREIGN KEY (image_id) REFERENCES image_tests(image_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS daily_summaries (
  shop_id TEXT NOT NULL,
  stat_date TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  PRIMARY KEY (shop_id, stat_date)
);

CREATE TABLE IF NOT EXISTS adjustment_items (
  shop_id TEXT NOT NULL,
  stat_date TEXT NOT NULL,
  item_key TEXT NOT NULL,
  priority TEXT NOT NULL,
  category TEXT NOT NULL,
  target_id TEXT NOT NULL,
  target_name TEXT NOT NULL,
  action TEXT NOT NULL,
  title TEXT NOT NULL,
  reason TEXT NOT NULL,
  suggestion TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  status TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (shop_id, stat_date, item_key)
);

CREATE TABLE IF NOT EXISTS alerts (
  shop_id TEXT NOT NULL,
  stat_date TEXT NOT NULL,
  code TEXT NOT NULL,
  level TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  read_at TEXT,
  PRIMARY KEY (shop_id, stat_date, code)
);

CREATE TABLE IF NOT EXISTS operation_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id TEXT,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_goods_daily_shop_date ON goods_daily(shop_id, stat_date);
CREATE INDEX IF NOT EXISTS idx_campaign_daily_shop_date ON campaign_daily(shop_id, stat_date);
CREATE INDEX IF NOT EXISTS idx_image_daily_shop_date ON image_daily(shop_id, stat_date);
"""

_SETTING_KEYS = {field["key"] for field in SETTING_FIELDS}


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
            (key, value),
        )
    conn.commit()


def load_setting_map(conn: sqlite3.Connection) -> dict[str, str]:
    stored = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in stored.items() if k in _SETTING_KEYS})
    return merged


def save_setting_map(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    with conn:
        for key, value in values.items():
            if key not in _SETTING_KEYS:
                continue
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


def log_op(conn: sqlite3.Connection, action: str, detail: str) -> None:
    started = not conn.in_transaction
    conn.execute(
        "INSERT INTO operation_logs(created_at, action, detail) VALUES(?, ?, ?)",
        (iso(now_local()), action, detail),
    )
    if started:
        conn.commit()
