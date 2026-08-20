#!/usr/bin/env python3
"""
104 人力銀行職缺自動通知程式（多使用者版）
GitHub Actions 排程固定每小時觸發一次這支程式，但實際上多久幫某個訂閱者「檢查一次」
是由他自己的 notify_interval_hours 決定（自然語言訂閱時可以說「改成每 3 小時通知我」）；
每次執行會：
  1. 從 Supabase 讀取所有啟用中的訂閱者（各自的關鍵字／地區／薪資／通知頻率條件）
  2. 依「所有訂閱者關鍵字聯集」向 104 職缺搜尋 API 抓取候選職缺（通勤地區 + 全遠端 兩種查詢）
  3. 對每個「該檢查了」的訂閱者（距離上次檢查已超過他設定的頻率），用他自己的條件過濾候選職缺
  4. 排除已經通知過該訂閱者的職缺（記錄在 Supabase 的 seen_jobs 表）
  5. 把新符合條件的職缺（每次最多 MAX_JOBS_PER_RUN 筆），透過 LINE Messaging API 推播給該訂閱者
"""

import os
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import db  # noqa: E402

# ============================================================
# 全域規則（對所有訂閱者一律生效，不因人而異）
# ============================================================

# 想直接排除的關鍵字（比對職缺名稱），例如 派遣、建教、工讀 等，預設不排除
EXCLUDE_KEYWORDS: list[str] = []

# 明確沒有經驗、不想要的技術棧：只要出現在「職缺名稱」或「技能需求」裡就直接排除，
# 就算標題也含「前端/後端/全端工程師」也一樣剔除（例如「全端工程師(iOS/Android)」）
STACK_EXCLUDE_KEYWORDS = [
    "iOS", "Android", "Swift", "Objective-C", "Kotlin", "Flutter",
    "React Native", "小程式", "鴻蒙", "HarmonyOS", "App工程師", "APP工程師",
    "Unity", "遊戲工程師", "嵌入式", "韌體", "firmware",
]

# 依常見 .NET/前端技術棧設定的關鍵字。職缺的「技能需求」或「職缺內容」至少要
# 命中一個，才會通知——用來避免收到跟目標技術棧差太多的職缺。
PREFERRED_SKILL_KEYWORDS = [
    ".NET", "C#", "ASP.NET", "MVC", "Web API", "Entity Framework", "EF Core",
    "Vue", "Vue.js", "JavaScript", "TypeScript", "jQuery",
    "SQL Server", "T-SQL", "MSSQL", "MS SQL",
    "Web Forms", "Razor", "Bootstrap", "Element Plus", "Vite",
]

# 月薪 x 幾個月概估年薪；104 顯示「面議」時一律先納入，讓使用者自行判斷
BONUS_MONTHS = 14

# 每個關鍵字 / 每種查詢類型，最多翻幾頁（每頁 30 筆）；避免對 104 伺服器造成太大負擔
PAGE_LIMIT = 6

# 只抓「isnew 天數內」有更新的職缺，降低資料量（104 的 isnew 語意較接近「近期有更新」而非嚴格新刊登）
ISNEW_DAYS = 14

# 訂閱者沒有自訂「每次最多幾筆」時的預設值（使用者可透過 LINE 訊息自訂，範圍 1~20，
# 存在 Supabase 的 max_jobs_per_run 欄位）。優先送最近更新的職缺，沒送到的職缺會留到
# 下次執行繼續判斷是否還沒通知過，之後幾次執行會陸續送出
DEFAULT_MAX_JOBS_PER_RUN = 5

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://www.104.com.tw/jobs/search/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9",
}

API_URL = "https://www.104.com.tw/jobs/search/api/jobs"


# ============================================================
# 104 職缺搜尋
# ============================================================

def fetch_jobs(keyword: str, *, area: str | None, remote: bool) -> list[dict]:
    """向 104 職缺搜尋 API 抓取符合關鍵字的職缺，最多翻 PAGE_LIMIT 頁。"""
    jobs: list[dict] = []
    for page in range(1, PAGE_LIMIT + 1):
        params = {
            "jobsource": "index_s",
            "order": 1,
            "asc": 0,
            "keyword": keyword,
            "mode": "s",
            "page": page,
            "isnew": ISNEW_DAYS,
        }
        if area:
            params["area"] = area
        if remote:
            params["remoteWork"] = "1,2"

        try:
            resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=20)
        except requests.RequestException as exc:
            print(f"  [警告] 請求失敗：{exc}", file=sys.stderr)
            break

        if resp.status_code != 200:
            print(f"  [警告] HTTP {resp.status_code}，關鍵字={keyword} page={page}", file=sys.stderr)
            break

        try:
            payload = resp.json()
        except ValueError:
            print("  [警告] 回傳內容不是合法 JSON（可能被 Cloudflare 擋下）", file=sys.stderr)
            break

        page_jobs = payload.get("data", [])
        if not page_jobs:
            break

        for job in page_jobs:
            job["_source_remote_query"] = remote
        jobs.extend(page_jobs)

        pagination = payload.get("metadata", {}).get("pagination", {})
        if page >= pagination.get("lastPage", page):
            break

        time.sleep(random.uniform(1.5, 2.5))  # 對 104 伺服器客氣一點

    return jobs


def collect_candidate_jobs(
    keywords: set[str], area_codes: set[str], any_nationwide: bool
) -> tuple[dict[str, dict], dict[str, set[str]], set[str]]:
    """對所有訂閱者的關鍵字聯集，各做「訂閱者所在城市聯集」與「全遠端」查詢。

    回傳 (candidates, job_keywords, remote_job_nos)：
      candidates      -- job_no -> job 原始資料
      job_keywords    -- job_no -> 命中的關鍵字集合（用來還原「這個職缺是哪些關鍵字查到的」）
      remote_job_nos  -- 「全遠端」查詢查到的 job_no 集合
    """
    candidates: dict[str, dict] = {}
    job_keywords: dict[str, set[str]] = defaultdict(set)
    remote_job_nos: set[str] = set()

    area_param = ",".join(sorted(area_codes)) if area_codes else None

    for keyword in keywords:
        if area_param:
            print(f"搜尋關鍵字：{keyword}（地區：{area_param}）")
            for job in fetch_jobs(keyword, area=area_param, remote=False):
                candidates.setdefault(job["jobNo"], job)
                job_keywords[job["jobNo"]].add(keyword)
            time.sleep(random.uniform(1.5, 2.5))

        if any_nationwide or not area_codes:
            print(f"搜尋關鍵字：{keyword}（不限地區）")
            for job in fetch_jobs(keyword, area=None, remote=False):
                candidates.setdefault(job["jobNo"], job)
                job_keywords[job["jobNo"]].add(keyword)
            time.sleep(random.uniform(1.5, 2.5))

        print(f"搜尋關鍵字：{keyword}（全遠端）")
        for job in fetch_jobs(keyword, area=None, remote=True):
            candidates.setdefault(job["jobNo"], job)
            job_keywords[job["jobNo"]].add(keyword)
            remote_job_nos.add(job["jobNo"])
        time.sleep(random.uniform(1.5, 2.5))

    return candidates, job_keywords, remote_job_nos


# ============================================================
# 條件過濾
# ============================================================

def is_location_ok(
    job: dict,
    area_label: str | None,
    mrt_stations: list[str] | None,
    max_walk_km: float | None,
    remote_job_nos: set[str],
    include_remote: bool = True,
) -> bool:
    is_remote = job["jobNo"] in remote_job_nos or (job.get("remoteWorkType") or 0) > 0
    if is_remote and include_remote:
        return True

    if mrt_stations:
        # 精準通勤過濾：限定捷運線範圍，步行距離門檻是額外的條件（沒設定就不檢查）
        mrt_desc = job.get("mrtDesc") or ""
        on_line = any(name in mrt_desc for name in mrt_stations)
        if not on_line:
            return False
        if max_walk_km is None:
            return True  # 只限定捷運站範圍，沒有設定步行距離門檻
        mrt_dist = job.get("mrtDist")  # 公里，104 沒提供時是 None
        return mrt_dist is not None and mrt_dist <= max_walk_km

    if area_label is None:
        return True  # 訂閱者不限地區

    return area_label in (job.get("jobAddrNoDesc") or "")


def estimate_annual_salary(job: dict) -> int | None:
    salary_high = job.get("salaryHigh") or 0
    salary_low = job.get("salaryLow") or 0
    monthly = salary_high or salary_low
    if not monthly:
        return None  # 面議 / 未提供數字
    return monthly * BONUS_MONTHS


def is_salary_ok(job: dict, min_annual_salary: int | None) -> bool:
    annual = estimate_annual_salary(job)
    if annual is None:
        return True  # 面議：先納入，讓使用者自己判斷
    if min_annual_salary is None:
        return True  # 訂閱者不限薪資
    return annual >= min_annual_salary


def is_title_ok(job: dict) -> bool:
    name = job.get("jobName") or ""
    return not any(bad in name for bad in EXCLUDE_KEYWORDS)


def _skill_text(job: dict) -> str:
    """把職缺名稱、內容、技能需求標籤合併成一段文字，方便關鍵字比對。"""
    skill_names = " ".join(s.get("description", "") for s in job.get("pcSkills") or [])
    return " ".join([
        job.get("jobName") or "",
        job.get("description") or "",
        skill_names,
    ])


def is_stack_ok(job: dict) -> bool:
    """排除明確不想要的技術棧（例如 iOS/Android 為主的職缺）。"""
    text = _skill_text(job)
    return not any(bad.lower() in text.lower() for bad in STACK_EXCLUDE_KEYWORDS)


def is_skill_match(job: dict) -> bool:
    """職缺內容/技能需求至少要命中偏好技術棧之一。"""
    if not PREFERRED_SKILL_KEYWORDS:
        return True
    text = _skill_text(job).lower()
    return any(skill.lower() in text for skill in PREFERRED_SKILL_KEYWORDS)


# ============================================================
# LINE 推播
# ============================================================

def format_job_message(job: dict) -> str:
    name = job.get("jobName", "（無標題）")
    company = job.get("custName", "（未知公司）")
    district = job.get("jobAddrNoDesc", "")
    mrt = job.get("mrtDesc", "")
    mrt_dist = job.get("mrtDist")
    is_remote = job.get("_source_remote_query") or (job.get("remoteWorkType") or 0) > 0

    location_bits = []
    if is_remote:
        location_bits.append("可遠端")
    if district:
        location_bits.append(district)
    if mrt:
        if mrt_dist is not None:
            location_bits.append(f"{mrt}（步行約 {int(mrt_dist * 1000)} 公尺）")
        else:
            location_bits.append(mrt)
    location_line = " / ".join(location_bits) if location_bits else "地點未提供"

    salary_low = job.get("salaryLow") or 0
    salary_high = job.get("salaryHigh") or 0
    annual = estimate_annual_salary(job)
    if annual is None:
        salary_line = "薪資面議（請自行洽談確認是否達標）"
    else:
        salary_line = f"月薪 {salary_low:,}~{salary_high:,}，概估年薪約 {annual:,}"

    link = job.get("link", {}).get("job", "")

    return (
        f"📌 {name}\n"
        f"🏢 {company}\n"
        f"📍 {location_line}\n"
        f"💰 {salary_line}\n"
        f"🔗 {link}"
    )


def send_line_messages(texts: list[str], to: str) -> None:
    if not LINE_CHANNEL_ACCESS_TOKEN or not to:
        print("[錯誤] 尚未設定 LINE_CHANNEL_ACCESS_TOKEN 或收件者 LINE user id，無法推播。", file=sys.stderr)
        return

    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    # LINE 一次 push 最多帶 5 則訊息，超過就分批送
    for i in range(0, len(texts), 5):
        batch = texts[i : i + 5]
        body = {
            "to": to,
            "messages": [{"type": "text", "text": t} for t in batch],
        }
        resp = requests.post(LINE_PUSH_URL, headers=headers, json=body, timeout=15)
        if resp.status_code != 200:
            print(f"[錯誤] LINE 推播失敗（{to}）：HTTP {resp.status_code} {resp.text}", file=sys.stderr)
        time.sleep(1)


# ============================================================
# 主流程
# ============================================================

def main() -> None:
    subscribers = db.get_active_subscribers()
    if not subscribers:
        print("目前沒有啟用中的訂閱者，結束。")
        return

    print(f"共有 {len(subscribers)} 位啟用中的訂閱者")

    all_keywords = {kw for sub in subscribers for kw in sub["keywords"]}
    area_codes = {sub["area_code"] for sub in subscribers if sub["area_code"]}
    any_nationwide = any(sub["area_code"] is None for sub in subscribers)

    print("開始抓取 104 職缺 ...")
    candidates, job_keywords, remote_job_nos = collect_candidate_jobs(
        all_keywords, area_codes, any_nationwide
    )
    print(f"候選職缺共 {len(candidates)} 筆（所有訂閱者關鍵字聯集，合併去重後）")

    globally_ok = {
        job_no: job
        for job_no, job in candidates.items()
        if is_title_ok(job) and is_stack_ok(job) and is_skill_match(job)
    }
    print(f"通過全域規則（標題/技術棧）共 {len(globally_ok)} 筆")

    now = datetime.now(timezone.utc)

    for sub in subscribers:
        interval_hours = sub.get("notify_interval_hours") or 1
        last_checked = sub.get("last_checked_at")
        due = last_checked is None or (now - last_checked) >= timedelta(hours=interval_hours)
        if not due:
            next_check = last_checked + timedelta(hours=interval_hours)
            print(
                f"訂閱者 {sub['line_user_id']}：通知頻率每 {interval_hours} 小時一次，"
                f"還沒到檢查時間（預計 {next_check.isoformat()} 之後），本次跳過"
            )
            continue

        sub_keywords = set(sub["keywords"])
        sub_candidates = [
            job for job_no, job in globally_ok.items()
            if job_keywords[job_no] & sub_keywords
        ]
        matched = [
            job for job in sub_candidates
            if is_location_ok(
                job, sub["area_label"], sub["mrt_stations"], sub["max_walk_km"],
                remote_job_nos, sub["include_remote"],
            )
            and is_salary_ok(job, sub["min_annual_salary"])
        ]

        seen = db.get_seen_job_nos(sub["line_user_id"])
        new_jobs = [job for job in matched if job["jobNo"] not in seen]
        new_jobs.sort(key=lambda job: job.get("appearDate") or "", reverse=True)
        max_jobs = sub.get("max_jobs_per_run") or DEFAULT_MAX_JOBS_PER_RUN
        jobs_to_send = new_jobs[:max_jobs]

        print(
            f"訂閱者 {sub['line_user_id']}：符合條件 {len(matched)} 筆，"
            f"其中尚未通知過 {len(new_jobs)} 筆，本次推播 {len(jobs_to_send)} 筆"
        )

        if jobs_to_send:
            messages = [format_job_message(job) for job in jobs_to_send]
            send_line_messages(messages, to=sub["line_user_id"])
            db.mark_seen(sub["line_user_id"], [job["jobNo"] for job in jobs_to_send])

        db.mark_checked(sub["line_user_id"])


if __name__ == "__main__":
    main()
