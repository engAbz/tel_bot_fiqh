import asyncio

from db import connect_db


async def main() -> None:
    db = await connect_db()

    try:
        await db.execute("DELETE FROM cooldowns")
        await db.execute("DELETE FROM pending_confirmations")

        # نحتفظ بمتجهات النصوص فقط؛ متجهات أسئلة المستخدمين لا تلزم.
        cursor = await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='embedding_cache'"
        )
        if await cursor.fetchone():
            await db.execute(
                "DELETE FROM embedding_cache WHERE task != 'RETRIEVAL_DOCUMENT'"
            )

        await db.commit()
        await db.execute("VACUUM")

        cursor = await db.execute("SELECT COUNT(*) AS n FROM entries")
        entries = (await cursor.fetchone())["n"]

        print(f"القاعدة جاهزة للنشر. عدد النصوص: {entries}")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())