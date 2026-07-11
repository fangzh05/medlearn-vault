import { readFileSync, writeFileSync } from "node:fs";
import Ajv from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import standaloneCode from "ajv/dist/standalone/index.js";

const schema = JSON.parse(readFileSync(
  new URL("../../schemas/workflow/current/intake_envelope.schema.json", import.meta.url), "utf8",
));
const ajv = new Ajv({ strict: false, allErrors: true, code: { source: true, esm: true } });
addFormats(ajv, { keywords: true });
const validate = ajv.compile(schema);
const output = `// Generated from schemas/workflow/current/intake_envelope.schema.json.\n${standaloneCode(ajv, validate)}`;
writeFileSync(new URL("../src/generated/intake-validator.js", import.meta.url), output);
