# -*- coding: utf-8 -*-
"""PostgreSQL: подключение и upsert'ы. Прогон недели можно гонять повторно."""
import psycopg2
import psycopg2.extras

from .config import DATABASE_URL


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан в .env")
    return psycopg2.connect(DATABASE_URL)


def upsert_response(conn, week, qid, provider, qtext, rtext):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO aeo.responses (week_start, query_id, provider, query_text, response_text)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (week_start, query_id, provider)
               DO UPDATE SET response_text = EXCLUDED.response_text, fetched_at = now()""",
            (week, qid, provider, qtext, rtext),
        )
    conn.commit()


def upsert_mentions(conn, week, qid, provider, rows):
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            """INSERT INTO aeo.mentions
               (week_start, query_id, provider, brand, is_ours, mentioned, first_position, mention_count)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (week_start, query_id, provider, brand)
               DO UPDATE SET mentioned = EXCLUDED.mentioned,
                             first_position = EXCLUDED.first_position,
                             mention_count = EXCLUDED.mention_count""",
            [
                (week, qid, provider, r["brand"], r["is_ours"], r["mentioned"],
                 r["first_position"], r["mention_count"])
                for r in rows
            ],
        )
    conn.commit()


def upsert_citations(conn, week, qid, provider, rows):
    if not rows:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            """INSERT INTO aeo.citations
               (week_start, query_id, provider, url, domain, position, is_ours, source_type, title)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (week_start, query_id, provider, url)
               DO UPDATE SET position = EXCLUDED.position,
                             source_type = EXCLUDED.source_type,
                             title = EXCLUDED.title,
                             fetched_at = now()""",
            [
                (week, qid, provider, r["url"], r["domain"], r["position"],
                 r["is_ours"], r["source_type"], r.get("title"))
                for r in rows
            ],
        )
    conn.commit()
