/**
 * The size of the window this product actually ships in.
 *
 * Extracted from `createWindow` because it is not only Electron's business.
 * The queue table is `table-layout: fixed` with MEASURED per-column widths
 * (`gui/src/entities/task/model/columns.ts`), so whether the table fits is a
 * property of the PAIR -- and nothing was checking the pair.
 *
 * What that cost, measured 2026-08-30 against these exact numbers: the nine
 * fixed columns summed to 1118px and 來源 is the only absorber, so at the
 * default 1180-wide window 來源 rendered 46px wide, and at `minWidth` 0px --
 * the column carrying the one thing that says WHICH post a row is disappears
 * before the table starts scrolling, and 移除 (last button, last column) sits
 * 218px outside the window. Four geometry tests passed throughout.
 *
 * `gui/e2e/queue-geometry.spec.ts` reads these numbers and fails when the
 * table cannot be drawn inside them. Changing a number here changes what that
 * test asserts, which is the point: one fact, one place, and the shell and the
 * table cannot drift apart again.
 */
/**
 * 1360 wide, not the 1180 this shipped with. The full ten-column table needs
 * `FULL_LAYOUT_MIN` (1242px as of 2026-08-31) of content before 來源 gets its
 * floor, so 1180 could never draw it -- the app opened, every time, on a
 * layout it could not satisfy. 1360 leaves 來源 about 200px and still fits a
 * 1366-wide laptop. The exact number is DERIVED in `columns.ts` and is not
 * repeated here as a constant; only its order of magnitude belongs in this
 * note, because the pair is what `queue-geometry.spec.ts` holds together.
 *
 * `minWidth` stays 900: the table now stands four columns down rather than
 * scrolling, so a narrow window is a supported layout instead of a broken one.
 * It is also the tightest place in the product -- behind a real frame those
 * 900 are 884, and the narrow layout's five columns take 782 of them. 來源's
 * floor is what the rest is measured against there (`columns.SUBJECT_MIN`).
 */
export const WINDOW = {
  width: 1360,
  height: 760,
  minWidth: 900,
  minHeight: 560,
} as const;
