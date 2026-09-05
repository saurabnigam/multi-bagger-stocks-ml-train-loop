import sqlite3
from config import DB_PATH

# Columns added after the original schema. ensure_schema() applies them
# idempotently so old databases keep working.
MIGRATIONS = [
    # (table, column, DDL)
    ('daily_predictions', 'base_score', 'ALTER TABLE daily_predictions ADD COLUMN base_score REAL'),
    ('active_weights', 'trained_through', 'ALTER TABLE active_weights ADD COLUMN trained_through TEXT'),
    ('active_weights', 'note', 'ALTER TABLE active_weights ADD COLUMN note TEXT'),
]


def _columns(cursor, table):
    return {row[1] for row in cursor.execute(f'PRAGMA table_info({table})')}


def ensure_schema(conn):
    """Create tables if missing and apply column migrations. Safe to call every run."""
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            ticker TEXT,
            price REAL,
            quality_score REAL,
            valuation_score REAL,
            growth_score REAL,
            moat_score REAL,
            risk_score REAL,
            bs_score REAL,
            cap_alloc_score REAL,
            smart_money_score REAL,
            trap_score REAL,
            momentum_multiplier REAL,
            final_score REAL,
            latest_catalyst TEXT,
            news_link TEXT,
            raw_json TEXT,
            inst_flow_delta REAL DEFAULT 0.0,
            concall_sentiment_score REAL DEFAULT 0.0,
            concall_summary TEXT DEFAULT "No summary available"
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS active_weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            last_updated TEXT,
            quality_weight REAL,
            growth_weight REAL,
            valuation_weight REAL,
            risk_weight REAL,
            moat_weight REAL,
            bs_weight REAL,
            cap_alloc_weight REAL,
            smart_money_weight REAL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS performance_tracking (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_id INTEGER,
            forward_date TEXT,
            forward_price REAL,
            return_pct REAL,
            FOREIGN KEY(prediction_id) REFERENCES daily_predictions(id)
        )
    ''')
    # INSERT OR REPLACE in the optimizer relies on this uniqueness; without it
    # every optimizer run appended duplicate rows (6409 rows for 2043 ids).
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS ux_perf_pred_fwd
        ON performance_tracking(prediction_id, forward_date)
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS ix_pred_date ON daily_predictions(date)')
    cursor.execute('CREATE INDEX IF NOT EXISTS ix_pred_ticker_date ON daily_predictions(ticker, date)')

    for table, column, ddl in MIGRATIONS:
        if column not in _columns(cursor, table):
            cursor.execute(ddl)

    # Seed Initial V15 Weights if empty
    cursor.execute("SELECT COUNT(*) FROM active_weights")
    if cursor.fetchone()[0] == 0:
        cursor.execute('''
            INSERT INTO active_weights (
                last_updated, quality_weight, growth_weight, valuation_weight,
                risk_weight, moat_weight, bs_weight, cap_alloc_weight, smart_money_weight, note
            ) VALUES (
                date('now'), 0.20, 0.20, 0.15, 0.15, 0.10, 0.10, 0.05, 0.05, 'initial seed'
            )
        ''')
    conn.commit()


def dedupe_performance_tracking(conn):
    """Collapse historical duplicate (prediction_id, forward_date) rows, keeping the newest."""
    cursor = conn.cursor()
    cursor.execute('''
        DELETE FROM performance_tracking
        WHERE id NOT IN (
            SELECT MAX(id) FROM performance_tracking GROUP BY prediction_id, forward_date
        )
    ''')
    removed = cursor.rowcount
    conn.commit()
    return removed


def setup_db(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    removed = dedupe_performance_tracking(conn)
    ensure_schema(conn)
    conn.close()
    print(f"Database initialized successfully at {db_path} (removed {removed} duplicate tracking rows)")


if __name__ == '__main__':
    setup_db()
