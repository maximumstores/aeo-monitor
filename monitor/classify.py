# -*- coding: utf-8 -*-
"""Классификация домена цитаты: own_site / social / marketplace / third_party."""
from urllib.parse import urlparse

SOCIAL = (
    "reddit.com", "youtube.com", "youtu.be", "x.com", "twitter.com",
    "instagram.com", "tiktok.com", "facebook.com", "pinterest.com",
    "quora.com", "medium.com", "linkedin.com",
)
MARKETPLACE = (
    "amazon.", "ebay.", "walmart.com", "etsy.com", "aliexpress.",
    "target.com", "rei.com", "backcountry.com",
)


def extract_domain(url: str, title: str | None = None) -> str:
    """Домен из URL. Для Gemini grounding-редиректов домен лежит в title."""
    netloc = urlparse(url).netloc.lower().removeprefix("www.")
    if "grounding-api-redirect" in url or "vertexaisearch" in netloc:
        if title and "." in title and " " not in title.strip():
            return title.strip().lower().removeprefix("www.")
    return netloc


def source_type(domain: str, our_domains: list[str]) -> str:
    d = domain.lower()
    if any(d == od or d.endswith("." + od) for od in our_domains):
        return "own_site"
    if any(s in d for s in SOCIAL):
        return "social"
    if any(m in d for m in MARKETPLACE):
        return "marketplace"
    return "third_party"
