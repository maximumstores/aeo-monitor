# -*- coding: utf-8 -*-
from .. import config
from .base import SYSTEM_HINT, Citation, ProviderResult

NAME = "openai"


def available():
    return bool(config.OPENAI_API_KEY)


def ask(query):
    from openai import OpenAI

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    try:
        resp = client.responses.create(
            model=config.OPENAI_MODEL,
            tools=[{"type": "web_search"}],
            instructions=SYSTEM_HINT,
            input=query,
        )
        text_parts, citations = [], []
        for item in resp.output:
            if getattr(item, "type", "") != "message":
                continue
            for c in getattr(item, "content", []) or []:
                if getattr(c, "type", "") == "output_text":
                    text_parts.append(c.text)
                    for a in getattr(c, "annotations", []) or []:
                        if getattr(a, "type", "") == "url_citation":
                            citations.append(Citation(url=a.url, title=getattr(a, "title", None)))
        return ProviderResult(text="\n".join(text_parts), citations=citations)
    except Exception:
        chat = client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[{"role": "system", "content": SYSTEM_HINT},
                      {"role": "user", "content": query}],
        )
        return ProviderResult(text=chat.choices[0].message.content or "")
