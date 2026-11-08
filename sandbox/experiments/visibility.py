"""Shared execution/hospital input boundary, independent of data loading and actors.

Keys use the hospital export's normalized denylist. Clinical treatment content and
source_event_ids are ordinary visible data; reference labels and credentials are not.
JSON embedded in a string is inspected without changing its representation.
"""
import json
import math
import re


FORBIDDEN_VISIBLE = frozenset({
    "gold", "groundtruth", "diagnosisgroundtruth", "reference", "referencestandard",
    "hidden", "hiddeninputs", "expectedoutput", "expectedanswer", "correctanswer",
    "platformoutput", "simulationoutput", "caseresults", "evaluationresults",
    "label", "labels", "qualified", "answerkey", "expecteddecision", "referenceplan",
    "apikey", "password", "secret", "token", "authorization",
})


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key in serialized visible input")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite number in serialized visible input")


def assert_visible(value):
    """Reject hidden/reference fields and non-JSON values; never mutate inputs."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("visible input keys must be strings")
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if (normalized in FORBIDDEN_VISIBLE or normalized.endswith("groundtruth")
                    or normalized.startswith("gold")):
                raise ValueError("visible input contains a forbidden hidden/reference field")
            assert_visible(item)
    elif isinstance(value, list):
        for item in value:
            assert_visible(item)
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            decoded = json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except ValueError:
            raise ValueError("opaque serialized visible input must be structured JSON") from None
        assert_visible(decoded)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("visible input numbers must be finite")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("visible inputs must be JSON data")
