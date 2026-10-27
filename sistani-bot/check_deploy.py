import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REQUIRED_FILES = [
    "server.py", "bot.py", "db.py", "search_engine.py",
    "gemini_rank.py", "embed_store.py", "sync_sources.py",
    "build_embeddings.py", "pack_db.py",
    "requirements.txt", "Procfile", ".gitignore",
]

problems = []
warnings = []

for name in REQUIRED_FILES:
    if not Path(name).exists():
        problems.append(f"ملف مفقود: {name}")

db_path = Path(os.getenv("DB_PATH", "data/sistani.sqlite"))

if not db_path.exists():
    problems.append(
        f"قاعدة البيانات غير موجودة: {db_path} — شغّل sync_sources.py"
    )
else:
    size_mb = db_path.stat().st_size / (1024 * 1024)
    print(f"حجم القاعدة: {size_mb:.1f} MB")

    if size_mb > 95:
        problems.append("الحجم قارب حد GitHub (100MB). خفّض GEMINI_EMBED_DIM.")
    elif size_mb > 50:
        warnings.append("الحجم فوق 50MB؛ GitHub سيحذّر لكنه يقبل الملف.")

    con = sqlite3.connect(str(db_path))
    try:
        entries = con.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        sources = con.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        print(f"النصوص: {entries} | الصفحات: {sources}")

        if entries == 0:
            problems.append("الفهرس فارغ.")
        elif entries < 50:
            warnings.append(
                "الفهرس صغير؛ شغّل sync_sources.py --all-qa لنتائج أفضل."
            )

        has_cache = con.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='embedding_cache'"
        ).fetchone()

        if has_cache:
            docs = con.execute(
                "SELECT COUNT(*) FROM embedding_cache "
                "WHERE task = 'RETRIEVAL_DOCUMENT'"
            ).fetchone()[0]
            leftover = con.execute(
                "SELECT COUNT(*) FROM embedding_cache "
                "WHERE task != 'RETRIEVAL_DOCUMENT'"
            ).fetchone()[0]

            print(f"متجهات النصوص: {docs}")

            if docs < entries:
                warnings.append(
                    f"ناقص {entries - docs} متجهاً — أكمل build_embeddings.py"
                )
            if leftover:
                warnings.append("بقايا متجهات أسئلة — شغّل pack_db.py")
        else:
            warnings.append("لا يوجد جدول متجهات؛ البحث سيكون لفظياً فقط.")

        for table in ("cooldowns", "pending_confirmations"):
            count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count:
                warnings.append(f"جدول {table} غير فارغ — شغّل pack_db.py")
    finally:
        con.close()

try:
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        capture_output=True, text=True,
    )
    if tracked.returncode == 0:
        problems.append("خطر: ملف .env متتبَّع في git! نفّذ: git rm --cached .env")
    else:
        print("ملف .env غير متتبَّع (سليم).")
except FileNotFoundError:
    warnings.append("git غير متاح؛ لم أفحص تتبّع .env")

secret = os.getenv("WEBHOOK_SECRET", "").strip()
if not secret:
    warnings.append(
        "WEBHOOK_SECRET غير مضبوط محلياً — تأكد من ضبطه في لوحة المنصة."
    )
elif not (1 <= len(secret) <= 256) or not all(
    ch.isalnum() and ch.isascii() or ch in "-_" for ch in secret
):
    problems.append("WEBHOOK_SECRET يحتوي محارف غير مقبولة من تيليجرام.")

print()
for item in warnings:
    print(f"[تنبيه] {item}")
for item in problems:
    print(f"[خطأ] {item}")

if problems:
    print("\nلا ترفع قبل إصلاح الأخطاء أعلاه.")
    sys.exit(1)

print("\nجاهز للرفع.")