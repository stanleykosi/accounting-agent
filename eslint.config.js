/*
Purpose: Define the canonical flat ESLint configuration for the TypeScript workspace.
Scope: Type-aware linting for shared packages, the Next.js desktop UI, and repository-level JavaScript config files.
Dependencies: ESLint, typescript-eslint, the Next.js ESLint plugin, and workspace tsconfig files.
*/

const js = require("@eslint/js");
const nextPlugin = require("@next/eslint-plugin-next");
const eslintConfigPrettier = require("eslint-config-prettier");
const reactHooks = require("eslint-plugin-react-hooks");
const globals = require("globals");
const tseslint = require("typescript-eslint");

const typeCheckedConfigs = tseslint.configs.recommendedTypeChecked.map((config) => ({
  ...config,
  files: ["**/*.{ts,tsx}"],
  languageOptions: {
    ...config.languageOptions,
    parserOptions: {
      ...config.languageOptions?.parserOptions,
      projectService: true,
      tsconfigRootDir: __dirname,
    },
    globals: {
      ...globals.browser,
      ...globals.node,
    },
  },
}));

module.exports = [
  {
    ignores: [
      "**/.next/**",
      "**/coverage/**",
      "**/dist/**",
      "**/node_modules/**",
      "**/out/**",
      "**/*.d.ts",
    ],
  },
  {
    ...js.configs.recommended,
    files: ["**/*.{js,cjs,mjs}"],
    languageOptions: {
      ...js.configs.recommended.languageOptions,
      ecmaVersion: "latest",
      sourceType: "commonjs",
      globals: {
        ...globals.node,
      },
    },
  },
  ...typeCheckedConfigs,
  {
    files: ["**/*.{ts,tsx}"],
    rules: {
      "@typescript-eslint/consistent-type-imports": [
        "error",
        {
          fixStyle: "separate-type-imports",
          prefer: "type-imports",
        },
      ],
      "@typescript-eslint/no-floating-promises": "error",
    },
  },
  {
    files: ["apps/desktop-ui/**/*.{ts,tsx}"],
    plugins: {
      "@next/next": nextPlugin,
      "react-hooks": reactHooks,
    },
    rules: {
      ...nextPlugin.configs["core-web-vitals"].rules,
      ...reactHooks.configs.recommended.rules,
    },
  },
  eslintConfigPrettier,
];
