# Implementation review

## Review the study path

1. Read [the nine workflow conditions](README.md), [the ED case inputs](../data/README.md) and [the outcome definitions](../results/README.md).
2. Follow the [encounter runner](../simulator/sandbox/experiments/linked/clinical.py) from the verified opening IPS through intake, targeted chart search, the single prescription decision, pharmacy review and note signature. The physician uses verified IPS entries directly and searches by keyword when a decision requires additional evidence. The rationale identifies its factual sources. A search with no match leaves the fact unknown. Check the [response contracts](../simulator/sandbox/experiments/linked/response_contracts.py) and [model routing](../simulator/sandbox/experiments/linked/actors.py).
3. Inspect the [prescription encoding](../simulator/sandbox/experiments/linked/order_encoding.py) and [OpenHIS port](../simulator/sandbox/experiments/linked/native_his.py). Before signature, identical regimen lines are returned to the same physician. Explicit pharmacy returns produce revisions within the same decision. Approval can carry monitoring advice. The superseded version and withdrawn lines remain in the native history.
4. Check the [closure verifier](../simulator/sandbox/experiments/linked/emergency.py) and [measurement code](../simulator/handoff/emergency_results.py) for native read-backs and evaluation scope. Confirm that the reference and agent inputs are separate.
5. Run the [fictional input check](../simulator/README.md#fictional-example) and the focused tests. Run a live test in a separate local OpenHIS instance with a fresh experiment name.

From the repository root, use Python 3.12 with the simulator dependencies and pytest:

```sh
PYTHONPATH=simulator python -m pytest -q tests
```

## Clinician assessment

Within the authorized data environment, run `python -m handoff.clinician_review` from the simulator directory with the prepared cohort, a fresh output directory and a private path for tabular inputs. The review pages show the principal-condition stratum, preprescription clinical evidence, medication history and the fixed allergy record. Tabular inputs list the same orderable products as the simulator.

The opening medication section lists documented entries. An undocumented opening list is labeled `无用药记录` (no medication record). The opening status and the results of any targeted medication queries are preserved separately. Medication records released later become queryable at their recorded availability time.

Reviewers independently enter whether medication is indicated, the necessary generic ingredients, complete reasonable alternatives, and the clinical basis or missing information. Preserve independent judgments for adjudication. Historical regimen agreement remains available as a separate outcome. Individual case packets and reviewer annotations use the same credentialed-data environment as the cohort.
