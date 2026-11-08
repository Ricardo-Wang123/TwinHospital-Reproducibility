# Verified Results

The manuscript reports 170 verified paid encounters from ten source histories. [Encounter metrics](verified_paid_results_170.json), [operation replays](operation_replay_10.json), and [generated LaTeX](acl_results_170.tex) provide the table values. [Execution settings](cohort_170_execution.json) identify the frozen runtime.

| Application | Evidence and denominator |
| --- | --- |
| I · Artifacts | Handoff and note delivery use artifact checks and accepted versions. Prescription disposition uses the original order. Alert counts use review rounds, and the false-positive rate uses resolved rule-reference negatives. Human assessment coverage uses final prescriptions. |
| II · Staff effort | Modeled staff activity is divided by delivered artifacts. Matched contrasts hold source history and the other two configurations fixed, with equal weight per contributing history. Sensitivity varies collection and verification weights independently. |
| III · Workflow | The native audit identifies verified signed orders, locked notes, and payments. It partitions encounters by the first candidate's path to signing. Fault and compliant control replays use separate request denominators. |

[Batch status](batch_status.json) records dispatch and recovery counts. The [run guide](../handoff/README.md) describes continuing a batch.

## Recompute the encounter metrics

Run from the repository root on the authorized archive host. The summarizer checks trace digests against the native audit and evaluates the frozen case and product references.

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

The resulting `acl_results.tex` updates the abstract, body, and appendix together. Place it beside `twinhospital_acl.tex` and compile the paper.
