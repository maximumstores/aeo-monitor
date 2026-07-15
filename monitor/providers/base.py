# -*- coding: utf-8 -*-
from dataclasses import dataclass, field


@dataclass
class Citation:
    url: str
    title: str = None


@dataclass
class ProviderResult:
    text: str
    citations: list = field(default_factory=list)


SYSTEM_HINT = ("You are a helpful shopping assistant. Answer the user's question directly, "
               "recommend specific brands and products by name.")
