import json
from datetime import datetime, timezone

import pytest

from sandbox.experiments.linked.native_his import (
    ROLE_SETTINGS, NativeBlocked, NativeOpenHISPort, OpenHISClient, digest,
)


def test_native_role_session_renews_before_expiry(tmp_path, monkeypatch):
    username, organization, _ = ROLE_SETTINGS["doctor"]
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({username: "test-fixture-password"}))
    credentials.chmod(0o600)
    logins = []

    def login(client, user, password, *, organization):
        logins.append((user, organization))
        client.session_expires_at = datetime.now(timezone.utc).timestamp() + 7200

    monkeypatch.setattr(OpenHISClient, "login", login)
    port = NativeOpenHISPort(tmp_path, credentials_path=credentials, diagnosis_mapping={})
    initial = port._client("doctor")
    assert port._client("doctor") is initial
    initial.session_expires_at = datetime.now(timezone.utc).timestamp() + 20
    refreshed = port._client("doctor")
    assert refreshed is not initial
    assert port._client("doctor") is refreshed
    assert logins == [(username, organization)] * 2


@pytest.mark.parametrize(("status", "expected_writes"), [
    ("DRAFT", ["/document/submit", "/document/changeLockState"]),
    ("SUBMITTED", ["/document/changeLockState"]),
    ("LOCKED", []),
])
def test_unresolved_note_attestation_recovers_by_observed_state(tmp_path, status, expected_writes):
    body = {"text": "A test encounter note."}
    document = {"id": "doc", "patientId": "patient", "encounterId": "encounter", "status": status,
                "content": {"content": [{"content": [{"text": json.dumps(body)}]}]}}
    writes = []

    class DocumentClient:
        def post(self, path, payload):
            assert payload["documentId"] == document["id"]
            if path == "/document/getContent":
                return dict(document)
            writes.append(path)
            if path == "/document/submit":
                assert document["status"] == "DRAFT"
                document["status"] = "SUBMITTED"
            elif path == "/document/changeLockState":
                assert document["status"] == "SUBMITTED" and payload["action"] == "LOCK"
                document["status"] = "LOCKED"
            else:
                pytest.fail(f"Unexpected OpenHIS operation {path}")

    port = NativeOpenHISPort(tmp_path, clients={"doctor": DocumentClient()}, diagnosis_mapping={})
    port.state["bindings"]["run:case"] = {
        "patient_id": "patient", "encounter_id": "encounter",
        "note": {"native_id": "doc", "logical_id": "note", "version": 1, "status": "draft",
                 "content_hash": digest(body)},
    }
    operation = "note.attest"
    payload = {"note_id": "note", "version": 1}
    context = {"run_id": "run", "case_id": "case", "role": "doctor", "patient_id": "patient",
               "encounter_id": "encounter", "idempotency_key": "sign"}
    port.state["idempotency"]["sign"] = {
        "state": "unresolved", "operation": operation,
        "fingerprint": digest([operation, payload, "patient", "encounter", "doctor"]),
    }

    with pytest.raises(NativeBlocked):
        port.execute(operation, {**payload, "version": 2}, context)
    assert writes == []
    assert port.execute(operation, payload, context)["readback"]["status"] == "LOCKED"
    assert port.execute(operation, payload, context)["status"] == "replayed"
    assert writes == expected_writes
