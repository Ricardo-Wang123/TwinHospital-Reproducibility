"""Exclusive instance lifecycle. Recovery commands are operator-owned argv, never model tools."""
import json
import os
import subprocess
import time
from pathlib import Path
from .contracts import canonical, utc_now
from .store import file_hash, safe_child

COVERAGE = {"database", "sequences", "cache", "uploads", "background_tasks", "local_state"}


class InstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, 2)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            self.stream = None
            raise RuntimeError("OpenHIS instance is already in use") from error
        self.stream.seek(1)
        self.stream.truncate()
        self.stream.write(canonical({"pid": os.getpid(), "locked_at": utc_now()}).encode())
        self.stream.flush()
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()


def next_epoch(state_dir):
    """Call only under the instance lock; epoch storage is outside the restore scope."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "epoch.json"
    old = json.loads(path.read_text()) if path.exists() else {"epoch": 0}
    value = int(old["epoch"]) + 1
    temporary = path.with_suffix(".tmp")
    temporary.write_text(canonical({"epoch": value}), encoding="utf-8")
    os.replace(temporary, path)
    return value


class CommandSeedState:
    """A frozen, reviewed recovery manifest; no guessed container names or broad pruning.

    Commands are argv arrays. verify emits one JSON object with instance_id,
    fingerprint and coverage from actual state observations. Nonzero exit, timeout,
    missing coverage, altered archive, or mismatched fingerprint stops the run.
    """
    def __init__(self, manifest_path, instance_id):
        self.path = Path(manifest_path).resolve()
        self.manifest = json.loads(self.path.read_text(encoding="utf-8"))
        self.instance_id = instance_id
        self.receipts = []
        m = self.manifest
        if not m.get("dedicated_instance") or m.get("instance_id") != instance_id:
            raise ValueError("recovery requires an explicitly identified dedicated instance")
        if not COVERAGE.issubset(set(m.get("coverage", []))):
            raise ValueError("recovery coverage is incomplete")
        if not m.get("artifacts") or not m.get("expected_fingerprint"):
            raise ValueError("snapshot artifacts and expected fingerprint are required")
        for name, expected in m["artifacts"].items():
            if file_hash(safe_child(self.path.parent, name)) != expected:
                raise ValueError("snapshot artifact changed: " + name)
        for phase in ("drain", "restore", "verify", "start", "activate"):
            if not isinstance(m.get(phase), list) or not m[phase]:
                raise ValueError("missing recovery phase: " + phase)

    def _phase(self, phase, epoch, run_id=None, config_id=None):
        output = ""
        for spec in self.manifest[phase]:
            if not isinstance(spec, dict) or not isinstance(spec.get("argv"), list) or not all(isinstance(v, str) for v in spec["argv"]):
                raise ValueError("recovery commands must be argv arrays")
            argv = [arg.replace("{epoch}", str(epoch)).replace("{manifest_dir}", str(self.path.parent))
                    .replace("{run_id}", run_id or "").replace("{config_id}", config_id or "") for arg in spec["argv"]]
            start = time.monotonic()
            result = subprocess.run(argv, cwd=str(self.path.parent), capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=spec.get("timeout_s", 300), shell=False)
            self.receipts.append({"phase": phase, "exit_code": result.returncode,
                                  "duration_s": time.monotonic() - start, "occurred_at": utc_now()})
            if result.returncode:
                raise RuntimeError(f"recovery {phase} failed (exit {result.returncode}); next configuration prohibited")
            output = result.stdout
        return output

    def restore(self, epoch, run_id=None, config_id=None):
        if self.manifest.get("provision") and (not run_id or not config_id):
            raise ValueError("prepared native run requires explicit run_id and config_id")
        self._phase("drain", epoch)
        self._phase("restore", epoch)
        proof = json.loads(self._phase("verify", epoch))
        if proof.get("instance_id") != self.instance_id or proof.get("fingerprint") != self.manifest["expected_fingerprint"]:
            raise RuntimeError("restored baseline fingerprint mismatch")
        if not COVERAGE.issubset(set(proof.get("coverage", []))):
            raise RuntimeError("restored coverage not verified")
        if self.manifest.get("provision"):
            self._phase("provision", epoch, run_id, config_id)
        self._phase("start", epoch)
        self._phase("activate", epoch)
        return proof
