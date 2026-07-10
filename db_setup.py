import sqlite3
import os

DB_PATH = '/Users/saurabhnigam/.gemini/antigravity/brain/4ca10147-d4d2-4287-957e-cfadc0b4954e/scratch/quant_engine.db'

def setup_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Table 1: Daily Predictions (Snapshot)
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

    # Table 2: Active Weights
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

    # Table 3: Performance Tracking (Feedback Loop)
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

    # Seed Initial V15 Weights if empty
    cursor.execute("SELECT COUNT(*) FROM active_weights")
    if cursor.fetchone()[0] == 0:
        cursor.execute('''
            INSERT INTO active_weights (
                last_updated, quality_weight, growth_weight, valuation_weight, 
                risk_weight, moat_weight, bs_weight, cap_alloc_weight, smart_money_weight
            ) VALUES (
                date('now'), 0.20, 0.20, 0.15, 0.15, 0.10, 0.10, 0.05, 0.05
            )
        ''')
    
    conn.commit()
    conn.close()
    print("Database initialized successfully!")

if __name__ == '__main__':
    setup_db()
