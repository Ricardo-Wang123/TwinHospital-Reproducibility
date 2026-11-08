# TwinHospital

**Evaluating Clinical Language Agents through Linked Hospital Workflows**

TwinHospital evaluates clinical language agents in a linked outpatient encounter. It follows the handoff, prescription, and note from generation and review to the signed order, locked note, and simulated medication payment in OpenHIS.

![TwinHospital workflow from registration through simulated payment, showing the three evaluation applications](docs/workflow.png)

## Study snapshot

The current analysis covers **170 verified paid encounters** derived from ten MIMIC-IV histories. The study varies pre-consultation, prescription-review, and documentation configurations while holding the role and assistance models fixed. The operation replays use separate fault and control denominators.

- [Encounter aggregates](results/verified_paid_results_170.json)
- [Operation replay aggregates](results/operation_replay_10.json)
- [LaTeX results used in the manuscript](results/acl_results_170.tex)
- [Frozen execution settings and source checksums](results/cohort_170_execution.json)
- [Metric definitions and result regeneration](results/README.md)

## Run TwinHospital

| Purpose | Entry point |
| --- | --- |
| Start from a fictional case | [Setup and smoke check](handoff/README.md#在新电脑启动) · [Run configuration](config/run.synthetic.example.json) |
| Configure the role and tool models | [Provider template](config/provider.example.json) |
| Start a crossed batch | [Factorial configuration](config/run.factorial.example.json) |
| Resume a verified batch | [Recovery configuration](config/run.recover.example.json) |
| Audit the traces and regenerate tables | [Results and commands](results/README.md) |

The [run guide](handoff/README.md) covers setup, credential entry, and batch recovery. The native smoke check uses an isolated OpenHIS installation and independent fictional inputs. The model-backed run uses local credentials. The tag `cohort-170-runtime` preserves the source files bound by the analyzed cohort's execution specification.

Full clinical-cohort reproduction requires authorized MIMIC access, the protected case and trace archive, and the authorized OpenHIS distribution. The repository contains code, configuration templates, fictional inputs, and aggregates. [PhysioNet data-use guidance](https://physionet.org/news/post/llm-responsible-use/)
