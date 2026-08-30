/**
 * Global configuration (PSM Batch 2 §9). The global quality policy lives here
 * because it is a persisted config field, not view state — a row with
 * `policy: null` follows it live, so it has to survive a reload.
 */

import { create } from "zustand";
import { ApiError, api } from "@/shared/api/client";
import type { AppConfig } from "@/shared/api/types";

export interface ConfigStoreState {
  config: AppConfig | null;
  lastError: string | null;
  load: () => Promise<void>;
  setGlobalPolicy: (policy: string) => Promise<void>;
  /** Write one or more §9 fields. The server is the only place a config
   *  exists, so the store never keeps an optimistic copy: what comes back
   *  from the PUT is what is true. */
  patch: (fields: Partial<AppConfig>) => Promise<void>;
}

export const POLICY_OPTIONS = [
  { value: "best", label: "最佳" },
  { value: "max-height:1080", label: "最高 1080p" },
  { value: "max-height:720", label: "最高 720p" },
  { value: "smallest", label: "最小" },
] as const;

/** Falls back to `best`, matching the backend default (§9). */
export function globalPolicyOf(config: AppConfig | null): string {
  return config?.policy ?? "best";
}

export const useConfigStore = create<ConfigStoreState>()((set, get) => ({
  config: null,
  lastError: null,

  load: async () => {
    try {
      set({ config: await api.getConfig(), lastError: null });
    } catch (error) {
      set({ lastError: error instanceof ApiError ? error.message : String(error) });
    }
  },

  patch: async (fields) => {
    const current = get().config;
    if (!current) return;
    try {
      set({ config: await api.putConfig({ ...current, ...fields }), lastError: null });
    } catch (error) {
      set({ lastError: error instanceof ApiError ? error.message : String(error) });
    }
  },

  setGlobalPolicy: async (policy) => {
    const current = get().config;
    if (!current) return;
    try {
      set({ config: await api.putConfig({ ...current, policy }), lastError: null });
    } catch (error) {
      set({ lastError: error instanceof ApiError ? error.message : String(error) });
    }
  },
}));
