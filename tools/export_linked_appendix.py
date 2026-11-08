"""Export a finished linked batch without publishing clinical text or internal IDs.

Standard-library only; no model/network/database mutations. Unselected JSON values
are streamed past, so full conversations, cases and backend bodies are never
materialized. The private crosswalk is separate from the public output directory.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

VERSION = "twinhospital.appendix-export/6"
S = object()
TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:/+-]{0,159}$")
HASH = re.compile(r"^[a-f0-9]{64}$")
UTC_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:\+00:00|Z)$")


class ProjectionReader:
    """Small streaming JSON projection; None skips a value without retaining it."""

    def __init__(self, stream, chunk_size=65536):
        self.stream, self.chunk_size = stream, chunk_size
        self.buf, self.pos, self.eof = "", 0, False

    def peek(self):
        if self.pos >= len(self.buf) and not self.eof:
            self.buf = self.stream.read(self.chunk_size)
            self.pos = 0
            self.eof = not self.buf
        return self.buf[self.pos:self.pos + 1]

    def take(self, expected=None):
        char = self.peek()
        if not char or (expected is not None and char != expected):
            raise ValueError("Malformed JSON in private evidence")
        self.pos += 1
        return char

    def ws(self):
        while self.peek() and self.peek().isspace():
            self.pos += 1

    def string(self, capture):
        self.take('"')
        parts, size = [], 0
        if capture:
            parts.append('"')
        while self.peek():
            match = re.search(r'["\\]', self.buf[self.pos:])
            end = self.pos + match.start() if match else len(self.buf)
            if capture:
                piece = self.buf[self.pos:end]
                size += len(piece)
                if size > 8192:
                    raise ValueError("Selected metadata string is unexpectedly large")
                parts.append(piece)
            self.pos = end
            char = self.take() if self.peek() else ""
            if char == '"':
                if capture:
                    parts.append(char)
                    return json.loads("".join(parts))
                return None
            if char == "\\":
                escaped = self.take()
                if capture:
                    parts.extend((char, escaped))
            elif char:
                # End of a buffer may be followed by ordinary content.
                if capture:
                    parts.append(char)
        raise ValueError("Unterminated JSON string")

    def value(self, schema, depth=0):
        if depth > 150:
            raise ValueError("Unexpected JSON nesting")
        self.ws()
        char = self.peek()
        if char == "{":
            if schema is S or isinstance(schema, list):
                raise ValueError("Unexpected object in selected scalar metadata")
            self.take()
            result = {} if schema is not None else None
            self.ws()
            if self.peek() != "}":
                while True:
                    self.ws()
                    key = self.string(True)
                    self.ws()
                    self.take(":")
                    child = schema.get(key, schema.get("*")) if isinstance(schema, dict) else None
                    item = self.value(child, depth + 1)
                    if child is not None:
                        result[key] = item
                    self.ws()
                    if self.peek() == "}":
                        break
                    self.take(",")
            self.take("}")
            return result
        if char == "[":
            if schema is S or isinstance(schema, dict):
                raise ValueError("Unexpected array in selected scalar metadata")
            self.take()
            child = schema[0] if isinstance(schema, list) else None
            result = [] if schema is not None else None
            self.ws()
            if self.peek() != "]":
                while True:
                    item = self.value(child, depth + 1)
                    if schema is not None:
                        result.append(item)
                    self.ws()
                    if self.peek() == "]":
                        break
                    self.take(",")
            self.take("]")
            return result
        if char == '"':
            return self.string(schema is not None)
        pieces = []
        while self.peek() and self.peek() not in ",]} \r\n\t":
            pieces.append(self.take())
            if len(pieces) > 128:
                raise ValueError("Malformed JSON primitive")
        parsed = json.loads("".join(pieces))
        return parsed if schema is not None else None


def project(path, schema):
    with Path(path).open(encoding="utf-8") as stream:
        reader = ProjectionReader(stream)
        result = reader.value(schema)
        reader.ws()
        if reader.peek():
            raise ValueError("Trailing JSON content")
        return result


def fields(*names):
    return {name: S for name in names}


USAGE = {**fields("prompt_tokens", "completion_tokens", "total_tokens"),
         "completion_tokens_details": fields("reasoning_tokens"),
         "prompt_tokens_details": fields("cached_tokens")}
CONTENT_BOUNDARY_POLICY = fields('version','model_family','required_finish_reason',
    'required_separator_count','final_shape','edit_final_content','infer_missing_final_content','retry_model')
CONTENT_BOUNDARY = {**fields('applied','original_content_sha256','final_content_sha256',
    'removed_prefix_characters'), 'policy':CONTENT_BOUNDARY_POLICY}
CONDITION = fields("task", "workflow", "model_family", "config_id")
GENERATION = {**fields("thinking", "reasoning_effort", "max_completion_tokens", "temperature", "completion_limit_field",
    "context_window_tokens", "completion_limit_includes_reasoning"),
    "request_parameters": {"thinking": fields("type"), "enable_thinking": S,
        "chat_template_kwargs": fields("thinking")},
    "truncation_retry": fields("max_retries", "max_completion_tokens", "same_messages", "shares_max_http_attempts")}
PARSING_POLICY = {**fields("version", "required_finish_reason", "top_level", "max_terminal_delimiters",
    "edit_keys_values_strings_commas", "retry_model", "reject_duplicate_keys", "reject_nonfinite_constants",
    "decoded_string_characters_changed"),
    "string_whitespace_encoding_only":[S],
    "allowed_operations":[S]}
RESPONSE_PARSE = {**fields("finish_reason", "original_text_sha256", "parser_input_sha256",
    "normalized_text_sha256", "normalization_performed"), "policy":PARSING_POLICY,
    "original_parse_error":fields("type", "line", "column", "position"),
    "operations":[fields("operation", "characters", "count")]}
DISPOSITION_POLICY = fields("version", "continue_documentation", "three_task_eligible",
    "no_drug_settlement", "preserve_original_disposition", "documentation_outcome_independent")
DISPOSITION = fields("status", "clinical_appropriateness", "three_task_eligible", "drug_settlement_attempted")
DOCUMENT_OUTCOME = fields("status", "input_hash", "note_hash", "clinical_quality",
    "qualifies_complete_three_task_chain", "drug_settlement_attempted")
SPEC = {**fields("batch", "mode", "business_protocol_version", "catalog_hash", "memory_policy",
                  "clinical_evaluation", "formal_experiment", "data_policy_hash", "diagnosis_catalog_hash"),
        "diagnosis_code_systems": [S],
        # Full policy prose and references stay in the frozen source. Public
        # metadata keeps only the version and safety properties; do not weaken
        # safe() to admit arbitrary text from a private specification.
        "diagnosis_code_policy": fields('version','clinical_inference','source_response_preserved'),
        'controlled_prescription_variant_policy': fields('version','kind','engineering_only','formal_eligible',
            'registry_hash','item_index','new_route','source_consultation_modified','patient_facts_modified',
            'same_variant_for_all_five_prescription_conditions','counts_as_new_patient',
            'all_category_normality_established','six_category_coverage_complete'),
        "execution_policy": {**fields("workers", "dispatch_unit", "dispatch_order", "admission_policy", "operator_interruption_policy",
            "formal_observation_window_seconds", "formal_timeout_assessment"),
            "formal_submission_window":fields("opens_at","closes_at","unadmitted_cases")},
        "workflow_observation_policy": {**fields("version","status","frozen_at","window_seconds","clock",
            "start_boundary","end_boundary","workers","admission_policy","denominator","late_outcomes",
            "clinical_reference_scope"), "constraints":[S],
            "submission_window":fields("opens_at","closes_at","unadmitted_cases")},
        "pricing_cny_per_million_tokens": {"*":[S]},
        "cases": [{**fields("case_id", "data_kind", "split", "file_sha256", "case_hash", "runtime_input_hash")}],
        "models": {"role_model": S, "tool_models": {"*": S}},
        'transport_policy':{**fields('read_timeout_seconds','connect_timeout_seconds','response_transport','stream_max_duration_seconds',
            'minimum_request_interval_seconds','tls_verification','follow_redirects'),
            'network_route':fields('mode')},
        "budget_ledger": fields("resolved_path"), "code_hashes": {"*": S},
        "rules": {**fields("engine", "version", "engineering_only", "development_only", "independent_reference_status",
            "registry_hash", "product_mapping_manifest_hash", "source_catalogue_file_sha256", "source_catalogue_hash"),
            "shared_context_policy": fields("version", "development_only", "clinical_reference_status",
                "reads_hidden_patient_or_evaluator_data", "natural_language_extraction", "current_exposure_inferred_from_historical_orders",
                "missing_current_medication_or_allergy_means_negative", "approximate_age_promoted_to_exact", "complete_index_visibility",
                "shared_conditions", "timestamps_added", "order_exposure_requires_literal_status_interval_intent_and_time_basis")},
        "conditions": [CONDITION],
        "generation_policy": {"version":S,"models":{"*":GENERATION},
            "role": GENERATION, "tools": {"*": GENERATION}, "response_parsing":PARSING_POLICY,
            'provider_content_boundary':CONTENT_BOUNDARY_POLICY,
            'model_parameter_overrides':{'*':fields('reasoning_effort','thinking','temperature','completion_limit_field')},
            'supported_role_families':[S]},
        "response_contract_policy": {"*": S},
        "clinical_disposition_policy": DISPOSITION_POLICY,
        "history_access_policy": {**fields("chart_policy", "initial_byte_limit", "page_byte_limit", "visible_history_byte_limit",
            "queries_per_physician_phase", "extra_reviewer_queries"),
            "clinical_projection": fields("version", "source_record_selection_changed", "clinical_values_changed", "context_budget_changed")},
        "anchor_reuse": {**fields("policy", "engineering_only", "source_generation_version", "source_specification_sha256",
                                     "new_anchor_execution", "source_anchors_counted_as_new_runs", "paired_task_conditions_per_case", "clinical_quality_of_source"),
            "cases": [{**fields("case_id", "runtime_input_hash", "source_run_file_sha256", "source_run_canonical_hash", "source_index_sha256"),
                       "stage_input_hashes": {"*": S}}]},
        "generation": fields("temperature", "max_completion_tokens", "max_attempts", "max_calls_per_run")}
CALL = {**fields("sequence", "role", "phase", "model_family", "model_name", "memory_policy",
                 "request_id", "status", "wall_seconds", "observation_hash", "response_hash"), "usage": USAGE,
        "response_parse":RESPONSE_PARSE}
EVENT = fields("sequence", "event", "operation", "role", "phase", "status", "model_family",
               "error_type", "payload_hash", "response_hash", "query_hash", "wall_seconds",
               "original_response_hash", "canonical_response_hash", "clinical_values_changed", "call_sequence",
               "readback_verified", "native_effect", "protection_source", "native_write_attempted",
               "evidence_index_hash", "structured_context_hash", "source_catalogue_hash", "policy_version",
               "lookup_number", "section", "returned_count", "accepted_into_context", "result_status",
               "retained_context_bytes", "remaining_visible_bytes", "raw_page_bytes", "projected_page_bytes",
               "raw_page_hash", "projected_page_hash", "projection_version", "next_offset", "matching_count")
EVENT.update({"already_visible_record_refs":[S], "new_record_refs":[S]})
RUN = {**fields("run_id", "case_id", "input_hash", "stage_input_hash", "controlled_variant_hash", "config_id", "status", "evaluation_unit"),
       "calls": [CALL]}
TRACE = {**fields("schema_version", "status", "evaluation_unit", "native_complete", "call_count",
                   "wall_seconds", "timing_source", "error_type", "failure_stage"),
         "clinical_quality": fields("status"), "artifact_hashes": {"*": S}, "events": [EVENT]}
TRACE.update({"clinical_disposition_policy":DISPOSITION_POLICY, "clinical_disposition":DISPOSITION,
              "disposition_documentation_outcome":DOCUMENT_OUTCOME})
PROVIDER = {**fields("request_id", "run_id", "recorded_at", "role", "model", "returned_model", "attempt",
                      "reserved_upper_cny", "accounted_upper_cny", "http_status", "outcome", "error_type",
                      "retry_planned", "elapsed_seconds", "finish_reason", "effective_max_completion_tokens", "completion_limit_field",
                      "truncation_retry_index", "failure_reason_code", "retry_reason", "provider_error_code",
                      "api_dispatch_wait_seconds", "minimum_request_interval_seconds",
                      'connect_timeout_seconds','read_timeout_seconds','transport_timeout_indicated','response_transport'),
            'stream_observation':fields('version','data_events','done_received','first_event_seconds',
                'first_content_seconds','max_duration_seconds','partial_content_used','received_data_sha256',
                'final_content_characters','private_reasoning_characters'),
            'network_route':fields('mode'), "usage": USAGE,
            'content_boundary':CONTENT_BOUNDARY}
INDEX = {"case_id": S, "attempts": {"*": fields("status", "native_complete", "call_count", "config_id",
                                               "error_type", "evaluation_unit", "result_path", "dependency")}}


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def validate_disposition_policy(value):
    if value is None:
        return
    expected = {"version":"clinical_incomplete_decision_note_v1", "continue_documentation":True,
        "three_task_eligible":False, "no_drug_settlement":True, "preserve_original_disposition":True,
        "documentation_outcome_independent":True}
    if value != expected or any(type(value[k]) is not type(v) for k,v in expected.items()):
        raise ValueError("Unknown clinical disposition policy metadata")


def safe_trace_metadata(value):
    """Closed clinical-state enums and numeric/hash evidence only; no chart IDs."""
    validate_disposition_policy(value.get('clinical_disposition_policy'))
    disposition = value.get('clinical_disposition')
    if disposition is not None:
        clinical_state=disposition.get('clinical_appropriateness')
        if clinical_state not in {'pending_independent_review','not_assessed_outside_programmatic_scope'}:
            raise ValueError('Invalid clinical assessment scope')
        expected={'status':'incomplete_clinical_decision','clinical_appropriateness':clinical_state,
            'three_task_eligible':False,'drug_settlement_attempted':False}
        if disposition!=expected or any(type(disposition[k])is not type(v) for k,v in expected.items()):
            raise ValueError('Invalid clinical disposition metadata')
        if value.get('native_complete') is True:
            raise ValueError('A decision-only note cannot establish native complete encounter')
        value['clinical_disposition_metadata_scope'] = ('source_context_for_fixed_input_task' if
            value.get('evaluation_unit')=='fixed_input_task' else 'original_encounter_clinical_termination')
    outcome=value.get('disposition_documentation_outcome')
    if outcome:
        if outcome.get('status') not in {'not_started','attested','failed','stopped_budget','stopped_provider'}:
            raise ValueError('Invalid decision documentation status')
        if (outcome.get('qualifies_complete_three_task_chain') is not False or
                outcome.get('drug_settlement_attempted') is not False):
            raise ValueError('Decision documentation cannot qualify as a complete three-task chain')
        for key in ('input_hash','note_hash'):
            if key in outcome and (not isinstance(outcome[key],str) or not HASH.fullmatch(outcome[key])):
                raise ValueError('Invalid decision documentation hash')
        if outcome.get('status')=='attested' and 'note_hash' not in outcome:
            raise ValueError('Attested decision note lacks its hash')
        if outcome.get('clinical_quality') not in {None,'pending_independent_review','not_assessed_outside_programmatic_scope'}:
            raise ValueError('Invalid decision documentation quality state')
    for event in value.get('events',[]):
        for field in ('already_visible_record_refs','new_record_refs'):
            refs=event.pop(field,None)
            if refs is not None:
                if not isinstance(refs,list) or len(refs)>20 or not all(isinstance(x,str) for x in refs):
                    raise ValueError('Invalid bounded history reference count')
                event[field.replace('_refs','_count')]=len(refs)
        for field in ('raw_page_hash','projected_page_hash','original_response_hash','canonical_response_hash'):
            if field in event and (not isinstance(event[field],str) or not HASH.fullmatch(event[field])):
                raise ValueError('Invalid history projection hash')
        for field in ('lookup_number','returned_count','retained_context_bytes','remaining_visible_bytes',
                'raw_page_bytes','projected_page_bytes','next_offset','matching_count','call_sequence'):
            if field in event and event[field] is not None and (type(event[field])is not int or event[field]<0):
                raise ValueError('Invalid history projection count')
        if 'accepted_into_context' in event and type(event['accepted_into_context'])is not bool:
            raise ValueError('Invalid history acceptance flag')
        if 'projection_version' in event and event['projection_version'] not in {
                'clinical_field_projection_v1','clinical_field_projection_v2_supplemental_history'}:
            raise ValueError('Unknown history projection version')
        if 'section' in event and event['section'] not in {'historical_encounters','encounter_headers',
            'diagnoses','historical_orders','historical_observations','current_observations','encounter_order_refs',
            'encounter_observation_refs','allergy_record','allergy_items','current_use_record','current_use_items',
            'demographics','time_basis','historical_administrations','historical_microbiology',
            'historical_outpatient_measurements'}:
            raise ValueError('Unknown history projection section')
        if 'result_status' in event and event['result_status'] not in {
            'visible','already_visible','requires_narrower_query_for_visible_context_budget',
            'requires_narrower_query_for_projected_page_budget','not_available_before_physician_consultation','failed_read','invalid_request'}:
            raise ValueError('Unknown history projection result status')
    return safe(value)


def safe_response_parse(value):
    """Only hashes, known encoding operations and numeric error positions leave."""
    if value is None:return None
    version=value.get('policy',{}).get('version')
    if version not in {'terminal_json_delimiters_v1','terminal_json_delimiters_and_string_whitespace_v2'}:
        raise ValueError('Unknown response parsing policy')
    result={}
    for key in ('original_text_sha256','parser_input_sha256','normalized_text_sha256'):
        hashed=value.get(key)
        if hashed is not None and (not isinstance(hashed,str) or not HASH.fullmatch(hashed)):
            raise ValueError('Invalid response parsing hash')
        result[key]=hashed
    if result['original_text_sha256'] is None:raise ValueError('Missing original response hash')
    finish=value.get('finish_reason')
    if finish not in {'stop','length','tool_calls','content_filter',None}:
        raise ValueError('Invalid response parsing finish reason')
    result['finish_reason']=finish
    if type(value.get('normalization_performed')) is not bool:
        raise ValueError('Invalid response normalization status')
    result['normalization_performed']=value['normalization_performed']
    # Policy text is defined by code, never free-form response content.
    policy=value['policy']
    if (policy.get('required_finish_reason')!='stop' or policy.get('top_level')!='single_object'
            or type(policy.get('max_terminal_delimiters')) is not int or policy['max_terminal_delimiters']!=2
            or policy.get('edit_keys_values_strings_commas') is not False or policy.get('retry_model') is not False):
        raise ValueError('Invalid response parsing policy values')
    allowed=['append_missing_terminal_closers','remove_extra_terminal_closers']
    if version=='terminal_json_delimiters_and_string_whitespace_v2':
        if (policy.get('string_whitespace_encoding_only')!=['LF','CR','TAB']
                or policy.get('decoded_string_characters_changed') is not False):
            raise ValueError('Invalid string encoding policy')
        allowed.append('escape_literal_string_whitespace')
    if policy.get('allowed_operations')!=allowed:
        raise ValueError('Invalid response parsing operation policy')
    for key in ('reject_duplicate_keys','reject_nonfinite_constants'):
        if key in policy and policy[key] is not True:raise ValueError('Invalid strict JSON policy')
    result['policy']=safe(policy)
    error=value.get('original_parse_error')
    result['original_parse_error']=None
    if error is not None:
        if error.get('type') not in {'JSONDecodeError','ValueError'}:raise ValueError('Invalid parse error type')
        public_error={'type':error['type']}
        for key in ('line','column','position'):
            number=error.get(key)
            if number is not None and (type(number)is not int or number<0):raise ValueError('Invalid parse error position')
            public_error[key]=number
        result['original_parse_error']=public_error
    operations=value.get('operations',[])
    if not isinstance(operations,list) or len(operations)>1:raise ValueError('Unbounded response parsing operations')
    result['operations']=[]
    for operation in operations:
        chars=operation.get('characters');count=operation.get('count')
        if operation.get('operation')=='escape_literal_string_whitespace':
            if (version!='terminal_json_delimiters_and_string_whitespace_v2'
                    or chars is not None or type(count)is not int or not 1<=count<=4000000):
                raise ValueError('Invalid string encoding operation')
            result['operations'].append({'operation':operation['operation'],'count':count,
                'encoding_only':['LF','CR','TAB']})
            continue
        if (operation.get('operation') not in allowed or not isinstance(chars,str)
                or not re.fullmatch(r'[}\]]{1,2}',chars) or type(count)is not int or count!=len(chars)):
            raise ValueError('Invalid response parsing delimiter operation')
        result['operations'].append({'operation':operation['operation'],'count':count,
            'closer_types':['object' if char=='}' else 'array' for char in chars]})
    if bool(operations)!=result['normalization_performed']:
        raise ValueError('Response normalization status and operations disagree')
    if operations and result['normalized_text_sha256'] is None:
        raise ValueError('Missing normalized response hash')
    return result


def safe(value):
    """Selected metadata may contain codes/numbers/hashes, never arbitrary prose."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and (TOKEN.fullmatch(value) or HASH.fullmatch(value) or UTC_STAMP.fullmatch(value)):
        return value
    if isinstance(value, list):
        return [safe(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not TOKEN.fullmatch(key):
                raise ValueError("Unexpected metadata key; export stopped")
            result[key] = safe(item)
        return result
    raise ValueError("Non-code text in selected metadata; export stopped")


def public_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def private_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def write_csv(path, rows, columns):
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def token_counts(usage):
    usage = usage or {}
    return {"prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "reasoning_tokens_subset_of_completion": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")}


def export(batch_dir, output_dir, private_map_dir, *, evidence_dir=None):
    batch, output, private = map(lambda p: Path(p).resolve(), (batch_dir, output_dir, private_map_dir))
    if private == output or output in private.parents or private in output.parents:
        raise ValueError("Private crosswalk must be separate from the public output")
    if output.exists():
        raise ValueError("Output must be a new directory; preserve earlier exports")
    if not (batch / "batch_summary.json").is_file():
        raise ValueError("A finished batch_summary.json is required")
    with (batch / "batch.lock").open("r") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Batch is still running; no export made") from None
        return _export_locked(batch, output, private, evidence_dir)


def _export_locked(batch, output, private, evidence_dir):
    spec = project(batch / "specification.json", SPEC)
    validate_disposition_policy(spec.get('clinical_disposition_policy'))
    evidence = Path(evidence_dir).resolve() if evidence_dir else batch.parent.parent / "model_calls"
    if not evidence.is_dir():
        raise ValueError("Provider evidence directory missing; cannot audit retries/cost")
    expected = [] if spec.get("anchor_reuse") else [("linked_baseline_anchor", None)]
    expected += [(c["config_id"].replace(":", "__"), c) for c in spec.get("conditions", [])]
    rows, traces, crosswalk, run_map, call_map, private_calls = [], {}, [], {}, {}, []
    all_events, call_rows = [], []
    expected_private_ids = set()
    for case_number, case in enumerate(spec["cases"], 1):
        sid = f"S{case_number:04d}"
        case_id = case["case_id"]
        expected_private_ids.add(case_id)
        index_path = batch / case_id / "attempts.json"
        # Do not follow an unexpected or escaped case directory supplied in metadata.
        if batch not in index_path.resolve().parents:
            raise ValueError("Case index escapes batch")
        index = project(index_path, INDEX) if index_path.exists() else {"attempts": {}}
        if index.get("case_id", case_id) != case_id:
            raise ValueError("Case index identity mismatch")
        if set(index["attempts"]) - {label for label, _ in expected}:
            raise ValueError("Indexed attempt absent from frozen specification")
        for attempt_number, (label, condition) in enumerate(expected, 1):
            aid = f"{sid}-A{attempt_number:02d}"
            record = index["attempts"].get(label, {"status": "not_run_no_indexed_attempt"})
            row = {"study_case": sid, "study_attempt": aid,
                   "condition": safe(condition or {"config_id": "linked_baseline_anchor"}),
                   "status": safe(record["status"]), "error_type": safe(record.get("error_type")),
                   "dependency": safe(record.get("dependency")), "observed": bool(record.get("result_path")),
                   "clinical_quality": "not_reported", "core_native_complete": None,
                   "call_count": None, "wall_seconds": None}
            cross = {"study_case": sid, "study_attempt": aid, "case_id": case_id, "attempt_label": label,
                     "run_id": None, "result_path": record.get("result_path")}
            if record.get("result_path"):
                path = Path(record["result_path"]).resolve()
                if batch not in path.parents or path.name != "run.json":
                    raise ValueError("Run result escapes batch")
                run = project(path, RUN)
                trace = safe_trace_metadata(project(path.with_name("appendix_trace.json"), TRACE))
                if run.get("case_id") != case_id or run.get("status") != record["status"]:
                    raise ValueError("Run identity or final status mismatch")
                if run["run_id"] in run_map:
                    raise ValueError("Duplicate run identity")
                run_map[run["run_id"]] = aid
                cross["run_id"] = run["run_id"]
                expected_private_ids.add(run["run_id"])
                row.update(status=trace["status"], core_native_complete=trace["native_complete"],
                           call_count=trace["call_count"], wall_seconds=trace["wall_seconds"],
                           evaluation_unit=trace["evaluation_unit"],
                           clinical_quality=trace.get("clinical_quality", {}).get("status", "not_reported"))
                row['clinical_disposition_status']=(trace.get('clinical_disposition') or {}).get('status')
                row['disposition_documentation_status']=(trace.get('disposition_documentation_outcome') or {}).get('status')
                trace.update(study_case=sid, study_attempt=aid, condition=row["condition"],
                             input_hash=safe(run.get("input_hash")), stage_input_hash=safe(run.get("stage_input_hash")),
                             input_hash_scope="runtime_case_and_optional_frozen_stage_input",
                             clinical_quality=trace.get("clinical_quality", {"status": "not_reported"}))
                if run.get('controlled_variant_hash') is not None:
                    trace['controlled_variant_hash']=safe(run['controlled_variant_hash'])
                trace["core_native_complete"] = trace.pop("native_complete")
                trace["model_calls"] = []
                for call_number, call in enumerate(run.get("calls", []), 1):
                    cid = f"{aid}-C{call_number:03d}"
                    request_id = call.pop("request_id", None)
                    if 'response_parse' in call:
                        call['response_parse']=safe_response_parse(call['response_parse'])
                    call_public = safe(call)
                    call_public.update(study_call=cid, study_attempt=aid, **token_counts(call_public.pop("usage", {})))
                    call_public["hash_scope"] = "actual_model_visible_observation_and_returned_response"
                    if request_id:
                        if request_id in call_map:
                            raise ValueError("Provider request linked to multiple calls")
                        call_map[request_id] = cid
                        expected_private_ids.add(request_id)
                    trace["model_calls"].append(call_public)
                    call_rows.append(call_public)
                all_events.extend(trace["events"])
                traces[aid] = trace
            else:
                traces[aid] = {**row, "events": [], "model_calls": [],
                               "execution_evidence": "no_indexed_final_run_available"}
            rows.append(row)
            crosswalk.append(cross)
    provider, unindexed_provider = [], []
    for path in sorted(evidence.glob("*.json")):
        item = project(path, PROVIDER)
        if item.get("run_id") in run_map:
            provider.append(item)
        elif isinstance(item.get("run_id"), str) and item["run_id"].startswith(spec["batch"] + ":"):
            # A driver failure may leave charged requests without a final indexed
            # run. Do not silently describe the indexed subtotal as complete.
            unindexed_provider.append({k: item.get(k) for k in ("run_id", "request_id")})
    provider.sort(key=lambda x: (run_map[x["run_id"]], x.get("recorded_at", ""), x["request_id"]))
    ledger_path = Path(spec["budget_ledger"]["resolved_path"]).resolve()
    connection = sqlite3.connect(ledger_path.as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    requests, seen = [], set()
    try:
        for number, item in enumerate(provider, 1):
            rid = item["request_id"]
            if rid in seen:
                raise ValueError("Duplicate provider request evidence")
            seen.add(rid)
            aid = run_map[item["run_id"]]
            public_id = f"REQUEST-{number:06d}"
            ledger = connection.execute("SELECT reserved,charged,status FROM calls WHERE request_id=?", (rid,)).fetchone()
            request = safe({k: v for k, v in item.items() if k not in {
                "request_id", "run_id", "recorded_at", "usage"}})
            request.update(study_request=public_id, study_attempt=aid, study_call=call_map.get(rid),
                           **token_counts(item.get("usage")))
            request["call_link_status"] = "final_request_id_verified" if rid in call_map else "run_verified_request_only"
            request["ledger_status"] = safe(ledger[2]) if ledger else "missing"
            request["ledger_reserved_upper_cny"] = ledger[0] / 1_000_000 if ledger else None
            request["ledger_accounted_upper_cny"] = ledger[1] / 1_000_000 if ledger else None
            request["charge_uncertain"] = ledger is None or ledger[2] in {"reserved", "unknown_charge"}
            requests.append(request)
            private_calls.append({"study_request": public_id, "study_call": call_map.get(rid),
                                  "study_attempt": aid, "request_id": rid, "run_id": item["run_id"]})
            expected_private_ids.add(rid)
    finally:
        connection.close()
    missing = set(call_map) - seen
    evidence_counts = Counter()
    for event in all_events:
        if event.get("event") != "business_operation":
            continue
        verified, native = event.get("readback_verified"), event.get("native_effect")
        evidence_counts["native_effect_with_verified_readback" if verified is True and native is True else
                        "gateway_or_sidecar_verified" if verified is True and native is False else
                        "unverified_or_unknown_effect"] += 1
    status_counts = dict(Counter(row["status"] for row in rows))
    observed_core = [row for row in rows if row.get("evaluation_unit") == "complete_encounter"]
    summary = {"schema_version": VERSION,
               "experiment_class": "formal" if spec.get("formal_experiment") is True else
                                   "engineering" if spec.get("formal_experiment") is False else "unspecified",
               "formal_experiment": spec.get("formal_experiment"), "study_cases": len(spec["cases"]),
               "planned_attempts": len(rows), "observed_final_runs": sum(row["observed"] for row in rows),
               "attempt_status_counts": status_counts, "complete_encounter_runs": len(observed_core),
               "core_native_complete_encounters": sum(row["core_native_complete"] is True for row in observed_core),
               "linked_clinical_disposition_status_counts":dict(Counter(row['clinical_disposition_status'] for row in observed_core if row.get('clinical_disposition_status'))),
               "fixed_task_clinical_disposition_source_count":sum(row.get('evaluation_unit')=='fixed_input_task' and bool(row.get('clinical_disposition_status')) for row in rows),
               "disposition_documentation_status_counts":dict(Counter(row['disposition_documentation_status'] for row in rows if row.get('disposition_documentation_status'))),
               "decision_note_completion_qualifies_as_full_three_task_completion":False,
               "recorded_role_calls": len(call_rows), "provider_http_attempts": len(requests),
               "provider_attempt_number_gt_one": sum((row.get("attempt") or 0) > 1 for row in requests),
               "provider_retries_planned": sum(row.get("retry_planned") is True for row in requests),
               "provider_dispatch_wait_seconds": round(sum(row.get("api_dispatch_wait_seconds") or 0 for row in requests),6),
               "provider_dispatch_wait_observed_requests":sum(row.get("api_dispatch_wait_seconds") is not None for row in requests),
               "provider_http_elapsed_seconds":round(sum(row.get("elapsed_seconds") or 0 for row in requests),6),
               'provider_explicit_final_boundaries':sum((row.get('content_boundary') or {}).get('applied') is True for row in requests),
               "provider_completion_length_failures": sum(row.get("failure_reason_code") == "completion_length_limit" for row in requests),
               "provider_increased_completion_limit_attempts": sum((row.get("truncation_retry_index") or 0) > 0 for row in requests),
               "missing_provider_evidence_for_final_call": len(missing),
               "unindexed_provider_attempts_matching_batch_prefix": len(unindexed_provider),
               "uncertain_charge_requests": sum(row["charge_uncertain"] for row in requests),
               "batch_ledger_accounted_upper_cny": round(sum(row["ledger_accounted_upper_cny"] or 0 for row in requests), 6),
               "request_cost_index_complete": not missing and not unindexed_provider and not any(row["ledger_status"] == "missing" for row in requests),
               "cost_is_complete": not missing and not unindexed_provider and not any(row["charge_uncertain"] for row in requests),
               "billing_basis": "conservative_no_discount_upper_estimate_not_provider_invoice",
               "cost_scope": "only_provider_requests_matching_this_batch_run_ids_including_retries",
               "business_evidence_counts": dict(evidence_counts),
               "clinical_evaluation": safe(spec.get("clinical_evaluation")),
               "evaluation_protocol": safe(spec.get("evaluation_protocol", "legacy_protocol")),
               "external_human_review_required": spec.get("external_human_review_required"),
               "clinical_quality_claim": "no_unrestricted_clinical_appropriateness_claim",
               "timing_distinction": "Provider HTTP duration and dispatch queue delay are separate. Role-call wall time may include both. None is measured human work time.",
               "evidence_boundary": "core_native_business_readback_plus_gateway_queue_and_signatory_policy_not_native_only_safety"}
    manifest = {"schema_version": VERSION, "source_specification_sha256": sha(batch / "specification.json"),
                "source_batch_summary_sha256": sha(batch / "batch_summary.json"),
                "exporter_sha256": sha(__file__), "source_versions_from": "frozen_batch_specification",
                "specification": safe({k: v for k, v in spec.items() if k not in {"cases", "budget_ledger", "batch", "anchor_reuse"}}),
                "study_inputs": [{"study_case": f"S{i:04d}", **safe({k: v for k, v in case.items() if k != "case_id"})}
                                 for i, case in enumerate(spec["cases"], 1)]}
    if spec.get("anchor_reuse"):
        reuse = spec["anchor_reuse"]
        study_ids = {case["case_id"]: f"S{i:04d}" for i, case in enumerate(spec["cases"], 1)}
        manifest["source_anchor_reuse"] = {**safe({k: v for k, v in reuse.items() if k != "cases"}),
            "sources": [{"study_case": study_ids[row["case_id"]], **safe({k: v for k, v in row.items() if k != "case_id"})}
                        for row in reuse["cases"]]}
        summary["source_anchors_counted_as_new_runs"] = False
    # Serialize and check exact internal identifiers before creating any public file.
    public_objects = [manifest, summary, rows, requests, *traces.values()]
    serialized = json.dumps(public_objects, ensure_ascii=False)
    for identifier in expected_private_ids:
        if len(identifier) >= 6 and identifier in serialized:
            raise ValueError("Internal identifier found in public projection")
    output.mkdir(parents=True, exist_ok=False)
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_path = private / (output.parent.name + "-" + sha(batch / "specification.json")[:12] + ".json")
    private_payload = {"schema_version": VERSION, "source_batch": str(batch), "public_bundle": str(output),
                       "attempts": crosswalk, "requests": private_calls,
                       "final_call_requests_without_provider_file": sorted(missing),
                       "unindexed_provider_attempts": unindexed_provider}
    if private_path.exists() and json.loads(private_path.read_text()) != private_payload:
        # A second public reproduction may use a new output directory. Never overwrite a crosswalk.
        private_path = private / (private_path.stem + "-" + hashlib.sha256(str(output).encode()).hexdigest()[:10] + ".json")
    if private_path.exists():
        if json.loads(private_path.read_text()) != private_payload:
            raise ValueError("Conflicting private crosswalk; earlier mapping preserved")
        private_path.chmod(0o600)
    else:
        private_json(private_path, private_payload)
    public_json(output / "manifest.json", manifest)
    public_json(output / "summary.json", summary)
    for aid, trace in traces.items():
        public_json(output / "traces" / (aid + ".json"), trace)
    flat_rows = [{**row, "config_id": row["condition"]["config_id"]} for row in rows]
    write_csv(output / "attempts.csv", flat_rows, ["study_case", "study_attempt", "config_id", "status",
              "observed", "evaluation_unit", "core_native_complete", "clinical_quality", "call_count",
              "wall_seconds", "error_type", "dependency"])
    write_csv(output / "model_calls.csv", call_rows, ["study_attempt", "study_call", "role", "phase", "status",
              "model_family", "model_name", "memory_policy", "wall_seconds", "observation_hash", "response_hash",
              "prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens_subset_of_completion"])
    write_csv(output / "request_costs.csv", requests, ["study_attempt", "study_call", "study_request", "role",
              "model", "returned_model", "attempt", "outcome", "http_status", "provider_error_code", "error_type", "retry_planned",
              "finish_reason", "effective_max_completion_tokens", "completion_limit_field", "truncation_retry_index", "failure_reason_code", "retry_reason",
              "elapsed_seconds", "api_dispatch_wait_seconds", "minimum_request_interval_seconds",
              'connect_timeout_seconds','read_timeout_seconds','transport_timeout_indicated',
              "prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens_subset_of_completion",
              "ledger_status", "ledger_reserved_upper_cny", "ledger_accounted_upper_cny", "charge_uncertain", "call_link_status"])
    public_json(output / "request_index.json", requests)
    (output / "README.md").write_text(
        "# TwinHospital appendix evidence\n\n"
        "本包仅记录该已结束批次的实际运行证据。工程或正式属性见 summary.json；工程试跑不能作为正式临床有效性结果。\n\n"
        "- attempts.csv：所有计划条件、完成/失败/未执行状态；缺失记录不算成功。\n"
        "- traces/：研究编号下的业务事件、读回、角色调用与输入/输出哈希。哈希用于私有原始记录复核，不提供患者文本。\n"
        "- model_calls.csv：实际角色及工具模型路由、阶段、tokens；输入/输出指实际模型可见上下文和返回值。\n"
        "- request_costs.csv / request_index.json：每次 HTTP 尝试及费用，包含重试；无可靠逻辑调用关联时明确留空。\n"
        "- manifest.json：本批冻结规格中的代码、模型、输入及目录版本；不是导出时工作树版本。\n\n"
        "core_native_complete 只表示核心原生业务读回完成。队列、授权人员登记及签发人员策略来自网关/持久化 sidecar，不能解释为原生 HIS 本体安全。"
        "业务读回未知不等于无副作用。本包只输出声明范围内的程序核验，不生成真人评审或全面临床正确性分数。\n\n"
        "API wall time、模拟角色工作量和真人时间必须分开。reasoning_tokens 是 completion_tokens 的子集，不重复加总。"
        "费用是按冻结价率和保守计费账本汇总的上界估算，不是服务商账单；unknown_charge 保留不确定性。\n\n"
        "患者对话、病史、原生标识、来源编号、内部 run/case/request ID 和密钥未进入公开包。"
        "私有对照表在单独的受限目录，不能随本包发布。\n\n"
        "复跑：在原仓库运行 `python tools/export_linked_appendix.py --batch-dir <已结束批次> "
        "--output-dir <新的公开目录> --private-map-dir <独立私有目录>`。工具只读本地记录和 SQLite 账本，不调用模型。\n",
        encoding="utf-8")
    checksums = {str(path.relative_to(output)): sha(path) for path in sorted(output.rglob("*")) if path.is_file()}
    public_json(output / "checksums.json", checksums)
    return {"public_bundle": str(output), "private_map": str(private_path), "summary": summary}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--private-map-dir", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    result = export(args.batch_dir, args.output_dir, args.private_map_dir, evidence_dir=args.evidence_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
