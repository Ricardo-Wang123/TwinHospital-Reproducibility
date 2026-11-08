"""HIS implementations. The in-memory backend is explicitly engineering-only."""
import copy
import os
from decimal import Decimal, InvalidOperation
from threading import RLock, local
from .contracts import digest, utc_now


class BusinessRejected(ValueError):
    pass


def money(value):
    if not isinstance(value, str):
        raise BusinessRejected("amount must be a decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise BusinessRejected("invalid amount") from error
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        raise BusinessRejected("amount must be positive with at most two fractional digits")
    return amount


class InMemoryHIS:
    research_eligible = False

    def __init__(self):
        self._lock = RLock()
        self.active_epoch = None
        self.restore({})

    def configure_run(self, run_id, epoch, config_id=None, config=None):
        with self._lock:
            self.active_epoch = epoch

    def deactivate(self):
        with self._lock:
            self.active_epoch = -1

    def restore(self, seed):
        with self._lock:
            self.cases = copy.deepcopy(seed.get("cases", {}))
            self.personnel = copy.deepcopy(seed.get("personnel", {}))
            self.transactions = {}

    def fingerprint(self):
        with self._lock:
            return digest({"cases": self.cases, "personnel": self.personnel, "transactions": self.transactions})

    def authorized(self, signatory, operation):
        return operation in self.personnel.get(signatory, [])

    def snapshot(self, case_id):
        with self._lock:
            return copy.deepcopy(self.cases.get(case_id, {}))

    def execute(self, action, context):
        with self._lock:
            try:
                result = self._apply(action, context)
                return {"status": result or "executed", "reason": None,
                        "state": self.snapshot(action["case_id"]), "object_refs": {}}
            except (BusinessRejected, KeyError, TypeError, ValueError) as error:
                return {"status": "rejected", "reason": str(error), "state": self.snapshot(action["case_id"])}

    def _apply(self, action, context):
        if self.active_epoch is not None and action["epoch"] != self.active_epoch:
            raise BusinessRejected("stale_epoch")
        op, p, case_id = action["operation"], action["payload"], action["case_id"]
        if op == "case.bind":
            patient, encounter = p["patient_id"], p["encounter_id"]
            if not patient or not encounter or patient != context["patient_id"] or encounter != context["encounter_id"]:
                raise BusinessRejected("invalid case binding")
            if case_id in self.cases:
                state = self.cases[case_id]
                if (state["patient_id"], state["encounter_id"]) != (patient, encounter):
                    raise BusinessRejected("case already bound")
                return "replayed"
            self.cases[case_id] = {"patient_id": patient, "encounter_id": encounter,
                                   "queue_entries": [], "notes": {}, "orders": {}, "bills": {},
                                   "transactions": {}, "payment_effects": [], "encounter_ended_at": None}
            return
        state = self.cases[case_id]
        if state["patient_id"] != context["patient_id"] or state["encounter_id"] != context["encounter_id"]:
            raise BusinessRejected("case context mismatch")
        if op == "triage.enqueue":
            if p.get("encounter_id") != state["encounter_id"] or p.get("patient_id") != state["patient_id"]:
                raise BusinessRejected("queue requires matching patient and encounter")
            state["queue_entries"].append({"patient_id": p["patient_id"], "encounter_id": p["encounter_id"]})
        elif op == "encounter.end":
            state["encounter_ended_at"] = p.get("occurred_at") or utc_now()
        elif op in {"note.save", "note.revise"}:
            note_id = p["note_id"]
            previous = state["notes"].get(note_id)
            version = previous["version"] if previous else 0
            if p["expected_version"] != version:
                raise BusinessRejected("note version conflict")
            if previous and previous["status"] == "locked" and op != "note.revise":
                raise BusinessRejected("locked note requires new revision")
            if op == "note.revise" and (not previous or p.get("parent_version") != version):
                raise BusinessRejected("revision requires current parent")
            if not isinstance(p.get("content"), dict):
                raise BusinessRejected("structured note required")
            entry = {"version": version + 1, "parent_version": version or None, "content": copy.deepcopy(p["content"]),
                     "content_hash": digest(p["content"]), "author_id": context["actor_id"], "status": "draft"}
            versions = copy.deepcopy(previous["versions"]) if previous else []
            versions.append(entry)
            state["notes"][note_id] = {"version": version + 1, "status": "draft", "versions": versions}
        elif op == "note.attest":
            if not self.authorized(p["signatory_id"], op):
                raise BusinessRejected("signatory lacks note qualification")
            note = state["notes"][p["note_id"]]
            if p["version"] != note["version"] or note["status"] == "locked":
                raise BusinessRejected("cannot attest stale or locked version")
            note["status"] = "locked"
            note["versions"][-1].update(status="locked", signatory_id=p["signatory_id"], attested_at=utc_now())
        elif op == "order.save":
            if p["order_id"] in state["orders"]:
                raise BusinessRejected("order exists; create a revision")
            state["orders"][p["order_id"]] = {"status": "draft", "patient_id": state["patient_id"],
                "encounter_id": state["encounter_id"], "items": copy.deepcopy(p.get("items", [])), "signatory_id": None}
        elif op == "order.attest":
            if not self.authorized(p["signatory_id"], op):
                raise BusinessRejected("signatory lacks order qualification")
            order = state["orders"][p["order_id"]]
            if order["status"] != "draft":
                raise BusinessRejected("order is not draft")
            order.update(status="signed", signatory_id=p["signatory_id"])
        elif op == "payment.prepare":
            order = state["orders"][p["order_id"]]
            if order["status"] != "signed" or not self.authorized(order["signatory_id"], "order.attest"):
                raise BusinessRejected("payment requires authorized signed order")
            amount = money(p["amount"])
            if p["bill_id"] in state["bills"]:
                raise BusinessRejected("bill already exists")
            state["bills"][p["bill_id"]] = {"patient_id": state["patient_id"], "encounter_id": state["encounter_id"],
                "order_id": p["order_id"], "amount": str(amount), "currency": p["currency"], "status": "unpaid"}
        elif op == "payment.commit":
            bill = state["bills"][p["bill_id"]]
            money(p["amount"])
            request_hash = digest({"case_id": case_id, "payload": p})
            tx = p["transaction_id"]
            if not isinstance(tx, str) or not tx:
                raise BusinessRejected("transaction_id required")
            if tx in self.transactions:
                if self.transactions[tx]["request_hash"] != request_hash:
                    raise BusinessRejected("transaction key reused with different request")
                return "replayed"
            if bill["status"] != "unpaid" or Decimal(p["amount"]) != Decimal(bill["amount"]) or p["currency"] != bill["currency"]:
                raise BusinessRejected("bill state or amount mismatch")
            bill["status"] = "paid"
            tx_row = {"transaction_id": tx, "bill_id": p["bill_id"], "request_hash": request_hash, "status": "paid"}
            self.transactions[tx] = tx_row
            state["transactions"][tx] = copy.deepcopy(tx_row)
            state["payment_effects"].append({"transaction_id": tx, "bill_id": p["bill_id"], "amount": p["amount"]})
        else:
            raise BusinessRejected("unsupported operation")


class OpenHISPort:
    """No fallback to local state; every action and observation uses the sandbox API."""
    research_eligible = True

    def __init__(self, config):
        self.config = config
        self._local = local()
        self.base_url = config["base_url"].rstrip("/")
        self.personnel = config.get("personnel", {})
        self.run_id = None
        self.epoch = None

    def _session(self):
        if not hasattr(self._local, "session"):
            import requests
            session = requests.Session()
            service_key = os.environ.get(self.config.get("service_key_env", "TWINHOSPITAL_SERVICE_KEY"))
            if not service_key:
                raise RuntimeError("sandbox service key is missing; native login routes are disabled in sandbox mode")
            session.headers["X-Sandbox-Key"] = service_key
            self._local.session = session
        return self._local.session

    def _request(self, method, path, body=None):
        response = self._session().request(method, self.base_url + path, json=body,
            timeout=self.config.get("http_timeout_s", 15))
        value = response.json()
        if response.status_code == 409 and isinstance(value.get("data"), dict):
            return value["data"]
        response.raise_for_status()
        if "code" in value and value["code"] not in (0, 200):
            raise RuntimeError(f"OpenHIS business request rejected: code={value['code']}")
        return value.get("data", value)

    def execute(self, action, context):
        return self._request("POST", "/sandbox/v1/actions", {**action, "context": context})

    def snapshot(self, case_id):
        from urllib.parse import quote, urlencode
        if not self.run_id or self.epoch is None:
            raise RuntimeError("OpenHIS run context has not been activated")
        result = self._request("GET", "/sandbox/v1/cases/" + quote(case_id, safe="") + "/state?" + urlencode({"run_id": self.run_id, "epoch": self.epoch}))
        if result.get("status") == "rejected":
            raise RuntimeError("OpenHIS refused state observation")
        from .native_mapping import normalize_state
        return normalize_state(result)

    def authorized(self, signatory, operation):
        return operation in self.personnel.get(signatory, [])

    def health(self):
        return self._request("GET", "/sandbox/v1/health")

    def configure_run(self, run_id, epoch, config_id, config):
        proof = self.health()
        if proof.get("instance_id") != config["instance_id"] or str(proof.get("external_mode")).replace("-", "_") != "stub_only":
            raise RuntimeError("OpenHIS identity or external-effect isolation mismatch")
        result = self._request("POST", "/sandbox/v1/runs/activate", {"run_id": run_id, "config_id": config_id,
            "epoch": epoch, "tenant_id": self.config.get("tenant_id", 1), "instance_id": config["instance_id"],
            "seed_digest": proof["fingerprint"]})
        if result.get("status") == "rejected":
            raise RuntimeError("OpenHIS activation failed")
        self.run_id, self.epoch = run_id, epoch

    def deactivate(self):
        if self.run_id:
            result = self._request("POST", "/sandbox/v1/runs/deactivate", {"run_id": self.run_id, "epoch": self.epoch})
            if result.get("status") == "rejected":
                raise RuntimeError("OpenHIS deactivate failed; recovery prohibited")
            self.run_id = self.epoch = None

    def restore(self, seed):
        raise RuntimeError("OpenHIS restore must use the exclusive SeedState lifecycle")

    def fingerprint(self):
        raise RuntimeError("health seed_digest is not an actual state fingerprint; use independent SeedState verification")
