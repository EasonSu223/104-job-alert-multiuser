"""LINE Messaging API webhook：接收使用者訊息，讓他們用自然語言自助訂閱/取消訂閱職缺通知。

部署在 Render 免費方案上（見 README「Render 部署」章節）。注意 Render 免費方案的
硬碟是暫時性的，這支程式不能寫任何檔案到本機——所有訂閱者資料都存在 Supabase。
"""

import hashlib
import hmac
import base64
import os
import sys
from pathlib import Path

import requests
from flask import Flask, request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import db  # noqa: E402
from common.area_codes import AREA_CODE_MAP  # noqa: E402
from webhook_service import gemini_parser  # noqa: E402

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

UNSUBSCRIBE_KEYWORDS = ["取消訂閱", "退訂", "unsubscribe", "stop"]

WELCOME_MESSAGE = (
    "歡迎使用找工作小幫手！請直接傳一句話告訴我你想找什麼樣的工作，例如：\n"
    "「我想找台北的前端工程師工作，年薪至少100萬」\n\n"
    "之後我每小時都會自動幫你找符合條件的新職缺並傳訊息通知你。\n"
    "想取消訂閱，隨時傳「取消訂閱」即可。"
)
UNCLEAR_MESSAGE = (
    "不好意思，我沒有讀懂你的需求 🙏 可以換個方式描述嗎？例如：\n"
    "「我想找新北的後端工程師工作，年薪至少90萬」"
)
ERROR_MESSAGE = "系統暫時忙碌，請稍後再試一次 🙏"
UNSUBSCRIBE_MESSAGE = "已經幫你取消訂閱囉，之後不會再收到職缺通知。之後想重新開始，再傳一次你的需求給我即可。"

app = Flask(__name__)


@app.get("/")
def health_check():
    return "OK"


def _verify_signature(body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET or not signature:
        return False
    digest = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def _reply(reply_token: str, text: str) -> None:
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    resp = requests.post(LINE_REPLY_URL, headers=headers, json=body, timeout=15)
    if resp.status_code != 200:
        print(f"[錯誤] LINE 回覆失敗：HTTP {resp.status_code} {resp.text}", file=sys.stderr)


def _confirmation_message(parsed: dict) -> str:
    keywords_line = "、".join(parsed["keywords"])
    area_line = parsed["area"] or "不限（含遠端）"
    salary = parsed["min_annual_salary"]
    salary_line = f"{salary:,}" if salary else "不限"
    return (
        "已經幫你更新訂閱條件 ✅\n"
        f"關鍵字：{keywords_line}\n"
        f"地區：{area_line}\n"
        f"最低年薪：{salary_line}\n\n"
        "之後系統每小時會自動找符合條件的新職缺通知你。想修改條件，"
        "直接再傳一次新的需求就會覆蓋舊的設定；想取消訂閱，傳「取消訂閱」。"
    )


def _handle_text_message(user_id: str, reply_token: str, text: str) -> None:
    text = text.strip()

    if any(keyword.lower() in text.lower() for keyword in UNSUBSCRIBE_KEYWORDS):
        db.deactivate_subscriber(user_id)
        _reply(reply_token, UNSUBSCRIBE_MESSAGE)
        return

    parsed = gemini_parser.parse(text)
    if parsed is None:
        _reply(reply_token, UNCLEAR_MESSAGE)
        return

    if parsed["intent"] == "unsubscribe":
        db.deactivate_subscriber(user_id)
        _reply(reply_token, UNSUBSCRIBE_MESSAGE)
        return

    area_label = parsed["area"]
    area_code = AREA_CODE_MAP.get(area_label) if area_label else None
    db.upsert_subscriber(
        line_user_id=user_id,
        keywords=parsed["keywords"],
        area_code=area_code,
        area_label=area_label,
        min_annual_salary=parsed["min_annual_salary"],
    )
    _reply(reply_token, _confirmation_message(parsed))


@app.post("/webhook")
def webhook():
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")
    if not _verify_signature(body, signature):
        return "invalid signature", 400

    payload = request.get_json(silent=True) or {}
    for event in payload.get("events", []):
        event_type = event.get("type")
        try:
            if event_type == "follow":
                _reply(event["replyToken"], WELCOME_MESSAGE)
            elif event_type == "unfollow":
                db.deactivate_subscriber(event["source"]["userId"])
            elif event_type == "message" and event.get("message", {}).get("type") == "text":
                _handle_text_message(
                    user_id=event["source"]["userId"],
                    reply_token=event["replyToken"],
                    text=event["message"]["text"],
                )
        except Exception as exc:  # noqa: BLE001 - 單一事件出錯不能影響其他事件，也不能讓 LINE 重試風暴
            print(f"[錯誤] 處理事件失敗：{exc}", file=sys.stderr)
            reply_token = event.get("replyToken")
            if reply_token:
                _reply(reply_token, ERROR_MESSAGE)

    return "OK", 200


if __name__ == "__main__":
    app.run(debug=True)
