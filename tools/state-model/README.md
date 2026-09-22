# `tools/state-model` — 逐字稿家族移除之前，這個 repo 的形狀（歷史快照）

> **2026-09-16 起這是歷史快照，不是現況。** 逐字稿家族在這一天從產品移除
> （D-162）。這份模型畫的是移除**之前**的形狀，接縫兩側是「留下的」與「要走的」；
> 要走的那一側已經不在 repo 裡了。
>
> **決定是封存、不重畫**：重畫之後只剩接縫的一側，那張圖已經沒有它原本要回答的
> 問題（「哪些東西要搬、搬走會斷哪裡」）。
>
> 所以**對著現在的 HEAD 跑 `verify.py`，rung 0 會失敗**——模型點名的模組已經
> 不存在。那是預期的，不是檢查器壞掉。要讓這份模型重新被驗證，先切到
> `pre-transcript-removal-2026-09-16` 這個 tag 再跑。

一份結構模型（`model.json`）加上兩張從它算出來的圖，以及兩支把圖、模型、實際
檔案三者對起來的檢查器。

**2026-09-09 從 `D:\output\mfp-state-20260909\` 搬進來。** 原本放在使用者的
媒體輸出根目錄，那是個放置錯誤：它量的是這個 repo，它就該住在這個 repo 裡，
而且 `verify.py` 的 `REPO` 常數本來就寫死指向這裡。

## 跑法（在 `pre-transcript-removal-2026-09-16` 上）

```powershell
.venv\Scripts\python.exe tools\state-model\verify.py          # 工程視角，3 個 rung
.venv\Scripts\python.exe tools\state-model\calibrate.py       # 5 根針，每根都必須讓上面那支失敗
.venv\Scripts\python.exe tools\state-model\verify_owner.py    # 負責人視角
.venv\Scripts\python.exe tools\state-model\calibrate_owner.py # 它的針
```

四支都不需要參數，也不需要網路。`verify.py` 讀 `tests/unit/test_layering.py`
與檔案本身，不讀任何一份文件。

## 檔案

| 檔案 | 角色 |
|---|---|
| `model.json` | **正典**。48 個模組、7 個分區、接縫、14 列狀態。圖上每一個座標都是從它算出來的，沒有一個是手打的 |
| `build.py` → `index.html` | 工程圖 |
| `build_owner.py` → `index-owner-view.html` | 負責人視角的重畫（13 個框，把 42 個模組聚合起來） |
| `verify.py` | 模型 ↔ repo ↔ 產物，3 個 rung |
| `verify_owner.py` | 負責人視角 ↔ 正典模型 ↔ 產物 |
| `calibrate.py` / `calibrate_owner.py` | 負對照。**沒失敗過的閘門等於沒測過失敗路徑**，所以每一根針都必須讓檢查器真的失敗，而且模型要按位元組還原 |
| `remeasure.py` | 從 repo 重新量行數，寫回 `model.json`。**不是閘門的一部分，也絕對不能被閘門呼叫**——會自己重算待驗claim的檢查器只可能通過 |
| `boxes.json` | `build.py` 順手吐出的幾何，`verify.py` rung 2 拿去驗重疊與接縫兩側 |

## 行數過期的時候（只在快照 tag 上有意義）

`verify.py` rung 0 報 `X: says N, file has M` 不是壞掉，是它的工作：圖上的
數字跟檔案不一樣了。判斷是**形狀變了**還是**只是長大了**：

- 只是長大了 → `remeasure.py`，然後 `build.py`，然後 `verify.py`、
  `build_owner.py`、`verify_owner.py`。`remeasure.py` 會印出每一項移動了多少，
  那份清單就是重點。
- 形狀變了（模組不見了、新增了、換邊了）→ `remeasure.py` 會拒絕寫入並以非零
  結束。那要改的是 `model.json` 的結構，不是行數。
