# -*- coding: utf-8 -*-
"""
crawler_check.py v3 — проверка доступности сайта для AI-краулеров.

Отвечает на вопрос: физически ли AI-движки могут прочитать страницу?
Если OAI-SearchBot получает 403 — качество JSON-LD не имеет значения,
страница просто не попадёт в ответ.

Три независимые проверки:
  1. robots.txt          — что владелец разрешил декларативно
  2. живой GET с UA бота — что реально отдаёт сервер/WAF/CDN
                           (WAF применяется ДО robots.txt и перекрывает его)
  3. объём контента      — не отдаётся ли боту урезанная версия страницы

Четыре исхода на бота:
  ok       — робот дошёл и получил полный контент
  blocked  — робот заблокирован (robots.txt, WAF, CDN, challenge, обрезка)
  unknown  — проверить не удалось (rate limit, таймаут, 5xx, сетевая ошибка)
  skipped  — проверка не проводилась (страница недоступна для всех)

unknown и skipped НИКОГДА не считаются блокировкой и не входят в score.
В отчёте клиенту ложная тревога дороже пропущенной проблемы.

Если страницы не существует (404/410) или сервер лежит (5xx) — проверка ботов
не проводится вообще: нечего проверять, и незачем слать 12 запросов впустую.

CLI:
    python crawler_check.py https://merino.tech
    python crawler_check.py https://merino.tech --sitemap 5
    python crawler_check.py https://a.com https://b.com --save --json

    PowerShell:  $env:DATABASE_URL="postgresql://..."
    bash:        export DATABASE_URL="postgresql://..."

Этика: инструмент диагностический. Запускай на своих доменах и доменах
клиентов с их согласия, с паузой между запросами.
"""

import html as _html
import re
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

# .env подхватывается автоматически: скрипт можно запускать из любой папки,
# переменные окружения задавать не нужно.
try:
    from pathlib import Path

    from dotenv import load_dotenv

    for _candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"):
        if _candidate.exists():
            load_dotenv(_candidate, override=False)
except ImportError:
    pass  # python-dotenv не установлен — работаем на переменных окружения

DDL_CRAWLER = """
CREATE SCHEMA IF NOT EXISTS aeo;

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

ALTER TABLE aeo.crawler_access  ADD COLUMN IF NOT EXISTS category text;
ALTER TABLE aeo.crawler_scores  ADD COLUMN IF NOT EXISTS page_status text;
ALTER TABLE aeo.crawler_scores  ADD COLUMN IF NOT EXISTS jsonld_blocks int;
ALTER TABLE aeo.crawler_scores  ADD COLUMN IF NOT EXISTS script_bytes int;
ALTER TABLE aeo.crawler_scores  ADD COLUMN IF NOT EXISTS style_bytes int;
ALTER TABLE aeo.crawler_scores  ADD COLUMN IF NOT EXISTS markup_bytes int;
"""

TIMEOUT = 20
PAUSE = 2.5
RATE_LIMIT_BACKOFF = 20
RETRIES = 1
SOFT_BLOCK_THRESHOLD = 50
THIN_CONTENT_RATIO = 5.0

SEARCH, AGENT, TRAINING = "search", "agent", "training"

BOTS = {
    # --- Search / Agent: блокировка = потеря видимости ---
    "OAI-SearchBot": ("OAI-SearchBot",
                      "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
                      "OAI-SearchBot/1.0; +https://openai.com/searchbot", SEARCH),
    "PerplexityBot": ("PerplexityBot",
                      "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
                      "PerplexityBot/1.0; +https://perplexity.ai/perplexitybot", SEARCH),
    "Amazonbot": ("Amazonbot",
                  "Mozilla/5.0 (compatible; Amazonbot/0.1; "
                  "+https://developer.amazon.com/amazonbot)", SEARCH),
    "Google-Extended": ("Google-Extended", None, SEARCH),
    "ChatGPT-User": ("ChatGPT-User",
                     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
                     "ChatGPT-User/1.0; +https://openai.com/bot", AGENT),
    "Claude-User": ("Claude-User",
                    "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
                    "Claude-User/1.0; +Claude-User@anthropic.com", AGENT),
    "Perplexity-User": ("Perplexity-User",
                        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
                        "Perplexity-User/1.0; +https://perplexity.ai/perplexity-user", AGENT),
    # --- Training: блокировка почти не влияет на видимость сегодня ---
    "GPTBot": ("GPTBot",
               "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
               "GPTBot/1.2; +https://openai.com/gptbot", TRAINING),
    "ClaudeBot": ("ClaudeBot",
                  "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
                  "ClaudeBot/1.0; +claudebot@anthropic.com", TRAINING),
    "CCBot": ("CCBot", "CCBot/2.0 (https://commoncrawl.org/faq/)", TRAINING),
    "meta-externalagent": ("meta-externalagent",
                           "meta-externalagent/1.1 (+https://developers.facebook.com/"
                           "docs/sharing/webmasters/crawler)", TRAINING),
    "Applebot-Extended": ("Applebot-Extended", None, TRAINING),
}

CRITICAL = {n for n, (_, _, cat) in BOTS.items() if cat in (SEARCH, AGENT)}

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

CF_MARKERS = ("just a moment", "cf-chl", "checking your browser",
              "attention required", "cloudflare ray id", "enable javascript and cookies")

# Реальная блокировка бота
BLOCKING = {"cloudflare_challenge", "cloudflare_block", "waf_or_server",
            "partial_content", "robots"}
# Не блокировка: проблема сети, лимитов или самой страницы
UNCERTAIN = {"rate_limit", "timeout", "error", "server_error", "not_found", "http_error"}
# Страница нерабочая для всех — проверять ботов бессмысленно
PAGE_LEVEL = {"not_found", "server_error", "timeout", "error"}


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------
def fetch_robots(base: str):
    try:
        r = requests.get(urljoin(base, "/robots.txt"), timeout=TIMEOUT,
                         headers={"User-Agent": BROWSER_UA})
        if r.status_code == 200 and "text" in r.headers.get("content-type", ""):
            return r.text, 200
        return None, r.status_code
    except Exception:
        return None, None


def robots_verdict(robots_text, token: str, path: str) -> tuple[str, str]:
    """allowed | blocked_explicit | blocked_wildcard | no_robots"""
    if robots_text is None:
        return "no_robots", "robots.txt не найден — по умолчанию доступ открыт"
    mentioned = re.search(rf"^\s*user-agent:\s*{re.escape(token)}\s*$",
                          robots_text, re.IGNORECASE | re.MULTILINE) is not None
    rp = RobotFileParser()
    rp.parse(robots_text.splitlines())
    if rp.can_fetch(token, path):
        return "allowed", ("своя разрешающая группа" if mentioned
                           else "не запрещён (действует User-agent: *)")
    if mentioned:
        return "blocked_explicit", f"в robots.txt есть Disallow для {token}"
    return "blocked_wildcard", "запрещён общим правилом User-agent: *"


# ---------------------------------------------------------------------------
# Живой запрос
# ---------------------------------------------------------------------------
def classify_response(r) -> tuple[str, str]:
    server = (r.headers.get("server") or "").lower()
    mitigated = (r.headers.get("cf-mitigated") or "").lower()
    body = (r.text or "")[:4000].lower()
    is_cf = "cloudflare" in server or bool(r.headers.get("cf-ray"))

    if r.status_code < 300:
        if any(m in body for m in CF_MARKERS):
            return "cloudflare_challenge", "200, но в теле JS-челлендж вместо контента"
        return "none", f"{r.status_code}, контент отдан"

    # Страницы просто нет — это не блокировка
    if r.status_code in (404, 410):
        return "not_found", f"HTTP {r.status_code} — страницы не существует"
    if r.status_code == 429:
        return "rate_limit", "429 — сработал rate limit сервера"
    if r.status_code >= 500:
        return "server_error", f"HTTP {r.status_code} — ошибка на стороне сервера"

    if mitigated == "challenge" or any(m in body for m in CF_MARKERS):
        return "cloudflare_challenge", f"{r.status_code}: Cloudflare показывает челлендж"
    if is_cf and r.status_code in (401, 403, 451):
        return "cloudflare_block", (f"{r.status_code} от Cloudflare — вероятно активна "
                                    f"блокировка AI-краулеров")
    if r.status_code in (401, 403, 451):
        return "waf_or_server", f"{r.status_code} — блокировка на уровне сервера или WAF"
    return "http_error", f"HTTP {r.status_code}"


def live_fetch(url: str, ua: str, retries: int = RETRIES) -> dict:
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, timeout=TIMEOUT, allow_redirects=True,
                             headers={"User-Agent": ua, "Accept": "text/html,*/*"})
            blocked_by, detail = classify_response(r)
            if blocked_by == "rate_limit" and attempt < retries:
                time.sleep(RATE_LIMIT_BACKOFF)
                continue
            return {"status": r.status_code, "blocked_by": blocked_by, "detail": detail,
                    "bytes": len(r.text or ""), "html": r.text or "",
                    "final_url": r.url,
                    "cf": bool(r.headers.get("cf-ray"))
                          or "cloudflare" in (r.headers.get("server") or "").lower()}
        except requests.Timeout:
            if attempt < retries:
                time.sleep(5)
                continue
            return {"status": None, "blocked_by": "timeout",
                    "detail": f"сервер не ответил за {TIMEOUT} сек",
                    "bytes": 0, "html": "", "final_url": url, "cf": False}
        except Exception as e:
            return {"status": None, "blocked_by": "error", "detail": str(e)[:200],
                    "bytes": 0, "html": "", "final_url": url, "cf": False}


# ---------------------------------------------------------------------------
# Контент
# ---------------------------------------------------------------------------
def visible_text(html: str) -> str:
    t = re.sub(r"<script.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def analyze_content(html: str) -> dict:
    if not html:
        return {"html_bytes": 0, "text_bytes": 0, "content_ratio_pct": None,
                "jsonld_blocks": 0, "jsonld_types": [],
                "script_bytes": 0, "style_bytes": 0, "markup_bytes": 0}
    txt = visible_text(html)
    blocks = re.findall(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
                        html, flags=re.IGNORECASE | re.DOTALL)
    types = sorted({t for b in blocks
                    for t in re.findall(r'"@type"\s*:\s*"([^"]+)"', b)})

    # v4: разбивка веса страницы — сколько байт уходит на script/style,
    # сколько на прочую разметку (теги/атрибуты/комментарии), сколько на видимый текст.
    script_bytes = sum(len(m) for m in re.findall(r"<script.*?</script>", html,
                       flags=re.IGNORECASE | re.DOTALL))
    style_bytes = sum(len(m) for m in re.findall(r"<style.*?</style>", html,
                      flags=re.IGNORECASE | re.DOTALL))
    html_bytes = len(html)
    text_bytes = len(txt)
    markup_bytes = max(html_bytes - script_bytes - style_bytes - text_bytes, 0)

    return {"html_bytes": html_bytes, "text_bytes": text_bytes,
            "content_ratio_pct": round(100 * text_bytes / html_bytes, 2),
            "jsonld_blocks": len(blocks), "jsonld_types": types,
            "script_bytes": script_bytes, "style_bytes": style_bytes,
            "markup_bytes": markup_bytes}


# ---------------------------------------------------------------------------
# Поиск реальных URL через sitemap
# ---------------------------------------------------------------------------
def _sitemap_locs(xml: str) -> list[str]:
    """Извлекает <loc> и раскодирует HTML-сущности (&amp; -> &)."""
    return [_html.unescape(u) for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)]


def _is_sitemap(u: str) -> bool:
    """Shopify отдаёт вложенные карты как sitemap_products_1.xml?from=..&to=..,
    поэтому смотрим на путь без query-строки."""
    path = urlparse(u).path.lower()
    return path.endswith(".xml") or "sitemap" in path


def discover_urls(base: str, limit: int = 5) -> list[str]:
    """Достаёт реальные URL товаров и коллекций из sitemap.xml.
    Нужен, чтобы не проверять выдуманные адреса."""
    def get(u):
        try:
            r = requests.get(u, timeout=TIMEOUT, headers={"User-Agent": BROWSER_UA})
            return r.text if r.status_code == 200 else ""
        except Exception:
            return ""

    root = get(urljoin(base, "/sitemap.xml"))
    if not root:
        return []

    locs = _sitemap_locs(root)
    children = [u for u in locs if _is_sitemap(u)]
    pages: list[str] = [u for u in locs if not _is_sitemap(u)]

    for child in children[:8]:
        if not re.search(r"product|collection|page", child, re.IGNORECASE):
            continue
        pages += [u for u in _sitemap_locs(get(child)) if not _is_sitemap(u)]
        time.sleep(0.5)

    def rank(u: str) -> int:
        if "/products/" in u:
            return 0
        if "/collections/" in u:
            return 1
        if "/pages/" in u or "/blogs/" in u:
            return 2
        return 3

    seen, result = set(), []
    for u in sorted(pages, key=rank):
        if u in seen:
            continue
        seen.add(u)
        result.append(u)
        if len(result) >= limit:
            break
    return result


# ---------------------------------------------------------------------------
# Основная проверка
# ---------------------------------------------------------------------------
def _empty(url, page_status, summary, baseline, content, robots_status, bots):
    return {"url": url, "page_status": page_status, "robots_txt_status": robots_status,
            "cdn": "cloudflare" if baseline.get("cf") else None,
            "browser_baseline": {k: baseline.get(k) for k in ("status", "blocked_by", "detail", "bytes")},
            "content": content, "score": None,
            "bots_ok": 0, "bots_blocked": 0, "bots_unknown": 0,
            "critical_blocked": [], "critical_unknown": [], "soft_blocked": [],
            "summary": summary,
            "bots": [{"bot": n, "category": c[2], "critical": n in CRITICAL,
                      "verdict": "skipped", "robots_verdict": "-", "robots_detail": "-",
                      "http_status": None, "blocked_by": None, "content_bytes": None,
                      "content_delta_pct": None, "detail": "проверка не проводилась"}
                     for n, c in bots.items()]}


def check_url(url: str, bots: dict = None) -> dict:
    bots = bots or BOTS
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    path = p.path or "/"

    robots_text, robots_status = fetch_robots(base)

    # Эталон: обычный браузер. Всё считается относительно него.
    baseline = live_fetch(url, BROWSER_UA)
    content = analyze_content(baseline["html"])

    # Страница нерабочая — ботов не мучаем
    if baseline["blocked_by"] in PAGE_LEVEL:
        return _empty(url, baseline["blocked_by"],
                      f"Проверка ботов не проводилась: {baseline['detail']}. "
                      f"Это состояние самой страницы, а не блокировка краулеров. "
                      f"Возьми реальный URL из sitemap: python crawler_check.py {base} --sitemap 5",
                      baseline, content, robots_status, bots)

    if baseline["blocked_by"] != "none":
        return _empty(url, baseline["blocked_by"],
                      f"Страница не отдаётся даже обычному браузеру ({baseline['detail']}). "
                      f"Сначала почини доступность, потом проверяй ботов.",
                      baseline, content, robots_status, bots)

    base_bytes = baseline["bytes"]
    order = sorted(bots.items(), key=lambda kv: (kv[0] not in CRITICAL, kv[0]))

    rows, n_ok, n_blocked, n_unknown = [], 0, 0, 0
    critical_blocked, critical_unknown, soft_blocked = [], [], []

    for name, (token, ua, category) in order:
        rv, rd = robots_verdict(robots_text, token, path)
        is_crit = name in CRITICAL
        row = {"bot": name, "category": category, "critical": is_crit,
               "robots_verdict": rv, "robots_detail": rd}

        if ua is None:
            row.update(verdict="blocked" if rv.startswith("blocked") else "ok",
                       http_status=None, blocked_by="robots" if rv.startswith("blocked") else None,
                       content_bytes=None, content_delta_pct=None,
                       detail="токен robots.txt, отдельного краулера нет")
        elif rv.startswith("blocked"):
            row.update(verdict="blocked", http_status=None, blocked_by="robots",
                       content_bytes=None, content_delta_pct=None, detail=rd)
        else:
            time.sleep(PAUSE)
            f = live_fetch(url, ua)
            bb, detail = f["blocked_by"], f["detail"]
            delta = round(100 * f["bytes"] / base_bytes, 1) if base_bytes and f["bytes"] else None

            if bb in UNCERTAIN:
                verdict = "unknown"
            elif bb in BLOCKING:
                verdict = "blocked"
            elif delta is not None and delta < SOFT_BLOCK_THRESHOLD:
                verdict, bb = "blocked", "partial_content"
                detail = f"{f['status']}, отдано лишь {delta}% от браузерной версии"
                soft_blocked.append(name)
            else:
                verdict = "ok"

            row.update(verdict=verdict, http_status=f["status"], blocked_by=bb,
                       content_bytes=f["bytes"], content_delta_pct=delta, detail=detail)

        if row["verdict"] == "ok":
            n_ok += 1
        elif row["verdict"] == "blocked":
            n_blocked += 1
            if is_crit:
                critical_blocked.append(name)
        else:
            n_unknown += 1
            if is_crit:
                critical_unknown.append(name)
        rows.append(row)

    decided = n_ok + n_blocked
    score = round(100 * n_ok / decided) if decided else None

    if critical_blocked:
        summary = ("Заблокированы краулеры, формирующие ответы в реальном времени: "
                   + ", ".join(critical_blocked)
                   + ". Страница не попадёт в ответ AI, какой бы ни была разметка.")
    elif soft_blocked:
        summary = ("Доступ открыт, но части ботов отдаётся урезанная версия: "
                   + ", ".join(soft_blocked))
    else:
        summary = "Все критичные AI-краулеры имеют доступ и получают полную страницу."

    if critical_unknown:
        summary += (" Не удалось проверить (не блокировка, нужен повтор): "
                    + ", ".join(critical_unknown) + ".")

    cr = content["content_ratio_pct"]
    if cr is not None and cr < THIN_CONTENT_RATIO:
        hb = content["html_bytes"]
        sb = content["script_bytes"]
        stb = content["style_bytes"]
        mb = content["markup_bytes"]
        summary += (f" Отдельно: полезный текст — {cr}% от {hb:,} байт HTML "
                    f"({content['text_bytes']:,} символов). Страница тяжёлая для машинного чтения. "
                    f"Разбивка веса: script {sb:,} б ({100*sb/hb:.0f}%), "
                    f"style {stb:,} б ({100*stb/hb:.0f}%), "
                    f"разметка/теги {mb:,} б ({100*mb/hb:.0f}%).")

    return {"url": url, "page_status": "ok", "robots_txt_status": robots_status,
            "cdn": "cloudflare" if baseline.get("cf") else None,
            "browser_baseline": {k: baseline.get(k) for k in ("status", "blocked_by", "detail", "bytes")},
            "content": content, "score": score,
            "bots_ok": n_ok, "bots_blocked": n_blocked, "bots_unknown": n_unknown,
            "critical_blocked": critical_blocked, "critical_unknown": critical_unknown,
            "soft_blocked": soft_blocked, "summary": summary, "bots": rows}


# ---------------------------------------------------------------------------
# Сохранение
# ---------------------------------------------------------------------------
def save_report(conn, report: dict):
    with conn.cursor() as cur:
        cur.execute(DDL_CRAWLER)
        for b in report["bots"]:
            if b["verdict"] == "skipped":
                continue
            cur.execute("""
                INSERT INTO aeo.crawler_access
                    (url, bot, verdict, is_critical, category, robots_verdict, http_status,
                     blocked_by, content_bytes, content_delta_pct, detail, checked_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (url, bot) DO UPDATE SET
                    verdict=EXCLUDED.verdict, is_critical=EXCLUDED.is_critical,
                    category=EXCLUDED.category, robots_verdict=EXCLUDED.robots_verdict,
                    http_status=EXCLUDED.http_status, blocked_by=EXCLUDED.blocked_by,
                    content_bytes=EXCLUDED.content_bytes,
                    content_delta_pct=EXCLUDED.content_delta_pct,
                    detail=EXCLUDED.detail, checked_at=now()""",
                (report["url"], b["bot"], b["verdict"], b["critical"], b["category"],
                 b["robots_verdict"], b["http_status"], b["blocked_by"],
                 b["content_bytes"], b["content_delta_pct"], b["detail"]))

        c = report["content"]
        cur.execute("""
            INSERT INTO aeo.crawler_scores
                (url, page_status, score, bots_ok, bots_blocked, bots_unknown,
                 html_bytes, text_bytes, content_ratio_pct, jsonld_blocks,
                 script_bytes, style_bytes, markup_bytes, summary, checked_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (url) DO UPDATE SET
                page_status=EXCLUDED.page_status, score=EXCLUDED.score,
                bots_ok=EXCLUDED.bots_ok, bots_blocked=EXCLUDED.bots_blocked,
                bots_unknown=EXCLUDED.bots_unknown, html_bytes=EXCLUDED.html_bytes,
                text_bytes=EXCLUDED.text_bytes, content_ratio_pct=EXCLUDED.content_ratio_pct,
                jsonld_blocks=EXCLUDED.jsonld_blocks,
                script_bytes=EXCLUDED.script_bytes, style_bytes=EXCLUDED.style_bytes,
                markup_bytes=EXCLUDED.markup_bytes,
                summary=EXCLUDED.summary, checked_at=now()""",
            (report["url"], report["page_status"], report["score"], report["bots_ok"],
             report["bots_blocked"], report["bots_unknown"], c["html_bytes"],
             c["text_bytes"], c["content_ratio_pct"], c["jsonld_blocks"],
             c["script_bytes"], c["style_bytes"], c["markup_bytes"], report["summary"]))
    conn.commit()


# ---------------------------------------------------------------------------
# Печать
# ---------------------------------------------------------------------------
MARK = {"ok": "OK ", "blocked": "BLK", "unknown": "UNK", "skipped": "---"}
GROUPS = ((SEARCH, "SEARCH — формируют ответ"),
          (AGENT, "AGENT — ходят по поручению пользователя"),
          (TRAINING, "TRAINING — собирают корпус для обучения"))


def print_report(rep: dict):
    c = rep["content"]
    print(f"\n{'=' * 72}\n{rep['url']}")

    if rep["page_status"] != "ok":
        print(f"\n{rep['summary']}\n")
        return

    score = f"{rep['score']}/100" if rep["score"] is not None else "н/д"
    print(f"Доступность для AI-краулеров: {score}   "
          f"ok {rep['bots_ok']} · blocked {rep['bots_blocked']} · unknown {rep['bots_unknown']}")
    if c["html_bytes"]:
        types = (", ".join(c["jsonld_types"]) or "нет") if c["jsonld_blocks"] else "нет"
        print(f"HTML {c['html_bytes']:,} б · текст {c['text_bytes']:,} "
              f"({c['content_ratio_pct']}%) · JSON-LD: {types}")
        print(f"Разбивка: script {c['script_bytes']:,} б · style {c['style_bytes']:,} б "
              f"· прочая разметка {c['markup_bytes']:,} б")
    if rep["cdn"]:
        print(f"CDN: {rep['cdn']}")
    print(f"\n{rep['summary']}\n")

    for cat, title in GROUPS:
        group = [b for b in rep["bots"] if b["category"] == cat]
        if not group:
            continue
        print(f"  {title}")
        for b in group:
            crit = "!" if b["critical"] and b["verdict"] == "blocked" else " "
            d = b.get("content_delta_pct")
            extra = f"  [{d}% байт]" if d is not None and d < 95 else ""
            print(f"  {MARK[b['verdict']]}{crit} {b['bot']:<20} "
                  f"{(b['blocked_by'] or '-'):<20} {b['detail']}{extra}")
        print()


if __name__ == "__main__":
    import json
    import os
    import sys

    argv = sys.argv[1:]
    flags = {a for a in argv if a.startswith("--")}
    positional = [a for a in argv if not a.startswith("--")]

    sitemap_n = 0
    if "--sitemap" in flags:
        i = argv.index("--sitemap")
        if i + 1 < len(argv) and argv[i + 1].isdigit():
            sitemap_n = int(argv[i + 1])
            positional = [a for a in positional if a != argv[i + 1]]
        else:
            sitemap_n = 5

    targets = positional or ["https://merino.tech"]

    if sitemap_n:
        p = urlparse(targets[0])
        found = discover_urls(f"{p.scheme}://{p.netloc}", sitemap_n)
        if found:
            print(f"Из sitemap выбрано страниц: {len(found)}")
            for u in found:
                print(f"  {u}")
            targets = found
        else:
            print("sitemap.xml не найден или пуст — проверяю указанный URL")

    conn = None
    if "--save" in flags:
        dsn = os.environ.get("DATABASE_URL")
        if dsn and ("..." in dsn or dsn.strip() in ("postgresql://", "postgres://")):
            print(f"\nDATABASE_URL выглядит как заглушка: {dsn}")
            print("  Убери её из сессии:  Remove-Item Env:DATABASE_URL")
            print("  и значение подхватится из .env")
            dsn = None
        if not dsn:
            print("\nDATABASE_URL не задан — сохранение пропущено.")
            print('  PowerShell:  $env:DATABASE_URL="postgresql://..."')
            print('  bash:        export DATABASE_URL="postgresql://..."')
        else:
            import psycopg2
            conn = psycopg2.connect(dsn)

    reports = []
    try:
        for t in targets:
            rep = check_url(t)
            reports.append(rep)
            print_report(rep)
            if conn:
                save_report(conn, rep)
    finally:
        if conn:
            conn.close()
            print(f"Сохранено: {len(reports)} → aeo.crawler_access / aeo.crawler_scores")

    if "--json" in flags:
        print(json.dumps(reports if len(reports) > 1 else reports[0],
                         ensure_ascii=False, indent=2))
