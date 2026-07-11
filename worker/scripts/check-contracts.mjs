import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import Ajv from "ajv/dist/2020.js";
import addFormats from "ajv-formats";

const read = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), "utf8"));
const ajv = new Ajv({ strict: false, allErrors: true });
addFormats(ajv);
const envelopeSchema = read("../../schemas/workflow/current/intake_envelope.schema.json");
const jobSchema = read("../contracts/job-record.schema.json");
const fixture = read("../../examples/intake/manual-copd.json");
const generatedUrl = new URL("../src/generated/intake-validator.js", import.meta.url);
const before = readFileSync(generatedUrl, "utf8");

if (!ajv.compile(envelopeSchema)(fixture)) throw new Error("shared intake fixture/schema mismatch");
ajv.compile(jobSchema);
execFileSync(process.execPath, [fileURLToPath(new URL("generate-intake-validator.mjs", import.meta.url))]);
if (readFileSync(generatedUrl, "utf8") !== before) throw new Error("generated intake validator drift");
process.stdout.write("contracts: ok (IntakeEnvelope 0.1.0, JobRecord 0.2.0)\n");
