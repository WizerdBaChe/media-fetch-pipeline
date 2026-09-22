/**
 * Wires the SSE stream into the stores, once, at the app root.
 *
 * A task event clears any budget-wait countdown for that task: the countdown
 * is only meaningful while the task is still waiting, and a stale "⏳ 等待配額"
 * on a row that has moved on is worse than showing nothing.
 */

import { useEffect } from "react";
import { subscribeEvents } from "@/shared/api/events";
import { useConfigStore } from "@/entities/config/model/store";
import { useSessionStore } from "@/entities/session/model/store";
import { useStackStore } from "@/entities/stack-job/model/store";
import { useTaskStore } from "@/entities/task/model/store";
import { useTools } from "@/features/setup-tools/model/store";
import { subscribe as subscribeTabs } from "@/shared/lib/tabSync";

export function useLiveUpdates(): void {
  useEffect(() => {
    const taskStore = useTaskStore.getState();
    const sessionStore = useSessionStore.getState();

    void sessionStore.probeHealth();
    void useConfigStore.getState().load();
    void taskStore.load();
    // Jobs outlive the workspace being open: one started before a
    // reload is still running, and its row has to come back with it.
    void useStackStore.getState().load();

    // Dismissing a banner is an acknowledgement by a person, and there is
    // one person behind however many tabs are open (UAT item 5-5). It
    // travels tab to tab rather than through the server: it is not the
    // server's fact to hold.
    const stopTabSync = subscribeTabs((message) => {
      if (message.type === "notice-dismissed") {
        useSessionStore.getState().applyRemoteDismiss(message.key);
      }
    });

    const stopEvents = subscribeEvents({
      onTask: (task) => {
        useTaskStore.getState().upsert(task);
        useSessionStore.getState().setBudgetWait(task.id, null);
      },
      // Without this a second tab keeps rows the server no longer has: the
      // removal used to travel as a prose `notice`, which says something
      // happened without saying what to do about it (R5-15).
      onRemoved: ({ ids }) => useTaskStore.getState().dropLocal(ids),
      onQueueStats: (stats) => useSessionStore.getState().setQueueStats(stats),
      onNotice: (notice) => useSessionStore.getState().pushNotice(notice),
      onBudgetWait: (wait) =>
        useSessionStore.getState().pushNotice({
          level: "info",
          code: "budget_wait",
          message: `${wait.platform} 配額用盡，約 ${Math.ceil(wait.resumesInMs / 1000)} 秒後繼續（${wait.reason}）`,
        }),
      onStackJob: (job) => useStackStore.getState().upsert(job),
      // Fetching ffmpeg is ~106 MB, and the banner that started it is the
      // only thing on screen saying anything is happening.
      onToolInstall: (progress) => useTools.getState().applyProgress(progress),
      onStatus: (status) => useSessionStore.getState().setStreamStatus(status),
    });

    return () => {
      stopTabSync();
      stopEvents();
    };
  }, []);
}
