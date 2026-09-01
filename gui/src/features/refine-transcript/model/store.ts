/**
 * 整理這份逐字稿：校正與去語助詞，一次決定、一份成品。
 *
 * 取代了 `correct-transcript` 與 `tidy-transcript` 兩個各自獨立的 slice。
 * 它們不是互斥的，但寫法讓它們變成互斥：兩個面板都指著原稿，套用哪一個
 * 都會得到一份缺了另一半的兄弟檔，而使用者要的是兩樣都做過的那一份。
 *
 *     idle ──ask──▶ offered ──confirm──▶ written
 *       ▲              │  ▲                  │
 *       └────discard───┘  └─ 改詞庫／清單就重問 ─┘
 *
 * `stages` 是**集合**不是順序：勾選的先後不影響輸出，伺服器一律照
 * `refine.ORDER`（先校正、後整理）跑。這是刻意的——手動串接兩個動作時，
 * 同一份內容會因為順序不同拿到兩個檔名，而兩份紀錄會落在兩個座標系。
 *
 * 兩邊的預設勾選狀態不同，而且不是不一致：
 *   * 校正的 `phonetic` 是這個模組的**猜測**，猜測不該預先打勾；
 *   * 要刪的句子一定是「整句都是使用者自己清單裡的詞」，那是使用者自己
 *     的規則在動作，所以預設打勾。
 */

import { create } from "zustand";
import { ApiError, api } from "@/shared/api/client";
import { useGlossary } from "@/entities/glossary/model/store";
import type {
  CorrectionProposal,
  FillerReport,
  RefineOffer,
  RefineStage,
  RefineWritten,
} from "@/shared/api/types";

export type RefinePhase = "idle" | "offered" | "written";

/** 兩段都做，是預設。使用者要的通常是「這份稿子可以讀了」，而不是
 *  「只做其中一半」。 */
export const ALL_STAGES: RefineStage[] = ["correct", "tidy"];

export interface RefineState {
  phase: RefinePhase;
  busy: boolean;
  error: string | null;
  source: string | null;
  /** 勾了哪幾段。可以是空的——那時候什麼都不能按，而且畫面會說為什麼。 */
  stages: Set<RefineStage>;
  offer: RefineOffer | null;
  /** 索引進 `offer.proposals`。 */
  acceptedCorrections: Set<number>;
  /** 索引進 `offer.removals`。 */
  acceptedRemovals: Set<number>;
  written: RefineWritten | null;
  fillers: FillerReport | null;

  setStage: (stage: RefineStage, on: boolean) => Promise<void>;
  ask: (source: string) => Promise<void>;
  toggleCorrection: (index: number) => void;
  setAllCorrections: (on: boolean) => void;
  toggleRemoval: (index: number) => void;
  setAllRemovals: (on: boolean) => void;
  confirm: () => Promise<RefineWritten | null>;
  enrol: (term: string, alias?: string) => Promise<void>;
  loadFillers: () => Promise<void>;
  addCommon: () => Promise<void>;
  addFiller: (term: string) => Promise<void>;
  removeFiller: (term: string) => Promise<void>;
  /** 用現在的詞庫／清單重算一次。改了白名單卻還讓舊清單留在畫面上，
   *  是讀者判定「剛剛那個編輯沒有作用」的方式。 */
  reask: () => Promise<void>;
  discard: () => void;
}

function reason(cause: unknown): string {
  if (cause instanceof ApiError) return cause.message;
  if (cause instanceof Error) return cause.message;
  return String(cause);
}

/** 哪些校正預設打勾。登記過的寫法是使用者自己的更正回來了；讀音推測是
 *  推論，推論不預先打勾。 */
export function defaultCorrections(proposals: CorrectionProposal[]): Set<number> {
  const chosen = new Set<number>();
  proposals.forEach((proposal, index) => {
    if (proposal.tier === "exact") chosen.add(index);
  });
  return chosen;
}

function allOf(count: number): Set<number> {
  return new Set(Array.from({ length: count }, (_, index) => index));
}

export const useRefine = create<RefineState>((set, get) => ({
  phase: "idle",
  busy: false,
  error: null,
  source: null,
  stages: new Set<RefineStage>(ALL_STAGES),
  offer: null,
  acceptedCorrections: new Set<number>(),
  acceptedRemovals: new Set<number>(),
  written: null,
  fillers: null,

  setStage: async (stage, on) => {
    const next = new Set(get().stages);
    if (on) next.add(stage);
    else next.delete(stage);
    set({ stages: next });
    // 勾選改變了「要看什麼」，畫面上的清單立刻就過期了。空集合不重問：
    // 沒有任何一段可跑的時候，該顯示的是那句話而不是一份空清單。
    if (get().phase === "offered" && next.size > 0) await get().reask();
  },

  ask: async (source) => {
    const stages = [...get().stages];
    if (stages.length === 0) return;
    set({ busy: true, error: null, written: null, source });
    try {
      const offer = await api.proposeRefine(source, stages);
      set({
        offer,
        acceptedCorrections: defaultCorrections(offer.proposals),
        acceptedRemovals: allOf(offer.removals.length),
        phase: "offered",
      });
    } catch (cause) {
      set({ error: reason(cause), phase: "idle", offer: null });
    } finally {
      set({ busy: false });
    }
  },

  toggleCorrection: (index) => {
    const next = new Set(get().acceptedCorrections);
    if (next.has(index)) next.delete(index);
    else next.add(index);
    set({ acceptedCorrections: next });
  },

  setAllCorrections: (on) => {
    const offer = get().offer;
    if (!offer) return;
    set({ acceptedCorrections: on ? allOf(offer.proposals.length) : new Set<number>() });
  },

  toggleRemoval: (index) => {
    const next = new Set(get().acceptedRemovals);
    if (next.has(index)) next.delete(index);
    else next.add(index);
    set({ acceptedRemovals: next });
  },

  setAllRemovals: (on) => {
    const offer = get().offer;
    if (!offer) return;
    set({ acceptedRemovals: on ? allOf(offer.removals.length) : new Set<number>() });
  },

  confirm: async () => {
    const { source, offer, stages, acceptedCorrections, acceptedRemovals } = get();
    if (!source || !offer || stages.size === 0) return null;
    const sorted = (chosen: Set<number>) => [...chosen].sort((a, b) => a - b);
    set({ busy: true, error: null });
    try {
      const written = await api.applyRefine(
        source,
        [...stages],
        stages.has("correct") ? sorted(acceptedCorrections) : [],
        stages.has("tidy") ? sorted(acceptedRemovals) : [],
      );
      set({ written, phase: "written" });
      return written;
    } catch (cause) {
      set({ error: reason(cause) });
      return null;
    } finally {
      set({ busy: false });
    }
  },

  enrol: async (term, alias = "") => {
    if (!term.trim()) return;
    set({ busy: true, error: null });
    try {
      // 經過 glossary entity，清單歸它管。這裡曾經自己留一份副本，而那份
      // 副本在同一個清單也能從 設定 編輯的那一刻就過期了。
      const ok = await useGlossary.getState().enrol(term, alias);
      if (!ok) {
        set({ error: useGlossary.getState().error });
        return;
      }
      await get().reask();
    } finally {
      set({ busy: false });
    }
  },

  loadFillers: async () => {
    try {
      set({ fillers: await api.fillers() });
    } catch (cause) {
      set({ error: reason(cause) });
    }
  },

  addCommon: async () => {
    set({ busy: true, error: null });
    try {
      const fillers = await api.addFillers([], true);
      set({ fillers, busy: false });
      await get().reask();
    } catch (cause) {
      set({ busy: false, error: reason(cause) });
    }
  },

  addFiller: async (term: string) => {
    const cleaned = term.trim();
    if (!cleaned) return;
    set({ busy: true, error: null });
    try {
      const fillers = await api.addFillers([cleaned]);
      set({ fillers, busy: false });
      await get().reask();
    } catch (cause) {
      set({ busy: false, error: reason(cause) });
    }
  },

  removeFiller: async (term: string) => {
    set({ busy: true, error: null });
    try {
      const fillers = await api.removeFillers([term]);
      set({ fillers, busy: false });
      await get().reask();
    } catch (cause) {
      set({ busy: false, error: reason(cause) });
    }
  },

  reask: async () => {
    const { source, stages, phase } = get();
    if (!source || phase === "idle" || stages.size === 0) return;
    set({ busy: true, error: null });
    try {
      const offer = await api.proposeRefine(source, [...stages]);
      set({
        offer,
        acceptedCorrections: defaultCorrections(offer.proposals),
        acceptedRemovals: allOf(offer.removals.length),
        phase: "offered",
      });
    } catch (cause) {
      set({ error: reason(cause) });
    } finally {
      set({ busy: false });
    }
  },

  discard: () =>
    set({
      phase: "idle",
      offer: null,
      acceptedCorrections: new Set<number>(),
      acceptedRemovals: new Set<number>(),
      written: null,
      error: null,
    }),
}));

/**
 * 一項校正建議會**產生**的那句話，切好給畫面標記。
 *
 * 使用者的裁決（2026-08-28）：顯示改好之後的句子並標出改動處，不要
 * before/after 兩行。兩句幾乎一樣的中文並排，正好在最需要看清楚的時候
 * 最看不清楚。
 *
 * 只套用這一項。同一句裡有兩個建議就有兩列，每一列顯示自己那個勾會做的
 * 事；把另一個也算進去，等於要讀者核准一個沒有任何單一勾選會產生的句子。
 *
 * 對不上時回傳 `null`：伺服器每次都會重算，真的走到這裡代表偏移量已經
 * 不準，硬標下去會標到錯的字。
 */
export function highlightOf(
  offer: RefineOffer | null,
  index: number,
): { head: string; mark: string; tail: string } | null {
  const proposal = offer?.proposals[index];
  if (!proposal) return null;
  const text = offer?.cues[proposal.cue]?.text;
  if (typeof text !== "string") return null;
  if (text.slice(proposal.start, proposal.end) !== proposal.was) return null;
  return {
    head: text.slice(0, proposal.start),
    mark: proposal.now,
    tail: text.slice(proposal.end),
  };
}

/** 校正那一段的一句話摘要，沒話說時回 null。
 *
 *  純函式。有兩件事是單純數數字說不出來的：有幾項是**猜的**，以及讀音
 *  比對這次到底能不能用——「沒有建議」在讀音比對關掉的時候意思不一樣，
 *  而沒被告知的讀者會以為稿子是乾淨的。 */
export function describeCorrections(offer: RefineOffer | null): string | null {
  if (!offer || !offer.stages.includes("correct")) return null;
  if (offer.glossaryEntries === 0) {
    return "詞庫是空的，所以不會有任何建議。先把常錯的術語加進去。";
  }
  if (offer.proposals.length === 0) {
    return offer.phoneticKeys
      ? "沒有找到需要校正的術語。"
      : "沒有找到登記過的錯字（讀音比對目前無法使用，只比對了完全相同的寫法）。";
  }
  const exact = offer.proposals.filter((p) => p.tier === "exact").length;
  const guessed = offer.proposals.length - exact;
  if (guessed === 0) return `${exact} 處是你登記過的寫法。`;
  if (exact === 0) return `${guessed} 處讀音相同，是推測的，請逐項確認。`;
  return `${exact} 處是登記過的寫法，${guessed} 處是讀音推測的，請逐項確認。`;
}

/** 整理那一段的一句話摘要，沒話說時回 null。
 *
 *  清單是空的自成一句，不是「0 句」：「沒有東西可刪」跟「你還沒告訴我
 *  要刪什麼」是兩種狀態，畫成一樣會讓還沒設定看起來像壞掉。 */
export function describeRemovals(offer: RefineOffer | null): string | null {
  if (!offer || !offer.stages.includes("tidy")) return null;
  if (offer.fillerTerms === 0) {
    return "語助詞清單是空的，所以不會刪掉任何東西。先加幾個常見的。";
  }
  if (offer.removals.length === 0) {
    return "這份逐字稿裡沒有整句都是語助詞的句子。";
  }
  const { cues, removed, kept } = offer.summary;
  return `${cues} 句裡有 ${removed} 句整句都是語助詞，整理後剩 ${kept} 句。`;
}

/** 一項校正建議在清單裡怎麼讀。信心值只有在真的量到時才顯示——
 *  沒量到不可以印成 0%。 */
export function describeProposal(proposal: CorrectionProposal): string {
  const how = proposal.tier === "exact" ? "登記過的寫法" : "讀音相同";
  const sure =
    proposal.confidence === null || proposal.confidence === undefined
      ? ""
      : `，引擎信心 ${Math.round(proposal.confidence * 100)}%`;
  return `${how}${sure}`;
}

/** 一句要刪的句子在清單裡怎麼讀。1 起算，跟這個產品其他地方一樣。 */
export function describeRemoval(removal: { cue: number; text: string }): string {
  return `第 ${removal.cue + 1} 句：${removal.text}`;
}

/** 按鈕上的那句話。兩段都勾、只勾一段，說的事情不一樣。 */
export function describeAction(stages: Set<RefineStage>): string {
  if (stages.has("correct") && stages.has("tidy")) return "校正並整理…";
  if (stages.has("correct")) return "智慧校正…";
  if (stages.has("tidy")) return "整理版（去掉語助詞）…";
  return "請至少選一項";
}
