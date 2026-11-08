"""Outpatient workflow execution; qualification belongs to an independent scorer."""
from copy import deepcopy
from datetime import datetime, timezone
from .actors import (ACTOR_IDS, INQUIRY_TOPICS, TranscriptSource, assert_runtime_visible,
                     input_hash, merge_facts, normalize_facts, topic_name)
from .contracts import require_finite


VARIANTS = {"prescription": {"pprs_baseline", "sequential", "parallel"},
            "preconsult": {"nurse_only", "ai_only", "ai_nurse_review"},
            "documentation": {"manual_documentation", "realtime", "postvisit"}}
HUMAN_ROLES = {"doctor", "nurse", "pharmacist", "senior_pharmacist", "scribe"}


class WorkflowFailure(RuntimeError):
    pass


def _time(value):
    if isinstance(value, dict):
        return value.get("occurred_at") or value.get("timestamp")
    return getattr(value, "occurred_at", None)


class _Execution:
    def __init__(self, case, variant, actor_factory, pprs, emit, perform, effort, max_interactions):
        self.case, self.variant, self.factory = deepcopy(case), deepcopy(variant), actor_factory
        self.input = self.case.get("input") or {}
        self.pprs, self.emit_callback, self.perform_callback, self.effort_callback = pprs, emit, perform, effort
        self.limit = max_interactions
        self.interactions = 0
        self.sessions = {}
        self.result = {"case_id": case["case_id"], "task": case["task"], "workflow": variant["workflow"],
                       "config_id": variant["config_id"], "status": "running", "final_output": None,
                       "final_decision": None, "first_pass_correct": None, "artifact_versions": [],
                       "encounter_ended_at": None, "physician_attested_at": None, "interaction_count": 0,
                       "rework_rounds": 0}
        self.note_id = f"{variant['config_id']}:{case['case_id']}:note"
        self.note_version = 0
        self.latest_risk_flags = []

    def emit(self, event_type, **fields):
        event = self.emit_callback(event_type, **fields)
        return _time(event) or datetime.now(timezone.utc).isoformat()

    def session(self, role, intervention=False):
        key = role, intervention
        if key not in self.sessions:
            family = self.variant.get("model_family") if intervention else None
            self.sessions[key] = self.factory.session(self.case["case_id"], role, model_family=family)
        return self.sessions[key]

    def respond(self, role, observation, intervention=False):
        assert_runtime_visible(observation)
        response = self.session(role, intervention).respond(deepcopy(observation))
        if not isinstance(response, dict):
            raise WorkflowFailure("actor returned a non-object")
        assert_runtime_visible(response)
        self.emit("actor_response", role=role, action=response.get("action"), decision=response.get("decision"))
        return response

    def effort(self, role, activity_type):
        self.effort_callback(role, activity_type, seconds=None)

    def perform(self, operation, payload, role):
        response = self.perform_callback(operation, deepcopy(payload), role)
        if not isinstance(response, dict):
            raise WorkflowFailure("gateway returned a non-object")
        self.emit("operation_completed", operation=operation, role=role, status=response.get("status"),
                  outcome=response.get("outcome"), request_id=response.get("request_id"))
        self.result["last_operation"] = {"operation": operation, "status": response.get("status"),
                                         "outcome": response.get("outcome")}
        # success supports legacy offline injected ports; real gateway statuses are authoritative.
        success = response.get("status") in {"executed", "replayed"} if "status" in response else response.get("success") is True
        if not success or response.get("outcome") in {"unsafe_execution", "unresolved"}:
            raise WorkflowFailure(f"{operation}: {response.get('reason') or response.get('error') or response.get('status')}")
        return response

    def end_encounter(self, encounter_end_ms):
        response = self.perform("encounter.end", {}, "doctor")
        state = response.get("after_state") or response.get("state") or {}
        actual = state.get("encounter_ended_at") or _time(response)
        event_time = self.emit("encounter_ended", encounter_end_ms=encounter_end_ms,
                               actual_encounter_ended_at=actual)
        self.result["encounter_ended_at"] = actual or event_time

    def save_note(self, content, role, revision=False):
        if not isinstance(content, dict):
            raise WorkflowFailure("structured note content required")
        operation = "note.revise" if revision else "note.save"
        payload = {"note_id": self.note_id, "content": content, "expected_version": self.note_version}
        if revision:
            payload["parent_version"] = self.note_version
        response = self.perform(operation, payload, role)
        state = response.get("after_state") or response.get("state") or {}
        note = state.get("notes", {}).get(self.note_id, {})
        version = note.get("version", response.get("version"))
        if not isinstance(version, int) or isinstance(version, bool) or version != self.note_version + 1:
            raise WorkflowFailure("gateway did not confirm the new note version")
        self.result["artifact_versions"].append({"artifact_id": self.note_id, "note_id": self.note_id,
            "version": version, "parent_version": self.note_version or None, "content": deepcopy(content),
            "content_hash": input_hash(content), "status": "draft", "author_role": role})
        self.note_version = version
        self.result["final_output"] = deepcopy(content)
        self.emit("note_draft_created" if version == 1 else "note_revision_created",
                  note_id=self.note_id, version=version, is_rework=revision)

    def attest(self):
        signatory = self.factory.actor_id("doctor") if hasattr(self.factory, "actor_id") else ACTOR_IDS["doctor"]
        response = self.perform("note.attest", {"note_id": self.note_id, "version": self.note_version,
                                               "signatory_id": signatory}, "doctor")
        state = response.get("after_state") or response.get("state") or {}
        versions = state.get("notes", {}).get(self.note_id, {}).get("versions") or []
        actual = versions[-1].get("attested_at") if versions else None
        self.result["physician_attested_at"] = actual or _time(response) or self.emit("physician_attested",
            note_id=self.note_id, version=self.note_version, signatory_id=signatory)
        self.result["artifact_versions"][-1].update(status="locked", signatory_id=signatory)
        self.effort("doctor", "attestation")

    def documentation(self):
        source = TranscriptSource(self.input.get("transcript", []))
        complete = source.complete()
        last_release_ms = complete[-1].get("available_at_ms", 0)
        # This case-defined boundary is independent of writing/review effort and
        # is shared by all five conditions. Preserve it even if review overruns.
        encounter_end_ms = max(last_release_ms,
            require_finite(self.input.get("encounter_end_ms", last_release_ms), "encounter_end_ms"),
            *(require_finite(s.get("end_ms", 0), "transcript.end_ms") for s in complete))
        self.result["encounter_end_ms"] = encounter_end_ms
        self.emit("encounter_timeline", encounter_end_ms=encounter_end_ms,
                  last_transcript_available_ms=last_release_ms)

        def advance_and_end():
            # Reuse the clock-advance event understood by existing runtimes.
            # No new transcript is disclosed by this clock-only event.
            self.emit("transcript_available", available_at_ms=encounter_end_ms,
                      source_sequences=[], clock_only=True)
            self.emit("encounter_end_reached", encounter_end_ms=encounter_end_ms)
            self.end_encounter(encounter_end_ms)

        workflow = self.variant["workflow"]
        manual = workflow == "manual_documentation"
        streams = source.prefixes() if workflow == "realtime" else [complete]
        for transcript in streams:
            self.emit("transcript_available", available_at_ms=transcript[-1].get("available_at_ms", 0),
                      source_sequences=[row["sequence"] for row in transcript])
            if workflow != "realtime" or transcript[-1].get("available_at_ms", 0) >= encounter_end_ms:
                if self.result["encounter_ended_at"] is None:
                    advance_and_end()
            role = "doctor" if manual else "scribe"
            if manual:
                self.effort("doctor", "documentation")
            elif self.variant.get("human_transcript_handling") is True:
                # Only a configured human activity enters APP II effort, never automatic input reading.
                self.effort("scribe", "transcript_handling")
            draft = self.respond(role, {"action": "document", "transcript": transcript,
                                        "previous_draft": self.result["final_output"]}, intervention=not manual)
            self.save_note(draft.get("content"), role)
            # Real-time review sees the same prefix, never the later full transcript.
            if workflow == "realtime":
                self._review_note(transcript)
        if self.result["encounter_ended_at"] is None:
            advance_and_end()
        if workflow != "realtime":
            self._review_note(complete)
        self.attest()

    def _review_note(self, transcript):
        for _ in range(self.limit):
            self.effort("doctor", "note_review")
            response = self.respond("doctor", {"action": "review_note", "content": self.result["final_output"],
                "transcript": transcript, "version": self.note_version})
            decision = response.get("decision")
            self.emit("note_reviewed", version=self.note_version, decision=decision)
            if decision in {"accept", "approve"}:
                return
            if decision not in {"revise", "changes_requested"}:
                raise WorkflowFailure("physician did not accept the note")
            self.effort("doctor", "note_rework")
            self.save_note(response.get("content"), "doctor", revision=True)
            self.result["rework_rounds"] += 1
        raise WorkflowFailure("note review limit reached without acceptance")

    def prescription(self):
        prescription = deepcopy(self.input.get("prescription"))
        if not isinstance(prescription, dict):
            raise ValueError("prescription input required")
        rule = self.pprs.review(prescription)
        if not isinstance(rule, dict) or rule.get("decision") not in {"pass", "flag", "reject"}:
            raise WorkflowFailure("PPRS returned no valid decision")
        self.emit("pprs_reviewed", decision=rule["decision"], rule_version=rule.get("rule_version"),
                  input_hash=rule.get("input_hash"))
        workflow = self.variant["workflow"]
        ai = pharmacist = senior = None
        # Parallel means independent review coverage, not this executor's thread setting.
        if workflow == "parallel":
            pharmacist = self._rx_human("pharmacist", prescription)
            ai = self.respond("rx_ai", {"action": "review_prescription", "prescription": prescription}, intervention=True)
        elif rule["decision"] != "reject":
            if workflow == "sequential":
                ai = self.respond("rx_ai", {"action": "review_prescription", "prescription": prescription}, intervention=True)
            if rule["decision"] == "flag" or (ai and ai.get("decision") in {"flag", "reject", "revise"}):
                pharmacist = self._rx_human("pharmacist", prescription, ai_review=ai, rule_review=rule)
        for review in (ai, pharmacist):
            if review is not None and review.get("decision") not in {"approve", "pass", "flag", "reject", "revise"}:
                raise WorkflowFailure("invalid prescription review decision")
        decision = "approve"
        if rule["decision"] == "reject":
            decision = "reject"
        else:
            automatic_flag = rule["decision"] == "flag" or bool(ai and ai["decision"] in {"flag", "reject", "revise"})
            human_flag = bool(pharmacist and pharmacist["decision"] in {"flag", "reject", "revise"})
            if workflow == "parallel" and pharmacist and automatic_flag != human_flag:
                self.effort("senior_pharmacist", "prescription_adjudication")
                senior = self.respond("senior_pharmacist", {"action": "adjudicate", "prescription": prescription,
                    "reviews": [r for r in (rule, ai, pharmacist) if r is not None]})
                if senior.get("decision") not in {"approve", "reject"}:
                    raise WorkflowFailure("senior pharmacist did not adjudicate")
                decision = senior["decision"]
            elif pharmacist:
                decision = "reject" if human_flag else "approve"
            elif automatic_flag:
                decision = "reject"
        # Revision requests never silently replace the original test prescription.
        if decision == "reject":
            self.emit("prescription_returned_for_revision", permitted_revision_role="doctor")
        self.result["final_decision"] = decision
        self.result["final_output"] = {"prescription": prescription, "rule_review": rule, "ai_review": ai,
                                       "pharmacist_review": pharmacist, "senior_review": senior,
                                       "final_decision": decision}
        self.emit("prescription_decided", final_decision=decision)
        if decision == "reject" and any(review and review.get("decision") == "revise" for review in (ai, pharmacist, senior)):
            self._revise_prescription(prescription, [r for r in (rule, ai, pharmacist, senior) if r is not None])

    def _revise_prescription(self, original, reviews):
        self.effort("doctor", "prescription_revision_review")
        answer = self.respond("doctor", {"action": "revise_prescription", "prescription": original, "reviews": reviews})
        if answer.get("decision") == "retain_rejected":
            return
        revised = answer.get("prescription")
        if answer.get("decision") != "revise" or not isinstance(revised, dict) or not isinstance(revised.get("items"), list):
            raise WorkflowFailure("doctor returned no valid prescription revision")
        self.effort("doctor", "prescription_revision")
        parent_id = (self.input.get("native_refs") or self.case.get("native_refs") or {}).get("order_id")
        parent_id = parent_id or f"{self.variant['config_id']}:{self.case['case_id']}:order"
        order_id = f"{self.variant['config_id']}:{self.case['case_id']}:order-revision-1"
        # A new draft preserves the source order. Reviewers never issue this operation.
        response = self.perform("order.save", {"order_id": order_id, "parent_order_id": parent_id,
                                               "items": revised["items"]}, "doctor")
        state = response.get("after_state") or response.get("state") or {}
        saved = state.get("orders", {}).get(order_id, {})
        if saved.get("items") != revised["items"] or saved.get("status") != "draft":
            raise WorkflowFailure("gateway did not confirm the revised draft order")
        artifact = {"artifact_id": order_id, "parent_artifact_id": parent_id, "version": 1,
                    "content": deepcopy(revised), "content_hash": input_hash(revised),
                    "author_role": "doctor", "status": "draft"}
        self.result["artifact_versions"].append(artifact)
        self.result["revision_proposal"] = {"order_id": order_id, "parent_order_id": parent_id,
            "prescription": deepcopy(revised), "status": "draft", "requires_new_review": True}
        self.emit("prescription_revision_created", order_id=order_id, parent_order_id=parent_id, role="doctor")

    def _rx_human(self, role, prescription, **review_context):
        self.effort(role, "prescription_review")
        return self.respond(role, {"action": "review_prescription", "prescription": deepcopy(prescription), **review_context})

    def preconsult(self):
        workflow = self.variant["workflow"]
        initial = normalize_facts(self.input.get("initial_visible_facts", []))
        self.patient_pool = normalize_facts(self.input.get("patient_known_facts", []))
        self.required_topics = self.input.get("required_topics") or list(INQUIRY_TOPICS)
        self.collected = deepcopy(initial)
        primary_role = "nurse" if workflow == "nurse_only" else "preconsult_ai"
        facts = self._inquiry(primary_role, initial, intervention=workflow != "nurse_only")
        if workflow == "nurse_only":
            self.effort("nurse", "summary_documentation")
        if workflow == "ai_nurse_review":
            self.effort("nurse", "summary_review")
            facts = self._inquiry("nurse", facts, review=True)
        self.result["first_handoff_output"] = {"facts": deepcopy(facts), "risk_flags": deepcopy(self.latest_risk_flags)}
        self.result["final_output"] = deepcopy(self.result["first_handoff_output"])
        self.emit("preconsult_first_handoff", facts=deepcopy(facts))
        self.effort("doctor", "summary_verification")
        # Only collection/handoff deficiencies are handled here, not routine diagnostic questioning.
        before = deepcopy(facts)
        facts = self._inquiry("doctor", facts, review=True)
        # Reordering the same facts is not a physician correction.
        self.result["downstream_supplementation"] = {f['fact_id']:f for f in before} != {f['fact_id']:f for f in facts}
        self.result["post_consultation_output"] = {"facts": facts, "risk_flags": deepcopy(self.latest_risk_flags)}
        self.result["interaction_count"] = self.interactions
        self.emit("preconsult_completed", interaction_count=self.interactions)

    def _inquiry(self, role, facts, intervention=False, review=False):
        asked = []
        while True:
            response = self.respond(role, {"action": "review_summary" if review else "collect",
                "facts": deepcopy(facts), "asked_topics": asked, "required_topics": self.required_topics,
                "remaining_interactions": self.limit - self.interactions}, intervention=intervention)
            action = response.get("action")
            if action in {"summary", "handoff", "accept"}:
                flags = response.get("risk_flags", [])
                if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
                    raise WorkflowFailure("summary risk_flags must be a list of strings")
                self.latest_risk_flags = deepcopy(flags)
                return normalize_facts(response.get("facts", facts))
            if action != "ask":
                raise WorkflowFailure("inquiry response must ask or summarize")
            if self.interactions >= self.limit:
                raise WorkflowFailure("interaction limit reached")
            topics = response.get("question_topics")
            if not isinstance(topics, list) or not topics or not all(isinstance(t, str) and t for t in topics):
                raise WorkflowFailure("question_topics must be a nonempty string list")
            topics = list(dict.fromkeys(topic_name(t) for t in topics))
            if role == "doctor":
                documented = {topic_name(f["fact_id"]) for f in facts if f["status"] != "not_asked"}
                if any(t in documented for t in topics):
                    raise WorkflowFailure("routine diagnostic/confirmation questions are outside preconsult effort")
                already_collected = {topic_name(f["fact_id"]) for f in self.collected if f["status"] != "not_asked"}
                activity = "redundant_inquiry" if all(t in already_collected for t in topics) else "supplemental_inquiry"
                self.effort("doctor", activity)
            elif role == "nurse":
                self.effort("nurse", "review_supplementation" if review else "information_collection")
            self.emit("question_asked", role=role, question_topics=topics)
            # Enforce the release boundary before even calling the patient model.
            released = []
            for fact in self.patient_pool:
                release_on = fact.get("release_on") or [fact["fact_id"]]
                if isinstance(release_on, str):
                    release_on = [release_on]
                if set(topics).intersection(topic_name(t) for t in release_on):
                    released.append(fact)
            answer = self.respond("patient", {"action": "answer", "question_topics": topics,
                                              "patient_known_facts": released})
            answer_facts = normalize_facts(answer.get("facts", []))
            allowed = {f["fact_id"]: f for f in released}
            for fact in answer_facts:
                if fact["fact_id"] in allowed:
                    original = allowed[fact["fact_id"]]
                    if (fact["value"], fact["status"]) != (original["value"], original["status"]):
                        raise WorkflowFailure("patient actor changed a released structured fact")
                elif topic_name(fact["fact_id"]) not in topics or fact["status"] != "unknown" or fact["value"] is not None:
                    raise WorkflowFailure("patient actor disclosed an unreleased fact")
            self.interactions += 1
            self.result["interaction_count"] = self.interactions
            self.emit("patient_answered", role=role, question_topics=topics, facts=answer_facts,
                      interaction=self.interactions)
            facts = merge_facts(facts, answer_facts)
            self.collected = merge_facts(self.collected, answer_facts)
            asked.extend(topics)


def execute_workflow(case: dict, variant: dict, *, actor_factory, pprs, emit, perform, effort,
                     max_interactions=10) -> dict:
    """Execute visible inputs only. No scoring, clinical qualification or hidden reference access."""
    assert_runtime_visible(case)
    if not isinstance(max_interactions, int) or isinstance(max_interactions, bool) or max_interactions < 1:
        raise ValueError("max_interactions must be a positive integer")
    task = case.get("task")
    if task not in VARIANTS or variant.get("task") != task or variant.get("workflow") not in VARIANTS[task]:
        raise ValueError("unsupported task/workflow combination")
    if not case.get("case_id") or not variant.get("config_id"):
        raise ValueError("case_id and config_id required")
    baseline = variant["workflow"] in {"pprs_baseline", "nurse_only", "manual_documentation"}
    if not baseline and variant.get("model_family") not in {"medical", "foundation"}:
        raise ValueError("intervention requires medical or foundation model_family")
    run = _Execution(case, variant, actor_factory, pprs, emit, perform, effort, max_interactions)
    try:
        run.emit("workflow_started", task=task, workflow=variant["workflow"])
        getattr(run, task)()
        run.result["status"] = "completed"
        run.emit("workflow_completed", task=task)
    except Exception as exc:
        run.result.update(status="failed", failure_reason=str(exc), error_type=type(exc).__name__)
        run.emit("workflow_failed", reason=str(exc), error_type=type(exc).__name__)
    return run.result
