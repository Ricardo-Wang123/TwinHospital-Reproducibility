"""Copy the linked experiment's code and public dictionaries before execution.

Run with the project's Python environment. Credentials, provider configuration,
patient data, outputs and the environment itself are deliberately not copied.
The destination must be new; failed snapshots remain available for diagnosis.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def freeze(destination: Path) -> dict:
    repo = Path(__file__).resolve().parents[1]
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    files = list((repo / "sandbox").rglob("*.py"))
    files += list((repo / "sandbox/experiments/linked").glob("*.json"))
    files += list((repo / "sandbox/experiments/linked/case_preparation").glob("*.json"))
    files += list((repo / "deploy/openhis-experiment").glob("catalog_mapping_v[0-9]*.json"))
    files += list((repo / "deploy/openhis-experiment").glob("catalogue_metadata_v[0-9]*.json"))
    files += list((repo / "deploy/openhis-experiment").glob("formulary_v[0-9]*.json"))
    files += list((repo / "deploy/openhis-experiment").glob("diagnosis_mapping*.json"))
    files += [repo / name for name in (
        "tools/__init__.py", "tools/pharmacy_inventory.py",
        "tools/freeze_linked_runtime.py", "requirements-linked.txt",
        "deploy/openhis-experiment/diagnosis_mapping.json",
        "deploy/openhis-experiment/catalogue_metadata_v2.json",
    )]
    hashes = {}
    for source in sorted(set(files)):
        relative = source.relative_to(repo)
        raw = source.read_bytes()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(raw)
        hashes[str(relative)] = hashlib.sha256(raw).hexdigest()
    # A snapshot is valid only if all sources stayed stable during the copy.
    for relative, expected in hashes.items():
        if hashlib.sha256((repo / relative).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Source changed during snapshot: {relative}")
    packages = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)
    (destination / "python_packages.txt").write_text(packages)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_repo": str(repo),
        "base_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
        "python_executable": sys.executable,
        "files_sha256": hashes,
        "external_inputs": ["case manifest and files", "provider configuration",
                            "secret key file", "shared budget ledger",
                            "native credentials and isolated native database"],
        "clinical_validation": "Not implied by a source snapshot or import test.",
    }
    (destination / "source_snapshot_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    subprocess.run([sys.executable, "-c", "import sandbox.experiments.linked.run_batch"],
                   cwd=destination, check=True)
    for relative in hashes:
        (destination / relative).chmod(0o444)
    return {"destination": str(destination), "source_files": len(hashes),
            "import_check": "passed"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    print(json.dumps(freeze(parser.parse_args().destination), indent=2))
