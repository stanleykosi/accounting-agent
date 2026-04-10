const path = require("node:path");

/*
Purpose: Configure the canonical Next.js desktop UI build for standalone sidecar packaging.
Scope: Standalone output, React strictness, and workspace package transpilation for the shared UI package.
Dependencies: Next.js, the pnpm workspace symlink graph, and packages/ui source exports.
*/

/** @type {import("next").NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  turbopack: {
    root: path.join(__dirname, "..", ".."),
  },
  transpilePackages: ["@accounting-ai-agent/ui"],
};

module.exports = nextConfig;
