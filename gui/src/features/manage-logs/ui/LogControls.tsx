/**
 * 紀錄: how long the routine half is kept, and two buttons that delete each
 * half on its own.
 *
 * The split is the whole design (user ruling, 2026-08-23). Ordinary action
 * lines age out on a timer so the folder cannot grow without bound; error
 * bundles are kept until somebody says otherwise, because the failure a
 * bundle describes is usually noticed long after the day it happened. One
 * button that deleted both would make the second rule unenforceable.
 *
 * Both buttons say what they are about to delete, and confirm before doing
 * it. A delete button that reports nothing is a delete button nobody
 * trusts -- the same reason the sweep returns what it removed.
 */

import { useEffect, useState } from "react";
import { ApiError, api } from "@/shared/api/client";
import { useConfigStore } from "@/entities/config/model/store";
import { revealLogFolder } from "@/shared/lib/desktop";
import { Button } from "@/shared/ui/Button";
import type { LogsReport } from "@/shared/api/types";

const RETENTION_OPTIONS = [
  { value: 0, label: "不自動清除" },
  { value: 1, label: "1 天" },
  { value: 3, label: "3 天" },
  { value: 7, label: "7 天" },
  { value: 30, label: "30 天" },
] as const;

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function LogControls() {
  const config = useConfigStore((state) => state.config);
  const patch = useConfigStore((state) => state.patch);
  const [report, setReport] = useState<LogsReport | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  const refresh = () =>
    api
      .readLogs(1)
      .then(setReport)
      .catch((error) =>
        setFlash(error instanceof ApiError ? error.message : String(error)),
      );

  useEffect(() => {
    void refresh();
    // Read once when the panel opens: these numbers change when a job runs,
    // and a panel that polls would be asking a question nobody is looking at.
    //
    // No suppression here: `refresh` closes over nothing reactive -- the two
    // setters are stable and `api` is a module -- so exhaustive-deps has no
    // dependency to ask for. The directive this line used to carry named a
    // rule that was never installed, which is a different thing from a rule
    // that was considered and overruled.
  }, []);

  const clearLogs = async () => {
    if (!window.confirm(`要刪除一般紀錄嗎？共 ${report?.files ?? 0} 份。`)) return;
    try {
      const swept = await api.clearLogs();
      setFlash(`已刪除 ${swept.files} 份一般紀錄（${formatBytes(swept.bytesFreed)}）`);
      await refresh();
    } catch (error) {
      setFlash(error instanceof ApiError ? error.message : String(error));
    }
  };

  const clearErrors = async () => {
    if (
      !window.confirm(
        `要刪除錯誤資料嗎？共 ${report?.bundles ?? 0} 份，刪除後無法還原。`,
      )
    ) {
      return;
    }
    try {
      const swept = await api.clearErrorBundles();
      setFlash(`已刪除 ${swept.bundles} 份錯誤資料（${formatBytes(swept.bytesFreed)}）`);
      await refresh();
    } catch (error) {
      setFlash(error instanceof ApiError ? error.message : String(error));
    }
  };

  const reveal = async (kind: "actions" | "errors") => {
    const error = await revealLogFolder(kind);
    if (error) setFlash(error);
  };

  return (
    <section className="mfp-settings__section" data-testid="log-controls">
      <h3>紀錄</h3>

      <label className="mfp-settings__row">
        <span>一般紀錄保留</span>
        <select
          value={config?.logRetentionDays ?? 3}
          onChange={(event) =>
            void patch({ logRetentionDays: Number(event.target.value) })
          }
        >
          {RETENTION_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <small>每個動作都會記一行。錯誤資料不受這個設定影響，會一直留著</small>
      </label>

      <div className="mfp-settings__row">
        <span>一般紀錄</span>
        <strong data-testid="log-usage">
          {report ? `${report.files} 份・${formatBytes(report.fileBytes)}` : "讀取中…"}
        </strong>
        <div className="mfp-settings__buttons">
          <Button onClick={() => void reveal("actions")}>開啟資料夾</Button>
          <Button variant="danger" onClick={() => void clearLogs()}>
            刪除一般紀錄
          </Button>
        </div>
      </div>

      <div className="mfp-settings__row">
        <span>錯誤資料</span>
        <strong data-testid="error-usage">
          {report
            ? `${report.bundles} 份・${formatBytes(report.bundleBytes)}`
            : "讀取中…"}
        </strong>
        <div className="mfp-settings__buttons">
          <Button onClick={() => void reveal("errors")}>開啟資料夾</Button>
          <Button variant="danger" onClick={() => void clearErrors()}>
            刪除錯誤資料
          </Button>
        </div>
      </div>

      {flash && (
        <p className="mfp-settings__flash" role="status">
          {flash}
        </p>
      )}
    </section>
  );
}
