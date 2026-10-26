import argparse
import asyncio
import hashlib
import os
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from db import connect_db, init_db, normalize_text


QA_INDEX_URL = "https://www.sistani.org/arabic/qa/"


# مصادر النسخة الأولى.
# أضف صفحات أخرى هنا بعد التأكد من أنها من الموقع الرسمي.
BASE_SOURCES = [
    {
        "kind": "qa",
        "url": "https://www.sistani.org/arabic/qa/0620/",
        "section_hint": "الاستفتاءات » غسل الجمعة",
        "edition": "",
        "required_markers": ["السؤال", "الجواب", "غسل الجمعة"],
    },
    {
        "kind": "book",
        "url": "https://www.sistani.org/arabic/book/23720/4604/",
        "section_hint": "كتاب الطهارة » المقصد السابع: الأغسال المندوبة",
        "edition": "منهاج الصالحين ـ الجزء الأول، الطبعة المصححة 1445 هـ.",
        "required_markers": ["غسل الجمعة", "مسألة 337"],
    },
    {
        "kind": "book",
        "url": "https://www.sistani.org/arabic/book/23720/3624/",
        "section_hint": "كتاب الطهارة » الفصل الرابع: واجبات غسل الجنابة",
        "edition": "منهاج الصالحين ـ الجزء الأول، الطبعة المصححة 1445 هـ.",
        "required_markers": ["واجبات غسل الجنابة", "مسألة 184"],
    },
]


QUESTION_START_RE = re.compile(
    r"([٠-٩0-9]+)\s*السؤال\s*[:：]\s*"
)

MASAALA_RE = re.compile(
    r"مسألة\s*([٠-٩0-9]+)\s*[:：]"
)


def clean_text(value: str) -> str:
    """
    ينظّف المسافات فقط؛ لا يعيد صياغة الكلمات.
    """
    return re.sub(r"\s+", " ", str(value or "")).strip()


def is_official_arabic_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() == "www.sistani.org"
        and parsed.path.startswith("/arabic/")
    )


async def fetch_html(
    client: httpx.AsyncClient,
    url: str,
) -> str:
    if not is_official_arabic_url(url):
        raise ValueError(f"رُفض الرابط لأنه ليس ضمن المصدر المسموح: {url}")

    response = await client.get(url)
    response.raise_for_status()

    final_url = str(response.url)
    if not is_official_arabic_url(final_url):
        raise ValueError(
            f"رُفض التحويل إلى رابط خارج المسارات الرسمية العربية: {final_url}"
        )

    if len(response.content) > 15_000_000:
        raise ValueError("حجم الصفحة أكبر من الحد المسموح.")

    return response.text


def visible_text(soup: BeautifulSoup) -> str:
    for tag in soup.select("script, style, noscript, svg"):
        tag.decompose()

    return clean_text(soup.get_text(" ", strip=True))


def get_first_heading(soup: BeautifulSoup) -> str:
    for tag in soup.find_all(["h1", "h2", "h3"]):
        heading = clean_text(tag.get_text(" ", strip=True))
        if heading:
            return heading

    if soup.title:
        return clean_text(soup.title.get_text(" ", strip=True))

    return ""


def parse_qa_page(html: str, source: dict) -> tuple[str, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    page_title = get_first_heading(soup)
    text = visible_text(soup)

    starts = list(QUESTION_START_RE.finditer(text))
    if not starts:
        raise ValueError(
            "لم أجد نمط الأسئلة والأجوبة المتوقع في صفحة الاستفتاءات. "
            "لم تُستبدل بيانات هذه الصفحة."
        )

    entries = []

    for index, start in enumerate(starts):
        end_pos = (
            starts[index + 1].start()
            if index + 1 < len(starts)
            else len(text)
        )

        piece = text[start.end():end_pos]

        answer_marker = re.search(
            r"الجواب\s*[:：]\s*",
            piece,
        )
        if not answer_marker:
            continue

        question_text = clean_text(piece[:answer_marker.start()])
        answer_and_ref = piece[answer_marker.end():]

        ref_match = re.search(
            r"(?:https?://)?(?:www\.)?sistani\.org/(\d+)",
            answer_and_ref,
            flags=re.IGNORECASE,
        )

        site_ref_url = None

        if ref_match:
            answer_text = clean_text(answer_and_ref[:ref_match.start()])
            site_ref_url = (
                f"https://www.sistani.org/{ref_match.group(1)}/"
            )
        else:
            # احتياط لصفحة لا يظهر فيها رابط المرجع داخل النص.
            footer_match = re.search(
                r"(لإدلاء سؤال جديد|العربية\s+فارسی|العربية\s+فارسي)",
                answer_and_ref,
            )
            if footer_match:
                answer_and_ref = answer_and_ref[:footer_match.start()]

            answer_text = clean_text(answer_and_ref)

        if not question_text or not answer_text:
            continue

        sequence = int(normalize_text(start.group(1)))

        entries.append(
            {
                "entry_type": "qa",
                "title": question_text,
                "section": source.get("section_hint", page_title),
                "question_text": question_text,
                "answer_text": answer_text,
                "masaala_number": None,
                "qa_sequence": sequence,
                "site_ref_url": site_ref_url,
            }
        )

    if not entries:
        raise ValueError(
            "لم أستخرج أي سؤال وجواب صالح من الصفحة."
        )

    return page_title or source.get("section_hint", ""), entries


def split_book_text(text: str) -> list[tuple[str | None, str]]:
    """
    يقسم النص عند وجود رقم مسألة رسمي.
    الفقرة التي لا تحمل رقم مسألة تبقى بلا رقم.
    """
    matches = list(MASAALA_RE.finditer(text))

    if not matches:
        cleaned = clean_text(text)
        return [(None, cleaned)] if cleaned else []

    parts = []

    prefix = clean_text(text[:matches[0].start()])
    if prefix:
        parts.append((None, prefix))

    for index, match in enumerate(matches):
        end_pos = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(text)
        )

        part_text = clean_text(text[match.start():end_pos])
        number = normalize_text(match.group(1))

        if part_text:
            parts.append((number, part_text))

    return parts


def fallback_book_blocks(
    soup: BeautifulSoup,
    heading_text: str,
) -> list[str]:
    """
    احتياط إذا لم تستخدم الصفحة وسوم p أو li.
    يجمع النص الظاهر بعد عنوان الفصل وحتى شريط التنقل التالي.
    """
    lines = [
        clean_text(line)
        for line in soup.get_text("\n", strip=True).splitlines()
    ]
    lines = [line for line in lines if line]

    heading_norm = normalize_text(heading_text)
    start_index = None

    for index, line in enumerate(lines):
        line_norm = normalize_text(line)
        if heading_norm and (
            line_norm == heading_norm or heading_norm in line_norm
        ):
            start_index = index
            break

    if start_index is None:
        return []

    collected = []

    for line in lines[start_index + 1:]:
        if "←" in line or "→" in line:
            break

        if len(line) >= 8:
            collected.append(line)

    full_text = clean_text(" ".join(collected))
    return [full_text] if full_text else []


def parse_book_page(html: str, source: dict) -> tuple[str, str, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")

    heading_node = None
    heading_text = ""

    # نفضّل عنوان h1 لأنه يمثل عنوان الصفحة الفتوائية غالباً.
    for tag in soup.find_all("h1"):
        candidate = clean_text(tag.get_text(" ", strip=True))
        if candidate:
            heading_node = tag
            heading_text = candidate
            break

    # احتياط إذا كانت الصفحة لا تستخدم h1.
    if heading_node is None:
        for tag in soup.find_all(["h2", "h3"]):
            candidate = clean_text(tag.get_text(" ", strip=True))
            if candidate:
                heading_node = tag
                heading_text = candidate
                break

    if heading_node is None:
        raise ValueError("لم أجد عنوان الفصل في صفحة الكتاب.")

    blocks = []

    # نجمع الفقرات الظاهرة بعد عنوان الفصل فقط.
    for node in heading_node.find_all_next(["p", "li"]):
        if node.find_parent(["nav", "footer"]):
            continue

        block = clean_text(node.get_text(" ", strip=True))

        if not block:
            continue

        # شريط السابق/التالي في صفحات الكتب.
        if "←" in block or "→" in block:
            break

        if len(block) < 8:
            continue

        blocks.append(block)

    # بعض الصفحات قد لا تستخدم p أو li للمحتوى.
    if not blocks:
        blocks = fallback_book_blocks(soup, heading_text)

    if not blocks:
        raise ValueError(
            "لم أستخرج فقرات من صفحة الكتاب؛ لم تُستبدل بياناتها."
        )

    entries = []

    for block in blocks:
        for number, part_text in split_book_text(block):
            if not part_text:
                continue

            if number is not None:
                entry_type = "masala"
                title = f"مسألة {number}"
            else:
                entry_type = "paragraph"
                # بادئة من النص الأصلي تستخدم عنواناً للبحث والاختيار فقط.
                title = part_text[:120]

            entries.append(
                {
                    "entry_type": entry_type,
                    "title": title,
                    "section": heading_text or source.get("section_hint", ""),
                    "question_text": "",
                    "answer_text": part_text,
                    "masaala_number": number,
                    "qa_sequence": None,
                    "site_ref_url": None,
                }
            )

    if not entries:
        raise ValueError("لم يتم إنشاء أي مقطع صالح من صفحة الكتاب.")

    return (
        heading_text or source.get("section_hint", ""),
        heading_text or source.get("section_hint", ""),
        entries,
    )


async def save_source(
    source: dict,
    title: str,
    section: str,
    entries: list[dict],
) -> tuple[int, bool]:
    now = datetime.now(timezone.utc).isoformat()

    hash_material = "\n".join(
        "|".join(
            [
                str(entry.get("entry_type", "")),
                str(entry.get("title", "")),
                str(entry.get("question_text", "")),
                str(entry.get("answer_text", "")),
                str(entry.get("masaala_number", "")),
                str(entry.get("qa_sequence", "")),
            ]
        )
        for entry in entries
    )

    content_hash = hashlib.sha256(
        hash_material.encode("utf-8")
    ).hexdigest()

    db = await connect_db()

    try:
        await db.execute("BEGIN IMMEDIATE")

        cursor = await db.execute(
            "SELECT id, content_hash FROM sources WHERE url = ?",
            (source["url"],),
        )
        existing = await cursor.fetchone()

        if existing and existing["content_hash"] == content_hash:
            await db.execute(
                """
                UPDATE sources
                SET kind = ?, title = ?, section = ?, edition = ?, fetched_at = ?
                WHERE id = ?
                """,
                (
                    source["kind"],
                    title,
                    section,
                    source.get("edition", ""),
                    now,
                    existing["id"],
                ),
            )

            await db.commit()
            return len(entries), True

        if existing:
            source_id = existing["id"]

            await db.execute(
                """
                UPDATE sources
                SET kind = ?, title = ?, section = ?, edition = ?,
                    fetched_at = ?, content_hash = ?
                WHERE id = ?
                """,
                (
                    source["kind"],
                    title,
                    section,
                    source.get("edition", ""),
                    now,
                    content_hash,
                    source_id,
                ),
            )

            await db.execute(
                "DELETE FROM entries WHERE source_id = ?",
                (source_id,),
            )
        else:
            cursor = await db.execute(
                """
                INSERT INTO sources
                    (url, kind, title, section, edition, fetched_at, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source["url"],
                    source["kind"],
                    title,
                    section,
                    source.get("edition", ""),
                    now,
                    content_hash,
                ),
            )
            source_id = cursor.lastrowid

        for entry in entries:
            search_material = " ".join(
                [
                    entry.get("title", ""),
                    entry.get("section", ""),
                    entry.get("question_text", ""),
                    entry.get("answer_text", ""),
                ]
            )

            await db.execute(
                """
                INSERT INTO entries (
                    source_id,
                    kind,
                    entry_type,
                    title,
                    section,
                    question_text,
                    answer_text,
                    masaala_number,
                    qa_sequence,
                    site_ref_url,
                    source_url,
                    search_text
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    source["kind"],
                    entry.get("entry_type", ""),
                    entry.get("title", ""),
                    entry.get("section", section),
                    entry.get("question_text", ""),
                    entry.get("answer_text", ""),
                    entry.get("masaala_number"),
                    entry.get("qa_sequence"),
                    entry.get("site_ref_url"),
                    source["url"],
                    normalize_text(search_material),
                ),
            )

        await db.commit()
        return len(entries), False

    except Exception:
        await db.rollback()
        raise

    finally:
        await db.close()


async def discover_qa_sources(
    client: httpx.AsyncClient,
) -> list[dict]:
    html = await fetch_html(client, QA_INDEX_URL)
    soup = BeautifulSoup(html, "html.parser")

    discovered = {}

    for anchor in soup.find_all("a", href=True):
        full_url = urljoin(QA_INDEX_URL, anchor["href"])
        parsed = urlparse(full_url)

        if (parsed.hostname or "").lower() != "www.sistani.org":
            continue

        parts = parsed.path.strip("/").split("/")

        # نقبل روابط أقسام الاستفتاءات ذات المعرّف الرقمي من أربع خانات فقط.
        # الروابط الأخرى لا تُجلب تلقائياً.
        if (
            len(parts) != 3
            or parts[0] != "arabic"
            or parts[1] != "qa"
            or not re.fullmatch(r"\d{4}", parts[2])
        ):
            continue

        topic_id = parts[2]
        url = f"https://www.sistani.org/arabic/qa/{topic_id}/"
        topic_name = clean_text(anchor.get_text(" ", strip=True))

        discovered[url] = {
            "kind": "qa",
            "url": url,
            "section_hint": (
                f"الاستفتاءات » {topic_name}"
                if topic_name
                else f"الاستفتاءات » قسم {topic_id}"
            ),
            "edition": "",
            "required_markers": ["السؤال", "الجواب"],
        }

    if not discovered:
        raise ValueError(
            "لم أستطع اكتشاف أقسام الاستفتاءات من صفحة الفهرس. "
            "يمكن إضافة روابط الأقسام يدوياً إلى BASE_SOURCES."
        )

    return list(discovered.values())


async def sync_one(
    client: httpx.AsyncClient,
    source: dict,
) -> tuple[int, bool]:
    html = await fetch_html(client, source["url"])
    soup = BeautifulSoup(html, "html.parser")
    all_visible = normalize_text(visible_text(soup))

    for marker in source.get("required_markers", []):
        if normalize_text(marker) not in all_visible:
            raise ValueError(
                f"لم أجد علامة التحقق المطلوبة في الصفحة: {marker}"
            )

    if source["kind"] == "qa":
        page_title, entries = parse_qa_page(html, source)
        section = source.get("section_hint") or page_title
        title = page_title or section
    elif source["kind"] == "book":
        title, section, entries = parse_book_page(html, source)
    else:
        raise ValueError(f"نوع مصدر غير معروف: {source['kind']}")

    if not entries:
        raise ValueError("لم ينتج المحلل أي سجلات.")

    return await save_source(
        source=source,
        title=title,
        section=section,
        entries=entries,
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--all-qa",
        action="store_true",
        help="اكتشاف أقسام الاستفتاءات من فهرس الموقع ومزامنتها بالتتابع.",
    )
    args = parser.parse_args()

    await init_db()

    timeout = httpx.Timeout(30.0)
    delay = max(
        0.0,
        float(os.getenv("SYNC_DELAY_SECONDS", "3")),
    )

    headers = {
        "User-Agent": (
            "SistaniTextIndex/1.0 "
            "(manual low-rate source indexing)"
        )
    }

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
    ) as client:
        if args.all_qa:
            discovered = await discover_qa_sources(client)

            books = [
                source
                for source in BASE_SOURCES
                if source["kind"] == "book"
            ]

            qa_seeds = [
                source
                for source in BASE_SOURCES
                if source["kind"] == "qa"
            ]

            by_url = {}
            for source in qa_seeds + discovered + books:
                by_url[source["url"]] = source

            sources = list(by_url.values())

            print(
                f"اكتُشف {len(discovered)} قسماً للاستفتاءات. "
                f"سيجري التحديث بالتتابع، بفاصل {delay} ثانية."
            )
        else:
            sources = BASE_SOURCES

        failures = 0

        for index, source in enumerate(sources, start=1):
            try:
                count, unchanged = await sync_one(client, source)
                status = "دون تغيير" if unchanged else "تم التحديث"

                print(
                    f"[نجاح {index}/{len(sources)}] "
                    f"{source['url']} — {count} سجل — {status}"
                )

            except Exception as exc:
                failures += 1
                print(
                    f"[فشل {index}/{len(sources)}] "
                    f"{source['url']} — {exc}"
                )

            if index < len(sources) and delay:
                await asyncio.sleep(delay)

    if failures:
        raise SystemExit(
            f"انتهت المزامنة مع {failures} أخطاء. "
            "راجع رسائل [فشل] قبل تشغيل البوت للجمهور."
        )

    print("انتهت المزامنة بنجاح.")


if __name__ == "__main__":
    asyncio.run(main())