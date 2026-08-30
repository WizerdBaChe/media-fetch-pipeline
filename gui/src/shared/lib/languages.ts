/**
 * The languages this product offers, in the codes the models actually carry.
 *
 * FLORES-200, not ISO-639-1, because `zho_Hant` versus `zho_Hans` is a
 * distinction `zh` cannot make and it is exactly the one this product's
 * users care about.
 *
 * In `shared/` rather than in the slice that first needed it: two features
 * translate now -- a transcript and a document -- and slices in the same
 * layer may not import each other (the layering rule is linted, and
 * `shared/lib/layering.test.ts` runs the linter to prove it). A copy per
 * feature would be a second list to forget to update.
 */

export interface LanguageChoice {
  /** FLORES-200, as the engine wants it. */
  code: string;
  label: string;
}

export const LANGUAGES: readonly LanguageChoice[] = [
  { code: "zho_Hant", label: "繁體中文" },
  { code: "zho_Hans", label: "簡體中文" },
  { code: "eng_Latn", label: "英文" },
  { code: "jpn_Jpan", label: "日文" },
  { code: "kor_Hang", label: "韓文" },
  { code: "spa_Latn", label: "西班牙文" },
  { code: "fra_Latn", label: "法文" },
  { code: "deu_Latn", label: "德文" },
];

/**
 * Does this target need the clause-loss caution?
 *
 * Only Chinese targets, because that is where it was measured: `fra_Latn`,
 * `deu_Latn` and `jpn_Jpan` came back whole and `zho_Hant` -- this product's
 * default -- was the worst (D-130). A caution that fired on every target
 * would be one nobody reads.
 *
 * Here rather than written out at each call site: it is now asked in two
 * places, and two copies of a measured claim is one copy that goes stale
 * when the model changes.
 */
export function mayLoseAClause(targetCode: string): boolean {
  return targetCode.startsWith("zho");
}

/** The sentence that goes with it, so both surfaces say the same thing. */
export const CLAUSE_LOSS_NOTE =
  "中文譯文偶爾會漏掉句子裡的一個子句，重要內容請對照原文。";
