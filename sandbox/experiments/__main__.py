"""CLI only: generate, validate, run, seal, calibrate and compare experiments."""
import argparse
import csv
import json
from pathlib import Path
from .config import load_config, default_config, readiness, parameter_registry, resolve_path
from .contracts import canonical
from .data import CaseProvider, generate_demo, read_jsonl
from .store import verify_seal


def read_rows(path):
    path = Path(path)
    if path.suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            for name, value in row.items():
                if value == "":
                    row[name] = None
            for name in ("active_seconds", "score"):
                if row.get(name) not in (None, ""):
                    row[name] = float(row[name]) if name == "active_seconds" else int(row[name])
            for name in ("independent_execution", "valid"):
                if row.get(name) in ("true", "false", "True", "False"):
                    row[name] = row[name].lower() == "true"
        return rows
    if path.suffix == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    return read_jsonl(path)


def output(value, path=None):
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as stream:
            stream.write(text + "\n")
    print(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description="TwinHospital outpatient sandbox experiments")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate-demo", help="Create explicitly nonclinical synthetic fixtures")
    generate.add_argument("--output", required=True); generate.add_argument("--scale", choices=["small", "paper"], default="small")
    generate.add_argument("--seed", type=int, default=20260919)
    for name in ("validate", "parameters", "run", "write-config"):
        sub = commands.add_parser(name); sub.add_argument("--config")
        if name == "write-config": sub.add_argument("--output", required=True)
    check = commands.add_parser("verify-seal"); check.add_argument("--run", required=True)
    calibrate = commands.add_parser("calibrate"); calibrate.add_argument("--activities", required=True)
    calibrate.add_argument("--catalog", required=True); calibrate.add_argument("--output", required=True)
    export = commands.add_parser("export-hospital"); export.add_argument("--run", required=True); export.add_argument("--output", required=True)
    imp = commands.add_parser("import-hospital"); imp.add_argument("--run", required=True); imp.add_argument("--input", required=True)
    blind = commands.add_parser("blind-review"); blind.add_argument("--input", required=True); blind.add_argument("--output", required=True)
    adj = commands.add_parser("adjudicate"); adj.add_argument("--reviews", required=True); adj.add_argument("--adjudications", required=True); adj.add_argument("--output", required=True)
    survey = commands.add_parser("subjective"); survey.add_argument("--input", required=True); survey.add_argument("--output")
    compare = commands.add_parser("compare", help="Compare sealed reviewed evidence, or describe unsealed input")
    comparison_source = compare.add_mutually_exclusive_group(required=True)
    comparison_source.add_argument("--input", help="Unsealed rows: descriptive statistics only")
    comparison_source.add_argument("--run", help="Sealed run; requires --analysis and forbids --config")
    compare.add_argument("--analysis", help="Existing reviewed analysis bundle belonging to --run")
    compare.add_argument("--config", help="Unsealed input sampling settings only; never grants fidelity conclusions")
    compare.add_argument("--output", required=True, help="Exclusively create a new report outside the sealed run")
    evaluate = commands.add_parser("evaluate", help="Score frozen outputs from independent blinded review evidence")
    evaluate.add_argument("--run", required=True); evaluate.add_argument("--dataset", required=True)
    evaluate.add_argument("--reviews", required=True); evaluate.add_argument("--adjudications", required=True)
    evaluate.add_argument("--hospital-activities")
    formats = commands.add_parser("check-format", help="Validate one file against a bundled local JSON Schema")
    formats.add_argument("--input", required=True); formats.add_argument("--schema", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "check-format":
            from .formats import validate_format
            report = validate_format(args.input, args.schema); output(report)
            return 0 if report["valid"] else 2
        elif args.command == "generate-demo":
            catalog = generate_demo(args.output, scale=args.scale, seed=args.seed)
            output({"dataset": str(Path(args.output).resolve()), "research_eligible": False,
                    "counts": {r["path"]: r["record_count"] for r in catalog["artifacts"]}})
        elif args.command in {"validate", "parameters", "run", "write-config"}:
            config = load_config(args.config)
            if args.command == "write-config":
                import yaml
                with Path(args.output).open("x", encoding="utf-8") as stream:
                    yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)
                output({"config": str(Path(args.output).resolve())})
            elif args.command == "parameters":
                output(parameter_registry(config))
            elif args.command == "validate":
                data = CaseProvider(resolve_path(config["dataset"])) if resolve_path(config["dataset"]).is_dir() else None
                report = readiness(config, data)
                if data is None:
                    report["missing"].append({"field": "dataset", "reason": "data package does not exist"}); report["ready"] = False
                output(report)
                return 0 if report["ready"] else 2
            else:
                from .engine import run
                result = run(config)
                output({k: v for k, v in result.items() if k not in {"metrics", "safety", "end_to_end"}})
                return 1 if result["fatal_error"] else 0
        elif args.command == "verify-seal":
            result = verify_seal(args.run); output(result)
            return 0 if result["valid"] else 2
        else:
            from . import research
            if args.command == "calibrate":
                output(research.calibrate(read_rows(args.activities), read_rows(args.catalog)), args.output)
            elif args.command == "export-hospital":
                output(research.export_hospital_package(Path(args.run), Path(args.output)))
            elif args.command == "import-hospital":
                output(research.import_hospital_results(Path(args.run), Path(args.input)))
            elif args.command == "blind-review":
                pack = research.blind_review_pack(read_rows(args.input))
                target = Path(args.output); target.mkdir(parents=True, exist_ok=False)
                output(pack["reviewer_pack"], target / "reviewer_pack.json")
                # Keep the linking key out of the reviewer package and stdout.
                with (target / "PRIVATE_mapping.json").open("x", encoding="utf-8") as stream:
                    stream.write(canonical(pack["private_mapping"]))
            elif args.command == "adjudicate":
                output(research.adjudicate_reviews(read_rows(args.reviews), read_rows(args.adjudications)), args.output)
            elif args.command == "subjective":
                output(research.subjective_metrics(read_rows(args.input)), args.output)
            elif args.command == "evaluate":
                from .evaluation import evaluate_reviewed_run
                result = evaluate_reviewed_run(args.run, args.dataset, args.reviews, args.adjudications, args.hospital_activities)
                output({key: value for key, value in result.items() if key not in {"issues", "groups"}})
                return 0 if result["complete"] else 2
            elif args.command == "compare":
                if args.run is not None:
                    if args.analysis is None or args.config is not None or args.input is not None:
                        raise ValueError("sealed compare requires --run and --analysis, without --input or --config")
                    if Path(args.output).resolve().is_relative_to(Path(args.run).resolve()):
                        raise ValueError("comparison output must be outside the sealed run")
                elif args.analysis is not None:
                    raise ValueError("--analysis requires --run and cannot accompany raw --input")
                if Path(args.output).exists():
                    raise FileExistsError("comparison output already exists; choose a new path")
                if args.run is not None:
                    from .evaluation import compare_sealed_analysis
                    value = compare_sealed_analysis(args.run, args.analysis)
                else:
                    from .metrics import paired_comparison
                    config = load_config(args.config)
                    value = paired_comparison(read_rows(args.input), baseline_by_task={"prescription": "prescription:pprs_baseline",
                        "preconsult": "preconsult:nurse_only", "documentation": "documentation:manual_documentation"},
                        seed=config["seed"], bootstrap_samples=config["bootstrap_samples"], confidence=config["confidence"],
                        tolerances=None)
                    value.update(descriptive_only=True, conclusion_allowed=False,
                        conclusion_scope="descriptive_statistics_only", conclusion_reasons=["unsealed_input"],
                        statistics_source="unsealed_input_descriptive")
                    for task in value["tasks"].values():
                        for metric in task["metrics"].values():
                            metric.update(fidelity_pass=None, tolerance_decisions={}, conclusion_allowed=False)
                output(value, args.output)
        return 0
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        output({"error": type(error).__name__, "message": str(error)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
