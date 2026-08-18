# 104 職缺自動通知機器人（多使用者版）

自動去 104 人力銀行搜尋符合條件的職缺，有新職缺就用 LINE 傳訊息通知。現在支援
**多個使用者各自訂閱**：任何人加這個 LINE 官方帳號好友後，直接傳一句話說出自己
想找什麼工作（例如「我想找台北的前端工程師工作，年薪至少100萬」），機器人就會
記住這個人的條件，之後每小時自動幫他找符合的新職缺並通知他，跟其他人互不干擾。

系統由三個部分組成：

1. **GitHub Actions 排程**：每小時抓 104 職缺、對每位訂閱者各自過濾、推播 LINE
2. **Render 上的 Webhook 服務**：接收使用者在 LINE 傳的訊息，用 Google Gemini
   解析成訂閱條件
3. **Supabase 資料庫**：存放每個人的訂閱條件與「已通知過的職缺」紀錄

---

## 這份文件在教你什麼

1. 申請 GitHub 帳號、把這個資料夾建立成 GitHub repo
2. 建立 Supabase 專案並建表
3. 申請 LINE Bot（Messaging API），取得 Channel Access Token 與 Channel Secret
4. 申請 Google Gemini API Key
5. 把上面這些「鑰匙」設定到 GitHub Secrets
6. 把 Webhook 服務部署到 Render，並在 LINE 後台設定 Webhook URL
7. 手動測試排程一次、自己傳訊息測試訂閱流程

全部都是「點一點、複製貼上」，大概需要 40-60 分鐘（比單人版多了 Supabase／
Render／Gemini 三個新帳號要申請）。

---

## 第一步：申請 GitHub 帳號、建立 Repo

1. 開啟 https://github.com/signup 完成註冊（已有帳號可跳過）
2. 登入後點右上角 `+` → `New repository`
3. Repository name 填 `104-job-alert-multiuser`（跟單人版 `104-job-alert` 是**兩個獨立的 repo**，互不影響），建議選 **Private**
4. 不用勾選 "Add a README file"（我們已經有了）
5. 建立完成後，用網頁上傳或 Git 指令把這個資料夾的內容（含隱藏的 `.github` 資料夾）推上去：

```bash
cd /c/Project/104-job-alert-multiuser
git init
git add .
git commit -m "first commit"
git branch -M main
git remote add origin https://github.com/<你的帳號>/104-job-alert-multiuser.git
git push -u origin main
```

---

## 第二步：建立 Supabase 專案與資料表

Supabase 提供免費的 Postgres 資料庫，用來存放每個訂閱者的條件與已讀職缺紀錄。

1. 開啟 https://supabase.com ，用 GitHub 帳號登入即可
2. 點 `New project`，填專案名稱（例如 `104-job-alert`）、設一組資料庫密碼
   （記下來，等一下要用）、選離台灣近的地區（例如 Singapore）
3. 建立完成、專案初始化好後，點左側 `SQL Editor` → `New query`，貼上以下 SQL 並執行：

```sql
create table if not exists subscribers (
  line_user_id       text primary key,
  keywords           text[] not null default '{}',
  area_code          text,
  area_label         text,
  min_annual_salary  integer,
  mrt_stations       text[],
  max_walk_km        numeric,
  active             boolean not null default true,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

create index if not exists idx_subscribers_active on subscribers (active);

create table if not exists seen_jobs (
  line_user_id  text not null references subscribers(line_user_id) on delete cascade,
  job_no        text not null,
  seen_at       timestamptz not null default now(),
  primary key (line_user_id, job_no)
);

create index if not exists idx_seen_jobs_user on seen_jobs (line_user_id);
```

4. 點左側 `Project Settings` → `Database` → `Connection string` → 切到
   **Transaction pooler** 分頁（不要用預設的 Direct connection，那個是
   IPv6-only，GitHub Actions 跟 Render 免費方案連不上）
5. 複製那串連線字串，把裡面的 `[YOUR-PASSWORD]` 換成你剛剛設定的資料庫密碼，
   這一整串就是之後要用的 `SUPABASE_DB_URL`

---

## 第三步：申請 LINE Bot（Messaging API）

1. 開啟 https://developers.line.biz/console/ ，用你平常的 LINE 帳號登入
2. 第一次使用會要求建立一個 **Provider**，隨便取名（例如「MyJobAlert」）
3. 進入 Provider 頁面，點 `Create a channel` → **Messaging API**
4. 依畫面指示點 **`Create a LINE Official Account`**，到 LINE Official Account
   Manager 填帳號資訊（Account name、Email、Country=Taiwan、隨便選一個
   Category）並送出
5. 回到 LINE Official Account Manager 後台，點 **Settings（設定）** →
   **Messaging API** 分頁 → `Enable Messaging API`，選剛剛建立的 Provider
6. 完成後回 https://developers.line.biz/console/ 重新整理，就會看到這個
   Channel 出現在清單裡

### 取得 Channel Access Token

進入 Channel → **Messaging API** 分頁 → 找到
`Channel access token (long-lived)`，點 `Issue`，產生的那串就是
`LINE_CHANNEL_ACCESS_TOKEN`。

### 取得 Channel Secret

同一個 Channel → **Basic settings** 分頁 → 找到 `Channel secret`，那串就是
`LINE_CHANNEL_SECRET`（用來驗證 webhook 請求真的來自 LINE，不能外洩）。

### 加這個 Bot 為好友

**Messaging API** 分頁上有 QR Code，用手機 LINE「加好友」→「行動條碼」掃描。
之後其他朋友想訂閱，也是掃這個 QR Code加好友、傳訊息就好。

### 關閉自動回應（建議）

**Messaging API** 分頁 → `LINE Official Account features` → `Edit`，跳到
LINE Official Account Manager 把「自動回應訊息」「歡迎訊息」都關閉，這樣訊息
才會全部交給我們自己的 Webhook 服務處理，不會有兩邊都回覆的狀況。

---

## 第四步：申請 Google Gemini API Key

用來把使用者傳的自然語言解析成結構化的訂閱條件，有長期免費額度。

1. 開啟 https://aistudio.google.com/apikey ，用 Google 帳號登入
2. 點 `Create API key`，選一個 Google Cloud 專案（沒有的話會自動建一個）
3. 產生的字串就是 `GEMINI_API_KEY`

---

## 第五步：把所有「鑰匙」放進 GitHub Secrets

1. 到 `104-job-alert` repo 頁面 → `Settings` → `Secrets and variables` →
   `Actions` → `New repository secret`，新增：
   - `LINE_CHANNEL_ACCESS_TOKEN`
   - `SUPABASE_DB_URL`

（`LINE_CHANNEL_SECRET`、`GEMINI_API_KEY` 這兩個只有 Webhook 服務會用到，
設在 Render 就好，不用放進 GitHub Secrets。）

---

## 第六步：把 Webhook 服務部署到 Render

1. 開啟 https://render.com ，用 GitHub 帳號登入並授權存取你的 repo
2. 點 `New` → `Web Service`，選 `104-job-alert-multiuser` 這個 repo
3. 設定：
   - Root Directory：留空（用 repo 根目錄）
   - Runtime：Python 3
   - Build Command：`pip install -r webhook_service/requirements.txt`
   - Start Command：`gunicorn webhook_service.app:app --bind 0.0.0.0:$PORT`
   - Instance Type：Free
4. 在 `Environment` 分頁加入環境變數：
   - `LINE_CHANNEL_ACCESS_TOKEN`（跟 GitHub Secrets 那個一樣的值）
   - `LINE_CHANNEL_SECRET`
   - `GEMINI_API_KEY`
   - `SUPABASE_DB_URL`（跟 GitHub Secrets 那個一樣的值）
5. 點 `Create Web Service`，等待部署完成
6. 部署完成後會拿到一個網址（例如 `https://104-job-alert-webhook.onrender.com`），
   用瀏覽器打開確認畫面顯示 `OK`

> **免費方案限制**：閒置約 15 分鐘會睡眠，睡眠後第一個請求可能要等
> 30-50 秒才有回應（第一次點「Verify」或久違的第一則訊息可能感覺卡住一下，
> 屬正常現象）。想避免的話可以用免費的 UptimeRobot 每 10 分鐘打一次上面那個
> 網址保持喚醒。

---

## 第七步：在 LINE 後台設定 Webhook URL

1. 回到 https://developers.line.biz/console/ ，進入這個 Channel →
   **Messaging API** 分頁
2. 找到 `Webhook URL`，點 `Edit`，貼上 Render 給的網址 + `/webhook`
   （例如 `https://104-job-alert-webhook.onrender.com/webhook`），儲存
3. 打開 `Use webhook` 開關（設為 Enabled）
4. 點 `Verify` 按鈕，確認顯示 Success（如果服務剛好在睡眠中，第一次點可能會
   逾時，等個幾十秒再點一次）

---

## 第八步：測試

### 測試訂閱流程

用手機 LINE 傳訊息給這個 Bot，例如「我想找台北的前端工程師工作，年薪至少
100萬」，應該會收到條件確認的回覆。傳「取消訂閱」可以停止收到通知。之後想改
條件，直接再傳一次新的需求就會覆蓋舊的設定。

### 你自己這一戶：補上精準通勤過濾

如果你想比照原本單人版「板南線頂埔↔忠孝敦化、步行 5 分鐘內」這種精準過濾，
一般的 LINE 對話流程不支援設定這麼細，需要自己到 Supabase 後台手動補：

1. Supabase → `Table Editor` → `subscribers` 表，找到自己那一列（用你自己的
   LINE user id，可在 LINE Developers Console 的 Channel → Basic settings →
   `Your user ID` 查到）
2. 手動編輯該列的 `mrt_stations`、`max_walk_km` 兩欄，或用 SQL Editor 執行：

```sql
update subscribers
set mrt_stations = array['頂埔','永寧','土城','海山','亞東醫院','府中','板橋',
                          '新埔','江子翠','龍山寺','西門','台北車站','善導寺',
                          '忠孝新生','忠孝復興','忠孝敦化'],
    max_walk_km = 0.4
where line_user_id = '你的 LINE user id';
```

設定這兩欄之後，`scripts/job_alert.py` 會改用捷運站＋步行距離判斷你的地區條件，
不再用一般使用者的城市名比對。

### 測試排程

1. Repo 頁面 → `Actions` → 左側 `104 Job Alert` → `Run workflow`
2. 等 1-3 分鐘看執行紀錄是否成功；如果目前有啟用中的訂閱者且剛好有符合條件的
   新職缺，應該會收到 LINE 訊息
3. 之後系統會照 `.github/workflows/job_alert.yml` 裡的 `cron` 設定（預設每小時
   一次）自動執行

---

## 全域篩選規則（對所有使用者都生效，在 `scripts/job_alert.py` 最上面調整）

| 條件 | 目前設定 |
|---|---|
| 技術棧排除 | 排除 iOS/Android/Flutter 等明確不相關的技術棧 |
| 技術棧要求 | 職缺內容需命中至少一項 .NET/C#/ASP.NET/Vue/SQL Server 等關鍵字 |
| 面議薪資 | 一律先列入，讓使用者自己判斷 |
| 檢查頻率 | 每小時一次 |

各使用者自己的「關鍵字」「地區」「最低年薪」則是透過傳 LINE 訊息自助設定，
存在 Supabase 的 `subscribers` 表，不需要改程式碼。

想調整全域規則：打開 `scripts/job_alert.py` 最上面的設定區，改
`STACK_EXCLUDE_KEYWORDS`（排除的技術棧）、`PREFERRED_SKILL_KEYWORDS`（要求命中
的技術棧，清空 `[]` 可放寬給所有職缺）、`EXCLUDE_KEYWORDS`（排除的職稱關鍵
字）。想調整檢查頻率則改 `.github/workflows/job_alert.yml` 裡的 `cron`。改完
`git push` 回 repo 即可生效。

想支援更多城市：打開 `common/area_codes.py`，先到 104 網站實際套用該城市篩選、
從瀏覽器開發者工具的網路請求確認 `area` 參數值後再加進 `AREA_CODE_MAP`，不要
用猜的（猜錯會讓該城市搜尋結果悄悄變空的或錯的）。

---

## 需要知道的限制與注意事項

1. **薪資是概估年薪**：104 通常只列月薪區間，程式假設「月薪 × 14」概估年薪，
   實際獎金制度因公司而異，收到通知後仍要自己確認是否達標。
2. **可能偶爾被 104 的防護機制擋下**：GitHub Actions 用雲端機房 IP，少數情況
   可能被 Cloudflare 判定為機器人暫時擋下（該次不影響下次執行）。
3. **請勿把檢查頻率調得太高**：對 104 伺服器不禮貌，也更容易被擋。
4. **Gemini／Render 都是免費額度**：個人＋朋友群這種小規模用量不會超過，但都
   有速率限制，不是無限量。
5. **這不是自動投遞履歷的工具**：只負責通知，看到通知後仍需自行到 104 網站
   應徵。
6. **Render 免費方案硬碟是暫時性的**：webhook 服務重啟或重新部署時本機檔案會
   被清空，所以所有訂閱者資料都存在 Supabase，不能存在 Render 本機。

---

## 檔案說明

```
104-job-alert-multiuser/
├── .github/workflows/job_alert.yml   # GitHub Actions 排程設定（每小時執行一次）
├── scripts/job_alert.py               # 排程主程式：抓職缺、依各訂閱者條件過濾、推播 LINE
├── common/
│   ├── db.py                          # 排程與 Webhook 共用的 Supabase 存取層
│   └── area_codes.py                  # 城市名 ↔ 104 area code 對照表
├── webhook_service/
│   ├── app.py                         # Flask：接收 LINE webhook 事件
│   ├── gemini_parser.py               # 呼叫 Gemini 解析自然語言訂閱條件
│   ├── requirements.txt               # Webhook 服務的 Python 套件需求
│   └── render.yaml                    # Render 部署設定（Blueprint）
├── requirements.txt                   # 排程主程式的 Python 套件需求
└── README.md                          # 就是這份文件
```
