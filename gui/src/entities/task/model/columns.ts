/**
 * The queue table's columns, declared once.
 *
 * Alignment used to be declared twice: a width on `.mfp-col--*`, an alignment
 * on the header cell's class, and -- three columns out of ten -- nothing at
 * all on the body cell. Nothing made the two halves agree, so they drifted,
 * and three separate acceptance rounds found a header sitting over values it
 * did not belong to (UAT item 5-6; R7-5 on 2026-08-16; 操作 on 2026-08-23,
 * measured 229px from the buttons it labelled).
 *
 * The header cell and the body cell now read the same entry and emit the same
 * `data-align`, and one CSS rule per alignment keys off that attribute. A
 * header that disagrees with its own column is no longer a bug that can be
 * written -- which is the property, not the tidiness.
 *
 * This lives in `entities/task` rather than in the widget because both the
 * widget that draws the header and the entity that draws the row need it, and
 * a widget may import an entity while the reverse is forbidden (PSM §3.1).
 */

export type ColumnAlign = "left" | "right";

export type QueueColumnId =
  | "check"
  | "platform"
  | "subject"
  | "items"
  | "quality"
  | "state"
  | "progress"
  | "speed"
  | "eta"
  | "actions";

export interface QueueColumn {
  id: QueueColumnId;
  /** Header text. Empty for the select-all column, whose head is a control. */
  label: string;
  /** Fixed px width, or `null` to absorb whatever the fixed columns leave. */
  width: number | null;
  align: ColumnAlign;
  /**
   * Dropped when the window is too narrow to hold every column.
   *
   * Only for columns whose value can be read some other way or waited for:
   * 平台 repeats what 來源's URL says, 項目 is in the manifest, and 速度/剩餘
   * are moments in a transfer that the progress bar is already reporting --
   * they move to that cell's tooltip rather than vanishing.
   */
  optional?: boolean;
  /** A tighter width used only in the narrow layout. */
  narrowWidth?: number;
}

/**
 * Widths are MEASURED against the widest real value, not budgeted. The note
 * on each one is why it is that number; changing a width without re-measuring
 * is how 「已自訂」 got clipped the first time.
 */
export const QUEUE_COLUMNS: readonly QueueColumn[] = [
  { id: "check", label: "", width: 34, align: "left" },
  { id: "platform", label: "平台", width: 52, align: "left", optional: true },
  // No width: 來源 is the absorber. It truncates with an ellipsis and carries
  // the full URL in a title, which is what makes it the right column to take
  // the slack out of every time another one needs more.
  { id: "subject", label: "來源", width: null, align: "left" },
  // Split out of 畫質 (UAT item 5-6). A count is a number and belongs with the
  // other numbers: 「13 項」 is the widest real value, 「—」 the narrowest.
  { id: "items", label: "項目", width: 58, align: "right", optional: true },
  // The select alone is 126px and 「已自訂」 43px, so the content needs 197 and
  // gets 208. Both labels are R3-1 acceptance items and neither may clip.
  { id: "quality", label: "畫質", width: 208, align: "left" },
  // 128, not the 96 it had: at 96 the longest error label
  // 「⚠ 頁面結構改變，無法解析」 measured 161px of content in a 96px cell and
  // was cut mid-word with no ellipsis, so a failed row could not be read at
  // all (measured 2026-08-23). The 32px comes out of 來源.
  { id: "state", label: "狀態", width: 128, align: "left" },
  // 120 narrow: 16 of padding, 38 for the percentage and 8 of gap leave the
  // bar 58px, which still reads as a bar. Below that it is a coloured dash.
  { id: "progress", label: "進度", width: 210, align: "right", narrowWidth: 120 },
  { id: "speed", label: "速度", width: 92, align: "right", optional: true },
  { id: "eta", label: "剩餘", width: 68, align: "right", optional: true },
  // 292, not the 268 it had. MEASURED on a COMPLETED row in the DESKTOP
  // shell (2026-08-31): 重新下載 72 + 10 of zone gap + 延伸 42 + 3 +
  // 開啟檔案位置 97 + 3 + 移除 47 + 16 of cell padding = 290, so 移除 hung
  // 14px past the table's right edge and `.mfp-main` grew a horizontal
  // scrollbar. Two pixels over the measurement, so a sub-pixel change in one
  // label does not immediately clip again.
  //
  // Why no gate saw it: in a browser `desktop()` finds no preload bridge and
  // that button reads 複製路徑 -- 72px, not 97 -- so every geometry run so far
  // measured a cell 25px narrower than the one the product ships in. The
  // widest cell in the table existed only in the shell nothing was measuring
  // (`e2e/fixtures.installDesktopBridge` is what closes that).
  { id: "actions", label: "操作", width: 292, align: "left" },
];

const BY_ID = new Map(QUEUE_COLUMNS.map((column) => [column.id, column]));

/**
 * What 來源 needs before it stops saying which post a row is.
 *
 * 100px is seven CJK characters plus the ellipsis at the table's 13px. It is a
 * floor, not a target: 來源 is the absorber and takes every pixel the fixed
 * columns do not want, which at the window the app opens at is about 200.
 *
 * The number exists because it had no name and therefore no defender. 來源 is
 * the only column with no width of its own, so every widening of another one
 * came out of it silently -- measured 2026-08-30 at 46px in the window the app
 * opened at, and 0px at the smallest window it allowed. A column at zero is
 * worse than a scrollbar: nothing on screen suggests anything is missing.
 *
 * 100 rather than the 140 it was set at, on a 2026-08-31 ruling: a truncated
 * author name is a hint, not the identifier -- the full one is on the cell's
 * title and the URL is in the row -- so 來源 is where 操作's missing 24px come
 * from. What that buys is where the floor BINDS, not the shipped window: at
 * 1360 來源 draws ~200px either way, and the floor decides only the breakpoint
 * and what is left at the narrowest window.
 *
 * Where it is a guarantee and where it is a target: the full layout cannot be
 * chosen unless the space holds it (`FULL_LAYOUT_MIN`), so there it holds by
 * construction. The narrow layout has nothing below it to fall back to -- at
 * `WINDOW.minWidth` (884px of content behind a real frame) the five remaining
 * columns take 782 and 來源 gets 102, and a vertical scrollbar on a long queue
 * takes it to about 87. Below the floor there, and said out loud rather than
 * arithmetic nobody did: the alternative is a wider minimum window.
 */
export const SUBJECT_MIN = 100;

const widthOf = (column: QueueColumn, narrow: boolean) =>
  (narrow ? (column.narrowWidth ?? column.width) : column.width) ?? 0;

/** What a layout costs before 來源 gets anything. */
function fixedWidth(narrow: boolean): number {
  return QUEUE_COLUMNS.filter((column) => !(narrow && column.optional)).reduce(
    (total, column) => total + widthOf(column, narrow),
    0,
  );
}

/**
 * The narrowest SPACE the full column set can be drawn in.
 *
 * DERIVED from the widths above rather than chosen, so widening a column moves
 * the breakpoint with it instead of quietly eating 來源 again. A round number
 * here would be a second fact to keep in step, which is the shape of every
 * defect this file already carries a note about.
 *
 * Compared against the TABLE'S OWN CONTAINER, never against the window. A
 * media query answers about the viewport, and the table is drawn inside
 * `.mfp-main`'s content box; those differ by a scrollbar as soon as the queue
 * is long enough to need one, and the difference came out of 來源. Two rulers
 * for one decision is the whole bug (`shared/lib/useContainerWidth`).
 */
export const FULL_LAYOUT_MIN = fixedWidth(false) + SUBJECT_MIN;

/** What 來源 gets in a given space, so a caller can say so in a message. */
export function subjectWidthIn(space: number, narrow: boolean): number {
  return space - fixedWidth(narrow);
}

/**
 * The columns to draw, and how wide.
 *
 * ONE list, consumed by the `<colgroup>`, the header row and the body row, so
 * a column cannot be hidden in one place and drawn in another (D-83).
 */
export function visibleColumns(narrow: boolean): readonly QueueColumn[] {
  if (!narrow) return QUEUE_COLUMNS;
  return QUEUE_COLUMNS.filter((column) => !column.optional).map((column) =>
    column.narrowWidth === undefined ? column : { ...column, width: column.narrowWidth },
  );
}

/**
 * The attributes a header cell and its body cells must both carry.
 *
 * Spread onto the `<th>` and onto the `<td>`; there is deliberately no way to
 * get one without the other, because the pair IS the invariant.
 */
export function columnProps(id: QueueColumnId): {
  "data-col": QueueColumnId;
  "data-align": ColumnAlign;
} {
  const column = BY_ID.get(id);
  if (!column) {
    // Unreachable through the type, but a wrong id would otherwise fail as a
    // silently unaligned cell -- the exact class of defect this file exists
    // to end.
    throw new Error(`unknown queue column: ${id}`);
  }
  return { "data-col": column.id, "data-align": column.align };
}
