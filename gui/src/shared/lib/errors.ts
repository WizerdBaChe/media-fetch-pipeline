/**
 * `errorCode` → what the row says and what it offers (PSM Batch 2 §11).
 *
 * The governing rule: **no row may end in a state with neither a recovery
 * action nor an explanation.** A dead-end row is a defect. `UNKNOWN_ERROR` is
 * the fallback that keeps that true even for a code this table has not seen --
 * it explains that the code is unrecognized rather than rendering blank.
 */

export type Recovery = "remove" | "retry" | "reprobe" | "doctor" | "settings" | "wait" | "none";

export interface ErrorPresentation {
  label: string;
  recovery: Recovery;
  hint?: string;
}

export const ERROR_PRESENTATION: Record<string, ErrorPresentation> = {
  unsupported_url: { label: "不支援的網址", recovery: "remove" },
  upstream_structure_change: {
    label: "頁面結構改變，無法解析",
    recovery: "retry",
    hint: "平台可能改版了，重試若仍失敗請回報",
  },
  no_media_in_post: {
    // Split from upstream_structure_change (O-10). Recovery is "remove",
    // not "retry": the post is fine and retrying will find the same
    // nothing. Telling the user to retry here wastes their time and a
    // request the pacing guard has to pay for.
    label: "這則貼文沒有可下載的影片",
    recovery: "remove",
    hint: "貼文本身沒問題，只是沒有影片；X 的純圖片或純文字貼文會是這樣",
  },
  link_expired: {
    label: "連結過期",
    recovery: "reprobe",
    hint: "重新分析會消耗 1 個配額單位",
  },
  budget_exhausted: { label: "等待配額", recovery: "wait" },
  rate_limited: { label: "被限流，等待中", recovery: "wait" },
  dependency_missing: { label: "缺少相依工具", recovery: "doctor" },
  // Distinct from `dependency_missing` on purpose: that one means the tool
  // is not here, this one means fetching it did not work -- almost always
  // something outside this machine (a moved release page, a checksum that
  // did not match, a proxy). Retry first; the manual download link in
  // 需要的程式 is the road that always exists.
  tool_install_failed: {
    label: "自動安裝失敗",
    recovery: "retry",
    hint: "多半是網路或來源端的問題；也可以在「設定 → 診斷 → 需要的程式」用手動下載連結",
  },
  media_transfer_failed: {
    label: "下載失敗",
    recovery: "retry",
    hint: "會自已下載的位元組續傳",
  },
  platform_transfer_blocked: {
    // Hint corrected 2026-08-19. It used to end "yt-dlp 自己也被同樣擋住",
    // generalized from one machine whose yt-dlp was simply too old -- so it
    // told every future user their situation was hopeless when the actual
    // fix was an update. What the code means is only the first clause.
    label: "平台拒絕傳輸",
    recovery: "doctor",
    hint: "平台給得出畫質清單卻擋下實際下載，重試無效；先到「設定 → 相依工具」確認 yt-dlp 版本",
  },
  path_too_long: {
    label: "路徑過長",
    recovery: "settings",
    hint: "請改用較短的輸出資料夾",
  },
  outside_store: {
    label: "這個資料夾是放下載檔案的",
    recovery: "settings",
    hint: "分析結果請放到別的資料夾，不要跟你自己下載的東西混在一起",
  },
  analysis_write_failed: {
    // 圖片是好的，寫不進去的是解說。這句要說清楚，不然使用者會去重抓已經有的檔案。
    label: "圖片都在，但解說存不進去",
    recovery: "settings",
    hint: "可能是資料夾沒有寫入權限或磁碟滿了；圖片不用重抓",
  },
  nothing_to_stack: {
    label: "這段影片裡沒有可以疊的字幕",
    recovery: "none",
    hint: "換一段時間範圍，或確認字幕來源選對了",
  },
  tidy_refused: {
    // 整理版寫檔前會自證：刪掉的放回去必須逐字重建原稿。走到這裡代表
    // 對不起來，所以什麼都沒寫——原稿一定是完好的，而這句話要先講。
    label: "整理版沒有寫出來，原稿沒有動到",
    recovery: "none",
    hint: "逐字稿在中途被改過的話會這樣。重新讀一次再整理",
  },
  refine_refused: {
    // 同一條規則，但這次是「兩段合起來」沒過：每一段各自都自證成功，
    // 反過來還原卻回不到原稿。跟 tidy_refused 分開是因為原因不同——
    // 那個是某一段自己對不起來，這個是組合對不起來。
    label: "校正＋整理沒有寫出來，原稿沒有動到",
    recovery: "none",
    hint: "逐字稿在中途被改過的話會這樣。重新讀一次再做一遍",
  },
  no_subtitle_pixels_in_band: {
    // The M2 acceptance failure, made presentable: the band was aimed at a
    // video whose captions are a separate track. Recovery is neither retry
    // nor remove -- the same run with the same numbers fails the same way,
    // and what has to change is a parameter the user can see.
    label: "框選的位置沒有字幕",
    recovery: "none",
    hint: "拖動字幕帶對準畫面上的字，或改用字幕檔／貼文網址當來源",
  },
  asr_unavailable: {
    // Not 「失敗」: nothing failed. The engine is a separate install by
    // design -- it and its model are several GB against a 107 MB app -- so
    // this is a setup step nobody has done yet.
    //
    // The hint used to name `config.json`, `asr.python`, `faster-whisper`
    // and `mfp doctor` -- four things, none of which is on screen, to fix
    // one thing that now has a panel. Recovery moved from "doctor" to
    // "settings" for the same reason: `doctor` REPORTS this, and 設定 is
    // where it can be repaired.
    label: "還沒設定語音辨識",
    recovery: "settings",
    hint: "只有「聽音檔轉文字」需要它，其他功能都不受影響。到「設定 → 語音辨識」照著上面兩個步驟做一次就好，那裡也寫了引擎和模型要去哪裡拿",
  },
  no_captions_available: {
    // Not a failure of ours and not retryable: this video was never
    // captioned. The one thing that CAN still work is the other path, so
    // the hint names it rather than apologising.
    label: "這支影片沒有字幕軌",
    recovery: "none",
    hint: "字幕若是燒在畫面上，改用引用長圖並框選字幕位置",
  },
  media_tool_failed: {
    label: "影音處理失敗",
    recovery: "retry",
    hint: "ffmpeg 拒絕了這次工作，錯誤資料夾裡有完整訊息",
  },
  path_escape: { label: "輸出路徑不合法", recovery: "settings" },
  glossary_conflict: {
    // Not a breakage: the edit was refused because merging two entries into
    // one is a decision only the user can make. The hint says what to do
    // instead rather than what went wrong.
    label: "詞庫裡已經有這個詞",
    recovery: "none",
    hint: "要合併兩筆的話，先把其中一筆的錯字搬到另一筆，再刪掉多出來的那筆",
  },
  chrome_default_profile: {
    label: "Chrome 設定衝突",
    recovery: "doctor",
    hint: "Chrome 136+ 不允許在預設設定檔上開啟偵錯埠",
  },
  cdp_timeout: {
    label: "讀取頁面逾時",
    recovery: "retry",
    hint: "瀏覽器沒有在時限內回應，通常重試即可",
  },
  usage_error: {
    // Only reachable if something calls the API with a malformed request --
    // the GUI itself should never produce one.
    label: "請求格式錯誤",
    recovery: "none",
    hint: "呼叫端送出了服務無法解讀的參數",
  },
  login_wall: { label: "需要登入（本工具不登入）", recovery: "remove" },
  illegal_transition: { label: "狀態不允許這個操作", recovery: "none" },
  task_not_found: { label: "任務已不存在", recovery: "none" },
  stack_job_not_found: {
    // Stack jobs live in memory: a restart, or fifty jobs later, and the
    // record is gone. The IMAGE it produced is still on disk, which is what
    // the hint is for.
    label: "這個引用長圖工作已不在紀錄中",
    recovery: "none",
    hint: "工作紀錄只保留最近幾筆，產出的圖檔仍在原本的資料夾",
  },
  server_unreachable: {
    label: "無法連線到本機服務",
    recovery: "none",
    hint: "請確認 `mfp serve` 正在執行",
  },
  queue_locked: {
    // Never arrives over HTTP -- the server refuses to start at all when
    // another process holds the queue lock (G6 §5.1), so the desktop shell
    // reads it off stderr and shows it in the startup dialog. Present here
    // because the taxonomy is one surface, not two.
    label: "另一個實例正在使用佇列",
    recovery: "none",
    hint: "同時只能有一個 media-fetch-pipeline 寫入 queue.json，請先關掉另一個",
  },
  // These two never appear in normal use: they mean something that is not
  // this UI called the API cross-site (O-3).
  forbidden_host: { label: "拒絕：非 loopback 呼叫", recovery: "none" },
  cross_origin_denied: { label: "拒絕：跨站呼叫", recovery: "none" },
};

export function presentError(errorCode: string | null | undefined): ErrorPresentation {
  if (!errorCode) return { label: "未知錯誤", recovery: "retry" };
  return (
    ERROR_PRESENTATION[errorCode] ?? {
      label: `未預期的錯誤（${errorCode}）`,
      recovery: "retry",
      hint: "這個錯誤代碼不在對照表內，請回報",
    }
  );
}
