# -*- coding: utf-8 -*-
"""Бесплатная AI-персонализация текста рассылки.

Работает с любым OpenAI-совместимым API. По умолчанию — Groq
(бесплатный и быстрый). Ключ кладётся в .env:  AI_API_KEY=gsk_...
Где взять бесплатно: https://console.groq.com  ->  API Keys  ->  Create API Key.
Можно подменить провайдера через AI_BASE_URL и AI_MODEL (см. config.py).

Важно: текст рассылки уходит в Telegram с parse_mode=HTML, поэтому AI обязан
возвращать готовую HTML-разметку Telegram (<b>, <i>, <u>, <s>, <blockquote>),
а НЕ markdown (никаких ** и __ — они отрендерятся как обычные звёздочки).
"""
import re
import random
import aiohttp
import config

SYSTEM_PROMPT = (
    "Ты — топовый маркетинговый редактор продающих Telegram-рассылок. "
    "Перепиши присланный текст так, чтобы он стал максимально привлекательным, "
    "цепляющим и красиво оформленным.\n"
    "ОБЯЗАТЕЛЬНО оформи результат HTML-разметкой Telegram для красоты:\n"
    "- <b>жирный</b> для главного и заголовка;\n"
    "- <i>курсив</i> для акцентов;\n"
    "- <u>подчёркивание</u> при необходимости;\n"
    "- <blockquote>цитата</blockquote> чтобы выделить оффер или выгоду.\n"
    "Добавь уместные эмодзи (в заголовке и в начале пунктов). "
    "Сделай структуру: цепляющий заголовок, короткие строки или пункты, "
    "и чёткий призыв к действию в конце.\n"
    "Сохрани исходный смысл и ЯЗЫК оригинала.\n"
    "Каждый раз выдавай ЗАМЕТНО НОВЫЙ вариант: другой заголовок, другие формулировки, "
    "другой порядок. Не повторяй предыдущий вариант.\n"
    "Верни ТОЛЬКО готовый текст рассылки в HTML. Без пояснений, без markdown "
    "(никаких ** или __), без тройных кавычек и без обрамляющих кавычек."
)

STYLE_HINTS = {
    "sell": "Стиль: продающий, цепляющий, с сильным призывом к действию.",
    "official": "Стиль: официальный, деловой, уважительный.",
    "friendly": "Стиль: дружелюбный, живой, на «ты», с лёгкими эмодзи.",
}


def is_configured():
    """AI готов к работе только если задан ключ."""
    return bool(getattr(config, "AI_API_KEY", ""))


def _clean(out):
    """Подчищаем ответ модели: убираем ```-ограждения и markdown-жирный/курсив,
    чтобы в Telegram HTML всё рендерилось корректно."""
    out = (out or "").strip()
    if out.startswith("```"):
        out = re.sub(r"^```[a-zA-Z]*\n?", "", out)
        out = re.sub(r"\n?```$", "", out).strip()
    # markdown -> HTML (на случай если модель всё же добавила ** или __)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.S)
    out = re.sub(r"__(.+?)__", r"<u>\1</u>", out, flags=re.S)
    return out.strip()


async def personalize(text, style="sell"):
    """Возвращает улучшенный HTML-текст или None (если AI не настроен/недоступен).

    Каждый вызов даёт новый вариант: высокая температура + случайный «нонс»,
    который заставляет модель не повторяться при «Перегенерировать».
    """
    if not text or not text.strip():
        return None
    if not is_configured():
        return None
    hint = STYLE_HINTS.get(style, STYLE_HINTS["sell"])
    nonce = random.randint(1000, 999999)
    payload = {
        "model": config.AI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + " " + hint},
            {"role": "user", "content": text},
            {"role": "system", "content": "Сгенерируй свежий вариант №%d, отличающийся от любых предыдущих." % nonce},
        ],
        "temperature": 1.0,
        "top_p": 0.95,
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
                out = _clean(out)
                return out or None
    except Exception:
        return None
