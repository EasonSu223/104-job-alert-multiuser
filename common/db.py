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
                select line_user_id, keywords, area_codes, area_labels,
                       min_annual_salary, mrt_stations, max_walk_km, include_remote,
                       notify_interval_hours, max_jobs_per_run, last_checked_at
                from subscribers
                where active = true
                """
            )
            return [dict(row) for row in cur.fetchall()]


def upsert_subscriber(
    line_user_id: str,
    keywords: list[str],
    area_codes: list[str] | None,
    area_labels: list[str] | None,
    min_annual_salary: int | None,
    mrt_stations: list[str] | None = None,
    max_walk_km: float | None = None,
    include_remote: bool | None = None,
    notify_interval_hours: int | None = None,
    max_jobs_per_run: int | None = None,
) -> None:
    """新增或覆蓋一位訂閱者的職缺條件（訊息裡有職稱關鍵字時才會呼叫這個函式）。

    include_remote、notify_interval_hours、max_jobs_per_run 是唯一「不覆蓋」的三個
    欄位：訊息裡沒提到時（傳 None），沿用資料庫裡原本的值（新訂閱者則分別預設
    True、1 小時、5 筆），不會被重置，避免使用者每次調整職缺條件都要重講一次這些設定。
    """
    with closing(get_connection()) as conn, conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into subscribers
                    (line_user_id, keywords, area_codes, area_labels, min_annual_salary,
                     mrt_stations, max_walk_km, include_remote, notify_interval_hours,
                     max_jobs_per_run, active, updated_at)
                values (%s, %s, %s, %s, %s, %s, %s, coalesce(%s, true), coalesce(%s, 1),
                        coalesce(%s, 5), true, now())
                on conflict (line_user_id) do update set
                    keywords = excluded.keywords,
                    area_codes = excluded.area_codes,
                    area_labels = excluded.area_labels,
                    min_annual_salary = excluded.min_annual_salary,
                    mrt_stations = excluded.mrt_stations,
                    max_walk_km = excluded.max_walk_km,
                    include_remote = coalesce(%s, subscribers.include_remote),
                    notify_interval_hours = coalesce(%s, subscribers.notify_interval_hours),
                    max_jobs_per_run = coalesce(%s, subscribers.max_jobs_per_run),
                    active = true,
                    updated_at = now()
                """,
                (line_user_id, keywords, area_codes, area_labels, min_annual_salary,
                 mrt_stations, max_walk_km, include_remote, notify_interval_hours, max_jobs_per_run,
                 include_remote, notify_interval_hours, max_jobs_per_run),
            )


def get_subscriber(line_user_id: str) -> dict | None:
    with closing(get_connection()) as conn, conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                select notify_interval_hours, max_jobs_per_run, include_remote,
                       area_codes, area_labels, min_annual_salary, mrt_stations, max_walk_km
                from subscribers where line_user_id = %s
                """,
                (line_user_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def update_settings(
    line_user_id: str,
    notify_interval_hours: int | None = None,
    max_jobs_per_run: int | None = None,
    include_remote: bool | None = None,
    area_codes: list[str] | None = None,
    area_labels: list[str] | None = None,
    min_annual_salary: int | None = None,
    mrt_stations: list[str] | None = None,
    max_walk_km: float | None = None,
) -> bool:
    """只調整既有訂閱者訊息裡明確提到的設定，不動其他沒提到的職缺條件。

    用在訊息裡沒有職稱關鍵字的情況（例如「改成每3小時通知我」「地區改成新北市」
    「不要遠端職缺」）——這種訊息不足以構成一次完整的職缺條件重新訂閱，只更新使用者
    明確提到的那幾項。參數全部是 None 時什麼都不做、直接回傳 False。回傳 False 也代表
    這個 line_user_id 還沒有啟用中的訂閱（呼叫端應該請使用者先描述一次想找的工作，
    而不是默默建立一筆沒有關鍵字的訂閱）。
    """
    updates = []
    params: list = []
    if notify_interval_hours is not None:
        updates.append("notify_interval_hours = %s")
        params.append(notify_interval_hours)
    if max_jobs_per_run is not None:
        updates.append("max_jobs_per_run = %s")
        params.append(max_jobs_per_run)
    if include_remote is not None:
        updates.append("include_remote = %s")
        params.append(include_remote)
    if area_codes is not None:
        updates.append("area_codes = %s")
        params.append(area_codes)
    if area_labels is not None:
        updates.append("area_labels = %s")
        params.append(area_labels)
    if min_annual_salary is not None:
        updates.append("min_annual_salary = %s")
        params.append(min_annual_salary)
    if mrt_stations is not None:
        updates.append("mrt_stations = %s")
        params.append(mrt_stations)
    if max_walk_km is not None:
        updates.append("max_walk_km = %s")
        params.append(max_walk_km)
    if not updates:
        return False

    updates.append("updated_at = now()")
    params.append(line_user_id)
    with closing(get_connection()) as conn, conn:
        with conn.cursor() as cur:
            cur.execute(
                f"update subscribers set {', '.join(updates)} where line_user_id = %s and active = true",
                params,
            )
            return cur.rowcount > 0


def mark_checked(line_user_id: str) -> None:
    with closing(get_connection()) as conn, conn:
        with conn.cursor() as cur:
            cur.execute(
                "update subscribers set last_checked_at = now() where line_user_id = %s",
                (line_user_id,),
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
