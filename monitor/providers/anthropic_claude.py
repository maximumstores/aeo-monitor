# -*- coding: utf-8 -*-
from .. import config
from .base import SYSTEM_HINT, Citation, ProviderResult

NAME = "claude"


def available():
    return bool(config.ANTHROPIC_API_KEY)


def ask(query):
    import anthropic

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=1500,
        system=SYSTEM_HINT,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        messages=[{"role": "user", "content": query}],
    )

    text_parts = []
    citations = []
    seen = set()

    for block in resp.content:
        btype = getattr(block, "type", "")
        if btype == "text":
            text_parts.append(block.text)
            for c in getattr(block, "citations", None) or []:
                url = getattr(c, "url", None)
                if url and url not in seen:
                    seen.add(url)
                    citations.append(Citation(url=url, title=getattr(c, "title", None)))
        elif btype == "web_search_tool_result":
            for r in getattr(block, "content", None) or []:
                url = getattr(r, "url", None)
                if url and url not in seen:
                    seen.add(url)
                    citations.append(Citation(url=url, title=getattr(r, "title", None)))

    return ProviderResult(text="\n".join(text_parts), citations=citations)
