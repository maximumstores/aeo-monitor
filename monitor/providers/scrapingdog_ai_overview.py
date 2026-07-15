# -*- coding: utf-8 -*-
"""Google AI Overview через ScrapingDog — блок ИИ-ответа прямо в обычной выдаче Google
(в отличие от AI Mode, который отдельный режим). Показывается не на все запросы.

Контракт (проверено по docs.scrapingdog.com):
1) GET https://api.scrapingdog.com/google/?api_key=...&query=...  — обычный поиск,
   в ответе может быть ключ "ai_overview".
2) Если ai_overview уже содержит text_blocks/references — используем сразу (частый случай).
3) Если нет — там лежит одноразовая ссылка (scrapingdog_link или url), которую нужно
   дёрнуть вторым запросом в течение ~60-120 секунд, чтобы получить сам Overview.
"""
import requests

from .. import config
from .base import Citation, ProviderResult

NAME = "google_ai_overview"
SEARCH_URL = "https://api.scrapingdog.com/google/"
OVERVIEW_URL = "https://api.scrapingdog.com/google/ai_overview"


def available():
    return bool(config.SCRAPINGDOG_API_KEY)


def _flatten(blocks):
    parts = []
    for b in blocks or []:
        btype = b.get("type")
        if btype == "paragraph":
            snippet = b.get("snippet", "")
            if snippet:
                parts.append(snippet)
        elif btype == "heading":
            text = b.get("text", "")
            if text:
                parts.append(text)
        elif btype == "list":
            for item in b.get("list", []):
                snippet = item.get("snippet", "")
                if snippet:
                    parts.append(f"- {snippet}")
    return "\n".join(parts)


def _citations_from_refs(refs):
    citations, seen = [], set()
    for ref in refs or []:
        link = ref.get("link")
        if not link or link in seen:
            continue
        seen.add(link)
        citations.append(Citation(url=link, title=ref.get("title")))
    return citations


def ask(query):
    resp = requests.get(
        SEARCH_URL,
        params={"api_key": config.SCRAPINGDOG_API_KEY, "query": query, "country": "us"},
        timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()

    ov = data.get("ai_overview")
    if not ov:
        # Google просто не показал AI Overview на этот запрос — валидный результат, не ошибка.
        return ProviderResult(text="", citations=[])

    if ov.get("text_blocks") or ov.get("references"):
        blocks, refs = ov.get("text_blocks"), ov.get("references")
    else:
        # Нужен второй запрос по одноразовой ссылке
        follow_link = ov.get("scrapingdog_link")
        if follow_link:
            resp2 = requests.get(follow_link, timeout=60)
        else:
            url_param = ov.get("url")
            if not url_param:
                return ProviderResult(text="", citations=[])
            resp2 = requests.get(
                OVERVIEW_URL,
                params={"api_key": config.SCRAPINGDOG_API_KEY, "url": url_param},
                timeout=60,
            )
        resp2.raise_for_status()
        data2 = resp2.json()
        ov2 = data2.get("ai_overview", data2)
        blocks, refs = ov2.get("text_blocks"), ov2.get("references")

    text = _flatten(blocks)
    citations = _citations_from_refs(refs)
    return ProviderResult(text=text, citations=citations)
