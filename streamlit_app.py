# -*- coding: utf-8 -*-
"""AEO Radar — дашборд, всё в одном файле. Секрет: DATABASE_URL."""
import os
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
"""
with psycopg2.connect(DATABASE_URL) as _c, _c.cursor() as _cur:
    _cur.execute(DDL)
    _c.commit()

NICHE, N_QUERIES = "merino.tech", 16
BRAND_SITES = {}
try:
    import yaml
    _cfg = yaml.safe_load(Path("queries.yaml").read_text(encoding="utf-8"))
    NICHE = (_cfg.get("brands", {}).get("ours") or [NICHE])[0]
    N_QUERIES = len(_cfg.get("queries", [])) or N_QUERIES
    BRAND_SITES = _cfg.get("brand_sites", {}) or {}
except Exception:
    pass

@st.cache_data(ttl=600)
def _rows(sql: str, params=()):
    with psycopg2.connect(DATABASE_URL) as conn, \
         conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

def weeks():
    return [r["week_start"] for r in _rows(
        "SELECT DISTINCT week_start FROM aeo.mentions ORDER BY week_start")]

def sov_by_brand(week):
    return _rows("""SELECT brand, bool_or(is_ours) AS is_ours,
        round(100.0*sum(mentioned::int)/count(*),1) AS sov,
        round(avg(first_position) FILTER (WHERE mentioned),1) AS avg_pos
        FROM aeo.mentions WHERE week_start=%s GROUP BY brand ORDER BY sov DESC""", (week,))

def sov_by_brand_provider(week):
    return {(r["brand"], r["provider"]): float(r["sov"]) for r in _rows(
        """SELECT brand, provider, round(100.0*sum(mentioned::int)/count(*),0) AS sov
           FROM aeo.mentions WHERE week_start=%s GROUP BY brand, provider""", (week,))}

def sov_trend():
    return _rows("""SELECT week_start, brand,
        round(100.0*sum(mentioned::int)/count(*),1) AS sov
        FROM aeo.mentions GROUP BY week_start, brand ORDER BY week_start""")

def channel_shares(week):
    return _rows("""SELECT source_type, count(*) AS n,
        round(100.0*count(*)/sum(count(*)) OVER (),0) AS pct
        FROM aeo.citations WHERE week_start=%s GROUP BY source_type ORDER BY n DESC""", (week,))

def own_citation_share(week):
    r = _rows("""SELECT round(100.0*sum(is_ours::int)/greatest(count(*),1),1) AS pct
                 FROM aeo.citations WHERE week_start=%s""", (week,))
    return float(r[0]["pct"]) if r and r[0]["pct"] is not None else 0.0

def top_donors(week, limit=6):
    return _rows("""SELECT domain, bool_or(is_ours) AS is_ours, count(*) AS n,
        (array_agg(url ORDER BY position))[1] AS sample_url
        FROM aeo.citations WHERE week_start=%s GROUP BY domain
        ORDER BY n DESC LIMIT %s""", (week, limit))

def lost_own_urls(week, prev):
    return _rows("""SELECT DISTINCT url FROM aeo.citations
        WHERE week_start=%s AND is_ours AND url NOT IN
          (SELECT url FROM aeo.citations WHERE week_start=%s AND is_ours) LIMIT 5""",
        (prev, week))

def providers_in_week(week):
    return [r["provider"] for r in _rows(
        "SELECT DISTINCT provider FROM aeo.mentions WHERE week_start=%s ORDER BY provider", (week,))]

def our_query_matrix(week):
    return _rows("""SELECT m.query_id, r.query_text, m.provider, m.mentioned
        FROM aeo.mentions m
        JOIN aeo.responses r USING (week_start, query_id, provider)
        WHERE m.week_start=%s AND m.is_ours
        ORDER BY m.query_id, m.provider""", (week,))

def our_mentions_detail(week):
    """Конкретные случаи упоминания нас: запрос + движок + сам ответ AI целиком."""
    return _rows("""SELECT m.query_id, m.provider, m.first_position, m.mention_count,
        r.query_text, r.response_text
        FROM aeo.mentions m
        JOIN aeo.responses r USING (week_start, query_id, provider)
        WHERE m.week_start=%s AND m.is_ours AND m.mentioned
        ORDER BY m.first_position, m.query_id""", (week,))

st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@600;700;800&family=Golos+Text:wght@400;500&family=IBM+Plex+Mono:wght@400;500;600&display=swap');
.stApp{background:#F4F6FA}
#MainMenu,footer,header{visibility:hidden}
.block-container{padding-top:2rem;max-width:1160px;font-family:'Golos Text',sans-serif}
.aeo-logo{font-family:Manrope;font-weight:800;font-size:22px;color:#1A2233}
.aeo-logo span{color:#3D5AFE}
.aeo-meta{font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:#98A2B5;text-transform:uppercase;margin-top:2px}
.card{background:#FFF;border:1px solid #E4E8F0;border-radius:16px;padding:18px;height:100%}
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
.donor{display:flex;gap:8px;align-items:baseline;padding:5px 0;font-family:'IBM Plex Mono',monospace;font-size:11.5px;border-top:1px dashed #E4E8F0}
.donor a{flex:1;color:#5B6577;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-decoration:none}
.donor a:hover{text-decoration:underline;color:#3D5AFE}
.donor.ours a{color:#12946A}
.donor b{font-weight:600;color:#1A2233}
table.aeo{width:100%;border-collapse:collapse;font-size:13px}
table.aeo th{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:#98A2B5;text-align:left;padding:6px 8px;border-bottom:1.5px solid #1A2233}
table.aeo th.n,table.aeo td.n{text-align:right}
table.aeo td{padding:8px;border-bottom:1px solid #E4E8F0;color:#1A2233}
table.aeo td.n{font-family:'IBM Plex Mono',monospace;font-size:12.5px}
table.aeo td a{color:inherit;text-decoration:none}
table.aeo td a:hover{text-decoration:underline;color:#3D5AFE}
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
CH_LAB = {"own_site":"Наш сайт","social":"Reddit / соцсети","marketplace":"Маркетплейсы","third_party":"Чужие статьи"}
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
brands = sov_by_brand(week)
prev_sov = {r["brand"]: float(r["sov"]) for r in sov_by_brand(prev)} if prev else {}
for b in brands:
    b["delta"] = delta(b["sov"], prev_sov.get(b["brand"]))
per_prov = sov_by_brand_provider(week)
provs = providers_in_week(week)
ours = next((b for b in brands if b["is_ours"]), None)
own_c, own_c_prev = own_citation_share(week), (own_citation_share(prev) if prev else None)

st.markdown(f'<div class="aeo-meta">{NICHE} · {week} · {N_QUERIES} queries · '
            f'{" / ".join(provs).upper()} · weeks: {len(wks)}</div>', unsafe_allow_html=True)
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
    trend = sov_trend()
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
    st.markdown(f'<div class="card"><h2 class="sec">Тренд Share of Voice</h2>'
        f'<svg viewBox="0 0 600 165" width="100%">'
        f'<line x1="30" y1="130" x2="590" y2="130" stroke="#E4E8F0"/>'
        f'<line x1="30" y1="75" x2="590" y2="75" stroke="#EEF1F6"/>'
        f'<text x="4" y="78">50</text><text x="12" y="133">0</text>'
        f'{lines}{labels}</svg><div class="legend">{legend}</div></div>', unsafe_allow_html=True)
with right:
    rows = "".join(f'<div class="crow"><span class="nm">{CH_LAB.get(c["source_type"],c["source_type"])}</span>'
        f'<div class="cbar"><i style="width:{c["pct"]}%;background:{CH_COL.get(c["source_type"],"#98A2B5")}"></i></div>'
        f'<b>{c["pct"]}%</b></div>' for c in channel_shares(week))
    donors = "".join(
        f'<div class="donor {"ours" if x["is_ours"] else ""}">'
        f'<a href="{x["sample_url"]}" target="_blank" rel="noopener">{x["domain"]}</a>'
        f'<b>{x["n"]}</b></div>'
        for x in top_donors(week))
    st.markdown(f'<div class="card"><h2 class="sec">Откуда AI берёт информацию</h2>{rows}'
        f'<div class="lab" style="margin-top:14px">Топ-доноры цитат (клик — открыть статью)</div>{donors}</div>', unsafe_allow_html=True)
st.write("")

left, right = st.columns([1.4, 1])
with left:
    head = "".join(f'<th class="n">{P_SHORT.get(p, p[:4].upper())}</th>' for p in provs)
    body = ""
    for b in brands[:10]:
        dl = b["delta"]
        dcls = "up" if dl and dl>0 else "dn" if dl and dl<0 else ""
        pcells = "".join(f'<td class="n">{int(per_prov[(b["brand"],p)]) if (b["brand"],p) in per_prov else "·"}</td>' for p in provs)
        site = BRAND_SITES.get(b["brand"], "")
        name_html = (f'<a href="{site}" target="_blank" rel="noopener">{b["brand"]}</a>'
                     if site else b["brand"])
        body += (f'<tr class="{"ours" if b["is_ours"] else ""}"><td>{name_html}</td>'
                 f'<td class="n">{b["sov"]}%</td>'
                 f'<td class="n {dcls}">{f"{dl:+.0f}" if dl is not None else "—"}</td>'
                 f'<td class="n">{b["avg_pos"] or "—"}</td>{pcells}</tr>')
    st.markdown(f'<div class="card"><h2 class="sec">Кого рекомендуют AI (клик — сайт бренда)</h2>'
        f'<table class="aeo"><tr><th>Бренд</th><th class="n">SOV</th><th class="n">Δ</th>'
        f'<th class="n">поз.</th>{head}</tr>{body}</table></div>', unsafe_allow_html=True)
with right:
    alerts = [{"sev":"HI","text":f'Выпала наша цитата: {r["url"]}'} for r in (lost_own_urls(week, prev) if prev else [])]
    alerts += [{"sev":"MD","text":f'{b["brand"]} +{b["delta"]} п.п. за неделю'}
               for b in brands if not b["is_ours"] and b["delta"] and b["delta"] >= 5]
    dc = delta(own_c, own_c_prev)
    if dc is not None and dc <= -3:
        alerts.append({"sev":"HI","text":f"Доля own-site цитат упала на {abs(dc)} п.п."})
    items = "".join(f'<div class="al"><span class="badge {"b-red" if a["sev"]=="HI" else "b-amb"}">{a["sev"]}</span><p>{a["text"]}</p></div>'
        for a in alerts[:6]) or '<p style="color:#98A2B5;font-size:13px">Пока тихо — нужна вторая неделя данных для дельт.</p>'
    st.markdown(f'<div class="card"><h2 class="sec">Требует внимания</h2>{items}</div>', unsafe_allow_html=True)

st.write("")

# ── НОВОЕ: где мы реально упоминаемся — ссылки на живые ответы AI ──
mentions_detail = our_mentions_detail(week)
st.markdown(f'<div class="card"><h2 class="sec">Где мы упоминаемся ({len(mentions_detail)} случаев)</h2></div>',
            unsafe_allow_html=True)
if mentions_detail:
    for m in mentions_detail:
        label = (f'{P_SHORT.get(m["provider"], m["provider"].upper())} · позиция {m["first_position"]} · '
                 f'{m["query_id"]} — {m["query_text"][:70]}')
        with st.expander(label):
            st.markdown(f'<span class="mtag">{m["mention_count"]}× упоминаний в ответе</span>', unsafe_allow_html=True)
            st.write(m["response_text"])
else:
    st.info("На этой неделе ни один движок нас не упомянул ни разу.")

st.write("")
qm = our_query_matrix(week)
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
    st.markdown(f'<div class="card"><h2 class="sec">Где нас нет — задачи на контент</h2>'
        f'<table class="aeo"><tr><th>ID</th><th>Запрос</th>{head}</tr>{body}</table>'
        f'<p style="font-size:12px;color:#98A2B5;margin-top:8px">Красные строки — нас нет ни в одном движке: '
        f'кандидаты на /answers-страницу, тред или обзор</p></div>', unsafe_allow_html=True)

with st.expander("Сырые ответы AI (все, включая без упоминаний нас)"):
    resp = _rows("""SELECT query_id, provider, query_text, response_text
                    FROM aeo.responses WHERE week_start=%s ORDER BY query_id, provider""", (week,))
    qids = sorted({r["query_id"] for r in resp})
    if qids:
        sel = st.selectbox("Запрос", qids)
        for r in resp:
            if r["query_id"] == sel:
                st.markdown(f'**{r["provider"]}** — {r["query_text"][:90]}')
                st.write(r["response_text"])
                st.divider()
