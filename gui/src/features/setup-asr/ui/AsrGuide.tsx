/**
 * 「這東西要去哪裡拿、我的電腦跑不跑得動」 — the reference half of the setup
 * panel.
 *
 * Separate file, not separate concern: the panel above it is about THIS
 * machine's state and every sentence in it changes; everything here is fixed
 * knowledge about somebody else's project, and mixing the two made the panel
 * unreadable in both directions.
 *
 * Three rules govern what is allowed in here.
 *
 * **Official first, and labelled when it is not.** `large-v3-turbo` is the
 * one row whose repository does not belong to SYSTRAN -- faster-whisper's own
 * table points at a community conversion -- and it says so, because "official"
 * is the property the user is choosing on.
 *
 * **Every number is either measured or quoted, never remembered.** The
 * memory figures are faster-whisper's own published benchmark; the speed
 * figure is this machine, measured 2026-08-27. Both say which they are.
 *
 * **It is dated.** These are facts about a fast-moving project, and a
 * reference with no date is one nobody can tell has rotted.
 *
 * review-when: faster-whisper publishes a release that changes its model
 * table or its stated CUDA requirement. Verified against faster-whisper 1.2.1
 * and its README on 2026-08-28.
 */

import { useState } from "react";
import { Button } from "@/shared/ui/Button";

/**
 * `faster_whisper.utils._MODELS`, read out of the installed package rather
 * than off a web page (faster-whisper 1.2.1, 2026-08-28). This is the table
 * that decides what a model NAME downloads, so it is the table that decides
 * what a user should go and fetch by hand.
 */
const MODELS: {
  name: string;
  repo: string;
  size: string;
  note: string;
  official: boolean;
}[] = [
  {
    name: "large-v3",
    repo: "Systran/faster-whisper-large-v3",
    size: "約 3.1 GB",
    note: "準確度最高，中文最穩。沒有特別理由就選這個。",
    official: true,
  },
  {
    name: "large-v3-turbo",
    repo: "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    size: "約 1.6 GB",
    note: "快很多、準確度略降。這一列不是 SYSTRAN 官方，是 faster-whisper 自己指過去的社群轉檔。",
    official: false,
  },
  {
    name: "medium",
    repo: "Systran/faster-whisper-medium",
    size: "約 1.5 GB",
    note: "顯示卡記憶體不夠跑 large 時的折衷。",
    official: true,
  },
  {
    name: "small",
    repo: "Systran/faster-whisper-small",
    size: "約 0.5 GB",
    note: "只有 CPU、又要能跑得完的時候用。中文會明顯變差。",
    official: true,
  },
  {
    name: "distil-large-v3",
    repo: "Systran/faster-distil-whisper-large-v3",
    size: "約 1.5 GB",
    note: "蒸餾版，只支援英文。中文錄音不要選。",
    official: true,
  },
];

/**
 * Ready-converted NLLB-200 models. File listings read 2026-08-28: the
 * OpenNMT repositories ship `tokenizer.json` and NOT a SentencePiece file,
 * which is why this product's translation needs nothing installed beyond
 * what faster-whisper already brings.
 *
 * NLLB rather than a per-pair family because of one property this project
 * actually needs: FLORES-200 treats `zho_Hant` and `zho_Hans` as different
 * languages, so "translate into Traditional Chinese" is a thing the model
 * can be ASKED for rather than something a prompt has to beg for.
 */
const TRANSLATION_MODELS: { repo: string; size: string; note: string }[] = [
  {
    repo: "OpenNMT/nllb-200-distilled-1.3B-ct2-int8",
    size: "約 1.4 GB",
    note: "一般用這個。200 種語言，含繁體與簡體中文。",
  },
  {
    repo: "OpenNMT/nllb-200-3.3B-ct2-int8",
    size: "約 3.4 GB",
    note: "品質更好、更慢、更吃記憶體。長篇文件才值得。",
  },
];

/** Sensitive to the machine, so it is shown as a range and sourced. */
const HARDWARE: { need: string; gpu: string; cpu: string }[] = [
  {
    need: "large 系列",
    gpu: "約 4.5 GB 顯示卡記憶體（float16）／約 2.9 GB（int8）",
    cpu: "跑得動但很慢，通常比實際長度還久",
  },
  {
    need: "medium",
    gpu: "約 2.5 GB",
    cpu: "勉強可用",
  },
  {
    need: "small",
    gpu: "約 1.5 GB",
    cpu: "約 1.5 GB 記憶體（int8），是 CPU 唯一實際可用的等級",
  },
];

interface CopyableProps {
  label: string;
  command: string;
}

/** A command plus a button that puts it on the clipboard.
 *
 *  The clipboard failure is SHOWN rather than swallowed: this is the one
 *  place in the panel where the user's next action happens outside the app,
 *  and a copy button that silently did nothing would send them to a terminal
 *  with an empty clipboard. */
function Copyable({ label, command }: CopyableProps) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  return (
    <div className="mfp-asr__copy">
      <span className="mfp-asr__copy-label">{label}</span>
      <code className="mfp-mono">{command}</code>
      <Button
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(command);
            setState("copied");
          } catch {
            setState("failed");
          }
        }}
      >
        {state === "copied" ? "已複製" : state === "failed" ? "複製失敗" : "複製"}
      </Button>
      {state === "failed" && (
        <small className="mfp-asr__muted">
          這個環境不允許寫入剪貼簿，請直接反白上面那一行複製。
        </small>
      )}
    </div>
  );
}

export function AsrGuide() {
  return (
    <div className="mfp-asr__guide">
      <section>
        <h4>一、先準備「辨識引擎」</h4>
        <p>
          引擎是一個裝了 <code>faster-whisper</code> 的 Python 環境。它沒有放進本程式的安裝檔，
          因為引擎加上模型有好幾 GB，放進去會讓安裝檔從 100 MB 變成好幾 GB——
          而大部分的下載工作根本用不到它。
        </p>
        <p className="mfp-asr__muted">
          已經有其他語音轉文字工具的話，多半可以直接指過去用，不必再裝一次。
        </p>
        <Copyable label="① 建立環境" command="py -3.12 -m venv D:\asr-venv" />
        <Copyable
          label="② 安裝引擎"
          command="D:\asr-venv\Scripts\python.exe -m pip install faster-whisper"
        />
        <p className="mfp-asr__muted">
          裝完之後回到上面按「選擇 Python…」，選 <code>D:\asr-venv\Scripts\python.exe</code>。
          本程式會當場確認它能不能用，不會等到你真的要轉檔才失敗。
        </p>
      </section>

      <section>
        <h4>二、再準備「語音模型」</h4>
        <p>
          模型是一個資料夾，裡面有 <code>model.bin</code>。本程式吃的是 CTranslate2 格式；
          OpenAI 原版的 <code>.pt</code> 檔和 Hugging Face transformers 版面都要先轉檔，
          所以直接抓下面這些「已經轉好」的版本最省事。
        </p>
        <table className="mfp-asr__table">
          <thead>
            <tr>
              <th scope="col">名稱</th>
              <th scope="col">下載位置（Hugging Face）</th>
              <th scope="col">大小</th>
              <th scope="col">說明</th>
            </tr>
          </thead>
          <tbody>
            {MODELS.map((model) => (
              <tr key={model.name}>
                <th scope="row" className="mfp-mono">
                  {model.name}
                </th>
                <td>
                  <a
                    href={`https://huggingface.co/${model.repo}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {model.repo}
                  </a>
                  {!model.official && <span className="mfp-asr__badge">非官方</span>}
                </td>
                <td className="mfp-mono">{model.size}</td>
                <td>{model.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mfp-asr__muted">
          在該頁面按 <em>Files and versions</em>，把 <code>model.bin</code>、
          <code>config.json</code>、<code>tokenizer.json</code>、
          <code>vocabulary.json</code>、<code>preprocessor_config.json</code>{" "}
          下載到同一個資料夾，再回到上面按「瀏覽…」選它。
          少了任何一個，本程式會直接告訴你少了什麼，不會裝到一半才失敗。
        </p>
      </section>

      <section>
        <h4>三、想翻譯的話，再準備一個「翻譯模型」</h4>
        <p>
          翻譯是<strong>另一個功能</strong>，要另一種模型。它跟語音辨識共用同一個引擎環境，
          所以引擎設定好之後，翻譯只差一個模型檔——不必再裝任何套件。
        </p>
        <p className="mfp-asr__muted">
          下面這些是已經轉成 CTranslate2 的 NLLB-200，一個模型涵蓋 200 種語言，
          而且<strong>把繁體（zho_Hant）和簡體（zho_Hans）當成兩種語言</strong>，
          這正是本程式需要的區分。
        </p>
        <table className="mfp-asr__table">
          <thead>
            <tr>
              <th scope="col">下載位置（Hugging Face）</th>
              <th scope="col">大小</th>
              <th scope="col">說明</th>
            </tr>
          </thead>
          <tbody>
            {TRANSLATION_MODELS.map((entry) => (
              <tr key={entry.repo}>
                <td>
                  <a
                    href={`https://huggingface.co/${entry.repo}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {entry.repo}
                  </a>
                </td>
                <td className="mfp-mono">{entry.size}</td>
                <td>{entry.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mfp-asr__muted">
          整個資料夾抓下來就好，特別是 <code>tokenizer.json</code>——沒有它就不能用，
          而本程式在加入模型時就會告訴你少了哪一個。如果你抓到的模型附的是
          <code>sentencepiece</code> 的 <code>.model</code> 檔（例如 OPUS-MT 系列），
          清單上會標「需要 sentencepiece」，那表示要在引擎環境多裝一個套件。
        </p>
        <Copyable
          label="補裝（只有需要時）"
          command="D:\asr-venv\Scripts\python.exe -m pip install sentencepiece"
        />
      </section>

      <section>
        <h4>四、我的電腦跑得動嗎</h4>
        <table className="mfp-asr__table">
          <thead>
            <tr>
              <th scope="col">模型</th>
              <th scope="col">有 NVIDIA 顯示卡</th>
              <th scope="col">只有 CPU</th>
            </tr>
          </thead>
          <tbody>
            {HARDWARE.map((row) => (
              <tr key={row.need}>
                <th scope="row">{row.need}</th>
                <td>{row.gpu}</td>
                <td>{row.cpu}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mfp-asr__muted">
          顯示卡記憶體數字引自 faster-whisper 官方 README 的實測表（large-v2、13 分鐘音檔、beam 5）。
          本機 RTX 5070 實測 large-v3 float16 約為實際長度的 7–14 倍速（2026-08-27）。
          <strong> 沒有顯示卡也能用，只是慢</strong>——不會不能用。
        </p>
      </section>

      <section>
        <h4>五、版本上會卡住的地方</h4>
        <dl className="mfp-asr__faq">
          <dt>顯示卡版：需要 CUDA 12 的 cuBLAS 與 cuDNN 9</dt>
          <dd>
            這是 faster-whisper 官方寫明的需求。舊環境要降 CTranslate2 版本：
            CUDA 11 + cuDNN 8 用 <code>ctranslate2==3.24.0</code>；
            CUDA 12 + cuDNN 8 用 <code>ctranslate2==4.4.0</code>。
          </dd>
          <dt>RTX 50 系列（sm_120）不要用 int8</dt>
          <dd>
            會出現 <code>CUBLAS_STATUS_NOT_SUPPORTED</code>。本程式的預設值
            <code>auto</code> 在顯示卡上就是選 float16，正是為了避開它。
          </dd>
          <dt>「模型格式版本比引擎新」</dt>
          <dd>
            表示模型是用比較新的 CTranslate2 轉的。多半還是讀得起來；真的讀不了，
            升級引擎環境裡的 CTranslate2 即可（<code>pip install -U ctranslate2</code>）。
          </dd>
          <dt>只認得英文的模型</dt>
          <dd>
            名稱帶 <code>.en</code> 或 <code>distil-</code> 的多半只支援英文。
            本程式在加入模型時就會判斷並告訴你，不會等到轉出一片空白才發現。
          </dd>
        </dl>
      </section>

      <section>
        <h4>官方來源</h4>
        <ul className="mfp-asr__links">
          <li>
            <a
              href="https://github.com/SYSTRAN/faster-whisper"
              target="_blank"
              rel="noreferrer"
            >
              SYSTRAN/faster-whisper
            </a>
            ——引擎本體、安裝說明、上面那張記憶體實測表
          </li>
          <li>
            <a href="https://huggingface.co/Systran" target="_blank" rel="noreferrer">
              Hugging Face 上的 Systran
            </a>
            ——官方轉好的模型都在這裡
          </li>
          <li>
            <a
              href="https://github.com/openai/whisper"
              target="_blank"
              rel="noreferrer"
            >
              openai/whisper
            </a>
            ——模型的原始出處（原版格式，本程式不能直接讀）
          </li>
        </ul>
        <p className="mfp-asr__muted">
          以上資訊核對於 faster-whisper 1.2.1，日期 2026-08-28。
        </p>
      </section>
    </div>
  );
}
