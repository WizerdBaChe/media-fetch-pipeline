import { POLICY_OPTIONS, globalPolicyOf, useConfigStore } from "@/entities/config/model/store";

/**
 * The toolbar's global quality policy (§7.3). Rows with `policy: null` follow
 * it live; rows that were overridden are pinned and do not move (§7.4).
 */
export function GlobalPolicySelect() {
  const config = useConfigStore((state) => state.config);
  const setGlobalPolicy = useConfigStore((state) => state.setGlobalPolicy);

  return (
    <label className="mfp-field">
      畫質
      <select
        className="mfp-select"
        value={globalPolicyOf(config)}
        aria-label="全域畫質政策"
        disabled={config === null}
        onChange={(event) => void setGlobalPolicy(event.target.value)}
      >
        {POLICY_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}
