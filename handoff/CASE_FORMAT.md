# Case construction and input format

Each input describes one patient's starting state for one outpatient visit. The MAS generates the dialogue, handoff, consultation, prescription, review, note, and settlement. Reusing a case under another configuration creates separate OpenHIS business objects.

## Runtime case JSON

The schema version is `twinhospital.linked.case/1`. The [reader release](https://github.com/Ricardo-Wang123/TwinHospital-Reproducibility/releases/tag/openhis-reader-v1) supplies editable examples and a manifest. The [generator](prepare_synthetic.py) creates the same files locally.

| Top-level field | Contents |
| --- | --- |
| `schema_version` | Literal `twinhospital.linked.case/1` |
| `case_id`, `patient_key` | Stable, opaque identifiers. Use the same patient key for the same source patient across manifests. A bundle has distinct cases and source patients. |
| `group`, `split`, `data_kind` | One of the five group IDs below, a declared split such as `engineering` or `test`, and explicit origin (`mimic_derived`, `hospital_derived`, or `synthetic_independent`). |
| `patient_initial` | Demographics as perceived by the patient, communication profile, and facts from the patient view. Each fact has a unique `fact_id` and status `reported`, `denied`, or `unknown`. Include the current complaint and what the patient can report. |
| `his_initial` | Authorized demographics, historical encounters and orders, optional administration, lab, microbiology and outpatient history, current observations, current medication-use and allergy records, and timing basis. |
| `formulary` | Allowed virtual products. Preserve product code, ingredient, strength, formulation, route, and separate dose and dispensed quantity units. Use the corresponding native product dictionary. |
| `evaluation`, `provenance` | Evaluator-only reference fields and source-versus-constructed annotations. Independent fictional cases declare `uses_patient_records: false` and `source_dataset: null`. |

Use `type2_diabetes`, `heart_failure`, `copd`, `pneumonia`, or `uti` as the group ID. The group identifies a sampling stratum. The clinician agent chooses a diagnosis from the evidence visible during the visit.

Separate current patient-reported facts, historical chart entries, and current simulated observations. Record a historical prescription in `his_initial.historical_orders` and current use in `his_initial.current_use_record`. Preserve allergy and timing uncertainty. The patient agent receives `patient_initial`; other roles query the permitted chart and encounter records. The `evaluation` object is reserved for scoring and is excluded from the runtime case view.

## Dataset manifest

A manifest lists the case files and repeats their identities. Paths are relative to the manifest. Each case and source patient appears once.

~~~json
{
  "cases": [
    {
      "path": "cases/example_01.json",
      "case_id": "example_01",
      "patient_key": "fictional_patient_01",
      "group": "type2_diabetes",
      "split": "engineering",
      "data_kind": "synthetic_independent"
    }
  ]
}
~~~

When starting from an example, update the identity fields and case content for your dataset. Match the five metadata fields to the case JSON. You can omit the example's `file_sha256`, `case_hash`, and `runtime_input_hash` when authoring a new manifest. The packer computes those values and freezes the input. For a single case you can pack the JSON directly without writing a manifest. [Launch commands](REPRODUCE.md)

For an approved hospital dataset use `hospital_derived`, set `provenance.uses_patient_records` to `true`, and name the source in `provenance.source_dataset`. Supply the endpoint's processing record when configuring the run.

## Product and diagnosis mappings

The default OpenHIS dataset supports the five included diagnosis codes and five simulated products. The sample case files show the product codes and the clinical data structure. The [product mapping](../deploy/openhis-experiment/catalog_mapping.json) and [diagnosis mapping](../deploy/openhis-experiment/diagnosis_mapping.json) link the case-facing codes to native OpenHIS definitions.

To add terms, provide a versioned product or diagnosis map and the SQL fixture that installs its native definitions. Pass `--catalog`, `--diagnosis-catalog`, and `--fixture` to `handoff.inputs pack` as applicable. Each product mapping names the medication definition, permitted route and frequency codes, units, and dispensing location. The fixture registers the definition, virtual stock, and price. Keep source medication records as written when their product or units are unknown. A crossed study batch also uses the frozen clinical rule catalogue and workflow policy. [Fixture example](../deploy/openhis-experiment/experiment-fixtures.sql)

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
