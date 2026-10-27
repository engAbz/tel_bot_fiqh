import hashlib
from datetime import datetime, timezone

import numpy as np

from db import connect_db


SCHEMA = """
CREATE TABLE IF NOT EXISTS embedding_cache (
    key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    task TEXT NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL
);
"""


def make_key(model: str, dim: int, task: str, text: str) -> str:
    material = f"{model}|{dim}|{task}|{text}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def to_blob(values) -> bytes:
    return np.asarray(values, dtype=np.float32).tobytes()


def from_blob(blob: bytes, dim: int):
    vector = np.frombuffer(blob, dtype=np.float32)
    if vector.size != dim:
        return None
    return vector


async def ensure_table() -> None:
    db = await connect_db()
    try:
        await db.executescript(SCHEMA)
        await db.commit()
    finally:
        await db.close()


async def get_many(keys: list[str]) -> dict:
    if not keys:
        return {}

    found = {}
    db = await connect_db()

    try:
        for start in range(0, len(keys), 400):
            chunk = keys[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)

            cursor = await db.execute(
                f"SELECT key, dim, vector FROM embedding_cache "
                f"WHERE key IN ({placeholders})",
                chunk,
            )

            for row in await cursor.fetchall():
                vector = from_blob(row["vector"], row["dim"])
                if vector is not None:
                    found[row["key"]] = vector

        return found
    finally:
        await db.close()


async def put_many(items: list[dict]) -> None:
    if not items:
        return

    now = datetime.now(timezone.utc).isoformat()
    db = await connect_db()

    try:
        await db.executemany(
            """
            INSERT OR REPLACE INTO embedding_cache
                (key, model, dim, task, vector, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item["key"],
                    item["model"],
                    item["dim"],
                    item["task"],
                    to_blob(item["vector"]),
                    now,
                )
                for item in items
            ],
        )
        await db.commit()
    finally:
        await db.close()


async def count_rows() -> int:
    db = await connect_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) AS n FROM embedding_cache"
        )
        return (await cursor.fetchone())["n"]
    finally:
        await db.close()