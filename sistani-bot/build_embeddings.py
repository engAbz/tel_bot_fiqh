import argparse
import asyncio
import os

from dotenv import load_dotenv

import embed_store
import gemini_rank
from db import connect_db, init_db


load_dotenv()

BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "8"))
EMBED_RPM = float(os.getenv("EMBED_RPM", "60"))


async def fetch_entries() -> list:
    db = await connect_db()

    try:
        cursor = await db.execute(
            """
            SELECT
                e.id, e.title, e.section, e.question_text, e.answer_text,
                e.masaala_number, s.kind AS source_kind
            FROM entries AS e
            JOIN sources AS s ON s.id = e.source_id
            ORDER BY e.id
            """
        )
        return [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="حد أقصى لعدد النصوص الجديدة في هذا التشغيل (0 = بلا حد).",
    )
    args = parser.parse_args()

    if not gemini_rank.gemini_enabled():
        raise SystemExit(
            "أضف GEMINI_API_KEY في .env وثبّت google-genai أولاً."
        )

    await init_db()
    await embed_store.ensure_table()

    rows = await fetch_entries()

    if not rows:
        raise SystemExit("الفهرس فارغ. شغّل sync_sources.py أولاً.")

    pending = {}

    for row in rows:
        text = gemini_rank.entry_embed_text(row)

        if not text:
            continue

        key = gemini_rank.entry_embed_key(row)
        pending[key] = text

    existing = await embed_store.get_many(list(pending.keys()))

    todo = [
        (key, text)
        for key, text in pending.items()
        if key not in existing
    ]

    if args.limit > 0:
        todo = todo[:args.limit]

    print(
        f"نصوص مفهرسة: {len(rows)} | "
        f"متجهات جاهزة: {len(existing)} | "
        f"مطلوب حسابها الآن: {len(todo)}"
    )

    if not todo:
        print("لا جديد. كل النصوص لها متجهات محفوظة.")
        return

    gap = 60.0 / EMBED_RPM if EMBED_RPM > 0 else 0.0
    done = 0
    failures = 0

    for start in range(0, len(todo), BATCH_SIZE):
        batch = todo[start:start + BATCH_SIZE]
        texts = [text for _, text in batch]

        delay = 5.0
        vectors = None

        for attempt in range(4):
            try:
                vectors = await gemini_rank.embed_batch(
                    texts,
                    gemini_rank.DOC_TASK,
                )
                break
            except Exception as exc:
                message = str(exc)
                print(f"  محاولة {attempt + 1} فشلت: {message[:200]}")

                if attempt == 3:
                    break

                await asyncio.sleep(delay)
                delay *= 2

        if vectors is None:
            failures += len(batch)
            continue

        items = []

        for (key, _), vector in zip(batch, vectors):
            if vector is None:
                failures += 1
                continue

            items.append(
                {
                    "key": key,
                    "model": gemini_rank.EMBED_MODEL,
                    "dim": gemini_rank.EMBED_DIM,
                    "task": gemini_rank.DOC_TASK,
                    "vector": vector,
                }
            )

        await embed_store.put_many(items)
        done += len(items)

        print(f"[{done}/{len(todo)}] تم الحفظ.")

        if start + BATCH_SIZE < len(todo) and gap:
            await asyncio.sleep(gap)

    total = await embed_store.count_rows()

    print(
        f"انتهى. متجهات جديدة: {done} | إخفاقات: {failures} | "
        f"إجمالي المخزن: {total}"
    )

    if failures:
        print(
            "أعد تشغيل الأمر لاحقاً لإكمال ما فشل؛ "
            "المحفوظ لا يُعاد حسابه."
        )


if __name__ == "__main__":
    asyncio.run(main())