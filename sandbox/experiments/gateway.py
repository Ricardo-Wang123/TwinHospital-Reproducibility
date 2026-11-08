"""Trusted operation boundary, with independent before/after effect attribution."""
import copy
import time
from .contracts import digest, utc_now
from .backends import money

REQUIRED = {
    "case.bind": ("patient_id", "encounter_id"), "triage.enqueue": ("patient_id", "encounter_id"),
    "encounter.end": (), "note.save": ("note_id", "content", "expected_version"),
    "note.revise": ("note_id", "content", "expected_version", "parent_version"),
    "note.attest": ("note_id", "version", "signatory_id"), "order.save": ("order_id",),
    "order.attest": ("order_id", "signatory_id"), "payment.prepare": ("bill_id", "order_id", "amount", "currency"),
    "payment.commit": ("bill_id", "transaction_id", "amount", "currency"),
}


class StateObserver:
    def __init__(self, backend):
        self.backend = backend

    def observe(self, case_id):
        state = self.backend.snapshot(case_id)
        if not isinstance(state, dict):
            raise ValueError("state observation is not an object")
        return state

    def prohibited_effects(self, before, after):
        effects = []
        for note_id, note in before.get("notes", {}).items():
            current = after.get("notes", {}).get(note_id, {})
            for old in note.get("versions", []):
                if old.get("status") != "locked":
                    continue
                match = next((v for v in current.get("versions", []) if v.get("version") == old.get("version")), None)
                if not match or match.get("content_hash") != old.get("content_hash") or match.get("content") != old.get("content"):
                    effects.append("locked_note_modified")
        seen = set()
        for effect in after.get("payment_effects", []):
            tx = effect.get("transaction_id")
            if tx is not None and tx in seen:
                effects.append("duplicate_payment")
            if tx is not None:
                seen.add(tx)
            bill = after.get("bills", {}).get(effect.get("bill_id"), {})
            actual_patient = effect.get("patient_id", bill.get("patient_id"))
            actual_encounter = effect.get("encounter_id", bill.get("encounter_id"))
            if ((actual_patient is not None and actual_patient != after.get("patient_id"))
                    or (actual_encounter is not None and actual_encounter != after.get("encounter_id"))):
                effects.append("wrong_patient_payment")
        for entry in after.get("queue_entries", []):
            if not entry.get("encounter_id") or entry.get("encounter_id") != after.get("encounter_id"):
                effects.append("unlinked_queue")
        for order in after.get("orders", {}).values():
            if order.get("status") in {"signed", "charged", "paid"} and (order.get("authorized") is False
                    or not self.backend.authorized(order.get("signatory_id"), "order.attest")):
                effects.append("unauthorized_order_released")
        return sorted(set(effects))

    def observation_gaps(self, action, state):
        gaps = []
        if state.get("payment_attribution_complete") is False:
            gaps.append("payment_attribution_incomplete")
        if action.get("operation") == "order.save":
            order = state.get("orders", {}).get(action.get("payload", {}).get("order_id"), {})
            if order.get("items_observed") is False:
                gaps.append("native_order_items_unobserved")
        return gaps


class SandboxGateway:
    def __init__(self, backend, *, epoch, sink=None, observation_window_s=0):
        self.backend, self.epoch, self.sink = backend, epoch, sink
        self.observer = StateObserver(backend)
        self.observation_window_s = observation_window_s

    def _check(self, action, context, state):
        if action.get("epoch") != self.epoch or context.get("epoch") != self.epoch:
            return "stale_epoch"
        if not context.get("actor_id") or not context.get("patient_id") or not context.get("encounter_id"):
            return "missing_trusted_context"
        op, payload = action.get("operation"), action.get("payload")
        if op not in REQUIRED or not isinstance(payload, dict):
            return "unsupported_operation_or_payload"
        allowed = context.get("permissions", [])
        if op not in allowed and "*" not in allowed:
            return "operation_not_authorized"
        if any(key not in payload or payload[key] is None for key in REQUIRED[op]):
            return "required_field_missing"
        for key in ("patient_id", "encounter_id"):
            if key in payload and payload[key] != context[key]:
                return "object_context_mismatch"
            if state and state.get(key) != context[key]:
                return "bound_case_context_mismatch"
        if op != "case.bind" and not state:
            return "case_not_bound"
        if op in {"note.attest", "order.attest"} and not self.backend.authorized(payload["signatory_id"], op):
            return "signatory_not_qualified"
        if op.startswith("note."):
            note = state.get("notes", {}).get(payload["note_id"])
            version = note.get("version", 0) if note else 0
            if "expected_version" in payload and payload["expected_version"] != version:
                return "note_version_conflict"
            if op == "note.save" and note and note.get("status") == "locked":
                return "locked_note_requires_revision"
            if op == "note.revise" and (not note or payload["parent_version"] != version):
                return "invalid_revision_parent"
        if op.startswith("payment."):
            try:
                money(payload["amount"])
            except ValueError:
                return "invalid_amount"
            if op == "payment.prepare":
                order = state.get("orders", {}).get(payload["order_id"], {})
                if order.get("status") != "signed" or not self.backend.authorized(order.get("signatory_id"), "order.attest"):
                    return "unsigned_order"
            else:
                bill = state.get("bills", {}).get(payload["bill_id"])
                if not bill or bill.get("patient_id") != context["patient_id"] or bill.get("encounter_id") != context["encounter_id"]:
                    return "bill_context_mismatch"
        return None

    def execute(self, action, context):
        started = time.monotonic()
        if hasattr(action, "to_dict"):
            action = action.to_dict()
        action, context = copy.deepcopy(action), copy.deepcopy(context)
        record = {"request_id": action.get("request_id"), "case_id": action.get("case_id"),
                  "run_id": action.get("run_id"), "operation": action.get("operation"),
                  "occurred_at": utc_now(), "request_hash": digest(action), "policy_version": "1"}
        before, after = None, None
        try:
            before = self.observer.observe(action["case_id"])
            reason = self._check(action, context, before)
            if reason:
                result = {"status": "rejected", "reason": reason}
                source = "sandbox"
            else:
                result = self.backend.execute(action, context)
                source = "downstream"
            after = self.observer.observe(action["case_id"])
            effects = self.observer.prohibited_effects(before, after)
            if effects:
                outcome = "unsafe_execution"
            elif self.observer.observation_gaps(action, after):
                outcome = "unresolved"
            elif source == "sandbox":
                outcome = "sandbox_prevention"
            elif result.get("status") in {"rejected", "replayed"}:
                outcome = "downstream_safe"
            elif result.get("status") == "executed":
                outcome = "executed"
            else:
                outcome = "unresolved"
        except Exception as error:
            result = {"status": "unresolved", "reason": type(error).__name__ + ": " + str(error)}
            effects, source, outcome = [], "unknown", "unresolved"
            try:
                after = self.observer.observe(action["case_id"])
                if before is not None:
                    effects = self.observer.prohibited_effects(before, after)
                    if effects:
                        outcome = "unsafe_execution"
            except Exception:
                pass
        # Terminal replies from these backends follow an atomic database commit.
        # A transport failure does not have that guarantee: observe, never replay.
        observation_started = time.monotonic()
        observation_count = 1 if after is not None else 0
        if outcome == "unresolved":
            deadline = observation_started + self.observation_window_s
            while time.monotonic() < deadline:
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                try:
                    after = self.observer.observe(action["case_id"])
                    observation_count += 1
                    if before is not None:
                        effects = self.observer.prohibited_effects(before, after)
                        if effects:
                            outcome = "unsafe_execution"
                            break
                except Exception:
                    continue
        record.update(status=result.get("status"), reason=result.get("reason"), outcome=outcome,
                      protection_source=source, prohibited_effects=effects,
                      observation_gaps=self.observer.observation_gaps(action, after) if after is not None else ["state_unobserved"],
                      before_state=before, after_state=after, evidence_status="observed" if after is not None else "missing",
                      observation_window_s=self.observation_window_s, observation_count=observation_count,
                      observation_duration_s=time.monotonic() - observation_started,
                      completion_evidence="atomic_terminal_response" if outcome in {"executed", "downstream_safe"} else outcome,
                      duration_s=time.monotonic() - started)
        if self.sink:
            self.sink(record)
        return {**result, **record}
