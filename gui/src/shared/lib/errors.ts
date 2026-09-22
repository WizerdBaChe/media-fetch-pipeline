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
  /**
   * What pressing the recovery button COSTS, when it costs something.
   *
   * A separate field from `hint` because it is read at a different moment:
   * a hint explains a row that already went wrong, a cost is what the reader
   * needs BEFORE deciding, and it must therefore be on screen rather than in
   * a `title` only a mouse can reach (UX walkthrough F6; tours.ts's own rule
   * 3, 「說出代價」). `errors.test.ts` asserts no `hint` names one, because
   * the way this was lost the first time was by writing the sentence in the
   * field that happened to exist.
   */
  cost?: string;
}

export const ERROR_PRESENTATION: Record<string, ErrorPresentation> = {
  unsupported_url: { label: "不支援的網址", recovery: "remove" },
  upstream_structure_change: {
    // 這一格現在是「分不出來」，不是「平台改版了」。
    //
    // 舊的 hint 直接斷定改版，而這個代碼在 2026-09-03 之前是 yt-dlp 每一種
    // 失敗的收容所——限流、DNS 失敗、平台當機全都掛在這句話下面。使用者讀到
    // 的是「程式壞了要修」，真相卻常常是「等一下再試」，於是有人改用另一種
    // 複製網址的方式來閃避一個跟網址無關的問題。限流和連不上已經各自有代碼
    // 了，剩在這裡的是真的沒認出來的那些，所以這句話只講它知道的事。
    label: "無法解析這個頁面",
    recovery: "retry",
    hint: "認不出這個平台回來的內容。可能是平台改版，也可能是這次回應不完整；重試若仍失敗請連同錯誤訊息回報",
  },
  upstream_unreachable: {
    // 從 upstream_structure_change 拆出來（D-155），理由跟 no_media_in_post
    // 當初拆出來一樣：那個代碼的意思是「程式要修」，而網路斷線不是。
    label: "連不上平台",
    recovery: "retry",
    hint: "平台沒有回應，或它自己回報了錯誤。這不是程式的問題，檢查網路後稍後重試即可",
  },
  no_media_in_post: {
    // Split from upstream_structure_change (O-10). Recovery is "remove",
    // not "retry": the post is fine and retrying will find the same
    // nothing. Telling the user to retry here wastes their time and a
    // request the pacing guard has to pay for.
    //
    // The wording changed on 2026-09-11 because the code gained two
    // producers (P-88): until then only yt-dlp raised it, and only for X,
    // so "沒有可下載的影片" and "X 的…貼文" were accurate. An Instagram or
    // Threads text post reaches it now and has no IMAGE either, so the old
    // label answered a question the user did not ask and named a platform
    // they were not on. P-74's shape in the copy rather than in the switch.
    label: "這則貼文沒有可下載的媒體",
    recovery: "remove",
    hint: "貼文本身沒問題，只是沒有圖片或影片可以下載；Threads、Instagram 的純文字貼文，以及 X 的純圖片或純文字貼文都會是這樣",
  },
  link_expired: {
    label: "連結過期",
    recovery: "reprobe",
    cost: "重新分析會消耗 1 個配額單位",
  },
  budget_exhausted: { label: "等待配額", recovery: "wait" },
  rate_limited: {
    // 2026-09-03 起這個代碼多了一個來源：以前只有傳輸中 CDN 回 429 才會走到
    // 這裡，現在解析階段被平台擋下（Bilibili 的 412）也是這一格。加 hint 是
    // 因為使用者最需要知道的一件事在標籤裡放不下——被擋跟網址寫法無關，換一
    // 種複製網址的方式不會有幫助，而換平台繼續抓才是真的會讓事情變糟。
    label: "被限流，等待中",
    recovery: "wait",
    hint: "平台這次拒絕了我們，跟你貼的網址寫法無關。這個平台會先暫停一段時間再自動恢復，期間繼續重試只會拉長它",
  },
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
    hint: "重試會從已下載的位元組續傳，不會從頭再來",
  },
  platform_transfer_blocked: {
    // Hint corrected 2026-08-19. It used to end "yt-dlp 自己也被同樣擋住",
    // generalized from one machine whose yt-dlp was simply too old -- so it
    // told every future user their situation was hopeless when the actual
    // fix was an update. What the code means is only the first clause.
    label: "平台拒絕傳輸",
    recovery: "doctor",
    hint: "平台給得出畫質清單卻擋下實際下載，重試無效；先到「設定 → 診斷」確認 yt-dlp 版本",
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
  no_subtitle_pixels_in_band: {
    // The M2 acceptance failure, made presentable: the band was aimed at a
    // video whose captions are a separate track. Recovery is neither retry
    // nor remove -- the same run with the same numbers fails the same way,
    // and what has to change is a parameter the user can see.
    label: "框選的位置沒有字幕",
    recovery: "none",
    hint: "拖動字幕帶對準畫面上的字，或改用字幕檔／貼文網址當來源",
  },
  media_tool_failed: {
    label: "影音處理失敗",
    recovery: "retry",
    hint: "ffmpeg 拒絕了這次工作，錯誤資料夾裡有完整訊息",
  },
  path_escape: { label: "輸出路徑不合法", recovery: "settings" },
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
    //
    // That sentence was FALSE for a year, and this row was the only error
    // message a reader could get by mistyping a path in an analysis tool:
    // 「呼叫端送出了服務無法解讀的參數」 blamed the program for the most
    // ordinary user typo there is, so people reported a bug instead of
    // fixing the path (UX walkthrough 2026-09-07, F1). Every "no such file"
    // now carries `source_not_found` below, and this row is back to
    // describing only what it was written for.
    label: "請求格式錯誤",
    recovery: "none",
    hint: "呼叫端送出了服務無法解讀的參數",
  },
  source_not_found: {
    // The path was read and is not there. Recovery is "none" because there
    // is no button this row could offer that helps: the fix is in the field
    // the user is already looking at, and the hint names the control beside
    // it rather than a generic 重試.
    label: "找不到這個檔案",
    recovery: "none",
    // The button is called 「選擇檔案…」 in the workspace that takes a path
    // (F8), and a hint that names a control the reader cannot find is worse
    // than one that names none.
    hint: "請確認路徑，或用「選擇檔案…」重選；檔案可能被移走或改名",
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

/** The sentence a surface with only ONE line to spend should print: the
 *  hint if there is one, otherwise the cost. A workspace that shows a
 *  failure as one line must not be the surface that drops 「會消耗 1 個配額
 *  單位」 -- the queue row renders `cost` in its own right. */
export function hintOrCost(presented: ErrorPresentation): string | undefined {
  return presented.hint ?? presented.cost;
}

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
