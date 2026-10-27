import os
import re
import time

import numpy as np
from dotenv import load_dotenv
from rapidfuzz import fuzz

import embed_store
import gemini_rank
from db import connect_db, normalize_text


load_dotenv()

MIN_SUGGEST_SCORE = float(os.getenv("MIN_SUGGEST_SCORE", "48"))
AUTO_ANSWER_SCORE = float(os.getenv("AUTO_ANSWER_SCORE", "82"))
AUTO_ANSWER_GAP = float(os.getenv("AUTO_ANSWER_GAP", "8"))
SEMANTIC_WEIGHT = float(os.getenv("SEMANTIC_WEIGHT", "0.55"))
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "5"))
INDEX_TTL_SECONDS = int(os.getenv("INDEX_TTL_SECONDS", "300"))

ISSUE_NUMBER_RE = re.compile(r"(?:المساله|مساله)(?:\s*رقم)?\s*(\d+)")

_index = {"loaded_at": 0.0, "rows": [], "vectors": {}}


async def _fetch_rows() -> list:
    db = await connect_db()

    try:
        cursor = await db.execute(
            """
            SELECT
                e.id, e.title, e.section, e.question_text, e.answer_text,
                e.masaala_number, e.qa_sequence, e.entry_type,
                s.kind AS source_kind
            FROM entries AS e
            JOIN sources AS s ON s.id = e.source_id
            ORDER BY e.id
            """
        )
        return [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()


async def load_index(force: bool = False) -> tuple:
    now = time.time()

    if (
        not force
        and _index["rows"]
        and now - _index["loaded_at"] < INDEX_TTL_SECONDS
    ):
        return _index["rows"], _index["vectors"]

    rows = await _fetch_rows()

    for row in rows:
        row["_n_question"] = normalize_text(row["question_text"])
        row["_n_title"] = normalize_text(row["title"])
        row["_n_answer"] = normalize_text(row["answer_text"])
        row["_n_section"] = normalize_text(row["section"])

    vectors = {}

    if gemini_rank.gemini_enabled() and rows:
        keys = {}

        for row in rows:
            keys[row["id"]] = gemini_rank.entry_embed_key(row)

        stored = await embed_store.get_many(list(keys.values()))

        for entry_id, key in keys.items():
            if key in stored:
                vectors[entry_id] = stored[key]

    _index["rows"] = rows
    _index["vectors"] = vectors
    _index["loaded_at"] = now

    return rows, vectors


def invalidate_index() -> None:
    _index["loaded_at"] = 0.0


def lexical_score(query_normalized: str, row: dict) -> float:
    fields = (
        (row["_n_question"], 1.0),
        (row["_n_title"], 0.96),
        (row["_n_answer"], 0.72),
        (row["_n_section"], 0.55),
    )

    best = 0.0

    for value, weight in fields:
        if not value:
            continue

        score = max(
            fuzz.token_set_ratio(query_normalized, value),
            fuzz.partial_ratio(query_normalized, value),
        )
        best = max(best, score * weight)

    return float(best)


def combined_score(lex: float, cosine) -> float:
    if cosine is None:
        return lex

    semantic = max(0.0, min(1.0, float(cosine))) * 100.0
    return (SEMANTIC_WEIGHT * semantic) + ((1.0 - SEMANTIC_WEIGHT) * lex)


async def search_entries(query: str) -> tuple:
    """
    يعيد (mode, candidates).
    mode: empty | none | answer | choose
    لا يُنشئ نصاً جديداً؛ يعيد سجلات موجودة في قاعدة البيانات فقط.
    """
    rows, vectors = await load_index()

    if not rows:
        return "empty", []

    query_normalized = normalize_text(query)

    issue_match = ISSUE_NUMBER_RE.search(query_normalized)

    if issue_match:
        wanted = issue_match.group(1)

        numbered = [
            row
            for row in rows
            if str(row["masaala_number"] or "") == wanted
        ]

        if len(numbered) == 1:
            return "answer", numbered

        if numbered:
            return "choose", numbered[:MAX_CANDIDATES]

        return "none", []

    exact = [
        row
        for row in rows
        if row["source_kind"] == "qa"
        and row["_n_question"]
        and row["_n_question"] == query_normalized
    ]

    if len(exact) == 1:
        return "answer", exact

    query_vector = await gemini_rank.embed_query_cached(query)

    scored = []

    for row in rows:
        lex = lexical_score(query_normalized, row)

        cosine = None

        if query_vector is not None:
            stored = vectors.get(row["id"])

            if stored is not None:
                cosine = float(np.dot(query_vector, stored))

        total = combined_score(lex, cosine)

        if total >= MIN_SUGGEST_SCORE:
            scored.append((total, row))

    if not scored:
        return "none", []

    scored.sort(key=lambda item: item[0], reverse=True)

    top_score = scored[0][0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0

    candidates = [row for _, row in scored[:MAX_CANDIDATES]]

    choice, confidence = await gemini_rank.select_candidate(
        question=query,
        candidates=candidates,
    )

    if choice == 0:
        # المصنّف يرى أن لا شيء يطابق قصد السؤال.
        if top_score < AUTO_ANSWER_SCORE:
            return "none", []

    if choice and choice > 0:
        picked = candidates[choice - 1]

        reordered = [picked] + [
            row for row in candidates if row["id"] != picked["id"]
        ]
        candidates = reordered

        if confidence == "high" and top_score >= MIN_SUGGEST_SCORE + 10:
            return "answer", [picked]

    if (
        top_score >= AUTO_ANSWER_SCORE
        and (top_score - second_score) >= AUTO_ANSWER_GAP
    ):
        return "answer", [candidates[0]]

    return "choose", candidates