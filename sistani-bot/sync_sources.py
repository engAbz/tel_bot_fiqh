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


BASE = "https://www.sistani.org"
QA_INDEX_URL = f"{BASE}/arabic/qa/"

# الكتب المطلوبة فقط، بأرقامها كما في الموقع.
TARGET_BOOK_IDS = [
    "23720",   # منهاج الصالحين ـ الجزء الأول
    "15",      # منهاج الصالحين ـ الجزء الثاني
    "16",      # منهاج الصالحين ـ الجزء الثالث
    "13",      # المسائل المنتخبة
    "14",      # مناسك الحج وملحقاتها
    "24",      # الوجيز في أحكام العبادات
    "19",      # الفتاوى الميسّرة
    "18",      # الميسّر في الحج والعمرة
    "26278",   # الصيام جُنة من النار
]

# عنوان احتياطي يُستعمل فقط إذا غاب مسار التنقل في الصفحة.
BOOK_TITLE_FALLBACK = {
    "23720": "منهاج الصالحين ـ الجزء الأول (الطبعة المصححة 1445 هـ.)",
    "15": "منهاج الصالحين ـ الجزء الثاني (الطبعة المصححة 1445 هـ.)",
    "16": "منهاج الصالحين ـ الجزء الثالث (الطبعة المصححة 1445 هـ.)",
    "13": "المسائل المنتخبة - (الطبعة الجديدة المنقحة)",
    "14": "مناسك الحج وملحقاتها ـ (الطبعة الجديدة 1444 هـ)",
    "24": "الوجيز في أحكام العبادات",
    "19": "الفتاوى الميسّـرة",
    "18": "الميسّر في الحج والعمرة",
    "26278": "الصيام جُنة من النار",
}

MAX_PAGES_PER_BOOK = 1500

BOOK_PAGE_PATH_RE = re.compile(r"^/arabic/book/(\d+)/(\d+)/?$")
BOOK_ROOT_PATH_RE = re.compile(r"^/arabic/book/(\d+)/?$")
QA_PAGE_PATH_RE = re.compile(r"^/arabic/qa/(\d{3,5})/?$")

MASAALA_RE = re.compile(r"مس[أا]ل[ةه]\s*([0-9\u0660-\u0669]+)\s*[:\uFF1A]")
QUESTION_START_RE = re.compile(r"([\u0660-\u06690-9]+)\s*السؤال\s*[:\uFF1A]\s*")

MIN_CHUNK_CHARS = 25


def clean_line(value: str) -> str:
    """ينظّف المسافات داخل السطر فقط، دون تغيير الكلمات."""
    return re.sub(r"[ \t\u00a0\u200f\u200e]+", " ", str(value or "")).strip()


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def block_text(node) -> str:
    """يحوّل <br/> إلى أسطر ويحافظ على فقرات النص الأصلي."""
    if node is None:
        return ""

    for br in node.find_all("br"):
        br.replace_with("\n")

    lines = [clean_line(line) for line in node.get_text().split("\n")]
    return "\n".join(line for line in lines if line)


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


async def fetch_html(client: httpx.AsyncClient, url: str) -> str:
    if not is_official_arabic_url(url):
        raise ValueError(f"رابط خارج المصدر المسموح: {url}")

    response = await client.get(url)
    response.raise_for_status()

    if not is_official_arabic_url(str(response.url)):
        raise ValueError("تحويل إلى رابط غير رسمي؛ رُفض.")

    if len(response.content) > 15_000_000:
        raise ValueError("حجم الصفحة أكبر من الحد المسموح.")

    return response.text


# ------------------------- تحليل صفحات الكتب -------------------------

def parse_book_page(html: str, url: str) -> dict:
    """
    يعتمد على البنية الفعلية لصفحات الكتب:
      <h3> ... <a href="/arabic/book/15/">عنوان الكتاب والطبعة</a></h3>
      <h1 class="c">الباب » الفصل</h1>
      <div class="baz book-text"> ... النص مفصولاً بـ <br/> ... </div>
      <div class="baz book-footnote"> ... </div>
    """
    soup = BeautifulSoup(html, "html.parser")

    path = urlparse(url).path
    match = BOOK_PAGE_PATH_RE.match(path)

    if not match:
        raise ValueError(f"مسار صفحة كتاب غير متوقع: {path}")

    book_id = match.group(1)

    main = soup.find(id="main-book-content")

    if main is None:
        raise ValueError("لم أجد حاوية محتوى الكتاب (main-book-content).")

    content = main.find("div", class_="book-text")

    if content is None:
        raise ValueError("لم أجد div.book-text في الصفحة.")

    heading = main.find("h1")
    chapter_title = clean_text(heading.get_text(" ")) if heading else ""

    # عنوان الكتاب والطبعة من مسار التنقل أعلى الصفحة.
    book_title = ""

    for h3 in soup.find_all("h3"):
        for anchor in h3.find_all("a", href=True):
            root = BOOK_ROOT_PATH_RE.match(urlparse(anchor["href"]).path)
            if root and root.group(1) == book_id:
                book_title = clean_text(anchor.get_text(" "))

    if not book_title:
        book_title = BOOK_TITLE_FALLBACK.get(book_id, f"كتاب رقم {book_id}")

    body_text = block_text(content)
    footnote_text = block_text(main.find("div", class_="book-footnote"))

    if not body_text:
        raise ValueError("نص الصفحة فارغ.")

    # روابط الصفحة السابقة والتالية داخل الكتاب نفسه فقط.
    neighbours = set()

    for anchor in main.find_all("a", href=True):
        neighbour_path = urlparse(urljoin(url, anchor["href"])).path
        neighbour = BOOK_PAGE_PATH_RE.match(neighbour_path)

        if neighbour and neighbour.group(1) == book_id:
            neighbours.add(
                f"{BASE}/arabic/book/{neighbour.group(1)}"
                f"/{neighbour.group(2)}/"
            )

    return {
        "book_id": book_id,
        "book_title": book_title,
        "chapter_title": chapter_title or book_title,
        "body_text": body_text,
        "footnote_text": footnote_text,
        "neighbours": sorted(neighbours),
    }


def split_masaail(text: str) -> list:
    """
    يقسم النص عند كل «مسألة رقم:».
    الفقرات التابعة لمسألة تبقى ملحقة بها.
    ما قبل أول مسألة يبقى بلا رقم ولا يُخترع له رقم.
    """
    matches = list(MASAALA_RE.finditer(text))

    if not matches:
        stripped = text.strip()
        return [(None, stripped)] if stripped else []

    parts = []

    head = text[:matches[0].start()].strip()
    if head:
        parts.append((None, head))

    for index, match in enumerate(matches):
        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(text)
        )

        chunk = text[match.start():end].strip()

        if chunk:
            parts.append((normalize_text(match.group(1)), chunk))

    return parts


def build_book_entries(page: dict) -> list:
    entries = []

    for number, chunk in split_masaail(page["body_text"]):
        if len(chunk) < MIN_CHUNK_CHARS and number is None:
            continue

        if number is not None:
            entry_type = "masala"
            title = f"مسألة {number}"
        else:
            entry_type = "paragraph"
            title = clean_text(chunk)[:120]

        entries.append(
            {
                "entry_type": entry_type,
                "title": title,
                "section": page["chapter_title"],
                "question_text": "",
                "answer_text": chunk,
                "masaala_number": number,
                "qa_sequence": None,
                "site_ref_url": None,
            }
        )

    if page["footnote_text"] and len(page["footnote_text"]) >= MIN_CHUNK_CHARS:
        entries.append(
            {
                "entry_type": "footnote",
                "title": f"هوامش: {page['chapter_title']}",
                "section": page["chapter_title"],
                "question_text": "",
                "answer_text": page["footnote_text"],
                "masaala_number": None,
                "qa_sequence": None,
                "site_ref_url": None,
            }
        )

    return entries


# ----------------------- تحليل صفحات الاستفتاءات -----------------------

def parse_qa_page(html: str, section_hint: str) -> tuple:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.select("script, style, noscript, svg, .menu, .special-notif"):
        tag.decompose()

    page_title = ""

    for tag in soup.find_all(["h1", "h2"]):
        page_title = clean_text(tag.get_text(" "))
        if page_title:
            break

    text = clean_text(soup.get_text(" "))
    starts = list(QUESTION_START_RE.finditer(text))

    if not starts:
        raise ValueError("لم أجد نمط «السؤال/الجواب» في الصفحة.")

    entries = []

    for index, start in enumerate(starts):
        end = (
            starts[index + 1].start()
            if index + 1 < len(starts)
            else len(text)
        )

        piece = text[start.end():end]
        answer_marker = re.search(r"الجواب\s*[:\uFF1A]\s*", piece)

        if not answer_marker:
            continue

        question_text = clean_text(piece[:answer_marker.start()])
        tail = piece[answer_marker.end():]

        ref = re.search(
            r"(?:https?://)?(?:www\.)?sistani\.org/(\d+)",
            tail,
            flags=re.IGNORECASE,
        )

        site_ref_url = None

        if ref:
            answer_text = clean_text(tail[:ref.start()])
            site_ref_url = f"{BASE}/{ref.group(1)}/"
        else:
            footer = re.search(
                r"(لإدلاء سؤال جديد|العربية\s+فارسی|العربية\s+فارسي)",
                tail,
            )
            if footer:
                tail = tail[:footer.start()]
            answer_text = clean_text(tail)

        if not question_text or not answer_text:
            continue

        entries.append(
            {
                "entry_type": "qa",
                "title": question_text,
                "section": section_hint or page_title,
                "question_text": question_text,
                "answer_text": answer_text,
                "masaala_number": None,
                "qa_sequence": int(normalize_text(start.group(1))),
                "site_ref_url": site_ref_url,
            }
        )

    if not entries:
        raise ValueError("لم أستخرج أي سؤال وجواب صالح.")

    return page_title or section_hint, entries


async def discover_qa_pages(client: httpx.AsyncClient) -> list:
    html = await fetch_html(client, QA_INDEX_URL)
    soup = BeautifulSoup(html, "html.parser")

    found = {}

    for anchor in soup.find_all("a", href=True):
        path = urlparse(urljoin(QA_INDEX_URL, anchor["href"])).path
        match = QA_PAGE_PATH_RE.match(path)

        if not match:
            continue

        url = f"{BASE}/arabic/qa/{match.group(1)}/"
        name = clean_text(anchor.get_text(" "))

        found[url] = (
            f"الاستفتاءات » {name}"
            if name
            else f"الاستفتاءات » قسم {match.group(1)}"
        )

    return sorted(found.items())


# --------------------------- الحفظ في القاعدة ---------------------------

async def save_source(
    url: str,
    kind: str,
    title: str,
    section: str,
    edition: str,
    entries: list,
) -> tuple:
    now = datetime.now(timezone.utc).isoformat()

    material = "\n".join(
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

    content_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()

    db = await connect_db()

    try:
        await db.execute("BEGIN IMMEDIATE")

        cursor = await db.execute(
            "SELECT id, content_hash FROM sources WHERE url = ?",
            (url,),
        )
        existing = await cursor.fetchone()

        if existing and existing["content_hash"] == content_hash:
            await db.execute(
                "UPDATE sources SET fetched_at = ? WHERE id = ?",
                (now, existing["id"]),
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
                (kind, title, section, edition, now, content_hash, source_id),
            )

            await db.execute(
                "DELETE FROM entries WHERE source_id = ?",
                (source_id,),
            )
        else:
            cursor = await db.execute(
                """
                INSERT INTO sources
                    (url, kind, title, section, edition,
                     fetched_at, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (url, kind, title, section, edition, now, content_hash),
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
                    source_id, kind, entry_type, title, section,
                    question_text, answer_text, masaala_number,
                    qa_sequence, site_ref_url, source_url, search_text
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    kind,
                    entry.get("entry_type", ""),
                    entry.get("title", ""),
                    entry.get("section", section),
                    entry.get("question_text", ""),
                    entry.get("answer_text", ""),
                    entry.get("masaala_number"),
                    entry.get("qa_sequence"),
                    entry.get("site_ref_url"),
                    url,
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


# ------------------------------ الزحف ------------------------------

async def find_book_seeds(client: httpx.AsyncClient, book_id: str) -> list:
    """يأخذ أول روابط الصفحات من جذر الكتاب لبدء المسير."""
    root = f"{BASE}/arabic/book/{book_id}/"
    html = await fetch_html(client, root)
    soup = BeautifulSoup(html, "html.parser")

    main = soup.find(id="main-book-content") or soup.find(id="main-rtl") or soup

    seeds = []

    for anchor in main.find_all("a", href=True):
        path = urlparse(urljoin(root, anchor["href"])).path
        match = BOOK_PAGE_PATH_RE.match(path)

        if match and match.group(1) == book_id:
            url = f"{BASE}/arabic/book/{book_id}/{match.group(2)}/"
            if url not in seeds:
                seeds.append(url)

    return seeds


async def crawl_book(
    client: httpx.AsyncClient,
    book_id: str,
    delay: float,
) -> tuple:
    """
    يمشي بين صفحات الكتاب عبر روابط السابق/التالي.
    كل صفحة تُجلب مرة واحدة فقط.
    """
    try:
        queue = await find_book_seeds(client, book_id)
    except Exception as exc:
        print(f"  تعذر فتح جذر الكتاب {book_id}: {exc}")
        queue = []

    if not queue:
        print(f"  لم أجد صفحة بداية للكتاب {book_id}؛ تخطّيته.")
        return 0, 0, 1

    visited = set()
    pages = 0
    saved = 0
    failures = 0
    book_title = BOOK_TITLE_FALLBACK.get(book_id, "")

    while queue and pages < MAX_PAGES_PER_BOOK:
        url = queue.pop(0)

        if url in visited:
            continue

        visited.add(url)
        pages += 1

        try:
            html = await fetch_html(client, url)
            page = parse_book_page(html, url)
            entries = build_book_entries(page)

            if not entries:
                raise ValueError("لم أستخرج أي مقطع من الصفحة.")

            book_title = page["book_title"]

            count, unchanged = await save_source(
                url=url,
                kind="book",
                title=page["chapter_title"],
                section=page["chapter_title"],
                edition=page["book_title"],
                entries=entries,
            )

            saved += count
            state = "دون تغيير" if unchanged else "محدَّث"

            print(
                f"  [{pages}] {page['chapter_title'][:48]} "
                f"— {count} مقطع — {state}"
            )

            for neighbour in page["neighbours"]:
                if neighbour not in visited and neighbour not in queue:
                    queue.append(neighbour)

        except Exception as exc:
            failures += 1
            print(f"  [{pages}] فشل {url} — {exc}")

        if queue and delay:
            await asyncio.sleep(delay)

    if pages >= MAX_PAGES_PER_BOOK:
        print(f"  تنبيه: بلغت الحد الأقصى {MAX_PAGES_PER_BOOK} صفحة.")

    print(
        f"  ✔ {book_title or book_id}: "
        f"{pages} صفحة | {saved} مقطع | {failures} فشل"
    )

    return pages, saved, failures


async def crawl_qa(client: httpx.AsyncClient, delay: float) -> tuple:
    pages_list = await discover_qa_pages(client)

    print(f"  عدد أقسام الاستفتاءات المكتشفة: {len(pages_list)}")

    pages = 0
    saved = 0
    failures = 0

    for url, section_hint in pages_list:
        pages += 1

        try:
            html = await fetch_html(client, url)
            title, entries = parse_qa_page(html, section_hint)

            count, unchanged = await save_source(
                url=url,
                kind="qa",
                title=title,
                section=section_hint,
                edition="",
                entries=entries,
            )

            saved += count
            state = "دون تغيير" if unchanged else "محدَّث"

            print(
                f"  [{pages}/{len(pages_list)}] {section_hint[:48]} "
                f"— {count} سؤال — {state}"
            )

        except Exception as exc:
            failures += 1
            print(f"  [{pages}/{len(pages_list)}] فشل {url} — {exc}")

        if pages < len(pages_list) and delay:
            await asyncio.sleep(delay)

    print(f"  ✔ الاستفتاءات: {saved} سؤال | {failures} فشل")
    return pages, saved, failures


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="مزامنة الاستفتاءات والكتب المحددة من الموقع الرسمي."
    )
    parser.add_argument("--qa-only", action="store_true")
    parser.add_argument("--books-only", action="store_true")
    parser.add_argument(
        "--book",
        action="append",
        default=[],
        help="رقم كتاب واحد لمزامنته فقط. يمكن تكراره.",
    )
    parser.add_argument("--delay", type=float, default=None)
    args = parser.parse_args()

    await init_db()

    delay = (
        args.delay
        if args.delay is not None
        else float(os.getenv("SYNC_DELAY_SECONDS", "2"))
    )
    delay = max(0.0, delay)

    books = [
        book_id
        for book_id in (args.book or TARGET_BOOK_IDS)
        if book_id in TARGET_BOOK_IDS or args.book
    ]

    do_qa = not args.books_only
    do_books = not args.qa_only

    if args.book:
        do_qa = False
        do_books = True

    total_pages = 0
    total_saved = 0
    total_failures = 0

    headers = {
        "User-Agent": "SistaniTextIndex/2.0 (low-rate indexing for a reader bot)"
    }

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(40.0),
        follow_redirects=True,
        headers=headers,
    ) as client:
        if do_qa:
            print("\n=== الاستفتاءات ===")
            pages, saved, failures = await crawl_qa(client, delay)
            total_pages += pages
            total_saved += saved
            total_failures += failures

        if do_books:
            for book_id in books:
                name = BOOK_TITLE_FALLBACK.get(book_id, book_id)
                print(f"\n=== كتاب {book_id}: {name} ===")

                pages, saved, failures = await crawl_book(
                    client, book_id, delay
                )

                total_pages += pages
                total_saved += saved
                total_failures += failures

    print(
        f"\n=== المجموع ===\n"
        f"الصفحات: {total_pages} | المقاطع المحفوظة: {total_saved} | "
        f"الإخفاقات: {total_failures}"
    )

    if total_failures:
        print(
            "راجع رسائل الفشل أعلاه. "
            "لا تنشر الفهرس قبل التأكد من سلامة ما استُخرج."
        )


if __name__ == "__main__":
    asyncio.run(main())