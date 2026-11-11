# TwinHospital

**Simulating Hospitals with Real HIS Data Flow**

TwinHospital simulates outpatient visits in OpenHIS. CAMEL coordinates patient and staff agents through intake, consultation, prescription review, note signing, and simulated payment. Each visit produces a trace linking the agents' dialogue and decisions to records in the hospital system.

Each configuration starts from the same frozen case in an independent encounter. Retrieved handoffs, review feedback, and committed HIS records shape the next step. The archived cohort uses the native OpenHIS application APIs. New runs can select FHIR R4 workflow operations or a separate WS/T 846 SOAP service profile over the same OpenHIS connector. The WS/T profile identifies its national registration and order services separately from project workflow extensions. [Hospital interface and encounter state](handoff/HIS_INTERFACE.md)

[Run the same frozen assignments through both protocols](handoff/PROTOCOL_COMPARISON.md) · [Current DS/GLM model template](config/provider.ds-glm-20261111.example.json)

## Run the paper experiments with your data

The experiment kit includes the CAMEL MAS, OpenHIS 2.0.5, and five editable fictional cases. Start with one case across three whole-visit configurations, then run the full 27-combination design on your dataset. The report follows Applications I–III and includes artifact checks, modeled staff effort, and workflow integrity with paired fault and control requests.

[Experiment kit](https://github.com/Ricardo-Wang123/TwinHospital-Reproducibility/releases/tag/reader-experiments-v1) · [Experiment guide](handoff/REPRODUCE.md#4-run-a-paper-comparison) · [Case format](handoff/CASE_FORMAT.md) · [OpenHIS assets](https://github.com/Ricardo-Wang123/TwinHospital-Reproducibility/releases/tag/openhis-reader-v1)

## Data

The study combines MIMIC-IV v2.2 patient histories with constructed outpatient follow-ups. The archived native-interface cohort contains **170 encounters with simulated payment** from **10 development-set histories** across five diagnosis strata.

| Diagnosis stratum | Source histories | Encounters |
| --- | ---: | ---: |
| Type 2 diabetes | 2 | 35 |
| Heart failure | 2 | 33 |
| COPD | 2 | 35 |
| Pneumonia | 2 | 33 |
| Urinary tract infection | 2 | 34 |
| **Total** | **10** | **170** |

The case format shows how historical records, the patient's account, and findings from the simulated visit fit together. [Case format and examples](handoff/CASE_FORMAT.md) · [Encounter data summary](results/verified_paid_results_170.json)

## Framework

![TwinHospital workflow from registration through simulated payment](docs/workflow.png)

The patient agent answers from its own case view. Clinical roles use the handoff, dialogue, and chart records available to them. When an order is revised, the trace connects both versions to the same encounter. OpenHIS records the signed order, locked note, and simulated payment.

## Experimental design

The design crosses three configurations at each of three stages. All 27 combinations are represented in the reported cohort.

| Stage | Baseline | Assisted configurations |
| --- | --- | --- |
| Intake | Nurse agent | AI alone, or AI followed by nurse review |
| Prescription review | Rules with pharmacist routing | Rules with sequential or parallel AI and pharmacist review |
| Documentation | Physician agent | AI drafting during or after consultation |

The archived native-interface traces capture artifact revisions, staff actions used to model effort, and the resulting state in OpenHIS. Physician agents supplemented or corrected **140 of 170** handoffs. **14 of 141** prescriptions approved at first review needed revision for OpenHIS encoding. Median time from queue entry to simulated payment was **6.44 minutes**. [Study results and paper tables](results/README.md)

The study's case selection, approved access route, and frozen runtime are documented separately. [Study reproduction](handoff/REPRODUCE.md#repeat-the-study-configuration)
