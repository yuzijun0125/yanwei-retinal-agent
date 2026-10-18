"""Transactional feedback storage. JSONL remains a compatibility export."""
from pathlib import Path
from contextlib import closing
import json
import sqlite3


def append(folder, record):
    root = Path(folder)
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(root / "feedback.sqlite3"), timeout=10)) as db, db:
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("CREATE TABLE IF NOT EXISTS feedback (key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        # Import existing exports once without creating duplicate records.
        exported = root / "feedback.jsonl"
        if not db.execute("SELECT 1 FROM feedback LIMIT 1").fetchone() and exported.exists():
            for line in exported.read_text(encoding="utf-8").splitlines():
                try:
                    old = json.loads(line)
                    if old.get("idempotency_key"):
                        db.execute("INSERT OR IGNORE INTO feedback VALUES (?, ?)",
                                   (old["idempotency_key"], json.dumps(old, ensure_ascii=False)))
                except (ValueError, TypeError):
                    continue
        row = db.execute("SELECT payload FROM feedback WHERE key=?", (record["idempotency_key"],)).fetchone()
        if row:
            previous = json.loads(row[0])
            if any(previous.get(k) != record.get(k) for k in ("case_id", "verdict", "note")):
                raise ValueError("同一幂等键不能用于不同反馈内容")
            return previous, True
        db.execute("INSERT INTO feedback VALUES (?, ?)",
                   (record["idempotency_key"], json.dumps(record, ensure_ascii=False)))
        db.commit()
        # Serialize exports with a separate write transaction; rebuild atomically.
        db.execute("BEGIN IMMEDIATE")
        records = db.execute("SELECT payload FROM feedback ORDER BY rowid").fetchall()
        temporary = root / "feedback.jsonl.tmp"
        temporary.write_text("".join(row[0] + "\n" for row in records), encoding="utf-8")
        temporary.replace(exported)
        return record, False
