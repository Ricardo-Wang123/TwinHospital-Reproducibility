# Repeat the same encounters through FHIR R4 and WS/T

Use one frozen input bundle and one assignment plan for both protocol arms. The
plan selects exact case/configuration pairs from an earlier audited run. It does
not add cases or expand a partial cohort into a complete factorial. The same
source case remains the same source patient in both arms.

| Shared inputs | Separate execution artifacts |
| --- | --- |
| Case JSON files and source-patient keys | Batch name and run directory |
| Assignment plan and clinical configurations | Agent contexts and model responses |
| Product and diagnosis mappings, rules and prompts | Patient, encounter, order, note and payment identifiers |
| Role/tool models and generation settings | Protocol wire records and native observations |
| Activity weights and workflow limits | Audit, operation probes and result tables |

Prepare the input bundle and provider using [the execution guide](REPRODUCE.md).
Keep the authorized case files and API key outside the checkout. The
[DS/GLM profile template](../config/provider.ds-glm-20261111.example.json) records
the model-specific request settings used by the protocol runtime; configure your
own endpoint, access key and actual rates.

`provider_content_boundary` describes the original endpoint's GLM output framing.
Omit it if your endpoint already returns a clean final `content` channel. Record
the resulting provider settings before execution and use them in both arms.

## 1. Select the exact assignments

Set `ASSIGNMENTS` to a private JSON file with this shape:

```json
{
  "version": "twinhospital.same-assignment-rerun/1",
  "planned_assignments": 1,
  "source_case_count": 1,
  "assignments": [{
    "case_id": "fictional-case-id",
    "group": "type2_diabetes",
    "profile": "linked_baseline_anchor",
    "config_id": "copy-the-original-audited-config-id",
    "source_trace_sha256": "copy-the-original-64-character-trace-sha256"
  }]
}
```

This is a format illustration. Export the real values from your prior audited
specification and traces. Each selected pair must be unique; its case, disease
stratum and configuration must match the frozen inputs. Every source case in the
bundle must occur in the plan. The linked cohort builder also checks the source
count per stratum. For a new dataset with all 27 combinations, use
`--mode comparison` without `--quick` instead of creating historical attribution.

## 2. Configure two independent arms

Run from the repository root. The first command configures FHIR R4; the second
configures the SOAP-only WS/T service profile.

```sh
.venv/bin/python -m handoff.inputs configure \
  --bundle "$FROZEN_BUNDLE" --assets "$OPENHIS" --provider "$PROVIDER" \
  --mode repeat --assignment-plan "$ASSIGNMENTS" \
  --his-interface fhir-r4 --workers 20 \
  --batch protocol_fhir_01 --output "$WORK/protocol_fhir_01"

.venv/bin/python -m handoff.inputs configure \
  --bundle "$FROZEN_BUNDLE" --assets "$OPENHIS" --provider "$PROVIDER" \
  --mode repeat --assignment-plan "$ASSIGNMENTS" \
  --his-interface wst-846 --workers 20 \
  --batch protocol_wst_01 --output "$WORK/protocol_wst_01"
```

The provider must set a shared request interval of at least one second when
using more than five workers. Configure both arms before launching either one.
Use the same protected processing record for the same endpoint and data scope.

## 3. Execute FHIR first, then WS/T

For the first arm:

```sh
RUN="$WORK/protocol_fhir_01/run.local.json"
.venv/bin/python -m handoff.run --config "$RUN" provision
.venv/bin/python -m handoff.run --config "$RUN" set-key
.venv/bin/python -m handoff.run --config "$RUN" check
.venv/bin/python -m handoff.run --config "$RUN" run --execute
.venv/bin/python -m handoff.run --config "$RUN" audit --observe-native
.venv/bin/python -m handoff.run --config "$RUN" operations
.venv/bin/python -m handoff.run --config "$RUN" report
```

Then repeat those commands with
`RUN="$WORK/protocol_wst_01/run.local.json"`. The isolated HIS may be reused
sequentially after closing the first observation session; all encounter objects
are created afresh. Do not run two batch observation sessions against one
instance simultaneously. See [the exact protocol coverage](HIS_INTERFACE.md):
WS/T national services and project workflow extensions are identified separately.

## 4. Read and compare results

Each arm writes its own `reader_results/REPORT.md` and `summary.json`. Full
workflow completion requires the native audit to verify the signed medication
order, accepted locked note, matching bill and amount, and unique simulated cash
payment. A pending review or deferred prescription is not a completed encounter.
Payment verification is a workflow measure; the clinical checks retain their
declared reference scope.

```sh
.venv/bin/python tools/compare_protocol_runs.py \
  --fhir-run "$WORK/protocol_fhir_01/run.local.json" \
  --wst-run "$WORK/protocol_wst_01/run.local.json" \
  --output "$WORK/protocol_comparison.json"
```

The comparison verifies shared inputs and clinical settings, distinct business
identifiers, and separate outcome denominators. Join observations by source case
and configuration. Independently generated trajectories can differ even when the
transport mapping is equivalent.

If the shared workflow logic changes, preserve the previous batch and create
fresh versions of both arms. Do not replace a failed assignment with a different
case or merge successes from different rule versions into one final batch.

## 5. Transfer recorded evidence to an authorized workspace

Package each arm separately after its audit and operation replay. For the FHIR
example above:

```sh
.venv/bin/python -m handoff.evidence pack \
  --batch "$WORK/protocol_fhir_01/batches/protocol_fhir_01" \
  --audit "$WORK/protocol_fhir_01/batches/protocol_fhir_01_audit/audit.private.json" \
  --inputs "$FROZEN_BUNDLE" \
  --probes "$WORK/protocol_fhir_01/operation_replay/summary.json" \
  --output "$WORK/fhir_paid_evidence"

.venv/bin/python -m handoff.evidence recompute \
  --bundle "$WORK/fhir_paid_evidence" --output "$WORK/fhir_recomputed"
```

The package preserves exact case and trace bytes, uses relative index paths, and
checks hashes before regenerating the metrics and LaTeX. With no supplied human
assessment file, version 2 records the programmatic and agent-review scope and
creates no human-review claim. An actual cohort-bound assessment can be included
with `--human-review`; historical version 1 packages remain supported. Keep
MIMIC-derived cases and detailed traces in the authorized data environment.
