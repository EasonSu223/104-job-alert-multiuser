"""共用的 Supabase Postgres 存取層。

排程腳本 (scripts/job_alert.py) 與 webhook 服務 (webhook_service/app.py)
都透過這個模組讀寫同一個資料庫，避免兩邊各自維護一套存取邏輯。

連線字串請用 Supabase 後台的 Transaction pooler（6543 port），不要用 5432
的直連——GitHub Actions runner 與 Render 免費方案都是 IPv4-only，Supabase
的直連預設是 IPv6-only。
"""

import os
from contextlib import closing

import psycopg2
import psycopg2.extras


def get_connection():
    return psycopg2.connect(os.environ["SUPABASE_DB_URL"])


def get_active_subscribers() -> list[dict]:
    with closing(get_connection()) as conn, conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                select line_user_id, keywords, area_code, area_label,
                       min_annual_salary, mrt_stations, max_walk_km
                from subscribers
                where active = true
                """
            )
            return [dict(row) for row in cur.fetchall()]


def upsert_subscriber(
    line_user_id: str,
    keywords: list[str],
    area_code: str | None,
    area_label: str | None,
    min_annual_salary: int | None,
) -> None:
    with closing(get_connection()) as conn, conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into subscribers
                    (line_user_id, keywords, area_code, area_label, min_annual_salary, active, updated_at)
                values (%s, %s, %s, %s, %s, true, now())
                on conflict (line_user_id) do update set
                    keywords = excluded.keywords,
                    area_code = excluded.area_code,
                    area_label = excluded.area_label,
                    min_annual_salary = excluded.min_annual_salary,
                    active = true,
                    updated_at = now()
                """,
                (line_user_id, keywords, area_code, area_label, min_annual_salary),
            )


def deactivate_subscriber(line_user_id: str) -> None:
    with closing(get_connection()) as conn, conn:
        with conn.cursor() as cur:
            cur.execute(
                "update subscribers set active = false, updated_at = now() where line_user_id = %s",
                (line_user_id,),
            )


def get_seen_job_nos(line_user_id: str) -> set[str]:
    with closing(get_connection()) as conn, conn:
        with conn.cursor() as cur:
            cur.execute(
                "select job_no from seen_jobs where line_user_id = %s",
                (line_user_id,),
            )
            return {row[0] for row in cur.fetchall()}


def mark_seen(line_user_id: str, job_nos: list[str]) -> None:
    if not job_nos:
        return
    with closing(get_connection()) as conn, conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                insert into seen_jobs (line_user_id, job_no)
                values %s
                on conflict (line_user_id, job_no) do nothing
                """,
                [(line_user_id, job_no) for job_no in job_nos],
            )
