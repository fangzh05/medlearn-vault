import { readFileSync, writeFileSync } from "node:fs";
import Ajv from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import standaloneCode from "ajv/dist/standalone/index.js";

function generate(source, target) {
  const schema = JSON.parse(readFileSync(new URL(source, import.meta.url), "utf8"));
  const ajv = new Ajv({ strict: false, allErrors: true, code: { source: true, esm: true } });
  addFormats(ajv, { keywords: true });
  const validate = ajv.compile(schema);
  writeFileSync(new URL(target, import.meta.url), `// Generated from ${source}.\n${standaloneCode(ajv, validate)}`);
}

generate("../../schemas/workflow/current/intake_envelope.schema.json", "../src/generated/intake-validator.js");
generate("../../schemas/workflow/current/medlearn_handoff.schema.json", "../src/generated/handoff-validator.js");
