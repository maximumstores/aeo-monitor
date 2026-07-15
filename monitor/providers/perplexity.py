# -*- coding: utf-8 -*-
import requests

from .. import config
from .base import SYSTEM_HINT, Citation, ProviderResult

NAME = "perplexity"
API_URL = "https://api.perplexity.ai/chat/completions"


def available():
    return bool(config.PERPLEXITY_API_KEY)


def ask(query):
    resp = requests.post(
        API_URL,
        headers={"Authorization": "Bearer " + config.PERPLEXITY_API_KEY},
        json={"model": config.PERPLEXITY_MODEL,
              "messages": [{"role": "system", "content": SYSTEM_HINT},
                           {"role": "user", "content": query}]},
        timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    citations, seen = [], set()
    for r in data.get("search_results") or []:
        url = r.get("url")
        if url and url not in seen:
            seen.add(url)
            citations.append(Citation(url=url, title=r.get("title")))
    for url in data.get("citations") or []:
        if url and url not in seen:
            seen.add(url)
            citations.append(Citation(url=url))
    return ProviderResult(text=text, citations=citations)
