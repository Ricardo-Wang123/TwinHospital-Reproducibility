# TwinHospital

**Evaluating Clinical Language Agents through Linked Hospital Workflows**

TwinHospital links language-agent decisions to an outpatient encounter in an isolated OpenHIS instance. Pre-consultation, prescription review, and clinical documentation share the same encounter. The runtime records handoffs and revisions and verifies the signed order, locked note, and simulated medication payment from persisted HIS state.

The framework varies the assistance and review configuration at each stage while holding the role and tool models fixed. It records artifact delivery, modeled staff effort through verification and revision, and native-state outcomes.

## Evidence

The repository contains aggregate results for **170 verified paid encounters** derived from ten MIMIC-IV histories. The analysis describes these completed encounters, including initial prescription-review decisions, delivered artifacts, and modeled effort. [Result definitions and recomputation](results/README.md)

## Reproduction

Start with the independent fictional case and the isolated OpenHIS deployment. The native smoke check verifies the workflow through payment without model calls. Running the agent workflow requires the authorized OpenHIS assets and working credentials for the configured role and tool routes. [运行与扩跑指南](handoff/README.md)

| Task | Entry point |
| --- | --- |
| Check a new linked run | [Independent case configuration](config/run.synthetic.example.json) |
| Resume the audited batch | [Recovery configuration](config/run.recover.example.json) |
| Run a new crossed batch | [Factorial configuration](config/run.factorial.example.json) |
| Verify results and export tables | [Audit and export tools](tools/) |

The recovery entry point binds the original manifests and audit evidence, preserves verified payments, and schedules only unresolved assignments. Larger studies use a separately frozen cohort and analysis plan. Protected histories, run archives, and credentials stay in the authorized environment. [PhysioNet data-use guidance](https://physionet.org/news/post/llm-responsible-use/)
