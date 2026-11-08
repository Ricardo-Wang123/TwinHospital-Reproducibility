"""Conservative frequency encoding; no dose, timing, route or regimen inference."""
import unicodedata


VERSION = "explicit-frequency-aliases-v2"

# Only exact lexical equivalents are supported. Interval schedules remain
# interval schedules: Q12H is never turned into BID, nor Q8H into TID.
_ALIASES = {
    "QD": ("q.d.", "daily", "once daily", "once a day", "one time daily", "1 time daily",
           "每日一次", "每天一次", "一天一次", "每日1次", "每天1次", "1次/日", "1/day"),
    "BID": ("b.i.d.", "twice daily", "twice a day", "two times daily", "2 times daily",
            "每日两次", "每天两次", "一天两次", "每日二次", "每天二次", "每日2次", "每天2次", "2次/日", "2/day"),
    "TID": ("t.i.d.", "three times daily", "three times a day", "3 times daily",
            "每日三次", "每天三次", "一天三次", "每日3次", "每天3次", "3次/日", "3/day"),
    "QID": ("q.i.d.", "four times daily", "four times a day", "4 times daily",
            "每日四次", "每天四次", "一天四次", "每日4次", "每天4次", "4次/日", "4/day"),
    "Q12H": ("every 12 hours", "every twelve hours", "每12小时一次", "每十二小时一次"),
    "Q8H": ("every 8 hours", "every eight hours", "每8小时一次", "每八小时一次"),
    "Q6H": ("every 6 hours", "every six hours", "每6小时一次", "每六小时一次"),
    # The OpenHIS frequency dictionary defines HS as daily at bedtime.
    # This does not apply to a bare nightly or as-needed instruction.
    "HS": ("qhs", "q.h.s.", "at bedtime", "nightly at bedtime", "every night at bedtime",
           "临睡时", "每晚睡前", "每晚睡前一次"),
}


def _key(value):
    return " ".join(unicodedata.normalize("NFKC", value).strip().casefold().split())


_LOOKUP = {_key(alias): code for code, aliases in _ALIASES.items() for alias in aliases}


def normalize_frequency(raw, allowed_codes):
    """Return a reversible lexical annotation, or ValueError before HIS writes.

    ``allowed_codes`` is the actual native dictionary for this product. Exact
    native codes, including PRN if present, pass through; combinations, dose
    ranges, free-text PRN instructions and unknown schedules are never guessed.
    A recognized frequency code does not certify clinical appropriateness.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("frequency must be a nonempty explicit string")
    if not isinstance(allowed_codes, (dict, set, frozenset, list, tuple)) or not allowed_codes:
        raise ValueError("native frequency dictionary is required")
    allowed = set(allowed_codes)
    if any(not isinstance(code, str) or not code for code in allowed):
        raise ValueError("invalid native frequency dictionary")
    key = _key(raw)
    canonical = {code.casefold(): code for code in allowed}.get(key)
    basis = "exact_native_code"
    if canonical is None:
        canonical = _LOOKUP.get(key)
        basis = "explicit_lexical_equivalence"
    if canonical is None or canonical not in allowed:
        raise ValueError("unsupported or ambiguous frequency; no schedule was inferred")
    return {"original": raw, "canonical": canonical, "version": VERSION, "basis": basis,
            "clinical_appropriateness": "not_assessed", "dose_or_schedule_changed": False}
