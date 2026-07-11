import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  js.configs.recommended,
  ...tseslint.configs.recommended,
  { ignores: ["node_modules", "coverage", ".runtime-test", "src/generated"] },
  { languageOptions: { globals: { crypto: "readonly", Request: "readonly", Response: "readonly", URL: "readonly", TextEncoder: "readonly", TextDecoder: "readonly", console: "readonly", process: "readonly" } } },
);
