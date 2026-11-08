# Case construction and input format

One case is one source patient with a single anchor admission and one simulated follow-up visit. Each workflow configuration uses the same starting case and separate encounter objects.

## MIMIC-IV source selection

The study uses **MIMIC-IV v2.2, hospital module**. Use a credentialed local copy of the ZIP or the extracted CSV directory. The source package can also contain ICU tables. The study inputs below come from `hosp`. ED and MIMIC-IV-Note are separate resources.

| Source table in `hosp/` | Fields and use |
| --- | --- |
| `patients`, `admissions` | Join on `subject_id`. Use age, admission and discharge times, and survival to select a completed adult anchor admission and retain earlier eligible history. |
| `diagnoses_icd`, `d_icd_diagnoses` | Join by admission, then code and version. Use any diagnosis sequence for eligibility. Retain the original version, sequence, description, and other diagnoses. |
| `prescriptions`, `pharmacy` | Require a same-admission, stratum-related oral or inhaled order with parseable strength, dose, unit, and schedule. Pharmacy context can resolve a schedule when linked by patient, admission, and pharmacy ID. Keep original timing and uncertainty. |
| `labevents`, `d_labitems` | Retain eligible specimen, analyte, value, unit, and chart and result-availability times. Join the laboratory dictionary by item ID. |
| `emar`, `emar_detail` | Add available historical administration records, with statuses and components. An order and an administration are different records. |
| `microbiologyevents`, `omr` | Add microbiology available by the historical cutoff and date-bounded historical outpatient measurements where available. |

The material screen uses a cutoff 24 hours after anchor discharge. It uses availability and recency, independent of the magnitude of the measured value. Preserve unknown values and conflicting observations.

| Stratum | ICD-9 / ICD-10-CM screen | Extra material at the cutoff |
| --- | --- | --- |
| Type 2 diabetes | — / `E11*` | Creatinine within 168 h and glucose within 72 h |
| Heart failure | `428*` / `I50*` | Creatinine within 168 h and potassium within 72 h |
| COPD | — / `J44*` | Common eligibility requirements |
| Pneumonia | `486` / `J18.9` | Creatinine and white blood cell count within 168 h |
| Urinary tract infection | `599.0` / `N39.0` | Creatinine and urinalysis within 168 h |

Ignore punctuation when matching codes. `*` denotes a code family. Assign one stratum and one anchor per source patient, and freeze the partition by source patient before authoring cases. The diagnosis is a sampling label. Use the authorized source registry to check overlap before producing opaque patient keys.

## From history to the simulated visit

The historical chart preserves the earlier records and their provenance. In the study, the constructed outpatient visit begins 168 hours after discharge. A constructed prior outpatient medication plan, where present, starts at hour 24. The current complaint, reported medication use, and authored findings belong to the follow-up scenario. A historical prescription supplies historical exposure evidence. Current use and allergy status have separate fields.

For the blood panel, the study considers creatinine, potassium, glucose, sodium, urea nitrogen, and HbA1c. The carry-forward step uses the latest eligible blood result with recorded units and timestamps, then records the selected source. It retains missing results. See the [versioned case preparation code](../sandbox/experiments/linked/case_preparation/) for source selection, pharmacy linking, historical supplements, and the construction overlays. The historical runtime release uses frozen case files, including the authored fields and the per-case provenance.

Inspect the official archive locally without reading patient rows.

~~~sh
.venv/bin/python -m handoff.mimic inspect --source "$MIMIC_ZIP"
~~~

To start from selected admissions, create a private JSON array with string keys `subject_id`, `hadm_id`, and `group`. The group values are `type2_diabetes`, `heart_failure`, `copd`, `pneumonia`, and `uti`. Use the same private identity key across development and test. Extract the historical starter files with:

~~~sh
.venv/bin/python -m handoff.mimic extract \
  --source "$MIMIC_ZIP" --selection "$PRIVATE_SELECTION" \
  --identity-key "$WORK/identity.key" --output "$WORK/historical_starters"
~~~

The starter contains the core historical records and labs available at anchor discharge. It excludes and counts malformed prescription intervals and laboratory timestamps. It generates source and role-view scaffolds for explicit selections. The study's supplemental tables and constructed visit use the procedures above. Author the new visit fields and catalogue in the runtime format below, then freeze the case. For the exact study inputs, use the [frozen bundle route](REPRODUCE.md#repeat-the-study-configuration).

## Runtime case JSON

The schema version is `twinhospital.linked.case/1`. The [fictional generator](prepare_synthetic.py) writes a full editable case and its manifest.

| Top-level field | Contents |
| --- | --- |
| `schema_version` | Literal `twinhospital.linked.case/1` |
| `case_id`, `patient_key` | Stable, opaque identifiers. Use the same patient key for the same source patient across manifests. A bundle has distinct cases and source patients. |
| `group`, `split`, `data_kind` | One declared stratum, a frozen partition, and explicit origin (`mimic_derived`, `hospital_derived`, or `synthetic_independent`). |
| `patient_initial` | Demographics as perceived by the patient, communication profile, and facts from the patient view. Each fact has a unique `fact_id` and status `reported`, `denied`, or `unknown`. Include the current complaint and what the patient can report. |
| `his_initial` | Authorized demographics, historical encounters and orders, optional administration, lab, microbiology and outpatient history, current observations, current medication-use and allergy records, and timing basis. |
| `formulary` | Allowed virtual products. Preserve product code, ingredient, strength, formulation, route, dose and quantity units. The deployment must have matching native product codes. |
| `evaluation`, `provenance` | Evaluator-only reference fields and source-versus-constructed annotations. Independent fictional cases declare `uses_patient_records: false` and `source_dataset: null`. |

Generate a fictional example, then keep any authored clinical inputs outside the checkout.

~~~sh
.venv/bin/python -m handoff.prepare_synthetic --output "$WORK/example"
.venv/bin/python -m handoff.inputs pack \
  --case "$WORK/example/case.json" --assets "$OPENHIS" \
  --output "$WORK/my_case_bundle"
~~~

Pass `--case` more than once to pack multiple case files, or `--manifest` to use a validated existing selection. A crossed batch additionally needs the native and rule catalogues and their fixture SQL in installation order. The packer records the case bytes, runtime projection digests, and release checksums. The launcher creates machine-local paths from the portable manifest. [Launch commands](REPRODUCE.md)

For a different approved hospital dataset, use `hospital_derived`, set `provenance.uses_patient_records` to `true`, and name the source dataset in `provenance.source_dataset`. Supply the corresponding processing record and start with the linked case route. The study's held-out comparison uses disjoint MIMIC-IV cases under the original selection protocol.
