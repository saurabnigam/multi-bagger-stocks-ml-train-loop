-- Separate cache database. All price DDL belongs here, never inside PriceStore.
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS bm_symbols (
  security_id INTEGER PRIMARY KEY CHECK(security_id < 0), symbol TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS prices_daily (
  security_id INTEGER NOT NULL, date TEXT NOT NULL,
  open_raw REAL, high_raw REAL, low_raw REAL, close_raw REAL NOT NULL,
  volume_raw REAL, dividend_raw REAL NOT NULL, split_ratio REAL NOT NULL CHECK(split_ratio > 0),
  yahoo_close REAL, yahoo_adj_close REAL, observed_at TEXT NOT NULL,
  capture_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,
  PRIMARY KEY(security_id,date,observed_at)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_prices_date ON prices_daily(date);
CREATE TABLE IF NOT EXISTS prices_daily_quarantine (
  security_id INTEGER NOT NULL, date TEXT NOT NULL, observed_at TEXT NOT NULL,
  payload_json TEXT NOT NULL, reason TEXT NOT NULL, source_sha256 TEXT NOT NULL,
  PRIMARY KEY(security_id,date,observed_at)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS accepted_price_revisions (
  security_id INTEGER NOT NULL, date TEXT NOT NULL, observed_at TEXT NOT NULL,
  accepted_at TEXT NOT NULL, decision_id TEXT NOT NULL,
  PRIMARY KEY(security_id,date,observed_at)
);
