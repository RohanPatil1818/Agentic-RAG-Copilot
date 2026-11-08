
# Here we initialize the database and maintaining a looging info 
# Here we use sqlite database

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from app.core.config import get_settings

settings = get_settings()

# Define the schema
def init_db() -> None:
    Path(settings.audit_db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(settings.audit_db_path)
    con.execute(
        """CREATE TABLE IF NOT EXISTS query_audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            question TEXT NOT NULL,
            source_used TEXT NOT NULL,
            trace_json TEXT NOT NULL
        )"""
    )
    have = {r[1] for r in con.execute("PRAGMA table_info(query_audit)")}
    for col, ddl in [("cache_status", "TEXT"), ("cache_layer", "TEXT"), ("latency_ms", "INTEGER")]:
        if col not in have:
            con.execute(f"ALTER TABLE query_audit ADD COLUMN {col} {ddl}")
    con.commit();
    con.close()

# Connecting to Database and insert the LOGGING info values
def write_audit(question: str, source_used: str, trace: list[str], cache_status=None, cache_layer=None, latency_ms=None) -> None:
    con = sqlite3.connect(settings.audit_db_path)
    con.execute(
        "INSERT INTO query_audit(created_at, question, source_used, trace_json, cache_status, cache_layer, latency_ms) "
        "VALUES(?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), question, source_used, json.dumps(trace),
        cache_status, cache_layer, latency_ms))
    
    con.commit()
    con.close()
