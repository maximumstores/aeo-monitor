# -*- coding: utf-8 -*-
# AEO Radar — дашборд, всё в одном файле. Секрет: DATABASE_URL.
import hashlib
import os
import re
from collections import defaultdict
from pathlib import Path

try:
    import crawler_check
    HAS_CRAWLER_CHECK = True
except ImportError:
    HAS_CRAWLER_CHECK = False

import psycopg2
import psycopg2.extras
import streamlit as st

DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    try:
        DATABASE_URL = st.secrets["DATABASE_URL"]
    except Exception:
        pass

st.set_page_config(page_title="AEO Radar", page_icon="◎", layout="wide")

if not DATABASE_URL:
    st.error("DATABASE_URL не найден в Secrets")
    st.stop()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
if not ANTHROPIC_API_KEY:
    try:
        ANTHROPIC_API_KEY = st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        pass

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
VERTEX_SA_JSON_B64 = os.getenv("VERTEX_SA_JSON_B64", "")
VERTEX_PROJECT = os.getenv("VERTEX_PROJECT", "")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
for _k in ("GEMINI_API_KEY", "VERTEX_SA_JSON_B64", "VERTEX_PROJECT", "VERTEX_LOCATION"):
    if not globals()[_k]:
        try:
            globals()[_k] = st.secrets[_k]
        except Exception:
            pass

DDL = """
CREATE SCHEMA IF NOT EXISTS aeo;
CREATE TABLE IF NOT EXISTS aeo.responses (
    week_start date NOT NULL, query_id text NOT NULL, provider text NOT NULL,
    query_text text NOT NULL, response_text text,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week_start, query_id, provider));
CREATE TABLE IF NOT EXISTS aeo.mentions (
    week_start date NOT NULL, query_id text NOT NULL, provider text NOT NULL,
    brand text NOT NULL, is_ours boolean NOT NULL DEFAULT false,
    mentioned boolean NOT NULL DEFAULT false, first_position int,
    mention_count int NOT NULL DEFAULT 0,
    PRIMARY KEY (week_start, query_id, provider, brand));
CREATE TABLE IF NOT EXISTS aeo.citations (
    week_start date NOT NULL, query_id text NOT NULL, provider text NOT NULL,
    url text NOT NULL, domain text NOT NULL, position int,
    is_ours boolean NOT NULL DEFAULT false,
    source_type text NOT NULL DEFAULT 'third_party', title text,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week_start, query_id, provider, url));
CREATE TABLE IF NOT EXISTS aeo.brand_candidates (
    week_start date NOT NULL,
    brand text NOT NULL,
    mention_count int NOT NULL DEFAULT 1,
    status text NOT NULL DEFAULT 'new',
    PRIMARY KEY (week_start, brand));
CREATE TABLE IF NOT EXISTS aeo.experiments (
    id serial PRIMARY KEY,
    started_at date NOT NULL DEFAULT current_date,
    description text NOT NULL,
    query_id text,
    url text,
    created_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE aeo.experiments ADD COLUMN IF NOT EXISTS problem text;
ALTER TABLE aeo.experiments ADD COLUMN IF NOT EXISTS hypothesis text;
ALTER TABLE aeo.experiments ADD COLUMN IF NOT EXISTS baseline_sov numeric;
CREATE TABLE IF NOT EXISTS aeo.ai_insights (
    week_start date NOT NULL,
    provider text NOT NULL DEFAULT 'all',
    content text NOT NULL,
    generated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week_start, provider));
ALTER TABLE aeo.ai_insights ADD COLUMN IF NOT EXISTS data_hash text;

CREATE TABLE IF NOT EXISTS aeo.factcheck_flags (
    week_start date NOT NULL,
    query_id text NOT NULL,
    provider text NOT NULL,
    claim text NOT NULL,
    fact text NOT NULL,
    mismatch boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS aeo.ai_traffic (
    week_start date NOT NULL PRIMARY KEY,
    ai_sessions int,
    ai_orders int,
    ai_revenue numeric,
    note text,
    updated_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS aeo.site_audits (
    url text NOT NULL PRIMARY KEY,
    score int,
    content text NOT NULL,
    content_hash text NOT NULL,
    generated_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS aeo.site_score_history (
    checked_at date NOT NULL DEFAULT current_date,
    url text NOT NULL,
    crawler_score int,
    audit_score int,
    PRIMARY KEY (checked_at, url));

-- Таблицы краулер-чека создаются штатно самим crawler_check.py при запуске
-- (--save), но дашборд не должен зависеть от того, когда сервер его прогонит
-- в следующий раз — поэтому гарантируем ту же схему и здесь, идемпотентно.
CREATE TABLE IF NOT EXISTS aeo.crawler_scores (
    url            text PRIMARY KEY,
    page_status    text,
    score          int,
    bots_ok        int NOT NULL DEFAULT 0,
    bots_blocked   int NOT NULL DEFAULT 0,
    bots_unknown   int NOT NULL DEFAULT 0,
    html_bytes     int,
    text_bytes     int,
    content_ratio_pct numeric,
    jsonld_blocks  int,
    summary        text,
    checked_at     timestamptz NOT NULL DEFAULT now());
ALTER TABLE aeo.crawler_scores ADD COLUMN IF NOT EXISTS script_bytes int;
ALTER TABLE aeo.crawler_scores ADD COLUMN IF NOT EXISTS style_bytes int;
ALTER TABLE aeo.crawler_scores ADD COLUMN IF NOT EXISTS markup_bytes int;

CREATE TABLE IF NOT EXISTS aeo.crawler_access (
    url            text NOT NULL,
    bot            text NOT NULL,
    verdict        text NOT NULL,
    is_critical    boolean NOT NULL DEFAULT false,
    category       text,
    robots_verdict text NOT NULL,
    http_status    int,
    blocked_by     text,
    content_bytes  int,
    content_delta_pct numeric,
    detail         text,
    checked_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (url, bot));
"""
with psycopg2.connect(DATABASE_URL) as _c, _c.cursor() as _cur:
    _cur.execute(DDL)
    _c.commit()

NICHE, N_QUERIES = "merino.tech", 16
ALIASES = [NICHE]
COMPETITORS = []
try:
    import yaml
    _cfg = yaml.safe_load(Path("queries.yaml").read_text(encoding="utf-8"))
    NICHE = (_cfg.get("brands", {}).get("ours") or [NICHE])[0]
    N_QUERIES = len(_cfg.get("queries", [])) or N_QUERIES
    ALIASES = _cfg.get("aliases", {}).get(NICHE, [NICHE]) or [NICHE]
    COMPETITORS = _cfg.get("brands", {}).get("competitors", [])
except Exception:
    pass

def highlight_brand(text, aliases):
    if not text or not aliases:
        return text
    pattern = "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
    return re.sub(
        f"({pattern})",
        r'<mark style="background:#FFE58A;color:#1A2233;padding:1px 3px;'
        r'border-radius:3px;font-weight:700">\1</mark>',
        text, flags=re.IGNORECASE,
    )

def favicon(domain):
    return f'https://www.google.com/s2/favicons?domain={domain}&sz=32'


def source_row(domain, url, source_type, is_ours, title=None, mine=False):
    """Единая карточка источника: favicon + домен-ссылка + заголовок + тип."""
    t = (title or "")[:80]
    subtitle = f'<div style="font-size:11px;color:{"#8A6D00" if mine else "#98A2B5"};font-weight:400;white-space:normal;line-height:1.3;margin-top:1px">{t}</div>' if t else ""
    if mine:
        return (f'<div class="donor" style="background:#FFF6D9;border-radius:6px;padding:7px 9px;margin:3px 0;'
                f'border:1px solid #FFE58A;align-items:flex-start">'
                f'<img src="{favicon(domain)}" width="16" height="16" style="margin-top:2px;border-radius:3px;flex-shrink:0">'
                f'<a href="{url}" target="_blank" rel="noopener" style="color:#8A6D00;font-weight:700;display:block;flex:1">'
                f'★ {domain} — это мы{subtitle}</a></div>')
    return (f'<div class="donor" style="align-items:flex-start">'
            f'<img src="{favicon(domain)}" width="16" height="16" style="margin-top:2px;border-radius:3px;flex-shrink:0">'
            f'<a href="{url}" target="_blank" rel="noopener" style="display:block;flex:1">'
            f'{domain}{subtitle}</a>'
            f'<span class="stype">{CH_LAB.get(source_type, source_type)}</span></div>')

@st.cache_data(ttl=600)
def _rows(sql: str, params=()):
    with psycopg2.connect(DATABASE_URL) as conn, \
         conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

def weeks():
    return [r["week_start"] for r in _rows(
        "SELECT DISTINCT week_start FROM aeo.mentions ORDER BY week_start")]

def providers_in_week(week):
    return [r["provider"] for r in _rows(
        "SELECT DISTINCT provider FROM aeo.mentions WHERE week_start=%s ORDER BY provider", (week,))]

def _prov_clause(provider):
    return ("AND provider=%s", (provider,)) if provider else ("", ())

def sov_by_brand(week, provider=None):
    pc, pp = _prov_clause(provider)
    return _rows(f"""SELECT brand, bool_or(is_ours) AS is_ours,
        round(100.0*sum(mentioned::int)/count(*),1) AS sov,
        round(avg(first_position) FILTER (WHERE mentioned),1) AS avg_pos
        FROM aeo.mentions WHERE week_start=%s {pc} GROUP BY brand ORDER BY sov DESC""", (week, *pp))

def sov_by_brand_provider(week):
    return {(r["brand"], r["provider"]): float(r["sov"]) for r in _rows(
        """SELECT brand, provider, round(100.0*sum(mentioned::int)/count(*),0) AS sov
           FROM aeo.mentions WHERE week_start=%s GROUP BY brand, provider""", (week,))}

def sov_trend(provider=None):
    pc, pp = _prov_clause(provider)
    return _rows(f"""SELECT week_start, brand,
        round(100.0*sum(mentioned::int)/count(*),1) AS sov
        FROM aeo.mentions WHERE true {pc} GROUP BY week_start, brand ORDER BY week_start""", pp)

def channel_shares(week, provider=None):
    pc, pp = _prov_clause(provider)
    return _rows(f"""SELECT source_type, count(*) AS n,
        round(100.0*count(*)/sum(count(*)) OVER (),0) AS pct
        FROM aeo.citations WHERE week_start=%s {pc} GROUP BY source_type ORDER BY n DESC""", (week, *pp))

def own_citation_share(week, provider=None):
    pc, pp = _prov_clause(provider)
    r = _rows(f"""SELECT round(100.0*sum(is_ours::int)/greatest(count(*),1),1) AS pct
                 FROM aeo.citations WHERE week_start=%s {pc}""", (week, *pp))
    return float(r[0]["pct"]) if r and r[0]["pct"] is not None else 0.0

def top_donors(week, provider=None, limit=6):
    pc, pp = _prov_clause(provider)
    return _rows(f"""SELECT domain, bool_or(is_ours) AS is_ours, count(*) AS n,
        (array_agg(url ORDER BY position))[1] AS sample_url,
        (array_agg(title ORDER BY position))[1] AS sample_title
        FROM aeo.citations WHERE week_start=%s {pc} GROUP BY domain
        ORDER BY n DESC LIMIT %s""", (week, *pp, limit))

def lost_own_urls(week, prev, provider=None):
    pc, pp = _prov_clause(provider)
    pc2 = pc.replace("provider", "cur.provider") if pc else ""
    return _rows(f"""SELECT DISTINCT url FROM aeo.citations cur
        WHERE week_start=%s AND is_ours {pc2} AND url NOT IN
          (SELECT url FROM aeo.citations WHERE week_start=%s AND is_ours) LIMIT 5""",
        (week, *pp, prev))

def our_query_matrix(week, provider=None):
    pc, pp = _prov_clause(provider)
    pc2 = pc.replace("provider", "m.provider") if pc else ""
    return _rows(f"""SELECT m.query_id, r.query_text, m.provider, m.mentioned
        FROM aeo.mentions m
        JOIN aeo.responses r USING (week_start, query_id, provider)
        WHERE m.week_start=%s AND m.is_ours {pc2}
        ORDER BY m.query_id, m.provider""", (week, *pp))

def our_mentions_detail(week, provider=None):
    pc, pp = _prov_clause(provider)
    pc2 = pc.replace("provider", "m.provider") if pc else ""
    return _rows(f"""SELECT m.query_id, m.provider, m.first_position, m.mention_count,
        r.query_text, r.response_text
        FROM aeo.mentions m
        JOIN aeo.responses r USING (week_start, query_id, provider)
        WHERE m.week_start=%s AND m.is_ours AND m.mentioned {pc2}
        ORDER BY m.first_position, m.query_id""", (week, *pp))

def citations_for(week, query_id, provider):
    return _rows("""SELECT url, domain, source_type, is_ours, title
        FROM aeo.citations
        WHERE week_start=%s AND query_id=%s AND provider=%s
        ORDER BY position""", (week, query_id, provider))

def all_responses(week, provider=None):
    pc, pp = _prov_clause(provider)
    return _rows(f"""SELECT query_id, provider, query_text, response_text
                    FROM aeo.responses WHERE week_start=%s {pc} ORDER BY query_id, provider""", (week, *pp))

def brand_candidates(week):
    return _rows("""SELECT brand, mention_count FROM aeo.brand_candidates
        WHERE week_start=%s ORDER BY mention_count DESC""", (week,))

def factcheck_flags(week):
    return _rows("""SELECT query_id, provider, claim, fact, mismatch
        FROM aeo.factcheck_flags WHERE week_start=%s ORDER BY mismatch DESC, query_id""", (week,))

def ai_traffic_row(week):
    r = _rows("SELECT ai_sessions, ai_orders, ai_revenue, note FROM aeo.ai_traffic WHERE week_start=%s", (week,))
    return r[0] if r else None

def ai_traffic_trend():
    return _rows("SELECT week_start, ai_sessions, ai_orders, ai_revenue FROM aeo.ai_traffic ORDER BY week_start")

def save_ai_traffic(week, sessions, orders, revenue, note):
    with psycopg2.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO aeo.ai_traffic (week_start, ai_sessions, ai_orders, ai_revenue, note, updated_at)
            VALUES (%s,%s,%s,%s,%s,now())
            ON CONFLICT (week_start) DO UPDATE SET
              ai_sessions=EXCLUDED.ai_sessions, ai_orders=EXCLUDED.ai_orders,
              ai_revenue=EXCLUDED.ai_revenue, note=EXCLUDED.note, updated_at=now()""",
            (week, sessions, orders, revenue, note))
        conn.commit()
    _rows.clear()

def get_cached_audit(url):
    r = _rows("SELECT score, content, content_hash, generated_at FROM aeo.site_audits WHERE url=%s", (url,))
    return r[0] if r else None

def save_audit(url, score, content_json, content_hash):
    with psycopg2.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO aeo.site_audits (url, score, content, content_hash, generated_at)
            VALUES (%s,%s,%s,%s,now())
            ON CONFLICT (url) DO UPDATE SET
              score=EXCLUDED.score, content=EXCLUDED.content,
              content_hash=EXCLUDED.content_hash, generated_at=now()""",
            (url, score, content_json, content_hash))
        conn.commit()
    _rows.clear()

def log_site_score(url, crawler_score=None, audit_score=None):
    """Пишет сегодняшнюю оценку в историю нашего сайта. Если сегодня уже была
    запись с другим чеком — не затирает его COALESCE'ом."""
    with psycopg2.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO aeo.site_score_history (checked_at, url, crawler_score, audit_score)
            VALUES (current_date, %s, %s, %s)
            ON CONFLICT (checked_at, url) DO UPDATE SET
              crawler_score = COALESCE(EXCLUDED.crawler_score, aeo.site_score_history.crawler_score),
              audit_score   = COALESCE(EXCLUDED.audit_score, aeo.site_score_history.audit_score)""",
            (url, crawler_score, audit_score))
        conn.commit()
    _rows.clear()

def site_score_trend(url, limit=12):
    return _rows("""SELECT checked_at, crawler_score, audit_score
        FROM aeo.site_score_history WHERE url=%s
        ORDER BY checked_at DESC LIMIT %s""", (url, limit))[::-1]

def crawler_score_row(url):
    r = _rows("""SELECT page_status, score, bots_ok, bots_blocked, bots_unknown,
        html_bytes, text_bytes, content_ratio_pct, jsonld_blocks,
        script_bytes, style_bytes, markup_bytes, summary, checked_at
        FROM aeo.crawler_scores WHERE url=%s""", (url,))
    return r[0] if r else None

def crawler_bots_rows(url):
    return _rows("""SELECT bot, category, verdict, is_critical, blocked_by, detail, content_delta_pct
        FROM aeo.crawler_access WHERE url=%s
        ORDER BY category, is_critical DESC, bot""", (url,))

def run_crawler_check_live(url):
    report = crawler_check.check_url(url)
    with psycopg2.connect(DATABASE_URL) as conn:
        crawler_check.save_report(conn, report)
    _rows.clear()
    return report

def list_experiments():
    return _rows("SELECT id, started_at, description, query_id, url FROM aeo.experiments ORDER BY started_at DESC")

def log_experiment(started_at, problem, hypothesis, action, query_id, url, baseline_sov):
    with psycopg2.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO aeo.experiments
               (started_at, description, problem, hypothesis, query_id, url, baseline_sov)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (started_at, action, problem or None, hypothesis or None,
             query_id or None, url or None, baseline_sov))
        conn.commit()
    _rows.clear()


def list_experiments():
    return _rows("""SELECT id, started_at, description, problem, hypothesis, query_id, url, baseline_sov
                     FROM aeo.experiments ORDER BY started_at DESC""")


def experiment_effect_at(started_at, query_id, weeks_after):
    """SOV (доля упоминаний нас) в конкретной неделе через N недель после старта эксперимента,
    и последний известный SOV до старта (baseline, если явно не передан)."""
    from datetime import timedelta
    target = started_at + timedelta(weeks=weeks_after)
    qc = "AND query_id=%s" if query_id else ""
    params = (query_id, target) if query_id else (target,)
    r = _rows(f"""SELECT week_start, round(100.0*sum(mentioned::int)/greatest(count(*),1),1) AS v
        FROM aeo.mentions WHERE is_ours {qc} AND week_start <= %s
        GROUP BY week_start ORDER BY week_start DESC LIMIT 1""", (*params[:-1], params[-1]) if query_id else (params[-1],))
    if not r or r[0]["v"] is None:
        return None, None
    return r[0]["week_start"], float(r[0]["v"])


def experiment_effect(started_at, query_id=None):
    """Совместимость: средний SOV ДО и ПОСЛЕ даты эксперимента (для простого случая)."""
    if query_id:
        before = _rows("""SELECT round(100.0*sum(mentioned::int)/greatest(count(*),1),1) AS v
            FROM aeo.mentions WHERE is_ours AND query_id=%s AND week_start < %s""", (query_id, started_at))
        after = _rows("""SELECT round(100.0*sum(mentioned::int)/greatest(count(*),1),1) AS v
            FROM aeo.mentions WHERE is_ours AND query_id=%s AND week_start >= %s""", (query_id, started_at))
    else:
        before = _rows("""SELECT round(100.0*sum(mentioned::int)/greatest(count(*),1),1) AS v
            FROM aeo.mentions WHERE is_ours AND week_start < %s""", (started_at,))
        after = _rows("""SELECT round(100.0*sum(mentioned::int)/greatest(count(*),1),1) AS v
            FROM aeo.mentions WHERE is_ours AND week_start >= %s""", (started_at,))
    b = before[0]["v"] if before and before[0]["v"] is not None else None
    a = after[0]["v"] if after and after[0]["v"] is not None else None
    return b, a

def get_cached_insight(week, provider_key):
    r = _rows("SELECT content, generated_at, data_hash FROM aeo.ai_insights WHERE week_start=%s AND provider=%s",
               (week, provider_key))
    return r[0] if r else None

def save_insight(week, provider_key, content, data_hash):
    with psycopg2.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO aeo.ai_insights (week_start, provider, content, generated_at, data_hash)
               VALUES (%s,%s,%s,now(),%s)
               ON CONFLICT (week_start, provider)
               DO UPDATE SET content=EXCLUDED.content, generated_at=now(), data_hash=EXCLUDED.data_hash""",
            (week, provider_key, content, data_hash))
        conn.commit()
    _rows.clear()

def build_ai_context(week, provider, brands, own_c, channels, donors, qmatrix, niche):
    """Строгий JSON-снапшот — Claude/Gemini обязаны использовать эти числа дословно, не пересчитывать."""
    import json as _json

    payload = {
        "niche": niche, "week": str(week), "provider_filter": provider or "все",
        "brands": [
            {"brand": b["brand"], "is_ours": b["is_ours"], "sov_pct": b["sov"], "avg_position": b["avg_pos"]}
            for b in brands[:8]
        ],
        "own_site_citation_share_pct": own_c,
        "channel_shares_pct": {c["source_type"]: c["pct"] for c in channels},
        "top_donors": [{"domain": d["domain"], "citations": d["n"], "is_ours": d["is_ours"]} for d in donors],
    }
    if qmatrix:
        grid = defaultdict(dict)
        qtexts = {}
        for r in qmatrix:
            grid[r["query_id"]][r["provider"]] = r["mentioned"]
            qtexts[r["query_id"]] = r["query_text"]
        zero = [qid for qid, per in grid.items() if not any(per.values())]
        payload["queries_with_zero_mentions"] = [
            {"query_id": qid, "query_text": qtexts[qid]} for qid in zero[:10]
        ]
        payload["queries_with_zero_mentions_count"] = len(zero)
        payload["queries_total_count"] = len(grid)

    def _json_default(o):
        try:
            from decimal import Decimal
            if isinstance(o, Decimal):
                return float(o)
        except ImportError:
            pass
        return str(o)

    return _json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)


def context_hash(context_text):
    return hashlib.sha256(context_text.encode("utf-8")).hexdigest()[:12]


def fetch_page_for_audit(url):
    """Скачивает страницу сама (без AI web_search — надёжнее и дешевле для конкретного URL).
    Возвращает сырые данные + то, что определяется кодом напрямую, без Claude:
    llms.txt, sitemap.xml, реальные @type из найденных JSON-LD блоков."""
    import re
    import requests
    from urllib.parse import urlparse

    resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (AEO-Radar-Audit)"})
    resp.raise_for_status()
    html = resp.text

    jsonld_pattern = "<script[^>]+type=[\"']application/ld\\+json[\"'][^>]*>(.*?)</script>"
    jsonld_blocks = re.findall(jsonld_pattern, html, flags=re.IGNORECASE | re.DOTALL)
    jsonld_types = sorted({t for b in jsonld_blocks for t in re.findall(r'"@type"\s*:\s*"([^"]+)"', b)})

    no_script = re.sub(r'<script.*?</script>', ' ', html, flags=re.IGNORECASE | re.DOTALL)
    no_style = re.sub(r'<style.*?</style>', ' ', no_script, flags=re.IGNORECASE | re.DOTALL)
    visible_text = re.sub(r'<[^>]+>', ' ', no_style)
    visible_text = re.sub(r'\s+', ' ', visible_text).strip()[:4000]

    domain = urlparse(url).scheme + "://" + urlparse(url).netloc

    try:
        r2 = requests.get(domain + "/llms.txt", timeout=10)
        llms_txt_status = "найден" if r2.status_code == 200 else f"не найден (код {r2.status_code})"
    except Exception:
        llms_txt_status = "не найден (ошибка запроса)"

    try:
        r3 = requests.get(domain + "/sitemap.xml", timeout=10)
        sitemap_status = "найден" if r3.status_code == 200 else f"не найден (код {r3.status_code})"
    except Exception:
        sitemap_status = "не найден (ошибка запроса)"

    return {
        "jsonld_blocks": jsonld_blocks[:5],
        "jsonld_types": jsonld_types,
        "visible_text": visible_text,
        "llms_txt_status": llms_txt_status,
        "sitemap_status": sitemap_status,
        "raw_len": len(html),
    }


def build_audit_prompt(url, extracted, catalog_facts):
    import json as _json
    facts_str = _json.dumps(catalog_facts, ensure_ascii=False) if catalog_facts else "не заданы"
    jsonld_str = "\n---\n".join(extracted["jsonld_blocks"]) if extracted["jsonld_blocks"] else "не найдено ни одного JSON-LD блока"
    types_str = ", ".join(extracted["jsonld_types"]) if extracted["jsonld_types"] else "ни одного @type не найдено"

    return f"""Ты — аудитор "agent readiness" (готовности страницы к машинному чтению AI-агентами).

URL: {url}
Реально найденные @type в JSON-LD на странице (не гадай, используй только эти): {types_str}
Найденные JSON-LD блоки на странице:
{jsonld_str}

Видимый текст страницы (первые 4000 симв.):
{extracted["visible_text"]}

Реальные факты о товаре (наш каталог, для сверки точности разметки):
{facts_str}

Проверь страницу по чек-листу agent readiness и ответь СТРОГО JSON (без markdown-обёртки).
Используй "@type" из списка выше как источник правды — не утверждай "missing", если нужный @type там есть.
{{
  "score": 0-100,
  "findings": [
    {{"check": "Product/Offer JSON-LD", "status": "ok"|"missing"|"mismatch", "detail": "..."}},
    {{"check": "Review/AggregateRating JSON-LD", "status": "...", "detail": "..."}},
    {{"check": "FAQPage разметка", "status": "...", "detail": "..."}},
    {{"check": "BreadcrumbList JSON-LD", "status": "...", "detail": "..."}},
    {{"check": "Organization/WebSite JSON-LD", "status": "...", "detail": "..."}},
    {{"check": "машиночитаемые атрибуты (GSM/состав/размеры) в тексте или JSON-LD", "status": "...", "detail": "..."}}
  ],
  "recommendations": ["конкретное действие 1", "конкретное действие 2"]
}}
Оцени только эти 6 пунктов (llms.txt и sitemap.xml проверяются отдельно кодом, не тобой — не включай их в findings).
Будь строг: если нужного @type нет в списке выше — статус "missing", не "ok"."""


def run_site_audit(url, catalog_facts, log=None):
    """log — необязательная функция(str), вызывается на каждом шаге для показа прогресса в UI."""
    def _log(msg):
        if log:
            log(msg)

    import json as _json

    _log("Скачиваю страницу и разбираю HTML...")
    extracted = fetch_page_for_audit(url)
    _log(f"Найдено JSON-LD блоков: {len(extracted['jsonld_blocks'])}"
         + (f", типы: {', '.join(extracted['jsonld_types'])}" if extracted['jsonld_types'] else " (пусто)"))
    _log(f"llms.txt: {extracted['llms_txt_status']}")
    _log(f"sitemap.xml: {extracted['sitemap_status']}")

    prompt = build_audit_prompt(url, extracted, catalog_facts)
    _log("Отправляю на анализ в Claude (6 смысловых проверок)...")

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-sonnet-4-5", max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    t = _strip_json_fence(raw)
    try:
        data = _json.loads(t)
    except _json.JSONDecodeError as e:
        raise ValueError(f"Claude вернул не-JSON при аудите: {raw[:1200]!r}") from e
    if not ("score" in data and "findings" in data):
        raise ValueError(f"В ответе аудита нет score/findings: {t[:400]!r}")

    # Две проверки добавляем сами, кодом — это факты, не мнение, спрашивать Claude не нужно.
    data["findings"].append({
        "check": "llms.txt", "status": "ok" if extracted["llms_txt_status"] == "найден" else "missing",
        "detail": f"Проверено напрямую кодом: {extracted['llms_txt_status']}",
    })
    data["findings"].append({
        "check": "sitemap.xml", "status": "ok" if extracted["sitemap_status"] == "найден" else "missing",
        "detail": f"Проверено напрямую кодом: {extracted['sitemap_status']}",
    })
    ok_count = sum(1 for f in data["findings"] if f.get("status") == "ok")
    data["score"] = round(100 * ok_count / len(data["findings"]))
    _log(f"Готово: {ok_count}/{len(data['findings'])} проверок пройдено, score {data['score']}/100")

    content_hash = hashlib.sha256(
        (extracted["visible_text"] + "".join(extracted["jsonld_blocks"])).encode("utf-8")
    ).hexdigest()[:12]
    return _json.dumps(data, ensure_ascii=False), data.get("score"), content_hash



PRIORITY_LABEL = {"fast_cheap": "быстро / дёшево", "medium": "средне", "slow_expensive": "долго / дорого"}
PRIORITY_COLOR = {"fast_cheap": ("#E1F5EC", "#12946A"), "medium": ("#FBF1DC", "#C07E14"),
                   "slow_expensive": ("#FBE9E4", "#D6452C")}


def render_ai_report(content_json, model_choice, choice):
    """Рендерит структурированный JSON-разбор карточками. Фолбэк на обычный markdown
    для legacy-записей, сохранённых до перехода на JSON-формат."""
    import json as _json
    try:
        data = _json.loads(content_json)
        situation, conclusion = data["situation"], data["conclusion"]
        actions = data.get("actions", [])
        sources = data.get("sources", [])
    except (_json.JSONDecodeError, KeyError, TypeError):
        st.markdown(f'<div class="card"><h2 class="sec">🤖 Разбор недели — {model_choice} · {choice}</h2>'
                    f'{content_json}</div>', unsafe_allow_html=True)
        return

    action_cards = ""
    for a in actions:
        bg, fg = PRIORITY_COLOR.get(a.get("priority"), ("#EEF1F6", "#5B6577"))
        label = PRIORITY_LABEL.get(a.get("priority"), a.get("priority", "—"))
        action_cards += (
            f'<div style="background:#FAFBFC;border:1px solid #E4E8F0;border-radius:10px;'
            f'padding:12px 14px;margin-bottom:8px">'
            f'<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px">'
            f'<b style="font-size:14px;color:#1A2233">{a.get("title","")}</b>'
            f'<span style="background:{bg};color:{fg};font-family:\'IBM Plex Mono\',monospace;'
            f'font-size:10.5px;font-weight:600;padding:2px 9px;border-radius:999px;white-space:nowrap">{label}</span>'
            f'</div><div style="font-size:13px;color:#4E5C53;margin-top:5px;line-height:1.5">{a.get("detail","")}</div>'
            f'</div>')

    sources_html = ""
    if sources:
        rows = "".join(
            f'<div class="crow" style="align-items:flex-start">'
            f'<img src="{favicon(src.get("domain",""))}" width="14" height="14" '
            f'style="margin-top:2px;border-radius:3px;flex-shrink:0">'
            f'<a href="{src.get("url","#")}" target="_blank" rel="noopener" '
            f'style="margin-left:8px;flex:1;font-size:12px;color:#3D5AFE;overflow:hidden;'
            f'text-overflow:ellipsis;white-space:nowrap">{src.get("domain","") or src.get("url","")}</a>'
            f'<span style="font-size:11px;color:#98A2B5;margin-left:8px;max-width:45%;text-align:right">{src.get("note","")}</span>'
            f'</div>'
            for src in sources)
        sources_html = f'<div class="lab" style="margin-top:14px">Источники</div>{rows}'

    st.markdown(
        f'<div class="card"><h2 class="sec">🤖 Разбор недели — {model_choice} · {choice}</h2>'
        f'<div style="margin-bottom:14px">'
        f'<div class="lab">Ситуация</div>'
        f'<div style="font-size:13.5px;color:#1A2233;line-height:1.6">{situation}</div>'
        f'</div>'
        f'<div style="margin-bottom:14px;padding:12px 14px;background:#F4F6FA;border-radius:10px;'
        f'border-left:3px solid #3D5AFE">'
        f'<div class="lab">Вывод</div>'
        f'<div style="font-size:13.5px;color:#1A2233;line-height:1.6">{conclusion}</div>'
        f'</div>'
        f'<div class="lab" style="margin-bottom:8px">Что делать</div>'
        f'{action_cards}'
        f'<p style="font-size:11.5px;color:#98A2B5;margin-top:6px">Эффект каждого действия проверяется через '
        f'⚗ Эксперименты после публикации, а не оценивается заранее.</p>'
        f'{sources_html}'
        f'</div>', unsafe_allow_html=True)

def _build_report_prompt(context_text):
    return f"""Ты — консультант по GEO/AEO-стратегии (видимость бренда в ответах AI-агентов).
Ниже JSON-снапшот недельного мониторинга бренда в ответах Gemini/Claude/ChatGPT/Google по покупательским запросам ниши.

{context_text}

СТРОГИЕ ПРАВИЛА ТОЧНОСТИ (обязательны, нарушение недопустимо):
1. Используй ТОЛЬКО числа, буквально присутствующие в JSON выше. Никогда не пересчитывай, не округляй иначе и не придумывай проценты, доли, счётчики цитат — копируй их из JSON дословно.
2. НЕ придумывай прогнозные числа: никаких "+X п.п. за Y недель", никаких сумм в долларах, никаких таймлайнов. У нас нет исторических данных для таких оценок.
3. Каждое действие должно ссылаться на конкретный домен или query_id ИЗ JSON выше — не изобретай домены или запросы, которых там нет.
4. Приоритет каждого действия — строго одно из трёх значений: "fast_cheap", "medium", "slow_expensive" (без чисел, только эта категория).

ОТВЕТЬ СТРОГО В ФОРМАТЕ JSON, без markdown-обёртки (без ```), без пояснений до или после. Схема:
{{
  "situation": "2-3 предложения с точными цифрами из JSON выше, без оценок",
  "conclusion": "2-3 предложения: что это значит для бизнеса и к чему ведёт статус-кво, без новых чисел",
  "actions": [
    {{"title": "короткий заголовок действия (3-6 слов)",
      "detail": "конкретика: что сделать, на каком домене/query_id, зачем",
      "priority": "fast_cheap"}}
  ]
}}

В "actions" — от 3 до 5 пунктов, отсортированных от fast_cheap к slow_expensive."""




def _strip_json_fence(raw):
    """Достаёт JSON из ответа модели, даже если перед ним есть пояснительный текст
    (Claude иногда пишет "Вот результат в JSON:" перед самим блоком) — ищем ```json
    обёртку в ЛЮБОМ месте текста, а не только в начале строки. Если обёртки нет
    вовсе — берём подстроку от первой { до последней }, самый частый запасной случай."""
    import re
    t = (raw or "").strip()

    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, flags=re.DOTALL)
    if fence:
        return fence.group(1).strip()

    if t.startswith("```"):
        t2 = t.strip("`")
        if t2.startswith("json"):
            t2 = t2[4:]
        return t2.strip()

    first, last = t.find("{"), t.rfind("}")
    if first != -1 and last != -1 and last > first:
        return t[first:last + 1]

    return t


def _extract_json(raw):
    """Для AI-разбора недели: схема situation/conclusion/actions."""
    import json as _json
    t = _strip_json_fence(raw)
    try:
        data = _json.loads(t)
    except _json.JSONDecodeError as e:
        raise ValueError(f"Claude вернул не-JSON: {raw[:1200]!r}") from e
    if not ("situation" in data and "conclusion" in data and "actions" in data):
        raise ValueError(f"В ответе нет situation/conclusion/actions: {t[:300]!r}")
    return data


def ai_analyze(context_text):
    import json as _json
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": _build_report_prompt(context_text)}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    data = _extract_json(raw)
    return _json.dumps(data, ensure_ascii=False)


def ai_analyze_gemini(context_text):
    import base64
    import json

    from google import genai
    from google.genai import types

    if GEMINI_API_KEY:
        client = genai.Client(api_key=GEMINI_API_KEY)
    else:
        from google.oauth2 import service_account

        info = json.loads(base64.b64decode(VERTEX_SA_JSON_B64))
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
        project = VERTEX_PROJECT or info.get("project_id")
        client = genai.Client(vertexai=True, project=project, location=VERTEX_LOCATION, credentials=creds)

    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=_build_report_prompt(context_text),
        config=types.GenerateContentConfig(),
    )
    raw = resp.text or ""
    data = _extract_json(raw)
    return json.dumps(data, ensure_ascii=False)


WEB_RESEARCH_LANGS = {"Русский": "Отвечай строго на русском языке.",
                       "English": "Answer strictly in English."}


def build_web_research_prompt(niche, competitors, lang_instruction):
    comp_str = ", ".join(competitors[:8]) if competitors else "конкуренты не заданы в конфиге"
    return f"""Ты — консультант по AEO/GEO (видимость бренда в ответах AI-агентов).
Бренд: {niche}. Известные конкуренты в нише: {comp_str}.

Используй web_search, чтобы найти АКТУАЛЬНУЮ информацию и ответить на четыре вопроса:
1. Семантический профиль: с какими характеристиками, ценовым сегментом, качеством независимые
   источники (обзорники, маркетплейсы, отзовики) связывают этот бренд?
2. Конкуренты: кто реально доминирует в независимых обзорах/гайдах по этой нише сейчас,
   и почему (больше площадок с тестами, больше консенсуса)?
3. Репутация: есть ли независимые оценки (Trustpilot и подобные), что там говорят —
   это отдельный сигнал от того, что показывает сам сайт бренда.
4. Пробелы: чего не хватает в публичном присутствии бренда, чтобы AI увереннее его рекомендовал?

СТРОГИЕ ПРАВИЛА:
1. Используй ТОЛЬКО факты, реально найденные через web_search — ничего не выдумывай.
   Если что-то не нашлось — не упоминай это, а не додумывай.
2. Каждое утверждение о цифрах (рейтинги, оценки) должно быть тем, что ты реально увидел в поиске.
3. Приоритет каждого действия — строго одно из: "fast_cheap", "medium", "slow_expensive".
4. {lang_instruction} Весь текст внутри JSON (situation/conclusion/title/detail) должен быть
   на этом языке, независимо от языка найденных источников.

ОТВЕТЬ СТРОГО В ФОРМАТЕ JSON (без markdown-обёртки), когда закончишь поиск:
{{
  "situation": "2-4 предложения: семантический профиль + конкурентная картина + репутация, с конкретными находками из поиска",
  "conclusion": "2-3 предложения: что это значит для видимости бренда в AI-ответах",
  "actions": [
    {{"title": "короткий заголовок (3-6 слов)",
      "detail": "конкретное действие на основе найденного пробела",
      "priority": "fast_cheap"}}
  ],
  "sources": [
    {{"domain": "example.com", "url": "https://example.com/полный-реальный-url-со-страницы-из-поиска",
      "note": "что именно оттуда взято (1 короткая фраза)"}}
  ]
}}
В "actions" — от 3 до 5 пунктов. В "sources" — от 5 до 10 пунктов: РЕАЛЬНЫЕ url-адреса страниц,
которые ты реально открыл через web_search (не выдумывай и не сокращай их, копируй как есть)."""


def ai_web_research(niche, competitors, lang="Русский"):
    import json as _json
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    lang_instruction = WEB_RESEARCH_LANGS.get(lang, WEB_RESEARCH_LANGS["Русский"])
    prompt = build_web_research_prompt(niche, competitors, lang_instruction)
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=4000,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}],
        messages=[{"role": "user", "content": prompt}],
    )
    text_parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    raw = "\n".join(text_parts)
    data = _extract_json(raw)
    return _json.dumps(data, ensure_ascii=False)


def get_web_research_cache(week):
    return get_cached_insight(week, "web_research")


def save_web_research_cache(week, content_json):
    save_insight(week, "web_research", content_json, "n/a")


AI_MODELS = {}
if ANTHROPIC_API_KEY:
    AI_MODELS["Claude"] = ("claude", ai_analyze)
if GEMINI_API_KEY or VERTEX_SA_JSON_B64:
    AI_MODELS["Gemini"] = ("gemini", ai_analyze_gemini)

st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@600;700;800&family=Golos+Text:wght@400;500&family=IBM+Plex+Mono:wght@400;500;600&display=swap');
.stApp{background:#F4F6FA}
#MainMenu,footer,header{visibility:hidden}
.block-container{padding-top:2rem;max-width:1160px;font-family:'Golos Text',sans-serif}
.aeo-logo{font-family:Manrope;font-weight:800;font-size:22px;color:#1A2233}
.aeo-logo span{color:#3D5AFE}
.aeo-meta{font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:#98A2B5;text-transform:uppercase;margin-top:2px}
.card{background:#FFF;border:1px solid #E4E8F0;border-radius:16px;padding:18px;flex:1;display:flex;flex-direction:column}
div[data-testid="stHorizontalBlock"]{align-items:stretch}
div[data-testid="column"]{display:flex}
div[data-testid="column"]>div{display:flex;flex-direction:column;width:100%}
div[data-testid="stVerticalBlock"]{width:100%}
.lab{font-size:12px;color:#98A2B5;font-weight:500;margin-bottom:6px}
.big{font-family:Manrope;font-weight:800;font-size:29px;color:#1A2233;letter-spacing:-.02em}
.delta{display:inline-block;font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;border-radius:999px;padding:2px 9px;margin-top:8px}
.delta.up{background:#E1F5EC;color:#12946A}.delta.dn{background:#FBE9E4;color:#D6452C}
h2.sec{font-family:Manrope;font-weight:700;font-size:15px;color:#1A2233;margin:0 0 10px}
.crow{display:flex;align-items:center;gap:10px;padding:5px 0;font-size:12.5px;color:#5B6577}
.crow .nm{width:128px}
.cbar{flex:1;height:9px;background:#EEF1F6;border-radius:5px;overflow:hidden;position:relative}
.cbar i{position:absolute;left:0;top:0;bottom:0;border-radius:5px}
.crow b{width:38px;text-align:right;font-family:'IBM Plex Mono',monospace;font-size:12px;color:#1A2233}
.donor{display:flex;align-items:center;gap:8px;padding:6px 0;font-family:'IBM Plex Mono',monospace;font-size:11.5px;border-top:1px dashed #E4E8F0}
.donor a{flex:1;color:#5B6577;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-decoration:none}
.donor a:hover{text-decoration:underline;color:#3D5AFE}
.donor.ours a{color:#12946A}
.donor b{font-weight:600;color:#1A2233}
.donor .stype{font-size:10px;color:#98A2B5;white-space:nowrap}
table.aeo{width:100%;border-collapse:collapse;font-size:13px}
table.aeo th{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:#98A2B5;text-align:left;padding:6px 8px;border-bottom:1.5px solid #1A2233}
table.aeo th.n,table.aeo td.n{text-align:right}
table.aeo td{padding:8px;border-bottom:1px solid #E4E8F0;color:#1A2233}
table.aeo td.n{font-family:'IBM Plex Mono',monospace;font-size:12.5px}
tr.ours td{background:#E1F5EC}
tr.ours td:first-child{font-weight:600;color:#12946A;border-radius:8px 0 0 8px}
tr.ours td:last-child{border-radius:0 8px 8px 0}
.up{color:#12946A}.dn{color:#D6452C}
.al{display:flex;gap:12px;padding:10px 0;border-top:1px solid #E4E8F0;font-size:13px;color:#1A2233;align-items:flex-start}
.badge{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;border-radius:999px;padding:2px 8px;white-space:nowrap;margin-top:2px}
.b-red{background:#FBE9E4;color:#D6452C}.b-amb{background:#FBF1DC;color:#C07E14}
svg text{font-family:'IBM Plex Mono',monospace;font-size:9.5px;fill:#98A2B5}
.legend{display:flex;gap:16px;font-size:12px;color:#5B6577;margin-top:8px;flex-wrap:wrap}
.legend i{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px}
.mtag{font-family:'IBM Plex Mono',monospace;font-size:11px;background:#E1F5EC;color:#12946A;padding:2px 8px;border-radius:999px;margin-right:8px}
</style>""", unsafe_allow_html=True)

P_SHORT = {"gemini":"GEM","openai":"GPT","perplexity":"PPLX","claude":"CLD","rufus":"RUF"}
P_FULL = {"gemini":"Gemini","openai":"ChatGPT","claude":"Claude","perplexity":"Perplexity","rufus":"Rufus"}
CH_LAB = {"own_site":"свой сайт","social":"соцсети","marketplace":"маркетплейс","third_party":"чужая статья"}
CH_COL = {"own_site":"#12946A","social":"#C07E14","marketplace":"#3D5AFE","third_party":"#98A2B5"}

def delta(cur, prev):
    return None if cur is None or prev is None else round(float(cur)-float(prev),1)

def dhtml(d):
    if d is None: return ""
    return f'<span class="delta {"up" if d>=0 else "dn"}">{d:+.1f} pp WoW</span>'

def kpi(label, value, d=None):
    st.markdown(f'<div class="card"><div class="lab">{label}</div><div class="big">{value}</div>{dhtml(d)}</div>',
                unsafe_allow_html=True)

wks = weeks()
st.markdown(f'<div class="aeo-logo">AEO<span>Radar</span></div>', unsafe_allow_html=True)
if not wks:
    st.info("Данных пока нет — запусти прогон: `python -m monitor.run`")
    st.stop()

week, prev = wks[-1], (wks[-2] if len(wks) > 1 else None)
all_provs = providers_in_week(week)

options = ["Все"] + [P_FULL.get(p, p.upper()) for p in all_provs]
choice = st.radio("Провайдер", options, horizontal=True, label_visibility="collapsed")
rev_map = {P_FULL.get(p, p.upper()): p for p in all_provs}
provider = rev_map.get(choice)

provs = [provider] if provider else all_provs

brands = sov_by_brand(week, provider)
prev_sov = {r["brand"]: float(r["sov"]) for r in sov_by_brand(prev, provider)} if prev else {}
for b in brands:
    b["delta"] = delta(b["sov"], prev_sov.get(b["brand"]))
per_prov = sov_by_brand_provider(week)
ours = next((b for b in brands if b["is_ours"]), None)
own_c = own_citation_share(week, provider)
own_c_prev = own_citation_share(prev, provider) if prev else None

st.markdown(f'<div class="aeo-meta">{NICHE} · {week} · {N_QUERIES} queries · '
            f'{choice.upper()} · weeks: {len(wks)}</div>', unsafe_allow_html=True)
st.write("")

c1, c2, c3, c4 = st.columns(4)
with c1: kpi("Share of Voice", f'{ours["sov"] if ours else 0}%',
             delta(ours["sov"], prev_sov.get(ours["brand"])) if ours else None)
with c2: kpi("Цитаты нашего сайта", f'{own_c}%', delta(own_c, own_c_prev))
with c3: kpi("Средняя позиция", (ours or {}).get("avg_pos") or "—")
with c4: kpi("Недель данных", len(wks))
st.write("")

# ── Веб-исследование бренда: живой поиск в сети (не по нашим данным, а по интернету) ──
# Открытая секция, не аккордеон — видна сразу, в самом начале дашборда.
st.markdown('<div class="lab" style="font-size:15px;font-weight:700;color:#1A2233;margin-bottom:2px">🌐 Веб-исследование бренда</div>', unsafe_allow_html=True)
st.caption("Claude ищет в интернете: как независимые обзорники и отзовики видят бренд, "
           "кто реально побеждает в нише сейчас, и какие есть репутационные пробелы. "
           "Это медленно меняющиеся вещи — кэшируется на неделю, не на каждый клик.")
wr_lang_col, wr_btn_col = st.columns([2, 3])
with wr_lang_col:
    wr_lang = st.radio("Язык ответа", list(WEB_RESEARCH_LANGS.keys()), horizontal=True,
                        key="wr_lang_choice", label_visibility="collapsed")
_wr_cached = get_web_research_cache(week)
with wr_btn_col:
    wr_clicked = st.button(
        "🌐 Обновить веб-исследование" if _wr_cached else "🌐 Запустить веб-исследование",
        key="web_research_btn")
if wr_clicked:
    if not ANTHROPIC_API_KEY:
        st.error("ANTHROPIC_API_KEY не найден в Secrets")
    else:
        with st.spinner("Ищу в интернете (может занять минуту — несколько запросов)..."):
            try:
                _wr_content = ai_web_research(NICHE, COMPETITORS, wr_lang)
                save_web_research_cache(week, _wr_content)
                _wr_cached = {"content": _wr_content}
            except Exception as e:
                st.error(f"Ошибка веб-исследования: {e}")
if _wr_cached:
    render_ai_report(_wr_cached["content"], "Claude + web search", "внешний контекст")
else:
    st.caption("Ещё не запускалось на этой неделе — нажми кнопку выше")
st.write("")

leader = brands[0] if brands else None
insights = []
if leader and ours and leader["brand"] != ours["brand"]:
    insights.append(f'Лидер ниши — <b>{leader["brand"]}</b> ({leader["sov"]}% SOV), мы отстаём на {round(leader["sov"]-ours["sov"],1)} п.п.')
if own_c < 5:
    insights.append(f'Own-site цитаты всего <b>{own_c}%</b> — AI почти не читает наш сайт напрямую')
top_d = top_donors(week, provider, limit=1)
if top_d:
    insights.append(f'Главный донор ниши — <b>{top_d[0]["domain"]}</b> ({top_d[0]["n"]} цитат)')
insight_html = "".join(f'<div style="padding:8px 0;border-top:1px solid #E4E8F0;font-size:13px;color:#1A2233">→ {t}</div>' for t in insights)

def _render_donors_card():
    rows = "".join(f'<div class="crow"><span class="nm">{CH_LAB.get(c["source_type"],c["source_type"])}</span>'
        f'<div class="cbar"><i style="width:{c["pct"]}%;background:{CH_COL.get(c["source_type"],"#98A2B5")}"></i></div>'
        f'<b>{c["pct"]}%</b></div>' for c in channel_shares(week, provider))
    donors = "".join(
        source_row(x["domain"], x["sample_url"], "own_site" if x["is_ours"] else "third_party",
                   x["is_ours"], x.get("sample_title"), mine=x["is_ours"]).replace(
            '</div>', f'<b style="margin-left:6px">{x["n"]}</b></div>', 1)
        for x in top_donors(week, provider))
    st.markdown(f'<div class="card"><h2 class="sec">Откуда AI берёт информацию</h2>{rows}'
        f'<div class="lab" style="margin-top:14px">Топ-доноры цитат (клик — открыть статью)</div>{donors}</div>', unsafe_allow_html=True)

if len(wks) < 2:
    st.markdown(f'<div class="card"><h2 class="sec">Выводы недели — {choice}</h2>'
        f'<p style="font-size:12.5px;color:#98A2B5;margin:0 0 4px">График тренда появится, когда накопится 2+ недели данных</p>'
        f'<div style="margin-top:8px">{insight_html}</div></div>', unsafe_allow_html=True)
else:
    trend = sov_trend(provider)
    series = ([(ours["brand"], "#12946A", 2.6)] if ours else []) + \
        [(b, ["#C6CCDA","#8FB4F9"][i], 1.5)
         for i, b in enumerate([x["brand"] for x in brands if not x["is_ours"]][:2])]
    W,H,X,Y = 560,110,30,20
    step = W/max(len(wks)-1,1)
    def pts(brand):
        vals = {r["week_start"]: float(r["sov"]) for r in trend if r["brand"]==brand}
        return " ".join(f"{X+i*step:.0f},{Y+H-(vals.get(w,0)/100)*H:.0f}" for i,w in enumerate(wks))
    lines = "".join(f'<polyline points="{pts(b)}" fill="none" stroke="{c}" stroke-width="{sw}" stroke-linecap="round"/>' for b,c,sw in series)
    lw = [wks[0], wks[len(wks)//2], wks[-1]] if len(wks) > 2 else wks
    labels = "".join(f'<text x="{X+i*(W/max(1,len(lw)-1))}" y="150">{w}</text>' for i,w in enumerate(lw))
    legend = "".join(f'<span><i style="background:{c}"></i>{b}</span>' for b,c,_ in series)

    st.markdown(f'<div class="card"><h2 class="sec">Тренд Share of Voice — {choice}</h2>'
        f'<svg viewBox="0 0 600 165" width="100%">'
        f'<line x1="30" y1="130" x2="590" y2="130" stroke="#E4E8F0"/>'
        f'<line x1="30" y1="75" x2="590" y2="75" stroke="#EEF1F6"/>'
        f'<text x="4" y="78">50</text><text x="12" y="133">0</text>'
        f'{lines}{labels}</svg><div class="legend">{legend}</div>'
        f'<div style="margin-top:16px">{insight_html}</div></div>', unsafe_allow_html=True)
st.write("")
_render_donors_card()
st.write("")

# ── Кого рекомендуют AI (полная ширина) ──
head = "".join(f'<th class="n">{P_SHORT.get(p, p[:4].upper())}</th>' for p in all_provs)
body = ""
for b in brands[:10]:
    dl = b["delta"]
    dcls = "up" if dl and dl>0 else "dn" if dl and dl<0 else ""
    pcells = "".join(f'<td class="n">{int(per_prov[(b["brand"],p)]) if (b["brand"],p) in per_prov else "·"}</td>' for p in all_provs)
    body += (f'<tr class="{"ours" if b["is_ours"] else ""}"><td>{b["brand"]}</td>'
             f'<td class="n">{b["sov"]}%</td>'
             f'<td class="n {dcls}">{f"{dl:+.0f}" if dl is not None else "—"}</td>'
             f'<td class="n">{b["avg_pos"] or "—"}</td>{pcells}</tr>')
st.markdown(f'<div class="card"><h2 class="sec">Кого рекомендуют AI — {choice}</h2>'
    f'<table class="aeo"><tr><th>Бренд</th><th class="n">SOV</th><th class="n">Δ</th>'
    f'<th class="n">поз.</th>{head}</tr>{body}</table></div>', unsafe_allow_html=True)
st.write("")

def _short_url(u, maxlen=70):
    base = u.split("?")[0]
    return base if len(base) <= maxlen else base[:maxlen] + "…"
alerts = [{"sev":"HI","text":f'Выпала наша цитата: <a href="{r["url"]}" target="_blank" rel="noopener">{_short_url(r["url"])}</a>'} for r in (lost_own_urls(week, prev, provider) if prev else [])]
alerts += [{"sev":"MD","text":f'{b["brand"]} +{b["delta"]} п.п. за неделю'}
           for b in brands if not b["is_ours"] and b["delta"] and b["delta"] >= 5]
dc = delta(own_c, own_c_prev)
if dc is not None and dc <= -3:
    alerts.append({"sev":"HI","text":f"Доля own-site цитат упала на {abs(dc)} п.п."})
items = "".join(f'<div class="al"><span class="badge {"b-red" if a["sev"]=="HI" else "b-amb"}">{a["sev"]}</span><p>{a["text"]}</p></div>'
    for a in alerts[:6]) or '<p style="color:#98A2B5;font-size:13px">Пока тихо — нужна вторая неделя данных для дельт.</p>'
st.markdown(f'<div class="card"><h2 class="sec">Требует внимания</h2>{items}</div>', unsafe_allow_html=True)

st.write("")
cands = brand_candidates(week)
if cands:
    rows = "".join(
        f'<div class="al"><span class="badge b-amb">NEW</span>'
        f'<p><b>{c["brand"]}</b> — упомянут {c["mention_count"]}× в ответах этой недели, '
        f'не в списке отслеживаемых брендов</p></div>'
        for c in cands)
    st.markdown(f'<div class="card"><h2 class="sec">AI заметил новые бренды ({len(cands)})</h2>{rows}'
        f'<p style="font-size:12px;color:#98A2B5;margin-top:8px">Если это реальный конкурент — добавь его '
        f'в queries.yaml → competitors, и со следующей недели он появится в общей таблице</p></div>',
        unsafe_allow_html=True)

st.write("")

mentions_detail = our_mentions_detail(week, provider)
st.markdown(f'<div class="card"><h2 class="sec">Где мы упоминаемся — {choice} ({len(mentions_detail)} случаев)</h2>'
            f'<p style="font-size:12.5px;color:#98A2B5;margin:0">Открой запрос — увидишь источники и текст ответа '
            f'с подсветкой каждого упоминания {NICHE}</p></div>', unsafe_allow_html=True)
if mentions_detail:
    for m in mentions_detail:
        label = (f'{P_SHORT.get(m["provider"], m["provider"].upper())} · позиция {m["first_position"]} · '
                 f'{m["query_id"]} — {m["query_text"][:70]}')
        with st.expander(label):
            cits = citations_for(week, m["query_id"], m["provider"])
            if cits:
                cits_sorted = sorted(cits, key=lambda c: not c["is_ours"])
                links = "".join(
                    source_row(c["domain"], c["url"], c["source_type"], c["is_ours"],
                               c.get("title"), mine=c["is_ours"])
                    for c in cits_sorted)
                st.markdown(f'<div class="lab">Источники этого ответа ({len(cits)})</div>{links}',
                            unsafe_allow_html=True)
            else:
                st.caption("Источники не зафиксированы для этого запроса")
            st.markdown(f'<div style="margin-top:10px"><span class="mtag">{m["mention_count"]}× упоминаний нас в тексте</span></div>',
                        unsafe_allow_html=True)
            st.markdown("**Текст ответа с подсветкой упоминаний:**")
            st.markdown(highlight_brand(m["response_text"], ALIASES), unsafe_allow_html=True)
else:
    st.info("В этом срезе ни один движок нас не упомянул ни разу.")

st.write("")
qm = our_query_matrix(week, provider)
if qm:
    grid = defaultdict(dict)
    qtexts = {}
    for r in qm:
        grid[r["query_id"]][r["provider"]] = r["mentioned"]
        qtexts[r["query_id"]] = r["query_text"]
    rows_sorted = sorted(grid.items(), key=lambda kv: sum(kv[1].values()))
    head = "".join(f'<th class="n">{P_SHORT.get(p, p[:4].upper())}</th>' for p in provs)
    body = ""
    for qid, per in rows_sorted:
        total = sum(per.values())
        cells = "".join(
            f'<td class="n" style="color:{"#12946A" if per.get(p) else "#D6452C"}">{"✓" if per.get(p) else "✗"}</td>'
            for p in provs)
        row_style = ' style="background:#FBE9E4"' if total == 0 else ""
        body += (f'<tr{row_style}><td class="n">{qid}</td>'
                 f'<td>{qtexts[qid][:70]}</td>{cells}</tr>')
    st.markdown(f'<div class="card"><h2 class="sec">Где нас нет — {choice}</h2>'
        f'<table class="aeo"><tr><th>ID</th><th>Запрос</th>{head}</tr>{body}</table>'
        f'<p style="font-size:12px;color:#98A2B5;margin-top:8px">Красные строки — нас нет ни в одном из выбранных движков</p></div>', unsafe_allow_html=True)

CATEGORY_LABELS = {"search": "Search — формируют ответ", "agent": "Agent — по поручению пользователя",
                   "training": "Training — корпус для обучения"}
VERDICT_COLOR = {"ok": ("#E1F5EC", "#12946A"), "blocked": ("#FBE9E4", "#D6452C"),
                  "unknown": ("#FBF1DC", "#C07E14"), "skipped": ("#EEF1F6", "#98A2B5")}

def render_crawler_card(url, label):
    score_row = crawler_score_row(url)
    bots = crawler_bots_rows(url)
    if not score_row:
        st.caption(f"{label}: ещё не проверялся — нажми «Проверить»")
        return
    score = score_row["score"]
    if score is None:
        st.markdown(f'<div class="card"><h2 class="sec">{label}</h2>'
                    f'<p style="font-size:13px;color:#D6452C">{score_row["summary"]}</p></div>', unsafe_allow_html=True)
        return
    score_color = "#12946A" if score >= 70 else "#C07E14" if score >= 40 else "#D6452C"
    groups_html = ""
    for cat in ("search", "agent", "training"):
        group = [b for b in bots if b["category"] == cat]
        if not group:
            continue
        rows_html = "".join(
            (lambda bg, fg: f'<div class="crow"><span class="nm" style="width:150px">{b["bot"]}{" ★" if b["is_critical"] else ""}</span>'
             f'<span class="badge" style="background:{bg};color:{fg};margin-right:8px">{b["verdict"]}</span>'
             f'<span style="flex:1;font-size:11.5px;color:#98A2B5">{b["detail"]}</span></div>')(*VERDICT_COLOR.get(b["verdict"], ("#EEF1F6","#5B6577")))
            for b in group)
        groups_html += f'<div class="lab" style="margin-top:10px">{CATEGORY_LABELS[cat]}</div>{rows_html}'
    st.markdown(
        f'<div class="card"><div style="display:flex;justify-content:space-between;align-items:center">'
        f'<h2 class="sec" style="margin:0">{label}</h2>'
        f'<span style="font-family:Manrope;font-weight:800;font-size:26px;color:{score_color}">{score}/100</span></div>'
        f'<p style="font-size:13px;color:#1A2233;margin-top:8px">{score_row["summary"]}</p>'
        f'{groups_html}</div>', unsafe_allow_html=True)


def build_crawler_ai_context(url, score_row, bots):
    import json as _json
    payload = {
        "url": url,
        "score": score_row["score"],
        "bots_ok": score_row["bots_ok"], "bots_blocked": score_row["bots_blocked"],
        "bots_unknown": score_row["bots_unknown"],
        "html_bytes": score_row["html_bytes"], "text_bytes": score_row["text_bytes"],
        "content_ratio_pct": float(score_row["content_ratio_pct"]) if score_row["content_ratio_pct"] is not None else None,
        "script_bytes": score_row.get("script_bytes"), "style_bytes": score_row.get("style_bytes"),
        "markup_bytes": score_row.get("markup_bytes"),
        "summary": score_row["summary"],
        "bots_detail": [
            {"bot": b["bot"], "category": b["category"], "critical": b["is_critical"],
             "verdict": b["verdict"], "detail": b["detail"]}
            for b in bots
        ],
    }
    return _json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def build_crawler_report_prompt(context_text):
    return f"""Ты — консультант по технической доступности сайта для AI-краулеров (не по контент-разметке — это отдельная проверка).

Ниже JSON-снапшот проверки доступности одной страницы для 12 AI-краулеров (search/agent/training)
и разбивка веса HTML-страницы (script/style/разметка/текст).

{context_text}

СТРОГИЕ ПРАВИЛА ТОЧНОСТИ:
1. Используй ТОЛЬКО числа из JSON выше дословно. Не пересчитывай и не выдумывай новые.
2. Не придумывай прогнозы вида "+X% за Y недель" — у нас нет исторических данных для таких оценок.
3. Приоритет действия — строго одно из: "fast_cheap", "medium", "slow_expensive".
4. Каждое действие должно ссылаться на конкретный факт из JSON (конкретный бот, конкретный % веса).

ОТВЕТЬ СТРОГО В ФОРМАТЕ JSON, без markdown-обёртки:
{{
  "situation": "2-3 предложения с точными цифрами из JSON выше",
  "conclusion": "2-3 предложения: что это значит для видимости в AI и к чему ведёт статус-кво",
  "actions": [
    {{"title": "короткий заголовок (3-6 слов)",
      "detail": "конкретика: что сделать и зачем, со ссылкой на факт из JSON",
      "priority": "fast_cheap"}}
  ]
}}
В "actions" — от 2 до 4 пунктов."""


def ai_analyze_crawler(url, score_row, bots):
    context = build_crawler_ai_context(url, score_row, bots)
    prompt = build_crawler_report_prompt(context)

    import json as _json
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-sonnet-4-5", max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    data = _extract_json(raw)
    return _json.dumps(data, ensure_ascii=False)


st.write("")
# ── Модуль отслеживания нашего сайта: история готовности во времени ──
_own_domain_for_history = NICHE if NICHE.startswith("http") else f"https://{NICHE}"
_hist = site_score_trend(_own_domain_for_history)
if _hist:
    W, H, X, Y = 560, 100, 30, 15
    step = W / max(len(_hist) - 1, 1)
    def _line(key, color):
        pts = []
        for i, r in enumerate(_hist):
            v = r.get(key)
            if v is None:
                continue
            pts.append(f"{X+i*step:.0f},{Y+H-(v/100)*H:.0f}")
        if len(pts) < 2:
            return ""
        return f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="2.4" stroke-linecap="round"/>'
    lines_html = _line("crawler_score", "#3D5AFE") + _line("audit_score", "#12946A")
    labels_html = "".join(f'<text x="{X+i*step:.0f}" y="{Y+H+18}">{r["checked_at"]}</text>'
                         for i, r in enumerate(_hist) if len(_hist) <= 6 or i % max(1, len(_hist)//6) == 0)
    last = _hist[-1]
    latest_html = "".join([
        f'<span><i style="background:#3D5AFE"></i>Краулер: {last["crawler_score"]}/100</span>' if last.get("crawler_score") is not None else "",
        f'<span><i style="background:#12946A"></i>Разметка: {last["audit_score"]}/100</span>' if last.get("audit_score") is not None else "",
    ])
    st.markdown(
        f'<div class="card"><h2 class="sec">📡 Наш сайт — история готовности ({len(_hist)} провер.)</h2>'
        f'<svg viewBox="0 0 600 145" width="100%">'
        f'<line x1="30" y1="{Y+H}" x2="590" y2="{Y+H}" stroke="#E4E8F0"/>'
        f'<text x="4" y="{Y+5}">100</text><text x="12" y="{Y+H+3}">0</text>'
        f'{lines_html}{labels_html}</svg>'
        f'<div class="legend">{latest_html}</div></div>', unsafe_allow_html=True)
    st.write("")
else:
    st.caption("📡 История готовности нашего сайта появится после первой проверки краулера или разметки ниже")
    st.write("")

# ── Доступность для AI-краулеров: свой сайт + опционально чужой ──
with st.expander("🕷️ Доступность для AI-краулеров (до проверки разметки)"):
    if not HAS_CRAWLER_CHECK:
        st.caption("Файл crawler_check.py не найден в репо — положи его в корень рядом со streamlit_app.py")
    else:
        default_own = NICHE if NICHE.startswith("http") else f"https://{NICHE}"
        cc1, cc2 = st.columns(2)
        with cc1:
            own_url = st.text_input("Наш сайт", value=default_own, key="own_crawler_url")
            own_clicked = st.button("🕷️ Проверить наш сайт", key="check_own")
        with cc2:
            other_url = st.text_input("Чужой сайт (конкурент, опционально)", value="", key="other_crawler_url")
            other_clicked = st.button("🕷️ Проверить чужой сайт", key="check_other", disabled=not other_url)
        if own_clicked:
            with st.spinner("Проверяю доступность для 12 ботов, это займёт до минуты..."):
                try:
                    _rep = run_crawler_check_live(own_url)
                    log_site_score(own_url, crawler_score=_rep.get("score"))
                except Exception as e:
                    st.error(f"Ошибка: {e}")
        render_crawler_card(own_url, "Наш сайт")

        _own_score_row = crawler_score_row(own_url)
        if _own_score_row and _own_score_row.get("score") is not None:
            ai_col1, ai_col2 = st.columns([3, 2])
            with ai_col2:
                crawler_ai_clicked = st.button("🤖 Разбор доступности от Claude", key="crawler_ai_btn",
                                               use_container_width=True)
            if crawler_ai_clicked:
                if not ANTHROPIC_API_KEY:
                    st.error("ANTHROPIC_API_KEY не найден в Secrets")
                else:
                    with st.spinner("Claude анализирует доступность..."):
                        try:
                            _own_bots = crawler_bots_rows(own_url)
                            _crawler_report_json = ai_analyze_crawler(own_url, _own_score_row, _own_bots)
                            st.session_state["crawler_ai_report"] = _crawler_report_json
                        except Exception as e:
                            st.error(f"Ошибка разбора: {e}")
            if st.session_state.get("crawler_ai_report"):
                render_ai_report(st.session_state["crawler_ai_report"], "Claude", "Доступность сайта")

        st.write("")
        if other_clicked:
            with st.spinner("Проверяю доступность для 12 ботов, это займёт до минуты..."):
                try:
                    run_crawler_check_live(other_url)
                except Exception as e:
                    st.error(f"Ошибка: {e}")
        if other_url:
            render_crawler_card(other_url, "Чужой сайт")
        else:
            st.caption("Введи URL конкурента, чтобы сравнить доступность бок о бок")

st.write("")
# ── Agent Readiness: аудит страницы сайта (наш сайт + опционально чужой) ──
def _run_audit_and_show(url, label):
    if not url:
        st.caption(f"{label}: введи URL, чтобы запустить аудит")
        return
    cached_audit = get_cached_audit(url)
    clicked = st.button(
        f"🔍 {'Обновить' if cached_audit else 'Запустить'} аудит — {label}",
        use_container_width=True, key=f"audit_btn_{label}")
    if clicked:
        if not ANTHROPIC_API_KEY:
            st.error("ANTHROPIC_API_KEY не найден в Secrets — аудит недоступен")
        else:
            with st.status("Запускаю аудит agent readiness...", expanded=True) as status_box:
                try:
                    catalog_facts = _cfg.get("catalog_facts", {}) if "_cfg" in dir() else {}
                    content_json, score, chash = run_site_audit(url, catalog_facts, log=status_box.write)
                    save_audit(url, score, content_json, chash)
                    if label == "Наш сайт":
                        log_site_score(url, audit_score=score)
                    cached_audit = {"score": score, "content": content_json, "content_hash": chash, "generated_at": None}
                    status_box.update(label=f"Аудит завершён — score {score}/100", state="complete")
                except Exception as e:
                    status_box.update(label="Ошибка аудита", state="error")
                    st.error(f"Ошибка аудита: {e}")
                    cached_audit = None
    if cached_audit:
        import json as _json2
        try:
            adata = _json2.loads(cached_audit["content"])
            score = cached_audit["score"] or 0
            score_color = "#12946A" if score >= 70 else "#C07E14" if score >= 40 else "#D6452C"
            findings_html = "".join(
                f'<div class="al"><span class="badge" style="background:'
                f'{"#E1F5EC" if f["status"]=="ok" else "#FBF1DC" if f["status"]=="mismatch" else "#FBE9E4"};'
                f'color:{"#12946A" if f["status"]=="ok" else "#C07E14" if f["status"]=="mismatch" else "#D6452C"}">'
                f'{f["status"]}</span><p><b>{f["check"]}</b><br>{f["detail"]}</p></div>'
                for f in adata.get("findings", []))
            recs_html = "".join(f'<li style="font-size:13px;color:#1A2233;margin-bottom:4px">{r}</li>'
                                for r in adata.get("recommendations", []))
            st.markdown(
                f'<div class="card"><div style="display:flex;justify-content:space-between;align-items:center">'
                f'<h2 class="sec" style="margin:0">{label}</h2>'
                f'<span style="font-family:Manrope;font-weight:800;font-size:26px;color:{score_color}">{score}/100</span></div>'
                f'<div style="margin-top:10px">{findings_html}</div>'
                f'<div class="lab" style="margin-top:12px">Что делать</div>'
                f'<ul style="margin:4px 0 0;padding-left:18px">{recs_html}</ul></div>',
                unsafe_allow_html=True)
        except Exception:
            st.caption("Не удалось разобрать сохранённый результат — запусти аудит заново")
    else:
        st.caption(f"{label}: ещё не проверялся — нажми кнопку выше")


with st.expander("🔍 Аудит сайта — Agent Readiness"):
    default_audit_own = default_own if "default_own" in dir() else (NICHE if NICHE.startswith("http") else f"https://{NICHE}")
    ac1, ac2 = st.columns(2)
    with ac1:
        own_audit_url = st.text_input("Наш сайт", value=default_audit_own, key="own_audit_url")
    with ac2:
        other_audit_url = st.text_input("Чужой сайт (конкурент, опционально)", value="", key="other_audit_url")
    _run_audit_and_show(own_audit_url, "Наш сайт")
    st.write("")
    if other_audit_url:
        _run_audit_and_show(other_audit_url, "Чужой сайт")
    else:
        st.caption("Введи URL конкурента, чтобы сравнить agent readiness бок о бок")

st.write("")
# ── Rung 2: Фактчек бренда ──
fc = factcheck_flags(week)
mismatches = [f for f in fc if f["mismatch"]]
if fc:
    if mismatches:
        rows_html = "".join(
            f'<div class="al"><span class="badge b-red">{f["query_id"]}·{P_SHORT.get(f["provider"], f["provider"].upper())}</span>'
            f'<p><b>AI утверждает:</b> {f["claim"]}<br><b>На самом деле:</b> {f["fact"]}</p></div>'
            for f in mismatches)
        st.markdown(f'<div class="card"><h2 class="sec">✓ Фактчек бренда — найдено расхождений: {len(mismatches)}</h2>{rows_html}</div>',
                    unsafe_allow_html=True)
    else:
        st.markdown('<div class="card"><h2 class="sec">✓ Фактчек бренда</h2>'
                     '<p style="font-size:13px;color:#12946A">Расхождений с каталогом не найдено на этой неделе.</p></div>',
                     unsafe_allow_html=True)
else:
    st.caption("Фактчек ещё не запускался для этой недели (нужен catalog_facts в queries.yaml)")

st.write("")
# ── Rung 3: AI-трафик → заказы (ручной ввод до подключения Shopify/GA4 API) ──
with st.expander("📈 AI-трафик → заказы (Demand — мост к деньгам)"):
    existing = ai_traffic_row(week)
    with st.form("ai_traffic_form"):
        c1, c2, c3 = st.columns(3)
        with c1:
            sessions_in = st.number_input("Сессии с AI-рефереров (chatgpt.com, perplexity.ai, ...)",
                min_value=0, value=int(existing["ai_sessions"]) if existing and existing["ai_sessions"] else 0)
        with c2:
            orders_in = st.number_input("Заказы из этих сессий",
                min_value=0, value=int(existing["ai_orders"]) if existing and existing["ai_orders"] else 0)
        with c3:
            revenue_in = st.number_input("Выручка ($)",
                min_value=0.0, value=float(existing["ai_revenue"]) if existing and existing["ai_revenue"] else 0.0, step=10.0)
        note_in = st.text_input("Заметка (опционально)", value=existing["note"] if existing and existing.get("note") else "")
        if st.form_submit_button("Сохранить данные недели"):
            save_ai_traffic(week, int(sessions_in), int(orders_in), float(revenue_in), note_in.strip() or None)
            st.success("Сохранено")

    trend = ai_traffic_trend()
    if len(trend) >= 2:
        rows_html = "".join(
            f'<div class="crow"><span class="nm">{r["week_start"]}</span>'
            f'<span style="flex:1;font-family:\'IBM Plex Mono\',monospace;font-size:12px;color:#5B6577">'
            f'{r["ai_sessions"] or 0} сессий · {r["ai_orders"] or 0} заказов · ${r["ai_revenue"] or 0:.0f}</span></div>'
            for r in trend)
        st.markdown(f'<div class="lab" style="margin-top:10px">История</div>{rows_html}', unsafe_allow_html=True)
    else:
        st.caption("Заполни хотя бы 2 недели, чтобы увидеть тренд AI-трафика рядом с трендом SOV")

st.write("")
if not AI_MODELS:
    st.caption("Добавь ANTHROPIC_API_KEY или GEMINI_API_KEY/VERTEX_SA_JSON_B64 в Secrets, чтобы включить AI-разбор")
else:
    _ctx_now = build_ai_context(week, provider, brands, own_c, channel_shares(week, provider),
                                 top_donors(week, provider, limit=8), our_query_matrix(week, provider), NICHE)
    _hash_now = context_hash(_ctx_now)

    model_col, btn_col = st.columns([3, 2])
    with model_col:
        model_choice = st.radio("Аналитик", list(AI_MODELS.keys()), horizontal=True, label_visibility="collapsed")
    model_suffix, analyze_fn = AI_MODELS[model_choice]
    cache_key = f"{provider or 'all'}::{model_suffix}"
    cached = get_cached_insight(week, cache_key)
    is_stale = cached is not None and cached.get("data_hash") != _hash_now
    with btn_col:
        label = "🤖 Обновить AI-разбор" if is_stale else ("🤖 Обновить разбор" if cached else f"🤖 Сгенерировать разбор от {model_choice}")
        gen_clicked = st.button(label, use_container_width=True)
    if gen_clicked:
        with st.spinner(f"{model_choice} анализирует данные недели..."):
            try:
                content = analyze_fn(_ctx_now)
                save_insight(week, cache_key, content, _hash_now)
                cached = {"content": content, "generated_at": None, "data_hash": _hash_now}
                is_stale = False
            except Exception as e:
                st.error(f"Ошибка генерации: {e}")
                cached = None
    if cached:
        if is_stale:
            st.warning("⚠ Данные обновились с момента генерации этого разбора — цифры внутри могут не совпадать "
                       "с текущими карточками выше. Нажми «Обновить AI-разбор», чтобы получить актуальный.")
        render_ai_report(cached["content"], model_choice, choice)
    else:
        st.caption(f"Разбор от {model_choice} ещё не сгенерирован для этого среза — нажми кнопку выше")

st.write("")
st.write("")
with st.expander("⚗ Эксперименты — гипотеза → действие → измеренный результат"):
    with st.form("new_experiment", clear_on_submit=True):
        st.markdown("**Проблема** (что не так, по данным)")
        exp_problem = st.text_area("problem", label_visibility="collapsed",
            placeholder="Например: отсутствуем в q01, q04, q07 — нас не упоминает ни один движок")
        st.markdown("**Гипотеза**")
        exp_hyp = st.text_area("hypothesis", label_visibility="collapsed",
            placeholder="Например: присутствие в источниках, которые цитируют лидеров, повысит видимость")
        st.markdown("**Действие** (что конкретно сделали)")
        exp_action = st.text_input("action", label_visibility="collapsed",
            placeholder="Например: опубликовали 3 отзыва на reddit.com/r/ultralight со ссылкой на merino.tech")
        c1, c2, c3 = st.columns(3)
        with c1:
            exp_qid = st.text_input("query_id (опционально)")
        with c2:
            exp_url = st.text_input("URL действия (опционально)")
        with c3:
            exp_date = st.date_input("Дата начала")
        submitted = st.form_submit_button("Зафиксировать эксперимент")
        if submitted and exp_action:
            qid_clean = exp_qid.strip() or None
            ours_now = next((b for b in brands if b["is_ours"]), None)
            baseline = ours_now["sov"] if ours_now else None
            log_experiment(exp_date, exp_problem.strip() or None, exp_hyp.strip() or None,
                            exp_action.strip(), qid_clean, exp_url.strip() or None, baseline)
            st.success(f"Зафиксировано. Baseline SOV на старте: {baseline}%. "
                       "Проверки через 2/4/8 недель появятся ниже автоматически по мере накопления данных.")

    exps = list_experiments()
    if exps:
        for e in exps:
            scope = f'запрос {e["query_id"]}' if e["query_id"] else "вся ниша"
            checkpoints_html = ""
            for wk in (2, 4, 8):
                wk_date, val = experiment_effect_at(e["started_at"], e["query_id"], wk)
                if val is None:
                    cp = f'<span style="color:#98A2B5">через {wk} нед. — ещё рано, данных пока нет</span>'
                elif e["baseline_sov"] is not None:
                    d = round(val - float(e["baseline_sov"]), 1)
                    cls = "up" if d >= 0 else "dn"
                    cp = (f'через {wk} нед. ({wk_date}): {val}% '
                          f'<span class="delta {cls}" style="margin-left:4px">{d:+.1f} pp</span>')
                else:
                    cp = f'через {wk} нед. ({wk_date}): {val}%'
                checkpoints_html += f'<div style="font-size:12.5px;color:#1A2233;padding:3px 0">→ {cp}</div>'

            baseline_txt = f'{e["baseline_sov"]}%' if e["baseline_sov"] is not None else "—"
            body = ""
            if e.get("problem"):
                body += f'<div style="margin-top:6px"><b>Проблема:</b> {e["problem"]}</div>'
            if e.get("hypothesis"):
                body += f'<div style="margin-top:4px"><b>Гипотеза:</b> {e["hypothesis"]}</div>'
            link = f' · <a href="{e["url"]}" target="_blank" rel="noopener">ссылка</a>' if e["url"] else ""
            st.markdown(
                f'<div class="al" style="flex-direction:column;align-items:stretch">'
                f'<div style="display:flex;gap:12px;align-items:flex-start">'
                f'<span class="badge b-amb">{e["started_at"]}</span>'
                f'<div style="flex:1">'
                f'<b>{e["description"]}</b> ({scope}){link}'
                f'{body}'
                f'<div style="margin-top:6px;font-size:12px;color:#98A2B5">Baseline SOV: {baseline_txt}</div>'
                f'{checkpoints_html}'
                f'</div></div></div>', unsafe_allow_html=True)
    else:
        st.caption("Пока нет ни одного зафиксированного эксперимента")

with st.expander("Сырые ответы AI"):
    resp = all_responses(week, provider)
    qids = sorted({r["query_id"] for r in resp})
    if qids:
        sel = st.selectbox("Запрос", qids)
        for r in resp:
            if r["query_id"] == sel:
                st.markdown(f'**{r["provider"]}** — {r["query_text"][:90]}')
                st.markdown(highlight_brand(r["response_text"], ALIASES), unsafe_allow_html=True)
                st.divider()
