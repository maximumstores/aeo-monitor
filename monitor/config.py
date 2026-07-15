# -*- coding: utf-8 -*-
"""Загрузка конфига ниши (queries.yaml) и окружения."""
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.getenv("AEO_CONFIG", ROOT / "queries.yaml"))

DATABASE_URL = os.getenv("DATABASE_URL", "")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY", "")
VERTEX_SA_JSON_B64 = os.getenv("VERTEX_SA_JSON_B64", "")
VERTEX_PROJECT = os.getenv("VERTEX_PROJECT", "")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
SCRAPINGDOG_API_KEY = os.getenv("SCRAPINGDOG_API_KEY", "")

SLEEP_BETWEEN_CALLS = float(os.getenv("AEO_SLEEP", "2"))


@dataclass
class NicheConfig:
    our_domains: list[str] = field(default_factory=list)
    ours: list[str] = field(default_factory=list)
    competitors: list[str] = field(default_factory=list)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    queries: list[dict] = field(default_factory=list)

    @property
    def all_brands(self) -> list[str]:
        return self.ours + self.competitors

    def brand_aliases(self, brand: str) -> list[str]:
        return [a.lower() for a in self.aliases.get(brand, [])] or [brand.lower()]


def load_config(path: Path = CONFIG_PATH) -> NicheConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return NicheConfig(
        our_domains=[d.lower() for d in data.get("our_domains", [])],
        ours=data.get("brands", {}).get("ours", []),
        competitors=data.get("brands", {}).get("competitors", []),
        aliases=data.get("aliases", {}),
        queries=data.get("queries", []),
    )


def week_start(today: date | None = None) -> date:
    """Понедельник текущей недели — ключ прогона."""
    d = today or date.today()
    return d - timedelta(days=d.weekday())
