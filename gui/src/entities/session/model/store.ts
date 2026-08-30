/**
 * Live session state: what the backend can do, whether the event stream is
 * alive, and the transient signals it pushes (notices, budget waits, stats).
 *
 * Not in the §3 layout listing, which named `entities/task` and
 * `entities/config` illustratively. Folding this into `config` would have been
 * wrong: none of it is configuration and none of it persists. Reversible
 * implementation-layer call, logged here.
 */

import { create } from "zustand";
import { ApiError, api } from "@/shared/api/client";
import type { Capabilities, Notice, QueueStats } from "@/shared/api/types";
import type { StreamStatus } from "@/shared/api/events";
import { publish } from "@/shared/lib/tabSync";

export interface SessionStoreState {
  capabilities: Capabilities;
  /** null until /v1/health answers; distinguishes "unknown" from "unavailable". */
  reachable: boolean | null;
  streamStatus: StreamStatus;
  notices: Notice[];
  queueStats: QueueStats | null;
  /** taskId → ms remaining. Cleared when the task moves on. */
  budgetWaits: Record<string, number>;

  probeHealth: () => Promise<void>;
  setStreamStatus: (status: StreamStatus) => void;
  pushNotice: (notice: Notice) => void;
  /** By identity, not position: a second tab numbers its list its own way. */
  dismissNotice: (key: string) => void;
  /** Same removal, without echoing it back out again. */
  applyRemoteDismiss: (key: string) => void;
  setQueueStats: (stats: QueueStats) => void;
  setBudgetWait: (taskId: string, ms: number | null) => void;
}

const MAX_NOTICES = 5;

/**
 * A notice's identity, derived from its content.
 *
 * It cannot be a counter or a generated id: two tabs receive the same
 * server event independently and must arrive at the same key, or a
 * dismissal in one names nothing in the other. Content is the only thing
 * they are guaranteed to agree on.
 *
 * The separator is printable on purpose. The first version used a NUL,
 * which put a literal 0x00 in this file and made it binary to grep, to
 * diff and to every review tool -- a separator no code contains is all
 * this needs, and `code` is a snake_case identifier.
 */
export function noticeKey(notice: Notice): string {
  return `${notice.code}::${notice.message}`;
}

export const useSessionStore = create<SessionStoreState>()((set, get) => ({
  capabilities: { probe: false, download: false },
  reachable: null,
  streamStatus: "connecting",
  notices: [],
  queueStats: null,
  budgetWaits: {},

  probeHealth: async () => {
    try {
      const health = await api.health();
      set({ capabilities: health.capabilities, reachable: true });
      if (health.queueRebuilt) {
        // Saying what happened is not enough: the user is looking at an empty
        // queue and needs to know whether their list is recoverable and what
        // to do next (R5-12). The order matters -- reassurance, then the
        // repair, then the way out if repair is not worth it.
        get().pushNotice({
          level: "warn",
          code: "queue_rebuilt",
          message:
            "佇列檔無法讀取，已以空佇列啟動。已下載完成的檔案不受影響。" +
            "原檔完整保留在 %APPDATA%\\media-fetch-pipeline\\queue.json.corrupt。" +
            "要救回：先關掉服務，用文字編輯器修好那份檔案的 JSON（多半是少了括號或被截斷），" +
            "改名回 queue.json 再重開；不想救就直接把 .corrupt 檔刪掉，重新貼網址即可。",
        });
      }
    } catch (error) {
      const notice: Notice = {
        level: "error",
        code: error instanceof ApiError ? error.errorCode : "server_unreachable",
        message: "無法連線到本機服務，請確認 `mfp serve` 正在執行",
      };
      // Through `pushNotice`, not around it: this used to splice the list
      // itself, which meant the one place that could produce the same
      // notice on every retry was also the one place the deduplication did
      // not reach.
      set({ reachable: false, capabilities: { probe: false, download: false } });
      get().pushNotice(notice);
    }
  },

  setStreamStatus: (status) => set({ streamStatus: status }),

  pushNotice: (notice) => {
    // Deduplicated by identity. The same sentence twice tells the user
    // nothing the first one did not, and it HAPPENS: `useLiveUpdates` runs
    // its effect inside `<StrictMode>`, which deliberately mounts twice in a
    // development build, so `probeHealth` pushed `queue_rebuilt` twice and
    // two identical banners appeared (UAT §5-19). Guarding at the store
    // rather than at that one caller means every future pusher is covered.
    const key = noticeKey(notice);
    const notices = get().notices;
    if (notices.some((existing) => noticeKey(existing) === key)) return;
    set({ notices: [notice, ...notices].slice(0, MAX_NOTICES) });
  },

  dismissNotice: (key) => {
    get().applyRemoteDismiss(key);
    publish({ type: "notice-dismissed", key });
  },

  applyRemoteDismiss: (key) =>
    set({ notices: get().notices.filter((notice) => noticeKey(notice) !== key) }),

  setQueueStats: (stats) => set({ queueStats: stats }),

  setBudgetWait: (taskId, ms) => {
    const next = { ...get().budgetWaits };
    if (ms === null) delete next[taskId];
    else next[taskId] = ms;
    set({ budgetWaits: next });
  },
}));
