# 從原始碼建置、測試、發版

這份是給要動這份程式的人看的。產品本身在 [`../README.md`](../README.md)。

> **公開倉庫只有主程式。** 測試套件（`tests/`、`gui/e2e/`、所有 `*.test.*`）與
> 內部記錄留在開發樹裡，所以在公開樹上請用 `scripts\build-all.ps1 -SkipTests`
> 建置。下面「測試」那一節描述的是完整開發樹，寫在這裡是因為它說明了這個專案
> 為什麼是現在這個樣子——四個 runner 裡有兩個是刻意分開的。

## 需要什麼

| | 版本 | 備註 |
|---|---|---|
| Windows | 11 | 唯一被建置與測試過的平台 |
| Python | ≥ 3.12 | `pyproject.toml` 寫死 |
| Node.js | 建議 22 LTS | 前端與 Electron 各有自己的 `package.json` |
| Chrome | 任一近期版本 | `probe` 用真瀏覽器取 HTML，不渲染 |
| yt-dlp / ffmpeg | 近期版本 | `mfp tools` 印出狀態**與現在跑的是哪一份**；`mfp doctor` 只答有沒有 |

語音辨識與翻譯**不在**上表：它是選用的能力，要另外準備一個 Python 引擎環境
（`faster-whisper` / `CTranslate2`）與模型，由應用內的「語音辨識與翻譯」設定頁引導。

## 裝起來

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm --prefix gui install
npm --prefix electron install
```

## 跑起來（開發模式）

```powershell
scripts\dev-start.bat
```

它會在兩個標了名字的視窗裡起 `mfp serve`（port 47821）與 Vite 開發伺服器
（5173），等兩邊都在聽之後開瀏覽器。`scripts\dev-stop.bat` 用「誰佔著那個 port」
反查 PID 收掉它們。

要跑真的 Electron 殼：

```powershell
npm --prefix electron run dev
```

## 測試——四個 runner，其中兩個不在預設裡

```powershell
.venv\Scripts\python.exe -m pytest -q          # 2,583 passed, 2 skipped @ 2026-09-02
cd gui && npm run check                        # eslint + tsc --noEmit + vitest 689
cd electron && npm run check                   # tsc + vitest
scripts\test-all.bat                           # 上面三個一起跑，前面紅了後面照跑
```

另外兩個是**第二個 runner**，不是第二套單元測試，而且**不在** `pytest -q` 裡：

```powershell
cd gui && npm run test:geometry                # Playwright 29
.venv\Scripts\python.exe -m pytest tests\conformance   # 真引擎 + 已知音檔，9 項
```

- **Playwright**（D-84），因為 jsdom 沒有版面引擎——那裡每一個
  `getBoundingClientRect()` 都回 0，對齊缺陷在它眼裡不存在，而且一直不存在。
  裡面每一條斷言都是**相對的**：表頭的邊對上自己那一欄內容的邊，儲存格的中線
  對上自己那一列的中線。絕對像素期望值只會教人去改那個數字，而不是去讀那個失敗。
  四支 spec：`queue-geometry`、`design-system`、`settings-mode`、`resilience`。

  它預設**裝上 Electron 的 preload 橋接器**（`e2e/fixtures.installDesktopBridge`）。
  這不是方便，是必要：沒有橋接器時「開啟檔案位置」會畫成「複製路徑」，少 25px，
  於是表格裡最寬的那一格只存在於沒人量的那個殼裡——這正是 P-66。

- **`tests/conformance`**（D-110），因為它需要外部語音引擎、一個 3 GB 模型與
  合成音檔。它是**唯一**看得見「內容被刪掉」的那一層：一份掉光所有英文詞的
  逐字稿讀起來通順、標點正確，而且能通過全部單元測試（P-50）。任何動到辨識的
  改動，發版前跑它。音檔用 `python tests/conformance/make_fixtures.py` 重新產生；
  引擎、模型或語音缺任何一個，它會**跳過並說原因**，絕不因為缺東西而失敗。

## 發版

```powershell
powershell -File scripts\build-all.ps1
scripts\smoke-package.bat        # 讓打包出來的東西自己證明它跑得動
```

四個階段：測試 → renderer（`gui/dist`）→ sidecar（PyInstaller onedir，
`mfp-sidecar.exe` 與 `mfp.exe` 共用一個 `_internal`）→ 桌面殼（electron-builder）。
少了某個輸入的階段會**跳過並說出來**，不會安靜地少做事。紅的樹不會被拿去建置。

產出在 `release/windows/`（不進版控）。`build-all.ps1` 會把它建置的那個 commit
蓋進 `release/windows/BUILD.json`，所以「這份 release 是不是最新的」是
`git log <那個 commit>..HEAD --oneline`，不是看檔案日期猜。**開發過程中它本來就
會落後 HEAD，那不算失敗。**

> **一個驗證過但不在 `release/` 裡的功能，等於沒有交付**（D-100）。綠色的測試
> 描述的是原始碼樹；`release/windows/` 才是別人跑得到的東西。

## 專案長怎樣

```
src/mfp/          Python 核心。cli.py 是 19 個 verb 的入口，server/ 是 /v1
  adapters/       平台配接器（instagram/、ytdlp.py）
gui/              React renderer，FSD 六層
  src/app|pages|widgets|features|entities|shared
  e2e/            Playwright（第二個 runner）
electron/         桌面殼：main、preload、contracts
skill/SKILL.md    給 AI 助手的呼叫契約
scripts/          可以點兩下的 .bat + 它旁邊的 .ps1（改 .ps1，永遠別改 .bat）
tests/            pytest；tests/conformance 是第二個 runner
docs/             規格（PSM）、驗收清單、spike 記錄、架構健檢
references/       決策與踩坑、名詞表、phase log——這個專案的記憶
```

### 前端分層是被強制的，不是慣例

FSD 六層：`app / pages / widgets / features / entities / shared`，上層可以匯入
下層，反過來不行；**同一層的切片之間永遠不准互相匯入**。這條規則有牙齒：
`eslint-plugin-boundaries` 預設 disallow，而且 `layering.test.ts` 直接對真實
檔案樹跑 lint 並斷言違規清單為空——2026-08-23 有過一條側向匯入通過測試的前例。

所以一個 feature 要用另一個 feature 的東西時，答案是**由上一層的 widget 把兩者
擺在一起**，用 slot 傳進去，不是去 import 隔壁。

## Windows 上會省掉你一次除錯的幾件事

- **檔案內容用編輯器寫，不要用 heredoc 或 `>>`。** 對一個 CRLF 檔案做 append
  會做出一個混合行尾的檔案；`Out-File` 會多塞一個 BOM。行尾由版控裡的 `.gitattributes`
  釘住（`* text eol=crlf`）。
- **commit message 用 `-F <檔案>` 傳，不要寫在命令列上。** PowerShell 5.1 的
  原生參數編碼器不會跳脫 `"` 與 `[`，而且截斷是**安靜的**——鏈式的 git 指令會
  繼續往下跑。
- **commit 慣例**：`type(scope): subject`，subject 用祈使句、小寫、不加句點。
  程式、註解、commit message 一律英文；給人讀的文件用繁體中文。

## 動這份程式之前，先讀三個檔案

`CLAUDE.md` 只是索引，真正的記憶在這三份裡：

| 檔案 | 內容 |
|---|---|
| `references/media-fetch-pipeline-context.md` | 名詞表。一個詞一個定義，`docs/` 與程式逐字共用 |
| `references/media-fetch-pipeline-decisions.md` | `D-nn` 決策與 `P-nn` 踩坑，含當時的理由。**新的在最後，先讀尾巴** |
| `references/media-fetch-pipeline-phase-log.md` | 各階段檢查點，新的在最後；未解問題在每一段的結尾 |
