# TwinHospital

**Evaluating Clinical Language Agents through Linked Hospital Workflows**

TwinHospital runs a single outpatient encounter through intake, consultation, prescription review, note attestation, and simulated medication payment in OpenHIS. CAMEL coordinates the patient and staff agents. The encounter trace links language artifacts to the business operations that consume them.

![TwinHospital workflow from registration through simulated payment](docs/workflow.png)

## Published results

The current snapshot contains **170 verified paid encounters** from **10 MIMIC-IV v2.2 development-partition histories**, two per diagnosis stratum. All 27 combinations of intake, prescription review, and documentation are represented. The separate operation replay contains five fault requests and five compliant controls.

| Artifact | Contents |
| --- | --- |
| [Encounter results](results/verified_paid_results_170.json) | Delivery, prescription review and revision, staff effort, and audited settlement |
| [Operation replay results](results/operation_replay_10.json) | Fault and control outcomes for five business constraints |
| [Paper tables](results/acl_results_170.tex) | Generated LaTeX for the current results |
| [Execution profile](results/cohort_170_execution.json) | Model assignment, generation settings, dictionaries, and source digests |
| [Result definitions and commands](results/README.md) | Denominators and commands for recomputing and updating tables |

## Start here

| Goal | Entry point |
| --- | --- |
| Run TwinHospital with your own API and model | [Configure your model endpoint](handoff/REPRODUCE.md#use-your-own-model-endpoint) |
| Check the installation with a fictional encounter | [Quick start](handoff/REPRODUCE.md#fictional-encounter) |
| Run the paper's configurations with the frozen case bundle | [Repeat the study configuration](handoff/REPRODUCE.md#repeat-the-study-configuration) |
| Recompute the reported results from a received evidence bundle | [Offline recomputation](results/README.md#recompute-the-encounter-metrics) |
| Build your own case or extract historical material from MIMIC-IV | [Case format and data selection](handoff/CASE_FORMAT.md) |
| Evaluate a new, disjoint cohort | [Run a held-out cohort](handoff/REPRODUCE.md#run-a-held-out-cohort) |
| Resume an existing run | [Run and recovery guide](handoff/README.md) |

## Models and requirements

Use Python 3.12, Docker Compose v2, and the authorized OpenHIS release. The launcher accepts an OpenAI-compatible Chat Completions endpoint and model IDs of your choice for a new run. A single model can serve both the patient/staff agents and the AI tools. Case validation, role permissions, OpenHIS transaction checks, and the definition of a verified paid encounter apply to every model configuration. [Custom provider template](config/provider.custom.example.json)

The [paper cohort's provider configuration](config/provider.example.json) records `deepseek-v4.1-flash-1017` for patient and staff roles and `glm-5.2` for intake assistance, prescription screening, and drafting. Both use temperature 0.2 with reasoning disabled. Use the frozen configuration when repeating that execution profile and the evidence bundle when recomputing its published values.

The repository ships source, a fictional case generator, configuration templates, and verified aggregates. The study configuration also needs the separately authorized case and dictionary bundle, the matching OpenHIS release, and a model account approved for the data. Each researcher obtains access to MIMIC-IV under its data-use terms. [MIMIC-IV v2.2](https://physionet.org/content/mimiciv/2.2/) · [PhysioNet guidance on model endpoints](https://physionet.org/news/post/llm-responsible-use/)
