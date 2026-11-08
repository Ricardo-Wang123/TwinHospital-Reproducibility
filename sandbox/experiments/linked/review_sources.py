"""Stage-specific blinded-review sources resolved from actual generation calls.

No final chart or final prescription is backfilled into an earlier artifact.
Missing generation evidence is explicitly unresolved instead of guessed.
"""
from copy import deepcopy
from .contracts import digest


def _conversation(rows):
    return [{"sequence": row.get("sequence"),
        "speaker": "patient" if row.get("speaker_role") == "patient" else "clinical_staff",
        "phase": row.get("phase"), "text": row.get("text")} for row in rows]


def sources_from_observation(observation, *, resolution, call_sequence=None):
    obs = observation or {}
    context = obs.get("clinical_context") or {}
    transcript = obs.get("transcript", obs.get("conversation", context.get("conversation", [])))
    values = {"visible_hospital_history": obs.get("hospital_history", context.get("hospital_history")),
        "conversation": _conversation(transcript),
        "handoff": obs.get("handoff", obs.get("prior_summary", context.get("handoff"))),
        "consultation": obs.get("consultation", context.get("consultation")),
        "approved_prescription": obs.get("approved_prescription"),
        "observed_business_state": obs.get("observed_business_state"),
        "clinical_disposition": obs.get("clinical_disposition"),
        "current_encounter_measurements": obs.get("current_encounter_measurements", context.get("current_encounter_measurements")),
        "candidate_context": context if obs.get("action") == "review_prescription" else None,
        "is_final_prefix": obs.get("is_final_prefix"),
        "allowed_source_scope": obs.get("allowed_source_scope"),
        "source_resolution": {"status": resolution, "source_call_sequence": call_sequence,
            "observation_hash": digest(obs) if observation is not None else None},
        "evidence_scope": "actual_generation_inputs" if resolution == "matched_actual_generation_call" else
                          "recorded_frozen_sources" if resolution == "recorded_final_or_frozen_sources" else
                          "generation_sources_unavailable"}
    return deepcopy(values)


def _matches(run, content, actions, *, after=-1):
    expected = digest(content)
    return [(i, call) for i, call in enumerate(run.get("calls", [])) if i > after and
        call.get("status") not in {"invalid_schema", "failed"} and
        call.get("observation", {}).get("action") in actions and
        isinstance(call.get("response"), dict) and
        isinstance(call["response"].get("content"), dict) and digest(call["response"]["content"]) == expected]


def _source(call):
    return sources_from_observation(call["observation"], resolution="matched_actual_generation_call",
                                    call_sequence=call.get("sequence"))


def _final_fallback(run, task):
    artifacts, fixed = run.get("artifacts", {}), run.get("stage_inputs", {})
    if task == "preconsult":
        return {"conversation": [row for row in artifacts.get("transcript", [])
            if row.get("phase") in {"preconsult_collection", "nurse_summary_review"}]}
    if task == "prescription_reference":
        return {"action": "review_prescription", "clinical_context": fixed.get("prescription", {}).get("clinical_context", {})}
    stage = fixed.get("documentation", {})
    return {"transcript": stage.get("transcript", [row for row in artifacts.get("transcript", [])
                if row.get("phase") in {"doctor_handoff_verification", "consultation"}]),
        "hospital_history": stage.get("hospital_history", artifacts.get("visible_history")),
        "handoff": stage.get("handoff", artifacts.get("physician_verified_handoff")),
        "consultation": stage.get("consultation", artifacts.get("consultation")),
        "approved_prescription": stage.get("approved_prescription", artifacts.get("final_prescription")),
        "observed_business_state": stage.get("observed_business_state", {"prescription": artifacts.get("prescription_business_state")}),
        "clinical_disposition": stage.get("clinical_disposition", artifacts.get("clinical_disposition")),
        "current_encounter_measurements": stage.get("current_encounter_measurements", artifacts.get("current_encounter_measurements")),
        "is_final_prefix": True}


def review_candidates(run):
    """Return each saved note version and distinct handoff assessment stage."""
    artifacts = run.get("artifacts", {})
    candidates = []
    borrowed_handoff = (run.get('evaluation_unit') == 'fixed_input_task'
        and 'preconsult' not in run.get('conditions', {}))
    for key, stage, actions in (
        ("initial_summary", "initial_collection_summary", {"collect"}),
        ("final_handoff", "first_handoff_before_physician", {"collect", "review_summary"}),
        ("physician_verified_handoff", "after_physician_verification", {"verify_handoff"})):
        # A prescription/documentation branch inherits a frozen handoff as an
        # input. It did not generate a new preconsultation result to score.
        # Keep that handoff in the candidate's source materials, not as a
        # duplicate output with falsely missing generation evidence.
        if key not in artifacts or borrowed_handoff:
            continue
        content = artifacts[key]
        matches = _matches(run, content, actions)
        if matches:
            source = _source(matches[-1][1])
        elif key == "final_handoff":
            source = sources_from_observation(_final_fallback(run, "preconsult"),
                                              resolution="recorded_final_or_frozen_sources")
        else:
            source = sources_from_observation(None, resolution="unavailable_no_actual_generation_match")
        duplicate = next((c for c in candidates if c["task"] == "preconsult" and
            c["artifact"] == content and c["source_materials"] == source), None)
        if duplicate:
            duplicate["assessment_stages"].append(stage)
        else:
            candidates.append({"task": "preconsult", "artifact": deepcopy(content), "version": None,
                "assessment_stages": [stage], "source_materials": source})

    if "original_prescription" in artifacts:
        source = sources_from_observation(_final_fallback(run, "prescription_reference"),
                                          resolution="recorded_final_or_frozen_sources")
        candidates.append({"task": "prescription_reference", "artifact": deepcopy(artifacts["original_prescription"]),
            "version": 1, "assessment_stages": ["original_candidate_before_revision"], "source_materials": source})

    versions = artifacts.get("note_versions", [])
    attested = artifacts.get("attested_note") or {}
    cursor = -1
    saved_generation_calls = set()
    final_prefix_draft_seen = False
    for position, item in enumerate(versions):
        actions = {"review_note"} if item.get("reason") == "physician_revision" else {"document"}
        matches = _matches(run, item["content"], actions, after=cursor)
        if matches:
            cursor, call = matches[0]
            saved_generation_calls.add(cursor)
            source = _source(call)
        else:
            source = sources_from_observation(None, resolution="unavailable_no_actual_generation_match")
        stages = ["saved_draft" if position == 0 else "saved_revision"]
        if position == 0:
            stages.append("initial_draft")
            if source.get("is_final_prefix") is False:
                stages.append("initial_interim_draft")
        if actions == {"document"} and source.get("is_final_prefix") is True and not final_prefix_draft_seen:
            stages.append("first_final_prefix_draft")
            final_prefix_draft_seen = True
        is_attested = item.get("version") == attested.get("version") and item["content"] == attested.get("content")
        if is_attested:
            stages.append("attested_final")
        elif position == len(versions)-1:
            stages.append("latest_unattested")
            if run.get("status") != "completed":
                stages.append("failed_run_last_saved_note")
        if source.get("clinical_disposition"):
            stages.append("clinical_disposition_note")
        candidates.append({"task": "documentation", "artifact": deepcopy(item["content"]),
            "version": item.get("version"), "assessment_stages": stages, "source_materials": source})
    for index, call in enumerate(run.get("calls", [])):
        observation, response = call.get("observation") or {}, call.get("response") or {}
        action, content = observation.get("action"), response.get("content")
        if (index in saved_generation_calls or call.get("status") in {"invalid_schema", "failed"}
                or not isinstance(content, dict) or not isinstance(content.get("text"), str)
                or action not in {"document", "review_note"}
                or (action == "review_note" and response.get("decision") != "revise")):
            continue
        if not versions and attested.get("content") == content:
            continue # Legacy attested envelopes lack a version list; not evidence of an unsaved draft.
        # A generated draft can exist even when native save fails; a last
        # proposed revision can remain unsaved at the fixed review boundary.
        # Neither is an attested final artifact or an inferred qualification.
        source = _source(call)
        stages = ["unsaved_generated_draft" if action == "document" else "unsaved_proposed_revision", "unattested"]
        if source.get("clinical_disposition"):
            stages.append("clinical_disposition_note")
        candidates.append({"task": "documentation", "artifact": deepcopy(content), "version": None,
            "generation_call_sequence": call.get("sequence", index + 1),
            "assessment_stages": stages, "source_materials": source})
    if attested and not any("attested_final" in item["assessment_stages"] for item in candidates):
        # Legacy envelopes without per-version records can still supply final
        # frozen sources; this fallback is never used for an early draft.
        source = sources_from_observation(_final_fallback(run, "documentation"),
                resolution="recorded_final_or_frozen_sources")
        candidates.append({"task": "documentation", "artifact": deepcopy(attested["content"]),
            "version": attested.get("version"), "assessment_stages": ["attested_final"] +
                (["clinical_disposition_note"] if source.get("clinical_disposition") else []),
            "source_materials": source})
    return candidates
