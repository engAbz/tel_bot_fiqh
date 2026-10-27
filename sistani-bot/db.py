import os
import re
import unicodedata
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv


load_dotenv()

DB_PATH = Path(os.getenv("DB_PATH", "data/sistani.sqlite"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('qa', 'book')),
    title TEXT NOT NULL DEFAULT '',
    section TEXT NOT NULL DEFAULT '',
    edition TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    section TEXT NOT NULL DEFAULT '',
    question_text TEXT NOT NULL DEFAULT '',
    answer_text TEXT NOT NULL DEFAULT '',
    masaala_number TEXT,
    qa_sequence INTEGER,
    site_ref_url TEXT,
    source_url TEXT NOT NULL,
    search_text TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_entries_masaala
    ON entries(masaala_number);

CREATE INDEX IF NOT EXISTS idx_entries_kind
    ON entries(kind);

CREATE INDEX IF NOT EXISTS idx_entries_source
    ON entries(source_id);

CREATE TABLE IF NOT EXISTS cooldowns (
    user_id INTEGER PRIMARY KEY,
    last_search REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_confirmations (
    user_id INTEGER NOT NULL,
    entry_id INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (user_id, entry_id),
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
);
"""


def normalize_text(value: str) -> str:
    """
    تطبيع للبحث فقط.
    لا تستخدم هذه الدالة على النص الذي سيُعرض للمستخدم.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))

    digit_map = str.maketrans(
        "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
        "01234567890123456789",
    )
    text = text.translate(digit_map)

    text = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", text)

    text = text.translate(
        str.maketrans(
            {
                "أ": "ا",
                "إ": "ا",
                "آ": "ا",
                "ٱ": "ا",
                "ة": "ه",
                "ى": "ي",
            }
        )
    )

    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


async def connect_db() -> aiosqlite.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    db = await aiosqlite.connect(str(DB_PATH), timeout=10)
    db.row_factory = aiosqlite.Row

    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA busy_timeout = 10000")

    return db


async def init_db() -> None:
    db = await connect_db()
    try:
        await db.executescript(SCHEMA)
        await db.commit()
    finally:
        await db.close()