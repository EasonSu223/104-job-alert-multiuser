"""呼叫 Google Gemini，把使用者在 LINE 上輸入的自然語言解析成結構化的職缺訂閱條件。

使用新版統一 SDK `google-genai`（舊版 `google-generativeai` 已於 2025-08-31 停止維護）。
預設 model 用 `gemini-flash-latest` 這個別名，Google 會自動把它指向當前最新的
Flash 版本（不是寫死某個具體版號）——具體版號的 model（例如 gemini-2.0-flash、
gemini-2.5-flash-lite）會不定期被下架，用別名可以避免每隔幾個月就要改一次程式碼。
"""

import json
import os
import sys
from pathlib import Path

from google import genai
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.area_codes import SUPPORTED_CITY_NAMES  # noqa: E402

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))

_AREA_ENUM = SUPPORTED_CITY_NAMES + ["不限"]

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "intent": {"type": "STRING", "enum": ["subscribe_or_update", "unsubscribe", "unclear"]},
        "keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
        "area": {"type": "STRING", "enum": _AREA_ENUM},
        "min_annual_salary": {"type": "INTEGER"},
    },
    "required": ["intent"],
}

SYSTEM_INSTRUCTION = f"""你是一個求職職缺通知機器人的訊息解析器。使用者會用自然語言描述他想找的工作，
你要把它轉換成結構化的訂閱條件。

規則：
- 只有使用者明確表示要取消/退訂通知時，才把 intent 設為 "unsubscribe"。
- 如果訊息裡完全找不到任何職稱/職務相關的關鍵字，把 intent 設為 "unclear"。
- 其他情況一律設為 "subscribe_or_update"。
- keywords 是使用者想找的職稱關鍵字列表（例如「前端工程師」「後端工程師」），盡量精簡、每個是一個職稱。
- area 只能是這些值之一：{", ".join(_AREA_ENUM)}。如果使用者提到的城市不在這個清單裡，
  或使用者沒有指定地區，就整個省略這個欄位，絕對不要自己發明代碼或用清單以外的城市名。
- min_annual_salary 是使用者期望的最低年薪（新台幣，整數）。如果使用者講的是月薪，
  換算成年薪時用「月薪 x 14」概估；如果使用者沒有提到薪資，就省略這個欄位。
"""


def parse(text: str) -> dict | None:
    """解析使用者輸入，回傳 {"intent", "keywords", "area", "min_annual_salary"}；
    無法解析或發生錯誤時回傳 None。
    """
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
    except Exception as exc:  # noqa: BLE001 - 任何 Gemini/網路例外都視為解析失敗
        print(f"[警告] Gemini 解析失敗：{exc}", file=sys.stderr)
        return None

    intent = result.get("intent")
    if intent == "unclear":
        return None
    if intent == "subscribe_or_update" and not result.get("keywords"):
        return None

    area = result.get("area")
    if area == "不限":
        area = None

    return {
        "intent": intent,
        "keywords": result.get("keywords", []),
        "area": area,
        "min_annual_salary": result.get("min_annual_salary"),
    }


if __name__ == "__main__":
    samples = [
        "我想找台北的前端工程師工作，年薪至少100萬",
        "幫我找新北的後端工程師，月薪至少7萬",
        "取消訂閱",
        "asdkjaslkdj123",
        "我想找高雄的前端工程師",  # 高雄不在支援清單中，驗證 area 應該被省略
    ]
    for sample in samples:
        print(f"輸入：{sample}")
        print(f"解析結果：{parse(sample)}")
        print()
