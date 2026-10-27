import os

from aiohttp import web
from aiogram.webhook.aiohttp_server import (
    SimpleRequestHandler,
    setup_application,
)
from dotenv import load_dotenv

import embed_store
from bot import bot, dp
from db import init_db
from search_engine import load_index


load_dotenv()

WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "8000"))


def resolve_base_url() -> str:
    """
    يحدد الرابط العام للخدمة:
    1) WEBHOOK_BASE_URL إن ضُبط يدوياً.
    2) RENDER_EXTERNAL_URL (تضبطه Render تلقائياً).
    3) KOYEB_PUBLIC_DOMAIN (توفره Koyeb).
    """
    manual = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")
    if manual:
        return manual

    render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if render_url:
        return render_url

    koyeb_domain = os.getenv("KOYEB_PUBLIC_DOMAIN", "").strip().rstrip("/")
    if koyeb_domain:
        return f"https://{koyeb_domain}"

    raise RuntimeError(
        "لم أجد رابط الخدمة العام. اضبط WEBHOOK_BASE_URL في متغيرات البيئة."
    )


if not WEBHOOK_SECRET or not WEBHOOK_SECRET.replace("-", "").replace("_", "").isalnum():
    raise RuntimeError(
        "اضبط WEBHOOK_SECRET بقيمة عشوائية (حروف إنجليزية وأرقام و - _ فقط، "
        "1 إلى 256 حرفاً)."
    )

BASE_URL = resolve_base_url()
WEBHOOK_URL = BASE_URL + WEBHOOK_PATH


async def on_startup(app: web.Application) -> None:
    await init_db()
    await embed_store.ensure_table()

    # تحميل الفهرس إلى الذاكرة مرة واحدة عند الإقلاع.
    try:
        rows, vectors = await load_index(force=True)
        print(f"[index] سجلات: {len(rows)} | متجهات: {len(vectors)}")
    except Exception as exc:
        print(f"[index] تعذر التحميل المسبق: {exc}")

    # نضبط الـWebhook في كل إقلاع؛ العملية رخيصة وتصلح أي تغيير سابق.
    await bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=False,
        allowed_updates=["message", "callback_query"],
    )
    print(f"[webhook] مضبوط على: {WEBHOOK_URL}")


async def on_shutdown(app: web.Application) -> None:
    # لا نحذف الـWebhook عند الإيقاف، حتى تحتفظ تيليجرام بالرسائل
    # وتعيد إرسالها عندما تستيقظ الخدمة.
    await bot.session.close()


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "sistani-bot"})


def create_app() -> web.Application:
    app = web.Application()

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    ).register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)