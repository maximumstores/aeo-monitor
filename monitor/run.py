
# -*- coding: utf-8 -*-
"""
AEO Monitor — недельный прогон.

    python -m monitor.run
    python -m monitor.run --dry
    python -m monitor.run --provider perplexity --limit 2
"""
import argparse
import logging
import time

from . import config
from .classify import extract_domain, source_type
from .config import load_config, week_start
from .parse import find_mentions
from .providers import anthropic_claude, gemini, openai_gpt, perplexity, scrapingdog_ai_mode

log = logging.getLogger("aeo")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PROVIDERS = {p.NAME: p for p in (gemini, openai_gpt, anthropic_claude, perplexity, scrapingdog_ai_mode)}


def process_citations(result, cfg):
    rows, seen = [], set()
    for i, c in enumerate(result.citations, start=1):
        if c.url in seen:
            continue
        seen.add(c.url)
        domain = extract_domain(c.url, c.title)
        stype = source_type(domain, cfg.our_domains)
        rows.append({
            "url": c.url, "domain": domain, "position": i,
            "is_ours": stype == "own_site", "source_type": stype, "title": c.title,
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--provider", choices=list(PROVIDERS))
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    cfg = load_config()
    week = week_start()
    queries = cfg.queries[: args.limit] if args.limit else cfg.queries

    active = {n: p for n, p in PROVIDERS.items() if p.available()
              and (not args.provider or n == args.provider)}
    if not active:
        log.error("Нет доступных провайдеров - проверь API-ключи в .env")
        return
    log.info("Неделя %s - провайдеры: %s - запросов: %d", week, ", ".join(active), len(queries))

    conn = None
    if not args.dry:
        from . import db
        conn = db.get_conn()

    ok = err = 0
    for name, prov in active.items():
        for q in queries:
            qid, qtext = q["id"], q["text"]
            try:
                result = prov.ask(qtext)
                mentions = find_mentions(result.text, cfg)
                citations = process_citations(result, cfg)

                if args.dry:
                    hit = [m["brand"] for m in mentions if m["mentioned"]]
                    log.info("[%s %s] брендов: %s - цитат: %d", name, qid,
                             ", ".join(hit) or "-", len(citations))
                    for c in citations[:5]:
                        log.info("    %s (%s)", c["domain"], c["source_type"])
                else:
                    from . import db
                    db.upsert_response(conn, week, qid, name, qtext, result.text)
                    db.upsert_mentions(conn, week, qid, name, mentions)
                    db.upsert_citations(conn, week, qid, name, citations)
                    log.info("[%s %s] ok - цитат: %d", name, qid, len(citations))
                ok += 1
            except Exception as e:
                err += 1
                log.warning("[%s %s] FAIL: %s", name, qid, e)
            time.sleep(config.SLEEP_BETWEEN_CALLS)

    if conn:
        conn.close()
    log.info("Готово: ok=%d, fail=%d", ok, err)


if __name__ == "__main__":
    main()
