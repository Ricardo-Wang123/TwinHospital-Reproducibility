# Study Results

The manuscript reports 170 verified paid encounters from ten MIMIC-IV development-partition histories across 27 configuration combinations. [Encounter metrics](verified_paid_results_170.json), [operation replays](operation_replay_10.json), and [generated LaTeX](acl_results_170.tex) provide the table values. [Execution settings](cohort_170_execution.json) identify the case bundle and frozen runtime.

| Observed result | Current snapshot |
| --- | ---: |
| Handoffs delivered | 170/170 |
| Delivered handoffs with physician supplementation or correction | 140/170 |
| Original prescriptions approved at first review | 141/170 |
| First approvals requiring revision after native encoding preflight | 14/141 |
| Notes delivered and locked in OpenHIS | 170/170 |
| Accepted interim note available before consultation end | 55/55 during-consultation runs |
| Median queue-to-payment time | 6.44 min |
| Verified-safe fault requests and executed compliant controls | 5/5 and 5/5, separate replay sets |

| Application | Evidence and denominator |
| --- | --- |
| I · Artifacts | Handoff and note delivery use artifact checks and accepted versions. Prescription disposition uses the original order. Alert counts use review rounds, and the false-positive rate uses resolved rule-reference negatives. Human assessment coverage uses final prescriptions. |
| II · Staff effort | Modeled staff activity is divided by delivered artifacts. Matched contrasts hold source history and the other two configurations fixed, with equal weight per contributing history. Sensitivity varies collection and verification weights independently. |
| III · Workflow | The native audit identifies verified signed orders, locked notes, and payments. It partitions encounters by the first candidate's path to signing. Fault and compliant control replays use separate request denominators. |

The encounter rates above describe the paid analysis cohort. Held-out outcomes use a separate result snapshot. Operational progress and traces are retained in the local experiment archive. [Run and recovery guide](../handoff/README.md)

The held-out report uses the same matched effort contrasts. Its frozen specification records the analysis code and settings for 95% percentile intervals with 10,000 resamples of contributing source histories within each disease. Each represented disease needs at least two contributing histories. The report gives the number of matched blocks and histories for each contrast, with per-disease counts and interval status in the JSON. [Held-out execution and report](../handoff/REPRODUCE.md#run-a-held-out-cohort)

The [HIS interface check](his_interface_checks.json) compares the native and FHIR R4 transports on fictional scripts. The [archived comparison](archived_his_interface_checks.json) replays accepted workflow operations from five paid encounters, one per disease, and verifies both paths against the archived artifact values and settlement amount. [Reproduce the interface checks](../handoff/HIS_INTERFACE.md#run-and-compare)

The [paired operation check](app3_interface_checks.json) applies both interfaces to the same ten source-bound APP III requests. It verifies the expected state change for each compliant control and observes state after the four blocked faults and the receipt reuse. The simulator queue probe uses the shared gateway in both paths. [Replay the probes](../handoff/REPRODUCE.md#replay-paired-operations)

The archived workflow feedback check covers the reported 170 paid encounters. It reproduces the adapter's local encoding findings with the frozen OpenHIS mapping, matches them to the actual physician revision requests, and checks that the signed-order state appears in the final drafting request and supplies the native charges. [Feedback check results](workflow_feedback_checks_170.json)

## Recompute the encounter metrics

An authorized recipient can verify a received evidence bundle from any directory. This offline computation uses the frozen case bytes, completed traces, native audit, and assessment sidecars. It checks and scores one complete trace at a time, then groups the resulting measurements using the same scoring rules and denominators.

~~~sh
.venv/bin/python -m handoff.evidence recompute \
  --bundle "$PAID_EVIDENCE_BUNDLE" --output "$WORK/recomputed"
cmp "$WORK/recomputed/verified_paid_results.json" results/verified_paid_results_170.json
cmp "$WORK/recomputed/acl_results.tex" results/acl_results_170.tex
~~~

The original protected model-call archive and native mapping support a separate verification of the request and read-back links. The command reports counts and a cohort digest. Run it in the authorized data environment.

~~~sh
.venv/bin/python -m results.audit_workflow_feedback \
  --bundle "$PAID_EVIDENCE_BUNDLE" --model-evidence "$MODEL_CALL_ARCHIVE" \
  --catalogue "$FROZEN_BUNDLE/catalog.json" > "$WORK/workflow_feedback_checks.json"
cmp "$WORK/workflow_feedback_checks.json" results/workflow_feedback_checks_170.json
~~~

To create the evidence bundle in the authorized environment, use the frozen input bundle and the original completed batch and assessment files:

~~~sh
.venv/bin/python -m handoff.evidence pack \
  --batch "$BATCH_DIR" --audit "$NATIVE_AUDIT" --inputs "$FROZEN_BUNDLE" \
  --human-review "$HUMAN_REVIEW" --probes "$OPERATION_REPLAYS" \
  --output "$PAID_EVIDENCE_BUNDLE"
~~~

The packer binds the audit to the original batch specification, preserves the exact trace and case bytes, and replaces host-specific index paths with relative paths. It verifies the recomputed outputs before completing the bundle. Keep this material within the authorized data environment.

For direct analysis of the original archive, the same summarizer can be run separately:

~~~sh
.venv/bin/python -m results.summarize_paid_runs \
  --batch /private/path/to/frozen_batch \
  --audit /private/path/to/native_audit.json \
  --case-manifest /private/path/to/frozen_case_manifest.json \
  --clinical-catalogue /private/path/to/frozen_product_catalogue.json \
  --output /private/path/to/verified_paid_results_170.json
~~~

## Regenerate the manuscript tables

Use the encounter metrics, the corresponding native audit, the human-review record, and the separate replay summary.

~~~sh
.venv/bin/python -m results.render_linked_manuscript \
  --metrics /private/path/to/verified_paid_results_170.json \
  --workflow-audit /private/path/to/native_audit.json \
  --human-review /private/path/to/human_review.json \
  --probes /private/path/to/operation_replay/summary.json \
  --output /path/to/paper/acl_results.tex
~~~

The audit must contain run-level entries. In the review record, set `cohort_sha256` to `paid_cohort_sha256` from the metrics and set `reviewed` to the number of assessed final prescriptions. Use `outcomes: null` for coverage alone. When dispositions are recorded, supply `appropriate`, `inappropriate`, and `indeterminate` counts totaling `reviewed`.

The resulting `acl_results.tex` updates the abstract, body, and appendix together. Place it beside `twinhospital_acl.tex` and compile the paper. Publish a separate aggregate and rendered table file for each new analyzed cohort, together with its execution profile and partition. Keep the existing snapshot available for comparison.

The held-out `report` command generates `acl_heldout_results.tex` with the same artifact, review, and effort calculations, the paired intervals, and assignment and safety counts by disease. The fragment displays unavailable ratios as dashes. It includes operation results only when a replay binds to the frozen schedule. Include it with `\input{acl_heldout_results}` alongside the existing study results. The manuscript uses `booktabs`, `tabularx`, `float`, and `placeins` for the fragment. [Held-out export](../handoff/REPRODUCE.md#run-a-held-out-cohort)

## Content references

Fact-level assessment requires aligned expected items, normalized values, and an allowed source for each item or assertion. An incorrect value is present for omission counting and conflicting for mismatch counting. Unknown source values and unresolved paraphrases have separate labels.

| Metric | Count and eligible denominator |
| --- | --- |
| Handoff completeness | Handoffs matching all applicable items among handoffs with a complete reference |
| Handoff module coverage | Matched items among applicable items in the module |
| Handoff omission | Absent items among applicable required items |
| Handoff mismatch | Conflicting values among checkable reference-linked values |
| High-risk signal recall | Correctly flagged signals among explicit rule-supported reference signals |
| Note fact match | Agreeing assertions among checkable reference-linked assertions |
| Note unlinked facts | Assertions with missing or invalid links among parseable assertions |
| Note key information omission | Absent items among applicable required items |
