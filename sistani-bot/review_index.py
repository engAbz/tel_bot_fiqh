import asyncio

from db import connect_db


async def main() -> None:
    db = await connect_db()

    try:
        cursor = await db.execute(
            """
            SELECT
                s.edition AS book,
                s.kind AS kind,
                COUNT(DISTINCT s.id) AS pages,
                COUNT(e.id) AS items
            FROM sources AS s
            LEFT JOIN entries AS e ON e.source_id = s.id
            GROUP BY s.edition, s.kind
            ORDER BY s.kind, items DESC
            """
        )

        print("=== ما تم فهرسته فعلاً ===")
        for row in await cursor.fetchall():
            label = row["book"] or "(الاستفتاءات)"
            print(
                f"{row['kind']:5} | صفحات {row['pages']:4} | "
                f"نصوص {row['items']:6} | {label[:70]}"
            )

        cursor = await db.execute(
            "SELECT COUNT(*) AS n FROM entries WHERE masaala_number IS NOT NULL"
        )
        numbered = (await cursor.fetchone())["n"]

        cursor = await db.execute("SELECT COUNT(*) AS n FROM entries")
        total = (await cursor.fetchone())["n"]

        print(f"\nالإجمالي: {total} نصاً | منها مرقّمة بمسألة: {numbered}")

        cursor = await db.execute(
            """
            SELECT section, answer_text, source_url
            FROM entries
            WHERE kind = 'book'
            ORDER BY RANDOM()
            LIMIT 3
            """
        )

        print("\n=== عيّنة عشوائية للمقارنة اليدوية مع الموقع ===")
        for row in await cursor.fetchall():
            print(f"\nالموضع: {row['section']}")
            print(f"الرابط: {row['source_url']}")
            print(f"النص: {row['answer_text'][:220]}...")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())