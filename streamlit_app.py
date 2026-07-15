# -*- coding: utf-8 -*-
"""AEO Radar — дашборд, всё в одном файле. Секрет: DATABASE_URL."""
import os
import re
from collections import defaultdict
from pathlib import Path

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
CREATE TABLE IF NOT EXISTS aeo.ai_insights (
    week_start date NOT NULL,
    provider text NOT NULL DEFAULT 'all',
    content text NOT NULL,
    generated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week_start, provider));
"""
with psycopg2.connect(DATABASE_URL) as _c, _c.cursor() as _cur:
    _cur.execute(DDL)
    _c.commit()

NICHE, N_QUERIES = "merino.tech", 16
ALIASES = [NICHE]
try:
    import yaml
    _cfg = yaml.safe_load(Path("queries.yaml").read_text(encoding="utf-8"))
    NICHE = (_cfg.get("brands", {}).get("ours") or [NICHE])[0]
    N_QUERIES = len(_cfg.get("queries", [])) or N_QUERIES
    ALIASES = _cfg.get("aliases", {}).get(NICHE, [NICHE]) or [NICHE]
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

def list_experiments():
    return _rows("SELECT id, started_at, description, query_id, url FROM aeo.experiments ORDER BY started_at DESC")

def log_experiment(started_at, description, query_id, url):
    with psycopg2.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO aeo.experiments (started_at, description, query_id, url) VALUES (%s,%s,%s,%s)",
            (started_at, description, query_id or None, url or None))
        conn.commit()
    _rows.clear()


def experiment_effect(started_at, query_id=None):
    """Средний SOV (или own-site доля цитат, если query_id задан) ДО и ПОСЛЕ даты эксперимента."""
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
    r = _rows("SELECT content, generated_at FROM aeo.ai_insights WHERE week_start=%s AND provider=%s",
               (week, provider_key))
    return r[0] if r else None

def save_insight(week, provider_key, content):
    with psycopg2.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO aeo.ai_insights (week_start, provider, content, generated_at)
               VALUES (%s,%s,%s,now())
               ON CONFLICT (week_start, provider)
               DO UPDATE SET content=EXCLUDED.content, generated_at=now()""",
            (week, provider_key, content))
        conn.commit()
    _rows.clear()

def build_ai_context(week, provider, brands, own_c, channels, donors, qmatrix, niche):
    lines = [f"Ниша: {niche}. Неделя: {week}. Провайдер(ы): {provider or 'все'}.", ""]
    lines.append("SOV по брендам (кто и на сколько нас обходит):")
    for b in brands[:8]:
        mark = " <- ЭТО МЫ" if b["is_ours"] else ""
        lines.append(f"- {b['brand']}: SOV {b['sov']}%, средняя позиция {b['avg_pos']}{mark}")
    lines.append("")
    lines.append(f"Доля цитат с нашего сайта: {own_c}%")
    lines.append("Разбивка источников по типу:")
    for c in channels:
        lines.append(f"- {CH_LAB.get(c['source_type'], c['source_type'])}: {c['pct']}%")
    lines.append("")
    lines.append("Топ-доноры цитат в нише (чьи сайты AI цитирует чаще всего):")
    for d in donors:
        mine = " (это наш сайт)" if d["is_ours"] else ""
        lines.append(f"- {d['domain']}: {d['n']} цитат{mine}")
    lines.append("")
    if qmatrix:
        grid = defaultdict(dict)
        qtexts = {}
        for r in qmatrix:
            grid[r["query_id"]][r["provider"]] = r["mentioned"]
            qtexts[r["query_id"]] = r["query_text"]
        zero = [qid for qid, per in grid.items() if not any(per.values())]
        lines.append(f"Запросы, где нас НЕ упоминает ни один движок ({len(zero)} из {len(grid)}):")
        for qid in zero[:10]:
            lines.append(f"- {qid}: {qtexts[qid]}")
    return "\n".join(lines)

def _build_report_prompt(context_text):
    return f"""Ты — консультант по GEO/AEO-стратегии (видимость бренда в ответах AI-агентов).
Ниже данные недельного мониторинга бренда в ответах Gemini/Claude/ChatGPT по покупательским запросам ниши.

{context_text}

Дай разбор в стиле консалтингового отчёта (McKinsey-style), строго по структуре, на русском:

**Ситуация** — 2-3 предложения: что происходит объективно, без оценок.

**Вывод** — 2-3 предложения: что это значит для бизнеса, почему это важно, к чему ведёт при сохранении статус-кво.

**Что делать** — пронумерованный список из 3-5 КОНКРЕТНЫХ действий, каждое с указанием: что именно сделать, на каком домене/запросе, и ожидаемый эффект. Приоритет — от самого дешёвого/быстрого к более затратному. Никаких общих фраз вроде "улучшить контент" — только конкретика с именами доменов и id запросов из данных выше.

Пиши по делу, без вступлений и извинений, сразу с "**Ситуация**"."""




def ai_analyze(context_text):
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": _build_report_prompt(context_text)}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


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
    return resp.text or ""


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
.card{background:#FFF;border:1px solid #E4E8F0;border-radius:16px;padding:18px}
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

left, right = st.columns([1.7, 1])
with left:
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
with right:
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
st.write("")

left, right = st.columns([1.4, 1])
with left:
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
with right:
    alerts = [{"sev":"HI","text":f'Выпала наша цитата: {r["url"]}'} for r in (lost_own_urls(week, prev, provider) if prev else [])]
    alerts += [{"sev":"MD","text":f'{b["brand"]} +{b["delta"]} п.п. за неделю'}
               for b in brands if not b["is_ours"] and b["delta"] and b["delta"] >= 5]
    dc = delta(own_c, own_c_prev)
    if dc is not None and dc <= -3:
        alerts.append({"sev":"HI","text":f"Доля own-site цитат упала на {abs(dc)} п.п."})
    items = "".join(f'<div class="al"><span class="badge {"b-red" if a["sev"]=="HI" else "b-amb"}">{a["sev"]}</span><p>{a["text"]}</p></div>'
        for a in alerts[:6]) or '<p style="color:#98A2B5;font-size:13px">Пока тихо — нужна вторая неделя данных для дельт.</p>'
    st.markdown(f'<div class="card"><h2 class="sec">Требует внимания</h2>{items}</div>', unsafe_allow_html=True)

st.write("")
if not AI_MODELS:
    st.caption("Добавь ANTHROPIC_API_KEY или GEMINI_API_KEY/VERTEX_SA_JSON_B64 в Secrets, чтобы включить AI-разбор")
else:
    model_col, btn_col = st.columns([3, 2])
    with model_col:
        model_choice = st.radio("Аналитик", list(AI_MODELS.keys()), horizontal=True, label_visibility="collapsed")
    model_suffix, analyze_fn = AI_MODELS[model_choice]
    cache_key = f"{provider or 'all'}::{model_suffix}"
    cached = get_cached_insight(week, cache_key)
    with btn_col:
        gen_clicked = st.button(
            f"🤖 {'Обновить' if cached else 'Сгенерировать'} разбор от {model_choice}",
            use_container_width=True)
    if gen_clicked:
        with st.spinner(f"{model_choice} анализирует данные недели..."):
            ctx = build_ai_context(week, provider, brands, own_c, channel_shares(week, provider),
                                    top_donors(week, provider, limit=8), our_query_matrix(week, provider), NICHE)
            try:
                content = analyze_fn(ctx)
                save_insight(week, cache_key, content)
                cached = {"content": content, "generated_at": None}
            except Exception as e:
                st.error(f"Ошибка генерации: {e}")
                cached = None
    if cached:
        st.markdown(f'<div class="card"><h2 class="sec">🤖 Разбор недели — {model_choice} · {choice}</h2>{cached["content"]}</div>',
                    unsafe_allow_html=True)
    else:
        st.caption(f"Разбор от {model_choice} ещё не сгенерирован для этого среза — нажми кнопку выше")

st.write("")
with st.expander("⚗ Эксперименты — записать правку и увидеть эффект"):
    with st.form("new_experiment", clear_on_submit=True):
        exp_desc = st.text_input("Что сделали (например: добавили /answers на q07)")
        exp_qid = st.text_input("query_id (необязательно, если правка про конкретный запрос)")
        exp_url = st.text_input("URL правки (необязательно)")
        exp_date = st.date_input("Дата правки")
        submitted = st.form_submit_button("Записать эксперимент")
        if submitted and exp_desc:
            log_experiment(exp_date, exp_desc, exp_qid.strip() or None, exp_url.strip() or None)
            st.success("Записано — дельта появится ниже по мере накопления данных до/после этой даты")

    exps = list_experiments()
    if exps:
        rows_html = ""
        for e in exps:
            b, a = experiment_effect(e["started_at"], e["query_id"])
            if b is None or a is None:
                verdict = '<span style="color:#98A2B5">недостаточно данных до/после</span>'
            else:
                d = round(a - b, 1)
                cls = "up" if d >= 0 else "dn"
                verdict = f'{b}% → {a}% <span class="delta {cls}" style="margin-left:6px">{d:+.1f} pp</span>'
            scope = f'запрос {e["query_id"]}' if e["query_id"] else "вся ниша"
            link = f' · <a href="{e["url"]}" target="_blank" rel="noopener">ссылка</a>' if e["url"] else ""
            rows_html += (f'<div class="al"><span class="badge b-amb">{e["started_at"]}</span>'
                          f'<p><b>{e["description"]}</b> ({scope}){link}<br>{verdict}</p></div>')
        st.markdown(rows_html, unsafe_allow_html=True)
    else:
        st.caption("Пока нет ни одного зафиксированного эксперимента")

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
