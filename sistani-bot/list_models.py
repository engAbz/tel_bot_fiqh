import asyncio

import gemini_rank


async def main() -> None:
    client = gemini_rank.get_client()

    if client is None:
        raise SystemExit(
            "أضف GEMINI_API_KEY في .env وثبّت google-genai أولاً."
        )

    embed_models = []
    text_models = []

    async for model in await client.aio.models.list(
        config={"query_base": True}
    ):
        actions = list(getattr(model, "supported_actions", None) or [])
        name = (model.name or "").replace("models/", "")

        if "embedContent" in actions:
            embed_models.append(name)

        if "generateContent" in actions:
            text_models.append(name)

    print("نماذج التضمين المتاحة لمفتاحك:")
    for name in sorted(embed_models):
        print("  -", name)

    print("\nنماذج التوليد المتاحة لمفتاحك (للمصنّف فقط):")
    for name in sorted(text_models):
        print("  -", name)


if __name__ == "__main__":
    asyncio.run(main())