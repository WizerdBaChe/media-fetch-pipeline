/**
 * "What can I paste here?" -- answered next to the box it is about.
 *
 * Every line here is checked against `inputs.py`'s `identify()`, which is
 * the only thing that actually decides. The temptation in a list like this
 * is to name platforms the product would *like* to handle; a support list
 * that overstates is the same defect as a command named for one target in
 * four, one layer up. So each entry says which path it takes, and the
 * caveats are stated rather than left for the user to discover at 3am.
 *
 * When `identify()` learns or loses a host, this file changes with it.
 */

import { InfoPopover } from "@/shared/ui/InfoPopover";

interface SiteEntry {
  badge: string;
  name: string;
  /** Concrete URL shapes, straight from `identify()`. */
  shapes: string;
  /** How it is acquired. The user cares because it explains the caveats. */
  via: string;
}

/** Ordered by how well this product handles them, best first. */
const SUPPORTED: SiteEntry[] = [
  {
    badge: "IG",
    name: "Instagram",
    shapes: "/p/…、/reel/…、/tv/…",
    via: "專屬取得路徑",
  },
  {
    badge: "TH",
    name: "Threads",
    shapes: "/@帳號/post/…、/share/… 短連結",
    via: "同上，共用同一條路徑",
  },
  {
    badge: "YT",
    name: "YouTube",
    shapes: "watch?v=…、/shorts/…、/live/…、youtu.be/…",
    via: "yt-dlp（需要較新版本）",
  },
  {
    badge: "X",
    name: "X（Twitter）",
    shapes: "/帳號/status/…、/i/status/…（也接受 twitter.com）",
    via: "yt-dlp；只抓影片，純圖片或純文字的貼文抓不到",
  },
  {
    badge: "BL",
    name: "Bilibili",
    shapes: "/video/BV…、b23.tv 短連結",
    via: "yt-dlp；會一併存下彈幕；最高的大會員畫質（1080P 高碼率／60 幀）拿不到",
  },
  {
    badge: "其他",
    name: "其他影音站台",
    shapes: "直接貼網址即可",
    via: "交給 yt-dlp 判斷，它支援上千個站台",
  },
];

const UNSUPPORTED: string[] = [
  "個人檔案頁、動態牆、搜尋結果 —— 會被擋下，避免一次貼上變成整站抓取",
  "限時動態、私人貼文、任何需要登入的內容 —— 本工具不登入任何帳號",
  "Facebook 沒有專屬支援；貼上會被當成一般網址交給 yt-dlp，多數內容會失敗",
];

export function SupportedSitesInfo() {
  return (
    <InfoPopover label="支援哪些網站" testId="supported-sites">
      <h3 className="mfp-info__heading">可以貼這些</h3>
      <ul className="mfp-info__list">
        {SUPPORTED.map((entry) => (
          <li key={entry.name}>
            <span className="mfp-preview__platform">{entry.badge}</span>
            <span className="mfp-info__name">{entry.name}</span>
            <span className="mfp-mono mfp-info__shapes">{entry.shapes}</span>
            <span className="mfp-info__via">{entry.via}</span>
          </li>
        ))}
      </ul>

      <h3 className="mfp-info__heading">這些不會處理</h3>
      <ul className="mfp-info__list mfp-info__list--plain">
        {UNSUPPORTED.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>

      <p className="mfp-info__note">
        貼上時若有連結無法下載，會直接在確認視窗告訴你原因，其餘連結照常加入佇列。
        Bilibili 影片會多存一個 <code className="mfp-mono">.danmaku.xml</code>，
        每則彈幕都帶出現秒數。
      </p>
    </InfoPopover>
  );
}
