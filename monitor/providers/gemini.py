# -*- coding: utf-8 -*-
import base64
import json

from .. import config
from .base import Citation, ProviderResult

NAME = "gemini"


def available():
    return bool(config.GEMINI_API_KEY or config.VERTEX_SA_JSON_B64)


def _client():
    from google import genai

    if config.GEMINI_API_KEY:
        return genai.Client(api_key=config.GEMINI_API_KEY)

    from google.oauth2 import service_account

    info = json.loads(base64.b64decode(config.VERTEX_SA_JSON_B64))
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    project = config.VERTEX_PROJECT or info.get("project_id")
    return genai.Client(vertexai=True, project=project, location=config.VERTEX_LOCATION, credentials=creds)


def ask(query):
    from google.genai import types

    client = _client()
    resp = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=query,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )
    text = resp.text or ""

    citations = []
    try:
        gm = resp.candidates[0].grounding_metadata
        for ch in (gm.grounding_chunks or []):
            web = getattr(ch, "web", None)
            if web and web.uri:
                citations.append(Citation(url=web.uri, title=getattr(web, "title", None)))
    except (AttributeError, IndexError, TypeError):
        pass
    return ProviderResult(text=text, citations=citations)
