import js from "@eslint/js";
import tseslint from "typescript-eslint";
import boundaries from "eslint-plugin-boundaries";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";

// PSM Batch 2 §3.1 -- Feature-Sliced Design v2.1 layering, mechanically
// enforced. The rule that matters is not "layers exist" but:
//
//     slices within the same layer must never import each other.
//
// That is the one constraint a linter can check, and checking it is what
// turns "decoupled" from a slogan into a fact. Shared need moves DOWN a
// layer (usually into entities/task or shared/), never sideways.
//
// `shared` is deliberately captured without a slice: FSD gives it segments,
// not slices, so shared/ui importing shared/lib is correct, not a violation.

const LAYERS = ["app", "pages", "widgets", "features", "entities", "shared"];

/** Everything strictly below `layer`, which is what that layer may import. */
const below = (layer) => LAYERS.slice(LAYERS.indexOf(layer) + 1);

/** A layer may also import its OWN slice -- files inside one slice are one unit. */
const ownSliceThenBelow = (layer) => [
  [layer, { slice: "${from.slice}" }],
  ...below(layer),
];

export default tseslint.config(
  { ignores: ["dist", "node_modules", "coverage"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: { ...globals.browser },
    },
    rules: {
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      "@typescript-eslint/consistent-type-imports": "error",
    },
  },
  {
    // The two classic hook rules, and only those.
    //
    // These were being SUPPRESSED before they were ever installed: two files
    // carried `eslint-disable-next-line react-hooks/exhaustive-deps` while
    // the plugin was absent from package.json, so ESLint reported "rule not
    // found" and the check had never run a single time. Installing the
    // plugin is what turns those comments from decoration into a decision.
    //
    // `error`, not the plugin's own default of `warn`: `npm run lint` is a
    // plain `eslint .`, which exits 0 on warnings. A warning here would be
    // the same inert config the boundaries resolver already taught this
    // project to distrust -- present, green, and checking nothing.
    //
    // The REST of v7's `recommended-latest` -- the React Compiler rules
    // (`set-state-in-effect`, `purity`, `immutability`, ...) -- is
    // deliberately left off. Re-measured on 716f789 (2026-08-23), it reports
    // 6 `set-state-in-effect` errors:
    //
    //     features/pick-roi/ui/RoiPicker.tsx:73
    //     shared/lib/useNarrow.ts:28
    //     widgets/settings-panel/ui/SettingsPanel.tsx:72
    //     widgets/stack-workspace/ui/StackWorkspace.tsx:106, :120, :150
    //
    // Every one is a "derive state from a prop" or "load on mount" shape,
    // which is an effect to redesign rather than lint to fix; turning the
    // rules on without that work would only buy a wall of new suppressions.
    // Four of the six sit in the 引用長圖/逐字稿 surface that is awaiting an
    // acceptance run, and rewriting effects underneath unaccepted work is
    // how accepted behaviour gets silently overwritten. Revisit as one
    // deliberate piece of work after that run (ruling: aiwork-15 session).
    //
    // The line numbers move under active feature work -- they already have
    // once. Re-measure before trusting them; do not edit them by hand.
    files: ["src/**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "error",
    },
  },
  {
    files: ["src/**/*.{ts,tsx}"],
    plugins: { boundaries },
    settings: {
      // Without a resolver the plugin cannot follow "@/..." and silently
      // skips every aliased import -- the rule would pass vacuously. Verified
      // by planting a deliberate cross-slice import and watching it fail.
      "import/resolver": {
        typescript: { alwaysTryTypes: true, project: "./tsconfig.json" },
      },
      "boundaries/include": ["src/**/*.{ts,tsx}"],
      "boundaries/elements": [
        { type: "app", pattern: "src/app/**", mode: "full" },
        { type: "pages", pattern: "src/pages/*/**", mode: "full", capture: ["slice"] },
        { type: "widgets", pattern: "src/widgets/*/**", mode: "full", capture: ["slice"] },
        { type: "features", pattern: "src/features/*/**", mode: "full", capture: ["slice"] },
        { type: "entities", pattern: "src/entities/*/**", mode: "full", capture: ["slice"] },
        { type: "shared", pattern: "src/shared/**", mode: "full" },
      ],
    },
    rules: {
      // Default disallow: a new layer added later is denied until someone
      // states what it may reach, rather than silently inheriting access.
      "boundaries/element-types": [
        "error",
        {
          default: "disallow",
          rules: [
            // `app` is one slice, so intra-app imports are not lateral.
            { from: "app", allow: ["app", ...below("app")] },
            { from: "pages", allow: ownSliceThenBelow("pages") },
            { from: "widgets", allow: ownSliceThenBelow("widgets") },
            { from: "features", allow: ownSliceThenBelow("features") },
            { from: "entities", allow: ownSliceThenBelow("entities") },
            { from: "shared", allow: ["shared"] },
          ],
        },
      ],
      "boundaries/no-unknown-files": "error",
    },
  },
  {
    // Tests may reach across layers to build fixtures; the production graph
    // is what the rule protects.
    files: ["src/**/*.test.{ts,tsx}", "src/shared/lib/test-setup.ts"],
    rules: {
      "boundaries/element-types": "off",
      // `vi.importActual<typeof import("...")>` has no type-only form.
      "@typescript-eslint/consistent-type-imports": "off",
    },
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
  },
);
