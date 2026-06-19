# -*- coding: utf-8 -*-
"""Бесплатная AI-персонализация текста рассылки.

Работает с любым OpenAI-совместимым API. По умолчанию — Groq
(бесплатный и быстрый). Ключ кладётся в .env:  AI_API_KEY=gsk_...
Где взять бесплатно: https://console.groq.com  →  API Keys  →  Create API Key.
Можно подменить провайдера через AI_BASE_URL и AI_MODEL (см. config.py).
"""
import aiohttp
import config

SYSTEM_PROMPT = (
    "Ты — маркетинговый редактор Telegram-рассылок. Перепиши присланный текст так, "
    "чтобы он стал привлекательнее и лучше продавал. Сохрани смысл и язык оригинала. "
    "Можно добавить уместные эмодзи. НЕ добавляй пояснений, заголовков и кавычек вокруг ответа — "
    "верни ТОЛЬКО готовый текст рассылки."
)

STYLE_HINTS = {
    "sell": "Стиль: продающий, цепляющий, с призывом к действию.",
    "official": "Стиль: официальный, деловой, уважительный.",
    "friendly": "Стиль: дружелюбный, живой, на «ты».",
}


def is_configured():
    """AI готов к работе только если задан ключ."""
    return bool(getattr(config, "AI_API_KEY", ""))


async def personalize(text, style="sell"):
    """Возвращает улучшенный текст или None (если AI не настроен/недоступен)."""
    if not text or not text.strip():
        return None
    if not is_configured():
        return None
    hint = STYLE_HINTS.get(style, STYLE_HINTS["sell"])
    payload = {
        "model": config.AI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + " " + hint},
            {"role": "user", "content": text},
        ],
        "temperature": 0.8,
    }
    headers = {
        "Authorization": "Bearer %s" % config.AI_API_KEY,
        "Content-Type": "application/json",
    }
    url = config.AI_BASE_URL.rstrip("/") + "/chat/completions"
    try:
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=payload, headers=headers) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                choices = data.get("choices") or []
                if not choices:
                    return None
                out = (choices[0].get("message") or {}).get("content") or ""
                out = out.strip()
                return out or None
    except Exception:
        return None
