# Paper protocol gateway

Used by the complete MAS through `PaperProtocolPort(NativeOpenHISPort(...))`, and by the local fault-injection suite. It wraps the independent `twinhospital-exp` instance at `127.0.0.1:28081`; it does not test the original desktop HIS database.

## Lifecycle and provenance

1. `registration.create` authenticates the cashier route, creates a fresh patient and registered encounter, and reads registration back. The wrapper defers native reception.
2. `queue.enter({encounter_id})` requires the field in the queue entry itself even when runtime context already identifies the encounter. The persisted queue is a runtime sidecar (`native_effect=false`).
3. The pre-consultation roles work on their authorized views. `encounter.begin_consultation` verifies queue linkage and the authenticated doctor route, invokes the native reception API, and reads back `IN_PROGRESS` and the doctor's practitioner ID.
4. The doctor creates diagnoses/orders from the actual consultation. `personnel.registry` supplies the authenticated physician signatory to trusted orchestration, not to the model for guessing.
5. `order.attest({order_id, signatory_id})` checks a separate staff registry and the fixed executing actor. The registry contains native login identities for the doctor and nurse plus the experiment's explicit signing authorization. Native signed order state and author ID are read back after the request.
6. Before `payment.prepare` and `payment.commit`, the gateway again verifies the stored signatory, native request IDs, patient/encounter, author, signed state and prescription number. Only local CASH bookkeeping is supported.

The registry's authorization is an experiment gateway policy. Successful native authentication establishes the actual account/practitioner, but this adapter does not audit the native backend's independent RBAC. In the current release, native `signedById` is absent: that fact remains `not_exposed`; the request's authenticated operator and observed author/signed state must not be described as a separately observed native signer or cryptographic signature. A nonempty mismatching native signer field fails verification immediately.

## Exact local fault definitions

| Family | Fault | Control |
|---|---|---|
| Patient/bill | Substitute another newly created patient's real bill ID; fixed executor/context | Own prepared bill |
| Missing encounter linkage | Delete `encounter_id` from queue-entry payload; valid case context unchanged | Explicit bound encounter ID |
| Locked document | Replace original LOCKED document content | Revise an editable draft version |
| Invalid signatory | Substitute registered nurse ID; doctor execution actor, role/session and permissions unchanged | Authenticated registered doctor ID |
| Duplicate payment | Repeat identical transaction ID, payload and idempotency key after first successful payment | One payment |

Each family has 40 faults and 160 controls. Tests use independent fictional fixtures, no LLMs and no source clinical records. Observers independently re-read native encounter/orders/documents/payments and persisted queue/attestation/registry state before and after the target operation. Rejection without read-back remains unknown. These are gateway-plus-native results; they do not establish native-only safety, clinical quality, or 1,000 intact model-driven encounters.

## Reproduction and evidence

The native observer records writes by request window. At close it exports all committed rows from one read-only PostgreSQL snapshot in bounded fetches. The complete export replaces the prior local snapshot only after the scan finishes. The scan includes rows with lower event IDs that committed after an earlier read. [PostgreSQL psql fetch settings](https://www.postgresql.org/docs/16/app-psql.html)

```bash
.venv/bin/python -m sandbox.experiments.linked.safety_paper \
  --output output/safety_paper_NEW_VERSION --run-id safety_paper_NEW_VERSION
.venv/bin/python deploy/openhis-experiment/verify-payments.py output/safety_paper_NEW_VERSION
.venv/bin/python deploy/openhis-experiment/export_safety_paper.py output/safety_paper_NEW_VERSION
```

Each fresh output directory stores source hashes/snapshots, request changes, fixed executor identity, responses, independently read states and classifications. Supplemental payment verification runs SQL in a read-only transaction and checks one successful payment and one matching CASH detail for each repeated transaction; it does not replace original outcomes or create a retrospective pre/post observation window. `public/` contains aggregate counts and limitations without patient identifiers. Full records stay in private local artifacts.

Earlier `safety_native_1000_v1` is retained as an engineering variant. Its missing-link, signatory and mixed duplicate-key injections differ from the paper and are not relabeled as this protocol. The first 20-unit exact-protocol smoke retained a registry-initialization classification issue and a diagnostic note; the corrected second smoke initialized staff before the observation window and then verified the same definitions on fresh fixtures.
