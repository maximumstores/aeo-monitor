# -*- coding: utf-8 -*-
"""
AEO Radar — дашборд для Streamlit Cloud.
Main file: streamlit_app.py · секреты: DATABASE_URL(_TECH) в Secrets.
"""
import os

import streamlit as st

# ── Мост: Streamlit Secrets → env (до импорта monitor.config) ──
for k in ("DATABASE_URL_TECH", "DATABASE_URL", "GEMINI_API_KEY", "OPENAI_API_KEY",
          "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY"):
    try:
        if k not in os.environ and k in st.secrets:
            os.environ[k] = str(st.secrets[k])
    except Exception:
        pass

from dashboard import queries as q          # noqa: E402
from monitor.config import load_config      # noqa: E402

st.set_page_config(page_title="AEO Radar", page_icon="◎", layout="wide")

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@600;700;800&family=Golos+Text:wght@400;500&family=IBM+Plex+Mono:wght@400;500;600&display=swap');
:root{--panel:#FFF;--line:#E4E8F0;--ink:#1A2233;--dim:#5B6577;--mut:#98A2B5;
--acc:#3D5AFE;--ok:#12946A;--ok-soft:#E1F5EC;--red:#D6452C;--red-soft:#FBE9E4;--amb:#C07E14;--amb-soft:#FBF1DC;
--disp:'Manrope',sans-serif;--body:'Golos Text',sans-serif;--mono:'IBM Plex Mono',monospace}
.stApp{background:#F4F6FA}
html,body,[class*="css"]{font-family:var(--body)}
#MainMenu,footer,header{visibility:hidden}
.block-container{padding-top:2rem;max-width:1160px}
.aeo-logo{font-family:var(--disp);font-weight:800;font-size:22px;color:var(--ink)}
.aeo-logo span{color:var(--acc)}
.aeo-meta{font-family:var(--mono);font-size:11.5px;color:var(--mut);text-transform:uppercase;margin-top:2px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px;height:100%}
.lab{font-size:12px;color:var(--mut);font-weight:500;margin-bottom:6px}
.big{font-family:var(--disp);font-weight:800;font-size:29px;color:var(--ink);letter-spacing:-.02em}
.delta{display:inline-block;font-family:var(--mono);font-size:11px;font-weight:600;border-radius:999px;padding:2px 9px;margin-top:8px}
.delta.up{background:var(--ok-soft);color:var(--ok)}
.delta.dn{background:var(--red-soft);color:var(--red)}
h2.sec{font-family:var(--disp);font-weight:700;font-size:15px;color:var(--ink);margin:0 0 10px}
.crow{display:flex;align-items:center;gap:10px;padding:5px 0;font-size:12.5px;color:var(--dim)}
.crow .nm{width:128px}
.cbar{flex:1;height:9px;background:#EEF1F6;border-radius:5px;overflow:hidden;position:relative}
.cbar i{position:absolute;left:0;top:0;bottom:0;border-radius:5px}
.crow b{width:38px;text-align:right;font-family:var(--mono);font-size:12px;color:var(--ink)}
.donor{display:flex;gap:8px;align-items:baseline;padding:5px 0;font-family:var(--mono);font-size:11.5px;border-top:1px dashed var(--line)}
.donor span{flex:1;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.donor.ours span{color:var(--ok)}
.donor b{font-weight:600;color:var(--ink)}
table.aeo{width:100%;border-collapse:collapse;font-size:13px}
table.aeo th{font-family:var(--mono);font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);text-align:left;padding:6px 8px;border-bottom:1.5px solid var(--ink)}
table.aeo th.n,table.aeo td.n{text-align:right}
table.aeo td{padding:8px;border-bottom:1px solid var(--line);color:var(--ink)}
table.aeo td.n{font-family:var(--mono);font-size:12.5px}
tr.ours td{background:var(--ok-soft)}
tr.ours td:first-child{font-weight:600;color:var(--ok);border-radius:8px 0 0 8px}
tr.ours td:last-child{border-radius:0 8px 8px 0}
.up{color:var(--ok)}.dn{color:var(--red)}
.al{display:flex;gap:12px;padding:10px 0;border-top:1px solid var(--line);font-size:13px;color:var(--ink);align-items:flex-start}
.badge{font-family:var(--mono);font-size:10px;font-weight:600;border-radius:999px;padding:2px 8px;white-space:nowrap;margin-top:2px}
.b-red{background:var(--red-soft);color:var(--red)}
.b-amb{background:var(--amb-soft);color:var(--amb)}
svg text{font-family:var(--mono);font-size:9.5px;fill:var(--mut)}
.legend{display:flex;gap:16px;font-size:12px;color:var(--dim);margin-top:8px;flex-wrap:wrap}
.legend i{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

PROVIDER_SHORT = {"gemini": "GEM", "openai": "GPT", "perplexity": "PPLX",
                  "claude": "CLD", "rufus": "RUF"}
CHANNEL_LABELS = {"own_site": "Наш сайт", "social": "Reddit / соцсети",
                  "marketplace": "Маркетплейсы", "third_party": "Чужие статьи"}
CHANNEL_COLORS = {"own_site": "#12946A", "social": "#C07E14",
                  "marketplace": "#3D5AFE", "third_party": "#98A2B5"}


@st.cache_data(ttl=600)
def data():
    d = {"weeks": q.weeks()}
    if not d["weeks"]:
        return d
    week, prev = d["weeks"][-1], (d["weeks"][-2] if len(d["weeks"]) > 1 else None)
    d.update(week=week, prev=prev,
             brands=q.sov_by_brand(week),
             prev_sov={r["brand"]: float(r["sov"]) for r in q.sov_by_brand(prev)} if prev else {},
             per_prov=q.sov_by_brand_provider(week),
             providers=q.providers_in_week(week),
             trend=q.our_sov_trend(),
             channels=q.channel_shares(week),
             own_cite=q.own_citation_share(week),
             own_cite_prev=q.own_citation_share(prev) if prev else None,
             donors=q.top_donors(week),
             lost=q.lost_own_urls(week, prev) if prev else [])
    return d


def delta(cur, prev):
    return None if cur is None or prev is None else round(float(cur) - float(prev), 1)


def delta_html(d, suffix=" pp WoW"):
    if d is None:
        return ""
    cls = "up" if d >= 0 else "dn"
    return f'<span class="delta {cls}">{d:+.1f}{suffix}</span>'


def kpi(label, value, d=None):
    st.markdown(f'<div class="card"><div class="lab">{label}</div>'
                f'<div class="big">{value}</div>{delta_html(d)}</div>', unsafe_allow_html=True)


def trend_svg(trend, weeks, series):
    w, h, x0, y0 = 560, 110, 30, 20
    step = w / max(len(weeks) - 1, 1)

    def pts(brand):
        vals = {r["week_start"]: float(r["sov"]) for r in trend if r["brand"] == brand}
        return " ".join(f"{x0 + i*step:.0f},{y0 + h - (vals.get(wk,0)/100)*h:.0f}"
                        for i, wk in enumerate(weeks))
    lines = "".join(
        f'<polyline points="{pts(b)}" fill="none" stroke="{c}" stroke-width="{sw}" stroke-linecap="round"/>'
        for b, c, sw in series)
    labels = "".join(f'<text x="{x0 + i*(w/max(1,min(2,len(weeks)-1)))}" y="150">{wk}</text>'
                     for i, wk in enumerate([weeks[0], weeks[len(weeks)//2], weeks[-1]] if len(weeks) > 2 else weeks))
    legend = "".join(f'<span><i style="background:{c}"></i>{b}</span>' for b, c, _ in series)
    return (f'<div class="card"><h2 class="sec">Тренд Share of Voice</h2>'
            f'<svg viewBox="0 0 600 165" width="100%">'
            f'<line x1="30" y1="130" x2="590" y2="130" stroke="#E4E8F0"/>'
            f'<line x1="30" y1="75" x2="590" y2="75" stroke="#EEF1F6"/>'
            f'<text x="4" y="78">50</text><text x="12" y="133">0</text>'
            f'{lines}{labels}</svg><div class="legend">{legend}</div></div>')


d = data()
if not d["weeks"]:
    st.markdown('<div class="aeo-logo">AEO<span>Radar</span></div>', unsafe_allow_html=True)
    st.info("Данных пока нет — запусти прогон: `python -m monitor.run`")
    st.stop()

cfg = load_config()
brands, providers = d["brands"], d["providers"]
for b in brands:
    b["delta"] = delta(b["sov"], d["prev_sov"].get(b["brand"]))
ours = next((b for b in brands if b["is_ours"]), None)

# ── Шапка ──
st.markdown(
    f'<div class="aeo-logo">AEO<span>Radar</span></div>'
    f'<div class="aeo-meta">{cfg.ours[0] if cfg.ours else "—"} · {d["week"]} · '
    f'{len(cfg.queries)} queries · {" / ".join(providers).upper()} · weeks: {len(d["weeks"])}</div>',
    unsafe_allow_html=True)
st.write("")

# ── KPI ──
c1, c2, c3, c4 = st.columns(4)
with c1:
    kpi("Share of Voice", f'{ours["sov"] if ours else 0}%',
        delta(ours["sov"], d["prev_sov"].get(ours["brand"])) if ours else None)
with c2:
    kpi("Цитаты нашего сайта", f'{d["own_cite"]}%', delta(d["own_cite"], d["own_cite_prev"]))
with c3:
    kpi("Средняя позиция", ours["avg_pos"] if ours and ours["avg_pos"] else "—")
with c4:
    kpi("Недель данных", len(d["weeks"]))
st.write("")

# ── Тренд + каналы ──
left, right = st.columns([1.7, 1])
with left:
    series = []
    if ours:
        series.append((ours["brand"], "#12946A", 2.6))
    for i, b in enumerate([x["brand"] for x in brands if not x["is_ours"]][:2]):
        series.append((b, ["#C6CCDA", "#8FB4F9"][i], 1.5))
    st.markdown(trend_svg(d["trend"], d["weeks"], series), unsafe_allow_html=True)
with right:
    rows = "".join(
        f'<div class="crow"><span class="nm">{CHANNEL_LABELS.get(c["source_type"], c["source_type"])}</span>'
        f'<div class="cbar"><i style="width:{c["pct"]}%;background:{CHANNEL_COLORS.get(c["source_type"], "#98A2B5")}"></i></div>'
        f'<b>{c["pct"]}%</b></div>' for c in d["channels"])
    donors = "".join(
        f'<div class="donor {"ours" if x["is_ours"] else ""}"><span>{x["domain"]}</span><b>{x["n"]}</b></div>'
        for x in d["donors"])
    st.markdown(f'<div class="card"><h2 class="sec">Откуда AI берёт информацию</h2>{rows}'
                f'<div class="lab" style="margin-top:14px">Топ-доноры цитат</div>{donors}</div>',
                unsafe_allow_html=True)
st.write("")

# ── Таблица брендов + алерты ──
left, right = st.columns([1.4, 1])
with left:
    head = "".join(f'<th class="n">{PROVIDER_SHORT.get(p, p[:4].upper())}</th>' for p in providers)
    body = ""
    for b in brands[:10]:
        dl = b["delta"]
        dcls = "up" if dl and dl > 0 else "dn" if dl and dl < 0 else ""
        prov_cells = "".join(
            f'<td class="n">{int(d["per_prov"].get((b["brand"], p), 0)) if d["per_prov"].get((b["brand"], p)) is not None else "·"}</td>'
            for p in providers)
        body += (f'<tr class="{"ours" if b["is_ours"] else ""}"><td>{b["brand"]}</td>'
                 f'<td class="n">{b["sov"]}%</td>'
                 f'<td class="n {dcls}">{f"{dl:+.0f}" if dl is not None else "—"}</td>'
                 f'<td class="n">{b["avg_pos"] or "—"}</td>{prov_cells}</tr>')
    st.markdown(f'<div class="card"><h2 class="sec">Кого рекомендуют AI</h2>'
                f'<table class="aeo"><tr><th>Бренд</th><th class="n">SOV</th>'
                f'<th class="n">Δ</th><th class="n">поз.</th>{head}</tr>{body}</table></div>',
                unsafe_allow_html=True)
with right:
    alerts = [{"sev": "HI", "text": f'Выпала наша цитата: {r["url"]}'} for r in d["lost"]]
    alerts += [{"sev": "MD", "text": f'{b["brand"]} +{b["delta"]} п.п. за неделю'}
               for b in brands if not b["is_ours"] and b["delta"] and b["delta"] >= 5]
    dc = delta(d["own_cite"], d["own_cite_prev"])
    if dc is not None and dc <= -3:
        alerts.append({"sev": "HI", "text": f"Доля own-site цитат упала на {abs(dc)} п.п."})
    items = "".join(f'<div class="al"><span class="badge {"b-red" if a["sev"]=="HI" else "b-amb"}">'
                    f'{a["sev"]}</span><p>{a["text"]}</p></div>' for a in alerts[:6]) \
        or '<p style="color:#98A2B5;font-size:13px">Пока тихо — нужна вторая неделя данных для дельт.</p>'
    st.markdown(f'<div class="card"><h2 class="sec">Требует внимания</h2>{items}</div>',
                unsafe_allow_html=True)

# ── Сырые ответы (проверка глазами) ──
with st.expander("Сырые ответы AI"):
    import psycopg2, psycopg2.extras
    from monitor.config import DATABASE_URL
    with psycopg2.connect(DATABASE_URL) as conn, \
         conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT query_id, provider, query_text, response_text
                       FROM aeo.responses WHERE week_start = %s
                       ORDER BY query_id, provider""", (d["week"],))
        rows = cur.fetchall()
    qids = sorted({r["query_id"] for r in rows})
    if qids:
        sel = st.selectbox("Запрос", qids)
        for r in rows:
            if r["query_id"] == sel:
                st.markdown(f'**{r["provider"]}** — {r["query_text"][:90]}')
                st.write(r["response_text"])
                st.divider()
