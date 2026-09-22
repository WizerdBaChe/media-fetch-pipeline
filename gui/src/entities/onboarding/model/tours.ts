/**
 * What each screen expects you to already know, written down.
 *
 * The premise, stated so later edits keep it: **the reader has never used
 * this app, does not know what yt-dlp is, and has not read any
 * documentation.** Every sentence here is checked against that person, not
 * against somebody who built it.
 *
 * Three rules for the copy, all of them learned from what this project's own
 * panels got wrong before:
 *
 *   1. **Say what the screen is FOR in one sentence**, before anything else.
 *      A person who reads only the first line has to come out able to use it.
 *   2. **Never name a file format, a config key or a program** where a thing
 *      will do. 「聲音檔或影片檔」, not 「media container」.
 *   3. **Say what it costs.** Minutes, gigabytes, and what cannot be undone.
 *      A guide that only lists the happy path is an advertisement.
 *
 * Ids are the wire values `config.guides.seen` stores. Renaming one shows
 * its guide again to everybody, which is a migration, not a rename.
 */

export interface GuideStep {
  title: string;
  body: string;
}

export interface Tour {
  id: string;
  title: string;
  /** One sentence, above the steps. What this screen is for. */
  lede: string;
  steps: GuideStep[];
  /** The unflattering part. Rendered apart from the steps, deliberately:
   *  it is the paragraph a person needs BEFORE they spend twenty minutes,
   *  and folding it into step 4 is how it stops being read. */
  caveat?: string;
  /**
   * The control names these steps tell the reader to press, spelled exactly
   * as the workspace spells them.
   *
   * Declared rather than inferred from the prose, because a quote is not
   * always a control -- 「畫面有沒有變」 is a question, not a button. The
   * workspace's own test renders it and asserts each of these exists, and
   * `tours.test.ts` asserts each is actually quoted in a step: two
   * directions, so neither the guide nor the button can drift alone.
   *
   * The defect this exists for (F8): the guide said 「選擇檔案」, 逐字稿 and
   * 文件翻譯 called the button 選檔案…, and 引用長圖 called it 選擇檔案… --
   * three names, and a first-time reader looking for the words they had just
   * been given found none of them.
   */
  controls?: readonly string[];
  /**
   * Quotes in these steps that are NOT control names, with the reason they
   * are quoted at all. Listed so the rule above has no silent gap: every
   * 「」 in a tour is a settings pointer, a control, or one of these.
   */
  prose?: readonly string[];
}

export const TOURS: Record<string, Tour> = {
  quotestack: {
    id: "quotestack",
    title: "引用長圖怎麼用",
    lede: "把影片裡帶字幕的畫面一張一張疊成一張長圖，用來引用影片內容。",
    steps: [
      {
        title: "1. 選影片，然後告訴它字幕在哪一條",
        body:
          "挑好影片之後，畫面上會出現一張真實的畫格。用滑鼠在上面拉出一個橫向的帶狀範圍，" +
          "把字幕框起來就好——不需要輸入任何數字。",
      },
      {
        title: "2. 決定要擷取哪一段",
        body:
          "可以只做影片的其中一段。整部長片全做會花比較久，先用一小段試一次再決定要不要整部跑。",
      },
      {
        title: "3. 產生長圖",
        body: "按下去之後會逐格比對字幕有沒有變，只留下不一樣的那幾張，最後接成一張圖。",
      },
    ],
    caveat:
      "它是靠「畫面有沒有變」來判斷字幕換了沒有，所以字幕是硬燒在畫面上的影片效果最好；" +
      "字幕會慢慢淡入淡出、或背景一直在動的影片，可能會多出或漏掉幾張。",
    // The one quote here is the QUESTION the comparison asks, not a button.
    prose: ["畫面有沒有變"],
  },

  brief: {
    id: "brief",
    title: "貼文解說怎麼用",
    lede: "貼一個公開貼文的連結，把它的圖片、文字和影片整理成一包，交給 AI 助理讀。",
    steps: [
      {
        title: "1. 貼上連結",
        body: "支援 Instagram、Threads、YouTube、X、Bilibili 的公開貼文。不支援需要登入才看得到的內容。",
      },
      {
        title: "2. 產生資料包",
        body: "程式會把圖片、貼文文字（還有影片，如果你要的話）抓下來，整理成一個資料夾。",
      },
      {
        title: "3. 交給 AI 助理",
        body:
          "這個程式本身不會「看懂」貼文在說什麼——它只負責把材料備齊。" +
          "解讀的部分交給你自己的 AI 助理，把資料夾路徑給它就行。",
      },
    ],
    caveat:
      "這個程式永遠不會登入任何帳號，所以只能處理不用登入就看得到的貼文。" +
      "抓下來的內容請自行確認著作權和使用範圍。",
    // 「看懂」 is the thing this program does NOT do. Quoted for emphasis.
    prose: ["看懂"],
  },
};

/** The id the first-launch dialog stores. Not in `TOURS`: it is a different
 *  component -- it holds live install buttons, not a list of steps -- and
 *  putting it in this table would invite something to render it as one. */
export const FIRST_RUN = "first-run";
