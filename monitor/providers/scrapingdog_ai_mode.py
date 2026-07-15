# -*- coding: utf-8 -*-
"""Google AI Mode через ScrapingDog. Реальный контракт API (проверено вживую):
GET https://api.scrapingdog.com/google/ai_mode?api_key=...&query=...&country=us
Ответ: {"markdown": "...", "text_blocks": [{"type": "paragraph"/"heading"/"list", ...}],
        "references": [{"link","title","snippet","source",...}], ...}
"""
import requests

from .. import config
from .base import Citation, ProviderResult

NAME = "google_ai_mode"
API_URL = "https://api.scrapingdog.com/google/ai_mode"


def available():
    return bool(config.SCRAPINGDOG_API_KEY)


def _flatten_text_blocks(blocks):
    """Фолбэк, если markdown вдруг пуст: собираем текст из text_blocks вручную."""
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


def ask(query):
    resp = requests.get(
        API_URL,
        params={"api_key": config.SCRAPINGDOG_API_KEY, "query": query, "country": "us"},
        timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()

    text = data.get("markdown") or _flatten_text_blocks(data.get("text_blocks"))

    citations = []
    seen = set()
    for ref in data.get("references") or []:
        link = ref.get("link")
        if not link or link in seen:
            continue
        seen.add(link)
        citations.append(Citation(url=link, title=ref.get("title")))

    return ProviderResult(text=text, citations=citations)
