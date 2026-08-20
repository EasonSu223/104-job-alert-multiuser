"""呼叫 Google Gemini，把使用者在 LINE 上輸入的自然語言解析成結構化的職缺訂閱條件。

使用新版統一 SDK `google-genai`（舊版 `google-generativeai` 已於 2025-08-31 停止維護）。
預設 model 用 `gemini-flash-latest` 這個別名，Google 會自動把它指向當前最新的
Flash 版本（不是寫死某個具體版號）——具體版號的 model（例如 gemini-2.0-flash、
gemini-2.5-flash-lite）會不定期被下架，用別名可以避免每隔幾個月就要改一次程式碼。
"""

import json
import os
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.area_codes import SUPPORTED_CITY_NAMES  # noqa: E402
from common.mrt_lines import ALL_STATIONS, stations_between  # noqa: E402

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))

_AREA_ENUM = SUPPORTED_CITY_NAMES + ["不限"]

# 每分鐘走路概估距離（公尺），用來把「步行 X 分鐘」換算成公里數門檻
_WALK_KM_PER_MINUTE = 0.08

# 通知頻率的合理範圍（小時）；GitHub Actions 排程本身是每小時跑一次，
# 所以 1 小時是能做到的最高頻率，24 小時（一天一次）是上限
_MIN_NOTIFY_INTERVAL_HOURS = 1
_MAX_NOTIFY_INTERVAL_HOURS = 24

# 「每次最多通知幾筆」的合理範圍；預設 5 筆
_MIN_MAX_JOBS_PER_RUN = 1
_MAX_MAX_JOBS_PER_RUN = 20
_DEFAULT_MAX_JOBS_PER_RUN = 5

# Gemini 偶爾會回傳 503（暫時過載），重試個幾次通常就能成功；重試次數用完仍失敗
# 就把例外往外拋，讓呼叫端（app.py）知道這是「服務暫時忙碌」而不是「看不懂使用者的話」，
# 兩者要回覆不同的訊息給使用者
_MAX_RETRIES = 3
_RETRY_DELAY_SECONDS = 1.5

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "intent": {"type": "STRING", "enum": ["subscribe_or_update", "unsubscribe", "unclear"]},
        "keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
        "area": {"type": "STRING", "enum": _AREA_ENUM},
        "min_annual_salary": {"type": "INTEGER"},
        "mrt_start_station": {"type": "STRING", "enum": ALL_STATIONS},
        "mrt_end_station": {"type": "STRING", "enum": ALL_STATIONS},
        "max_walk_minutes": {"type": "INTEGER"},
        "include_remote": {"type": "BOOLEAN"},
        "notify_interval_hours": {"type": "INTEGER"},
        "max_jobs_per_run": {"type": "INTEGER"},
    },
    "required": ["intent"],
}

SYSTEM_INSTRUCTION = f"""你是一個求職職缺通知機器人的訊息解析器。使用者會用自然語言描述他想找的工作，
你要把它轉換成結構化的訂閱條件。

規則：
- 只有使用者明確表示要取消/退訂通知時，才把 intent 設為 "unsubscribe"。
- 如果訊息裡完全找不到任何職稱/職務相關的關鍵字、也沒有提到要調整通知頻率，把 intent 設為 "unclear"。
- 其他情況一律設為 "subscribe_or_update"。
- keywords 是使用者想找的職稱關鍵字列表（例如「前端工程師」「後端工程師」），盡量精簡、每個是一個職稱。
- area 只能是這些值之一：{", ".join(_AREA_ENUM)}。如果使用者提到的城市不在這個清單裡，
  或使用者沒有指定地區，就整個省略這個欄位，絕對不要自己發明代碼或用清單以外的城市名。
- min_annual_salary 是使用者期望的最低年薪（新台幣，整數）。如果使用者講的是月薪，
  換算成年薪時用「月薪 x 14」概估；如果使用者沒有提到薪資，就省略這個欄位。
- mrt_start_station / mrt_end_station：只有當使用者明確描述「捷運某一條線上某站到某站之間」這種
  通勤範圍時才填（例如「頂埔到忠孝敦化」「淡水到北投」「古亭到景安」），兩個欄位都只能是台北捷運
  現有車站名稱之一。如果使用者只提到一個站（例如「頂埔站附近」），兩個欄位都填那一站。如果使用者
  提到的站名不在捷運站清單裡、或完全沒提到捷運通勤範圍，就把這兩個欄位都省略——不要自己亂猜或
  硬套最接近的站名，也不要自己判斷兩站是否同一條線（這由後端程式檢查）。有填這兩個欄位時就不用
  再填 area。
- max_walk_minutes：使用者說的「步行 X 分鐘內」的 X（整數，分鐘）。只有在有講到步行時間時才填，
  沒提到就省略，不要自己編一個數字。
- include_remote：是否也要收到「不限地點的全遠端」職缺（跟通勤範圍/城市是 OR 的關係，遠端職缺會
  無視地區條件）。使用者明確表示「不要遠端」「只要通勤範圍內的」「不用遠端職缺」時設為 false；
  明確表示「含遠端」「可以遠端」「也要遠端職缺」時設為 true。訊息裡完全沒提到遠端相關字眼時，
  就省略這個欄位——省略時會沿用使用者原本的設定（新訂閱者預設 true）。
- notify_interval_hours：使用者想要「多久檢查一次、通知一次」的小時數（整數，介於
  {_MIN_NOTIFY_INTERVAL_HOURS} 到 {_MAX_NOTIFY_INTERVAL_HOURS} 之間）。例如「改成每 2 小時通知我」
  →2、「一天通知一次就好」→24、「恢復成每小時通知」→1。如果訊息裡完全沒提到通知頻率，就省略這個
  欄位——省略時會沿用使用者原本的設定，不會被重置成預設值。
- max_jobs_per_run：使用者想要「一次最多收到幾筆新職缺通知」的數字（整數，介於
  {_MIN_MAX_JOBS_PER_RUN} 到 {_MAX_MAX_JOBS_PER_RUN} 之間，預設 {_DEFAULT_MAX_JOBS_PER_RUN}）。
  例如「一次給我10筆就好」→10、「最多20筆」→20、「恢復預設」→{_DEFAULT_MAX_JOBS_PER_RUN}。
  如果訊息裡完全沒提到這個數字，就省略這個欄位——省略時會沿用使用者原本的設定，不會被重置。
- 如果使用者只是想調整通知頻率或每次通知筆數、沒有提到任何職稱關鍵字，intent 一樣設為
  "subscribe_or_update"，keywords 可以留空陣列。
"""


def parse(text: str) -> dict | None:
    """解析使用者輸入，回傳
    {"intent", "keywords", "area", "min_annual_salary", "mrt_stations", "max_walk_km"}；
    Gemini 判斷「看不懂使用者在說什麼」時回傳 None；重試用完仍然呼叫失敗（例如 Gemini
    暫時過載）則把例外往外拋，讓呼叫端能分辨這兩種不同情況、回覆不同的訊息給使用者。
    """
    result = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = _client.models.generate_content(
                model=GEMINI_MODEL,
                contents=text,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    response_schema=RESPONSE_SCHEMA,
                ),
            )
            result = json.loads(response.text)
            break
        except Exception as exc:  # noqa: BLE001 - 任何 Gemini/網路例外都視為暫時性失敗
            if attempt < _MAX_RETRIES:
                print(
                    f"[警告] Gemini 解析失敗（第 {attempt} 次，{_RETRY_DELAY_SECONDS} 秒後重試）：{exc}",
                    file=sys.stderr,
                )
                time.sleep(_RETRY_DELAY_SECONDS)
            else:
                print(f"[警告] Gemini 解析失敗（已重試 {_MAX_RETRIES} 次，放棄）：{exc}", file=sys.stderr)
                raise

    intent = result.get("intent")
    notify_interval = result.get("notify_interval_hours")
    if notify_interval is not None:
        notify_interval = max(
            _MIN_NOTIFY_INTERVAL_HOURS, min(_MAX_NOTIFY_INTERVAL_HOURS, int(notify_interval))
        )

    max_jobs_per_run = result.get("max_jobs_per_run")
    if max_jobs_per_run is not None:
        max_jobs_per_run = max(
            _MIN_MAX_JOBS_PER_RUN, min(_MAX_MAX_JOBS_PER_RUN, int(max_jobs_per_run))
        )

    include_remote = result.get("include_remote")

    if intent == "unclear":
        return None
    # 訊息裡沒有職稱關鍵字、也沒有要調整通知頻率/筆數/遠端與否，代表真的看不懂在說什麼
    if (
        intent == "subscribe_or_update"
        and not result.get("keywords")
        and notify_interval is None
        and max_jobs_per_run is None
        and include_remote is None
    ):
        return None

    area = result.get("area")
    if area == "不限":
        area = None

    mrt_start = result.get("mrt_start_station")
    mrt_end = result.get("mrt_end_station") or mrt_start
    mrt_stations = stations_between(mrt_start, mrt_end) if mrt_start else None
    if mrt_stations:
        area = None  # 有精確捷運範圍時，地區改用這個判斷，不用城市層級的 area

    walk_minutes = result.get("max_walk_minutes")
    max_walk_km = round(walk_minutes * _WALK_KM_PER_MINUTE, 2) if walk_minutes else None

    return {
        "intent": intent,
        "keywords": result.get("keywords", []),
        "area": area,
        "min_annual_salary": result.get("min_annual_salary"),
        "mrt_stations": mrt_stations,
        "max_walk_km": max_walk_km,
        "include_remote": include_remote,
        "notify_interval_hours": notify_interval,
        "max_jobs_per_run": max_jobs_per_run,
    }


if __name__ == "__main__":
    samples = [
        "我想找台北的前端工程師工作，年薪至少100萬",
        "幫我找新北的後端工程師，月薪至少7萬",
        "取消訂閱",
        "asdkjaslkdj123",
        "我想找高雄的前端工程師",  # 高雄不在支援清單中，驗證 area 應該被省略
        "我想找頂埔到忠孝敦化之間、步行五分鐘內的前後端工程師工作，年薪至少100萬",
        "改成每3小時通知我一次",  # 純調整頻率，keywords 應該是空陣列
        "一次給我10筆就好",  # 純調整筆數，keywords 應該是空陣列
        "不要遠端的職缺，只要通勤範圍內的",  # 純調整 include_remote，keywords 應該是空陣列
    ]
    for sample in samples:
        print(f"輸入：{sample}")
        print(f"解析結果：{parse(sample)}")
        print()
