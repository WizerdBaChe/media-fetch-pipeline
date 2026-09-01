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
}

export const TOURS: Record<string, Tour> = {
  transcript: {
    id: "transcript",
    title: "逐字稿怎麼用",
    lede: "把一段錄音或影片變成有時間標記的文字稿，全部在這台電腦上做，不會上傳到任何地方。",
    steps: [
      {
        title: "1. 選一個檔案",
        body:
          "按「選擇檔案」挑一個影片或錄音檔；也可以從佇列裡剛下載好的項目直接送過來。" +
          "如果那部影片本身就附了字幕，程式會先用字幕，這樣最快也最準。",
      },
      {
        title: "2. 第一次要先準備語音辨識",
        body:
          "沒有字幕的檔案要靠語音辨識聽寫。這需要另外準備一個辨識引擎和一份語音模型，" +
          "在「設定 → 語音辨識與翻譯」裡照著上面的指示做一次就好，之後都不用再設定。",
      },
      {
        title: "3. 按下開始，然後等",
        body:
          "一小時的錄音大約要幾分鐘到十幾分鐘，取決於這台電腦。" +
          "進行中可以切到別的畫面，進度會顯示在最下面那一行，不會因為離開就中斷。",
      },
      {
        title: "4. 修稿：校正與整理",
        body:
          "跑完之後可以用「校正」把專有名詞改對（要先在詞庫裡登記過那些詞），" +
          "用「整理」把口頭禪和重複的字拿掉。原始稿一定會留著，修改都會另存成新檔。",
      },
    ],
    caveat:
      "機器聽寫一定會有錯，尤其是專有名詞、人名和中英文夾雜的段落。這份稿子適合拿來找重點、做筆記，" +
      "不適合直接當成逐字紀錄使用——請自己看過一遍。",
  },

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
  },

  translatedoc: {
    id: "translatedoc",
    title: "文件翻譯怎麼用",
    lede: "把一份純文字或 Markdown 文件整份翻成另一種語言，一樣全部在本機處理。",
    steps: [
      {
        title: "1. 選文件、選語言",
        body: "支援 .txt 和 .md。選好來源語言和目標語言之後按開始。",
      },
      {
        title: "2. 第一次要先準備翻譯模型",
        body:
          "翻譯用的是另一份模型，跟語音辨識的不是同一個。" +
          "同樣在「設定 → 語音辨識與翻譯」裡加一次就好。",
      },
      {
        title: "3. 結果會另存成新檔",
        body: "原本那份文件不會被改到。翻譯結果會存成一個檔名帶語言代碼的新檔案。",
      },
    ],
    caveat: "機器翻譯適合用來看懂大意，正式用途請找人校對過。",
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
  },
};

/** The id the first-launch dialog stores. Not in `TOURS`: it is a different
 *  component -- it holds live install buttons, not a list of steps -- and
 *  putting it in this table would invite something to render it as one. */
export const FIRST_RUN = "first-run";
