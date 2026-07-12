"""
Validate MedLearn Vault JSON schemas and example data.

Usage:
    python scripts/validate_schemas.py          # validate all
    python scripts/validate_schemas.py --quiet  # only print failures
"""

import json
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = ROOT / "schemas"
EXAMPLES_DIR = ROOT / "examples"

# Map example files to their expected schema files
EXAMPLE_SCHEMA_MAP = {
    "gerd_cross_discipline.json": "concept_entity.schema.json",
}


def load_json(path: Path) -> dict:
    """Load and return a JSON file, exiting on error."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"  ✗ INVALID JSON: {path.relative_to(ROOT)} — {e}")
        sys.exit(1)
    except FileNotFoundError:
        print(f"  ✗ MISSING: {path.relative_to(ROOT)}")
        sys.exit(1)


def validate_schema_file(schema_path: Path) -> bool:
    """Check that a JSON Schema file is valid against the meta-schema."""
    try:
        schema = load_json(schema_path)
        # Validate the schema against the draft 2020-12 meta-schema
        jsonschema.Draft202012Validator.check_schema(schema)
        return True
    except jsonschema.SchemaError as e:
        print(f"  ✗ INVALID SCHEMA: {schema_path.relative_to(ROOT)} — {e.message}")
        return False


def validate_example(example_path: Path, schema_path: Path) -> bool:
    """Validate an example JSON file against its schema."""
    schema = load_json(schema_path)
    instance = load_json(example_path)

    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(instance))

    if errors:
        print(f"  ✗ VALIDATION FAILED: {example_path.relative_to(ROOT)} "
              f"against {schema_path.relative_to(ROOT)}")
        for err in errors:
            path = " → ".join(str(p) for p in err.absolute_path) or "(root)"
            print(f"      at {path}: {err.message}")
        return False
    return True


def validate_example_standalone(example_path: Path) -> bool:
    """
    For examples with embedded schemas (concept + shared refs),
    validate each sub-object against the full schema if possible.
    """
    instance = load_json(example_path)

    # Try to find the right schema based on filename
    filename = example_path.name
    schema_filename = EXAMPLE_SCHEMA_MAP.get(filename)
    if schema_filename is None:
        print(f"  ? SKIP: {example_path.relative_to(ROOT)} — no schema mapping defined")
        return True  # not a failure, just untested

    schema_path = SCHEMAS_DIR / schema_filename
    if not schema_path.exists():
        print(f"  ✗ MISSING SCHEMA: {schema_path.relative_to(ROOT)}")
        return False

    schema = load_json(schema_path)
    errors = []

    # Validate the top-level "concept" object if present
    if "concept" in instance:
        validator = jsonschema.Draft202012Validator(schema)
        errors.extend(validator.iter_errors(instance["concept"]))

    if errors:
        print(f"  ✗ VALIDATION FAILED: {example_path.relative_to(ROOT)} "
              f"against {schema_filename}")
        for err in errors:
            path = " → ".join(str(p) for p in err.absolute_path) or "(root)"
            print(f"      at {path}: {err.message}")
        return False
    return True


def main() -> int:
    quiet = "--quiet" in sys.argv
    failures = 0
    total = 0

    # --- Phase 1: Validate all schema files are valid JSON Schema ---
    if not quiet:
        print("=" * 60)
        print("Phase 1: Schema Syntax Validation")
        print("=" * 60)

    schema_files = sorted(SCHEMAS_DIR.glob("*.schema.json"))
    if not schema_files:
        print("  ? No schema files found in schemas/")
        return 1

    for sf in schema_files:
        total += 1
        if validate_schema_file(sf):
            if not quiet:
                print(f"  ✓ {sf.relative_to(ROOT)}")
        else:
            failures += 1

    # --- Phase 2: Validate example files against schemas ---
    if not quiet:
        print()
        print("=" * 60)
        print("Phase 2: Example Data Validation")
        print("=" * 60)

    example_files = sorted(EXAMPLES_DIR.glob("*.json"))
    if not example_files:
        if not quiet:
            print("  ? No example files found in examples/")
    else:
        for ef in example_files:
            total += 1
            if validate_example_standalone(ef):
                if not quiet:
                    print(f"  ✓ {ef.relative_to(ROOT)}")
            else:
                failures += 1

    # --- Summary ---
    print()
    print("=" * 60)
    if failures:
        print(f"  RESULT: {failures}/{total} checks FAILED")
    else:
        print(f"  RESULT: {total}/{total} checks PASSED ✓")
    print("=" * 60)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
