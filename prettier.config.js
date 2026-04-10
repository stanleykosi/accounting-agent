/*
Purpose: Define canonical formatting rules for the TypeScript workspace and shared frontend assets.
Scope: Prettier formatting for JavaScript, TypeScript, JSON, CSS, YAML, and Markdown files handled by the Node toolchain.
Dependencies: The root package.json formatting scripts and Prettier itself.
*/

module.exports = {
  printWidth: 100,
  semi: true,
  singleQuote: false,
  trailingComma: "all",
};
