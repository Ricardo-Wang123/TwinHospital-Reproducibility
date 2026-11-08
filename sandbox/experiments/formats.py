"""Validate local contracts without resolving any schema over the network."""
import csv
from pathlib import Path
from .data import read_jsonl, strict_json


def validate_format(input_path, schema_name):
    from jsonschema import Draft202012Validator, FormatChecker
    from referencing import Registry, Resource
    root = Path(__file__).parent / "schemas"
    if Path(schema_name).name != schema_name:
        raise ValueError("schema must name a bundled contract file")
    schemas = {path.name: strict_json(path.read_text(encoding="utf-8")) for path in root.glob("*.json")}
    if schema_name not in schemas:
        raise ValueError("unknown bundled schema")
    def no_remote(uri):
        raise ValueError("unbundled schema reference prohibited")
    registry = Registry(retrieve=no_remote).with_resources([
        (value["$id"], Resource.from_contents(value)) for value in schemas.values()])
    schema = schemas[schema_name]
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
    path = Path(input_path)
    if path.suffix.lower() in {".yaml", ".yml"}:
        import yaml
        rows = [yaml.safe_load(path.read_text(encoding="utf-8"))]
    elif path.suffix.lower() == ".json":
        rows = [strict_json(path.read_text(encoding="utf-8"))]
    elif path.suffix.lower() == ".csv":
        from .__main__ import read_rows
        rows = read_rows(path)
        # Only declared CSV numeric/boolean columns are normalized; IDs stay strings.
        for row in rows:
            for key in ("version", "sequence", "parent_version", "start_ms", "end_ms"):
                if row.get(key) is not None:
                    row[key] = int(row[key])
    else:
        rows = read_jsonl(path)
    errors = []
    for index, row in enumerate(rows, 1):
        for error in validator.iter_errors(row):
            # Include the location and rule, not potentially sensitive input values.
            errors.append({"row": index, "path": "/" + "/".join(str(v) for v in error.absolute_path),
                           "constraint": error.validator, "schema_path": "/".join(str(v) for v in error.absolute_schema_path)})
    return {"schema": schema_name, "rows": len(rows), "valid": not errors, "errors": errors,
            "qualification": "structure_only; cross-file provenance and clinical verification remain required"}
