import asyncio
import json
import os
import re

import numpy as np
from dotenv import load_dotenv

import embed_store


load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001").strip()
EMBED_DIM = int(os.getenv("GEMINI_EMBED_DIM", "768"))
SELECTOR_MODEL = os.getenv("GEMINI_SELECTOR_MODEL", "").strip()
USE_SELECTOR = os.getenv("USE_GEMINI_SELECTOR", "0").strip() == "1"
EMBED_MAX_CHARS = int(os.getenv("GEMINI_EMBED_MAX_CHARS", "6000"))

DOC_TASK = "RETRIEVAL_DOCUMENT"
QUERY_TASK = "RETRIEVAL_QUERY"

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

_client = None


def get_client():
    global _client

    if genai is None or not API_KEY:
        return None

    if _client is None:
        _client = genai.Client(api_key=API_KEY)

    return _client


def gemini_enabled() -> bool:
    return get_client() is not None


def selector_enabled() -> bool:
    return gemini_enabled() and USE_SELECTOR and bool(SELECTOR_MODEL)


def entry_embed_text(row) -> str:
    """
    النص الذي يُرسل للتضمين.
    يجب أن تُستخدم الدالة نفسها في build_embeddings.py وفي البحث،
    وإلا اختلف المفتاح ولن يُستفاد من المخزن.
    """
    if row["source_kind"] == "qa":
        parts = [
            str(row["section"] or ""),
            "سؤال: " + str(row["question_text"] or ""),
            "جواب: " + str(row["answer_text"] or ""),
        ]
    else:
        number = row["masaala_number"]
        parts = [
            str(row["section"] or ""),
            f"مسألة {number}" if number else "",
            str(row["answer_text"] or ""),
        ]

    text = " \n".join(part for part in parts if part)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:EMBED_MAX_CHARS]


def entry_embed_key(row) -> str:
    return embed_store.make_key(
        EMBED_MODEL,
        EMBED_DIM,
        DOC_TASK,
        entry_embed_text(row),
    )


def unit_vector(values):
    vector = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(vector))

    if norm == 0.0:
        return None

    return (vector / norm).astype(np.float32)


async def embed_batch(texts: list[str], task_type: str):
    """
    يعيد قائمة متجهات موحّدة الطول، أو يرفع استثناء عند فشل الطلب.
    """
    client = get_client()

    if client is None:
        raise RuntimeError("مفتاح Gemini غير مضبوط أو الحزمة غير مثبتة.")

    config = types.EmbedContentConfig(
        task_type=task_type,
        output_dimensionality=EMBED_DIM,
    )

    response = await client.aio.models.embed_content(
        model=EMBED_MODEL,
        contents=texts,
        config=config,
    )

    vectors = []

    for item in response.embeddings:
        vectors.append(unit_vector(item.values))

    if len(vectors) != len(texts):
        raise RuntimeError(
            "عدد المتجهات المعادة لا يطابق عدد النصوص المرسلة."
        )

    return vectors


async def embed_query_cached(query: str):
    """
    يعيد متجه السؤال، ويخزّنه في SQLite لتقليل الاستهلاك.
    يعيد None عند أي فشل، فيكمل البحث لفظياً دون Gemini.
    """
    if not gemini_enabled():
        return None

    text = re.sub(r"\s+", " ", str(query or "")).strip()[:EMBED_MAX_CHARS]

    if not text:
        return None

    key = embed_store.make_key(EMBED_MODEL, EMBED_DIM, QUERY_TASK, text)
    cached = await embed_store.get_many([key])

    if key in cached:
        return cached[key]

    try:
        vectors = await asyncio.wait_for(
            embed_batch([text], QUERY_TASK),
            timeout=20,
        )
    except Exception as exc:
        print(f"[gemini] تعذر تضمين السؤال: {exc}")
        return None

    vector = vectors[0]

    if vector is None:
        return None

    await embed_store.put_many(
        [
            {
                "key": key,
                "model": EMBED_MODEL,
                "dim": EMBED_DIM,
                "task": QUERY_TASK,
                "vector": vector,
            }
        ]
    )

    return vector


SELECTOR_SYSTEM = (
    "أنت مصنّف اختيار فقط، ولست مفتياً ولا كاتب نصوص.\n"
    "تحصل على سؤال مستخدم وقائمة عناوين نصوص مفهرسة من موقع رسمي.\n"
    "مهمتك الوحيدة اختيار رقم العنصر الذي يطابق قصد السؤال.\n"
    "ممنوع منعاً باتاً كتابة أي حكم شرعي أو شرح أو نص جديد.\n"
    "إن لم يطابق أي عنصر قصد السؤال بوضوح فاختر 0.\n"
    "أعد JSON فقط بالشكل: "
    '{"choice": رقم, "confidence": "high" أو "low"}'
)

SELECTOR_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "choice": {"type": "INTEGER"},
        "confidence": {"type": "STRING", "enum": ["high", "low"]},
    },
    "required": ["choice", "confidence"],
}


def _build_selector_config(disable_thinking: bool):
    kwargs = {
        "temperature": 0,
        "response_mime_type": "application/json",
        "response_schema": SELECTOR_SCHEMA,
        "system_instruction": SELECTOR_SYSTEM,
    }

    if disable_thinking:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

    return types.GenerateContentConfig(**kwargs)


async def select_candidate(question: str, candidates: list) -> tuple:
    """
    يعيد (choice, confidence).
    choice = 0 يعني «لا يوجد مطابق».
    يعيد (None, None) عند أي فشل، فلا يؤثر على النتيجة.
    """
    if not selector_enabled() or not candidates:
        return None, None

    lines = []

    for index, row in enumerate(candidates, start=1):
        if row["source_kind"] == "qa":
            label = str(row["question_text"] or row["title"] or "")
        elif row["masaala_number"]:
            label = f"مسألة {row['masaala_number']} — {row['section']}"
        else:
            label = str(row["title"] or row["section"] or "")

        label = re.sub(r"\s+", " ", label).strip()[:220]
        lines.append(f"{index}. {label}")

    prompt = (
        f"سؤال المستخدم:\n{question}\n\n"
        f"العناصر المتاحة:\n" + "\n".join(lines)
    )

    client = get_client()

    for disable_thinking in (True, False):
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=SELECTOR_MODEL,
                    contents=prompt,
                    config=_build_selector_config(disable_thinking),
                ),
                timeout=25,
            )
            break
        except Exception as exc:
            if disable_thinking:
                continue

            print(f"[gemini] تعذر تشغيل المصنّف: {exc}")
            return None, None

    raw = getattr(response, "text", None)

    if not raw:
        return None, None

    try:
        data = json.loads(raw)
        choice = int(data["choice"])
        confidence = str(data["confidence"]).lower()
    except Exception:
        return None, None

    if choice < 0 or choice > len(candidates):
        return None, None

    if confidence not in {"high", "low"}:
        confidence = "low"

    return choice, confidence