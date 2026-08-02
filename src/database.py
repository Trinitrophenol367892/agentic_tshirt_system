import sqlite3
from contextlib import contextmanager
from .logger import log_db

DB_PATH = "system_data.db"


def _ensure_meta_column(conn):
    """Add trends.meta_json and trend_posts table if missing (idempotent migration)."""
    try:
        conn.execute("ALTER TABLE trends ADD COLUMN meta_json TEXT")
        log_db.info("  Added trends.meta_json column (audience intel + references).")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            log_db.debug("  trends.meta_json already present.")
        else:
            log_db.warning("  meta_json migration note: %s", str(e)[:80])
    
    # Create dedicated table for raw social media posts (evidence-based architecture)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trend_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trend_id INTEGER NOT NULL,
            post_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            content TEXT NOT NULL,
            author_handle TEXT,
            likes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0,
            shares INTEGER DEFAULT 0,
            impressions INTEGER DEFAULT 0,
            post_timestamp DATETIME,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (trend_id) REFERENCES trends(id)
        );
        CREATE INDEX IF NOT EXISTS idx_trend_posts_trend_id ON trend_posts(trend_id);
        CREATE INDEX IF NOT EXISTS idx_trend_posts_platform ON trend_posts(platform);
    """)
    log_db.info("  Verified trend_posts table (raw social media evidence storage).")


def init_db():
    log_db.info("Initializing SQLite database at: %s", DB_PATH)
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal TEXT,
                volume INTEGER,
                growth REAL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS briefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trend_id INTEGER,
                brief_json TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS designs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brief_id INTEGER,
                design_json TEXT,
                score REAL,
                status TEXT DEFAULT 'pending',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                design_id INTEGER,
                channel TEXT,
                clicks INTEGER DEFAULT 0,
                sales INTEGER DEFAULT 0,
                revenue REAL DEFAULT 0.0,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS confirmed_trends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trend_name TEXT NOT NULL,
                brief_theme TEXT,
                design_slogan TEXT,
                judge_score REAL,
                published_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        _ensure_meta_column(conn)
    log_db.info("Tables verified: trends(+meta_json), briefs, designs, performance, confirmed_trends")


@contextmanager
def get_connection():
    log_db.debug("Opening SQLite connection...")
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
        log_db.debug("Transaction committed.")
    except Exception as e:
        conn.rollback()
        log_db.error("Transaction rolled back due to error: %s", e)
        raise
    finally:
        conn.close()
        log_db.debug("SQLite connection closed.")
