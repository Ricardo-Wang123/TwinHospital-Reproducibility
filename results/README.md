# Outcome measures and reports

## Emergency department study

| Outcome | Unit and denominator |
| --- | --- |
| Clinical closure | Assignments with a verified nonempty, pharmacy-approved final prescription and a locked note containing the physician's disposition, divided by all planned assignments. |
| Principal-condition regimen | Compare the final approved ingredients for the principal problem with the source regimen. Each ingredient counts once per encounter. Recall uses reference ingredients and precision uses signed ingredients. Exact agreement requires equal sets. A combination product contributes its constituent ingredients. |
| Prescribing evidence | The private report joins the available source inventory, chart delivery, reading receipts, medication advice and the physician's signed decision. It preserves reasons at the specificity recorded by the physician. |
| Disposition agreement | Matching physician decisions divided by closures with a paired reference. The ED-to-ward cohort provides admission positives only. |
| Consultation | Count physician questions before the prescription is first signed. In A2 and A3, use the report receipt to count questions after reading. Match the initial assessment with the source ED diagnosis group. |
| Pharmacy review | Count an explicit pharmacist return for each round requiring physician action. Monitoring advice on an approved prescription is retained with the review. Report technical verification blocks separately. The version chain retains the initial signature, review findings, revisions, replacement read-back and final approval. |
| Documentation | Compare the first complete draft with the locked physician-confirmed note after whitespace normalization. Count physician text edits from the version chain. Check the authored final-order and disposition entries against the same-encounter OpenHIS read-back. |
| Within-stage time | For A1–A3, measure registration to the physician opening the consultation. For C1–C3, measure the first saved complete draft to the verified lock of the same note. The summary reports the median, quartiles and number of observed pairs in seconds. The end-to-end interval runs directly from verified registration to verified lock of the same encounter’s note. First submission for pharmacy review marks the prescribing boundary. An absent intermediate timestamp leaves only the affected stage intervals unmeasured. All durations use the run clock. Source specimen-processing and report-turnaround intervals add no simulated workload. Physician lookups count as work. |

The closure denominator includes incomplete assignments. Historical agreement measures concordance with the recorded visit. Workload combines all logged attempts for an assignment and includes physician work before consultation, consultation and later review. Elapsed time uses the selected attempt's clock, preserved through continuation. The stage counts include verified prescription and note read-backs from partially completed visits.

The indication is fixed when the physician signs the order. Older runs without signed indications use the legacy scoring method in a separate batch. Execution signals are counted across attempts. A recovery links an observed repair to its triggering event.

```sh
python -m handoff.run --config "$RUN" audit --observe-native
python -m handoff.run --config "$RUN" report --reference "$ED_COHORT/reference.private.json"
```

## Metrics for `outpatient-v2`

The comparison writes a Markdown report and a JSON summary. The JSON records the workflow version, hospital interface, and model IDs.

| Measure | Calculation |
| --- | --- |
| Clinical artifacts | For each condition, count observed encounter traces, signed prescriptions, and locked notes. The JSON also counts runs with an original prescription candidate. The encounter files retain the intake report when present, prescription versions and pharmacist decisions, and note drafts. |
| History collection | Count direct physician questions in the consultation record frozen at the first submitted prescription. Count patient answers during AI intake, consultation and medication reconciliation separately. |
| Pharmacist returns | Among encounters that reached pharmacist review, report first-round returns, encounters ever returned, and total and mean returned rounds. Each blocking pharmacist decision counts once. Encoding-only revisions and verified payments after a return are reported separately. |
| Documentation edits | Compare successive saved versions. Count physician edits and rewrites that changed the note text. An unchanged signature adds no edit. |
| Modeled workload | Apply the configured activity weights to the physician and pharmacist separately and sum their work for each visit. Patient turns and API latency are reported separately. C3 supplementation is charged when the physician supplies an addendum. |
| Elapsed execution | Per visit, partition registration read-back to medication payment at the first submitted candidate, signed order and locked note. Each interval requires observed endpoints on one clock. |
| Matched workload | Within verified-paid visits, compare the assisted condition with its baseline for the same case and the same other two conditions. Average the differences within each source history, then weight each contributing history equally. When each represented diagnosis stratum has at least two contributing histories, the report adds a 95% interval from resampling histories within strata. |
| Encounter completion | For each whole-visit configuration, divide visits that pass the workflow checks and have a verified signed order, locked note, and matching medication payment in OpenHIS by all assigned visits. The denominator includes unpaid, unfinished, and undispatched assignments. |

The report derives these measures from the run traces and the OpenHIS audit. [Per-encounter measures](../simulator/sandbox/experiments/linked/scenario_design.py) · [Activity weights](../simulator/sandbox/experiments/linked/clinical.py) · [Matched workload calculation](../simulator/results/summarize_paid_runs.py)

### Score clinical content for your cases

Before running, identify the required facts, acceptable values, and source for each case. Store the references in `evaluation` for use with the saved artifacts. Apply the same criteria to every condition being compared. [Case and evaluation fields](../data/README.md#runtime-case-json)

| Measure | Scoring procedure |
| --- | --- |
| History coverage and contradictions | Evaluate the physician's history at first candidate submission in all three conditions. Divide correctly captured required facts by assessable required facts. Divide contradictory assertions by assessable assertions. Score the original AI report separately in A2 and A3. |
| Note quality | Score the first full draft and the physician-signed note separately against the accessible case facts, actual dialogue, observed findings and signed prescription. Record contradictory, unsupported and indeterminate assertions separately. |
| Prescription content | Apply the frozen medication and case criteria to the first submitted candidate. Divide satisfied criteria by assessable criteria and report coverage. Keep the final order and all revisions available for comparison. |

These content measures require an assessment of the artifacts using the reference you supply. `handoff.run report` generates the operational measures above.

## Generate a batch report

From the simulator directory, run the commands below for a completed comparison batch. `RUN` points to the batch's `run.local.json`. Keep the OpenHIS instance available for the state read-back.

```sh
.venv/bin/python -m handoff.run --config "$RUN" audit --observe-native
.venv/bin/python -m handoff.run --config "$RUN" report
```

The runner writes `reader_results/REPORT.md` and `reader_results/summary.json` in the run configuration directory. [Report implementation](../simulator/handoff/reader_report.py) · [Run configuration](../simulator/README.md#run-the-comparison)
