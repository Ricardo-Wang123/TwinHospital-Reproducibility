"""Append-only experiment outputs and content-addressed, verifiable seals."""
from pathlib import Path
from threading import RLock
import csv
import hashlib
import json
import os
from .contracts import canonical, utc_now


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_child(root, relative):
    root = Path(root).resolve()
    result = (root / relative).resolve()
    if result == root or not result.is_relative_to(root):
        raise ValueError("artifact path escapes its root")
    return result


class RunStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.mkdir(parents=True, exist_ok=False)
        self._lock = RLock()

    def _target(self, name):
        if (self.path / "seal.json").exists():
            raise RuntimeError("sealed run cannot be changed; create a new run")
        target = safe_child(self.path, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def write_json(self, name, value):
        with self._lock:
            target = self._target(name)
            with target.open("x", encoding="utf-8") as stream:
                stream.write(canonical(value) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def append(self, name, value):
        with self._lock:
            target = self._target(name)
            with target.open("a", encoding="utf-8") as stream:
                stream.write(canonical(value) + "\n")
                stream.flush()

    def write_csv(self, name, rows, fieldnames):
        with self._lock:
            with self._target(name).open("x", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="raise")
                writer.writeheader()
                writer.writerows(rows)

    def seal(self, custodian):
        with self._lock:
            self._target("seal.json")
            artifacts = {p.relative_to(self.path).as_posix(): file_hash(p)
                         for p in sorted(self.path.rglob("*")) if p.is_file()}
            value = {"schema_version": "1", "sealed_at": utc_now(), "custodian": custodian,
                     "manifest_sha256": artifacts.get("run_manifest.json"), "artifacts": artifacts,
                     "assurance": "local_content_integrity; independent custody required for prospective proof"}
            self.write_json("seal.json", value)
            return value


def verify_seal(path):
    path = Path(path).resolve()
    problems = []
    try:
        seal = json.loads((path / "seal.json").read_text(encoding="utf-8"))
        for name, expected in seal["artifacts"].items():
            target = safe_child(path, name)
            if not target.is_file() or file_hash(target) != expected:
                problems.append(name)
        if seal.get("manifest_sha256") != seal["artifacts"].get("run_manifest.json"):
            problems.append("manifest_sha256")
        originals = {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()
                     and p.relative_to(path).parts[0] not in {"hospital", "analysis"}
                     and p.name != "seal.json"}
        problems.extend(sorted(originals - set(seal["artifacts"])))
    except (OSError, ValueError, KeyError, TypeError) as error:
        problems.append(str(error))
    return {"valid": not problems, "problems": problems}
