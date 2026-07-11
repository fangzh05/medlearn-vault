import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  js.configs.recommended,
  ...tseslint.configs.recommended,
  { ignores: ["node_modules", "coverage"] },
  { languageOptions: { globals: { crypto: "readonly", Request: "readonly", Response: "readonly", URL: "readonly" } } },
);
