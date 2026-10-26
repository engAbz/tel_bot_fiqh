import math
import os
import re
import time
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv
from rapidfuzz import fuzz

from db import connect_db, init_db, normalize_text


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH_VALUE = os.getenv("DB_PATH", "data/sistani.sqlite")
COOLDOWN_SECONDS = max(0, int(os.getenv("COOLDOWN_SECONDS", "30")))

if not BOT_TOKEN or BOT_TOKEN == "ضع_توكن_البوت_هنا":
    raise RuntimeError(
        "أضف توكن البوت في ملف .env قبل تشغيل bot.py"
    )


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

NO_MATCH_TEXT = (
    "لم أعثر على نصٍّ مطابق في الفهرس الرسمي. "
    "يرجى ذكر سؤال آخر أو إضافة تفاصيل تميّز السؤال."
)

ISSUE_NUMBER_RE = re.compile(
    r"(?:المساله|مساله)(?:\s*رقم)?\s*(\d+)"
)

MIN_SUGGEST_SCORE = 52
MAX_CANDIDATES = 5
PENDING_SECONDS = 10 * 60


def is_official_url(url: str | None) -> bool:
    if not url:
        return False

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() == "www.sistani.org"
    )


def looks_understandable_enough_to_search(text: str) -> bool:
    """
    لا يحاول فهم المعنى.
    يرفض الرسائل القصيرة جداً والرموز، ثم يترك البحث المحلي يقرر.
    """
    normalized = normalize_text(text)

    if ISSUE_NUMBER_RE.search(normalized):
        return True

    arabic_words = re.findall(r"[\u0600-\u06FF]+", normalized)
    return len(arabic_words) >= 2


async def take_cooldown(user_id: int) -> int:
    if COOLDOWN_SECONDS <= 0:
        return 0

    now = time.time()
    db = await connect_db()

    try:
        await db.execute("BEGIN IMMEDIATE")

        cursor = await db.execute(
            "SELECT last_search FROM cooldowns WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()

        if row:
            remaining = COOLDOWN_SECONDS - (now - row["last_search"])

            if remaining > 0:
                await db.rollback()
                return math.ceil(remaining)

        await db.execute(
            """
            INSERT INTO cooldowns (user_id, last_search)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET last_search = excluded.last_search
            """,
            (user_id, now),
        )

        await db.commit()
        return 0

    except Exception:
        await db.rollback()
        raise

    finally:
        await db.close()


async def clear_pending(user_id: int) -> None:
    db = await connect_db()

    try:
        await db.execute(
            "DELETE FROM pending_confirmations WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()
    finally:
        await db.close()


async def save_pending(user_id: int, entries: list) -> None:
    expires_at = time.time() + PENDING_SECONDS
    db = await connect_db()

    try:
        await db.execute(
            "DELETE FROM pending_confirmations WHERE user_id = ?",
            (user_id,),
        )

        for entry in entries:
            await db.execute(
                """
                INSERT OR REPLACE INTO pending_confirmations
                    (user_id, entry_id, expires_at)
                VALUES (?, ?, ?)
                """,
                (user_id, entry["id"], expires_at),
            )

        await db.commit()
    finally:
        await db.close()


async def has_pending(user_id: int, entry_id: int) -> bool:
    db = await connect_db()

    try:
        cursor = await db.execute(
            """
            SELECT 1
            FROM pending_confirmations
            WHERE user_id = ?
              AND entry_id = ?
              AND expires_at > ?
            """,
            (user_id, entry_id, time.time()),
        )
        return await cursor.fetchone() is not None
    finally:
        await db.close()


async def load_entries() -> list:
    db = await connect_db()

    try:
        cursor = await db.execute(
            """
            SELECT
                e.*,
                s.kind AS source_kind,
                s.edition AS edition,
                s.fetched_at AS fetched_at
            FROM entries AS e
            JOIN sources AS s ON s.id = e.source_id
            ORDER BY e.id
            """
        )
        return await cursor.fetchall()
    finally:
        await db.close()


async def load_entry(entry_id: int):
    db = await connect_db()

    try:
        cursor = await db.execute(
            """
            SELECT
                e.*,
                s.kind AS source_kind,
                s.edition AS edition,
                s.fetched_at AS fetched_at
            FROM entries AS e
            JOIN sources AS s ON s.id = e.source_id
            WHERE e.id = ?
            """,
            (entry_id,),
        )
        return await cursor.fetchone()
    finally:
        await db.close()


async def get_status() -> tuple[int, int, str | None]:
    db = await connect_db()

    try:
        cursor = await db.execute("SELECT COUNT(*) AS n FROM entries")
        entry_count = (await cursor.fetchone())["n"]

        cursor = await db.execute("SELECT COUNT(*) AS n FROM sources")
        source_count = (await cursor.fetchone())["n"]

        cursor = await db.execute(
            "SELECT MAX(fetched_at) AS latest FROM sources"
        )
        latest = (await cursor.fetchone())["latest"]

        return entry_count, source_count, latest
    finally:
        await db.close()


def candidate_score(query_normalized: str, row) -> int:
    fields = [
        (row["question_text"], 1.0),
        (row["title"], 0.96),
        (row["answer_text"], 0.72),
        (row["section"], 0.55),
    ]

    best = 0

    for value, weight in fields:
        field_normalized = normalize_text(value)

        if not field_normalized:
            continue

        token_score = fuzz.token_set_ratio(
            query_normalized,
            field_normalized,
        )
        ratio_score = fuzz.ratio(
            query_normalized,
            field_normalized,
        )

        score = int(max(token_score, ratio_score) * weight)
        best = max(best, score)

    return best


def find_candidates(query: str, rows: list) -> tuple[str, list]:
    query_normalized = normalize_text(query)

    issue_match = ISSUE_NUMBER_RE.search(query_normalized)

    if issue_match:
        wanted_number = issue_match.group(1)

        numbered = [
            row
            for row in rows
            if str(row["masaala_number"] or "") == wanted_number
        ]

        return ("number", numbered[:MAX_CANDIDATES]) if numbered else (
            "none",
            [],
        )

    exact_questions = [
        row
        for row in rows
        if row["source_kind"] == "qa"
        and row["question_text"]
        and normalize_text(row["question_text"]) == query_normalized
    ]

    if exact_questions:
        return "exact", exact_questions[:MAX_CANDIDATES]

    scored = []

    for row in rows:
        score = candidate_score(query_normalized, row)

        if score >= MIN_SUGGEST_SCORE:
            scored.append((score, row))

    if not scored:
        return "none", []

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score = scored[0][0]

    selected = [
        row
        for score, row in scored
        if score >= best_score - 10
    ][:MAX_CANDIDATES]

    return "suggestions", selected


def candidate_label(row) -> str:
    if row["source_kind"] == "qa":
        return (
            f"{row['section']}\n"
            f"{row['question_text']}"
        )

    if row["masaala_number"]:
        return (
            f"{row['section']}\n"
            f"مسألة {row['masaala_number']}"
        )

    return (
        f"{row['section']}\n"
        f"{row['title']}"
    )


def short_button_label(row, number: int) -> str:
    if row["source_kind"] == "qa":
        text = row["question_text"] or row["title"]
    elif row["masaala_number"]:
        text = f"مسألة {row['masaala_number']} — {row['section']}"
    else:
        text = row["title"] or row["section"]

    text = re.sub(r"\s+", " ", text).strip()
    return f"{number}. {text[:48]}"


async def ask_user_to_choose(
    chat_id: int,
    user_id: int,
    candidates: list,
) -> None:
    await save_pending(user_id, candidates)

    if len(candidates) == 1:
        entry = candidates[0]

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="نعم، هذا هو المقصود",
                        callback_data=f"pick:{entry['id']}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="لا، سأعيد صياغة السؤال",
                        callback_data="cancel",
                    )
                ],
            ]
        )

        text = (
            "هل تقصد هذا النص؟ لم أعرض الجواب بعد.\n\n"
            f"{candidate_label(entry)}"
        )

        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
        )
        return

    keyboard_rows = []

    for index, entry in enumerate(candidates, start=1):
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=short_button_label(entry, index),
                    callback_data=f"pick:{entry['id']}",
                )
            ]
        )

    keyboard_rows.append(
        [
            InlineKeyboardButton(
                text="لا، سأعيد صياغة السؤال",
                callback_data="cancel",
            )
        ]
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    await bot.send_message(
        chat_id=chat_id,
        text=(
            "وجدت أكثر من نص محتمل. "
            "اختر السؤال أو النص المقصود؛ لن أعرض جواباً قبل اختيارك."
        ),
        reply_markup=keyboard,
    )


def split_for_telegram(text: str, limit: int = 3800) -> list[str]:
    parts = []
    remaining = text

    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)

        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)

        if cut <= 0:
            cut = limit

        parts.append(remaining[:cut])
        remaining = remaining[cut:]

        if remaining.startswith("\n") or remaining.startswith(" "):
            remaining = remaining[1:]

    if remaining:
        parts.append(remaining)

    return parts


async def send_entry(chat_id: int, entry_id: int) -> None:
    row = await load_entry(entry_id)

    if row is None:
        await bot.send_message(
            chat_id,
            "تعذر العثور على النص المختار في الفهرس الحالي. "
            "أعد إرسال السؤال من فضلك.",
        )
        return

    if not is_official_url(row["source_url"]):
        await bot.send_message(
            chat_id,
            "تعذر التحقق من رابط المصدر؛ لذلك لم أعرض النص.",
        )
        return

    source_link = (
        row["site_ref_url"]
        if is_official_url(row["site_ref_url"])
        else row["source_url"]
    )

    if row["source_kind"] == "qa":
        source_name = "الاستفتاءات"
        original_text = (
            f"السؤال: {row['question_text']}\n"
            f"الجواب: {row['answer_text']}"
        )
    else:
        source_name = "الكتب الفتوائية"
        original_text = row["answer_text"]

    header_lines = [
        f"المصدر: {source_name}",
        f"الموضع: {row['section']}",
    ]

    if row["edition"]:
        header_lines.append(f"الكتاب/الطبعة: {row['edition']}")

    if row["source_kind"] == "qa" and row["qa_sequence"] is not None:
        header_lines.append(
            f"ترتيب السؤال داخل صفحة الاستفتاءات: {row['qa_sequence']}"
        )

    if row["source_kind"] == "book" and row["masaala_number"]:
        header_lines.append(
            f"رقم المسألة: {row['masaala_number']}"
        )

    header_lines.append(f"آخر مزامنة للمصدر: {row['fetched_at']}")

    message_text = (
        "\n".join(header_lines)
        + "\n\nالنص المنقول من المصدر:\n"
        + original_text
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="فتح المصدر الرسمي",
                    url=source_link,
                )
            ]
        ]
    )

    parts = split_for_telegram(message_text)

    for index, part in enumerate(parts):
        is_last = index == len(parts) - 1

        await bot.send_message(
            chat_id=chat_id,
            text=part,
            reply_markup=keyboard if is_last else None,
        )


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        "هذا بوت بحث في النصوص المفهرسة من موقع مكتب السيد السيستاني.\n\n"
        "• لا ينشئ فتوى ولا يعيد صياغة النص.\n"
        "• يعرض النص المحفوظ مع موضعه ورابط المصدر.\n"
        "• إذا وجد أكثر من احتمال، يطلب منك الاختيار أولاً.\n"
        "• إذا لم يفهم الرسالة القصيرة أو غير الواضحة، لا يجيب.\n\n"
        "أرسل سؤالك باللغة العربية."
    )


@dp.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(
        "أرسل سؤالاً محدداً باللغة العربية.\n"
        "إذا كان هناك أكثر من نص محتمل، اختر النص من الأزرار.\n"
        "للبحث برقم مسألة اكتب مثلاً: مسألة 337\n"
        "يستغرق البحث محلياً، ولا يطلب صفحة الموقع عند كل سؤال."
    )


@dp.message(Command("status"))
async def status_handler(message: Message) -> None:
    entry_count, source_count, latest = await get_status()

    await message.answer(
        f"عدد النصوص المفهرسة: {entry_count}\n"
        f"عدد صفحات المصادر: {source_count}\n"
        f"آخر مزامنة: {latest or 'لا توجد مزامنة بعد'}"
    )


@dp.message(F.text)
async def text_handler(message: Message) -> None:
    if message.chat.type != "private":
        await message.answer(
            "يرجى استخدام البوت في محادثة خاصة."
        )
        return

    query = (message.text or "").strip()

    if not looks_understandable_enough_to_search(query):
        # وفق الشرط: لا نرسل جواباً إذا كانت الرسالة قصيرة جداً أو رموزاً.
        return

    if message.from_user is None:
        return

    remaining = await take_cooldown(message.from_user.id)

    if remaining > 0:
        await message.answer(
            f"يرجى الانتظار {remaining} ثانية قبل البحث التالي."
        )
        return

    await clear_pending(message.from_user.id)

    rows = await load_entries()

    if not rows:
        await message.answer(
            "الفهرس فارغ أو لم تتم مزامنته بعد. "
            "شغّل sync_sources.py أولاً."
        )
        return

    mode, candidates = find_candidates(query, rows)

    if mode == "exact" and len(candidates) == 1:
        await send_entry(message.chat.id, candidates[0]["id"])
        return

    if mode == "number" and len(candidates) == 1:
        await send_entry(message.chat.id, candidates[0]["id"])
        return

    if mode in {"exact", "number", "suggestions"} and candidates:
        await ask_user_to_choose(
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            candidates=candidates,
        )
        return

    await message.answer(NO_MATCH_TEXT)


@dp.callback_query(F.data.startswith("pick:"))
async def pick_handler(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return

    if callback.message.chat.type != "private":
        await callback.answer(
            "استخدم الاختيار في المحادثة الخاصة مع البوت.",
            show_alert=True,
        )
        return

    try:
        entry_id = int((callback.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("اختيار غير صالح.", show_alert=True)
        return

    if not await has_pending(callback.from_user.id, entry_id):
        await callback.answer(
            "انتهت صلاحية هذا الاختيار. أرسل السؤال من جديد.",
            show_alert=True,
        )
        return

    await callback.answer("تم تأكيد الاختيار.")
    await clear_pending(callback.from_user.id)
    await send_entry(callback.from_user.id, entry_id)


@dp.callback_query(F.data == "cancel")
async def cancel_handler(callback: CallbackQuery) -> None:
    await clear_pending(callback.from_user.id)
    await callback.answer("أُلغِي الاختيار.")

    if callback.message and callback.message.chat.type == "private":
        await bot.send_message(
            callback.from_user.id,
            "حسناً. أعد كتابة السؤال بصياغة أخرى.",
        )


async def main() -> None:
    await init_db()

    try:
        # نستخدم Long Polling؛ احذف webhook السابق إن كان مفعلاً.
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())