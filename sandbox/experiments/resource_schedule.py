"""Virtual role capacity and non-preemptive active-work reservations, in seconds.

Create one scheduler per configuration; share it across case runtimes, not sessions.
Capacities model virtual queues, not calibrated staffing or clinical qualifications.
Durations must come from the caller's explicit effort assumptions/calibration, never
model latency. Resource slots are not actor IDs, signatories or permission grants.
There is no wall-clock waiting and no service/API access in this module.
"""
from bisect import insort
from collections.abc import Mapping
import math
from threading import Lock


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _seconds(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite nonnegative number")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} must be a finite nonnegative number") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite nonnegative number")
    return number


def _first_gap(calendar: list[tuple[float, float]], ready: float,
               duration: float) -> tuple[float, float] | None:
    """Earliest whole interval that fits; endpoints are half open [start, end)."""
    start = ready
    for booked_start, booked_end in calendar:
        if start + duration <= booked_start:
            break
        if start < booked_end:
            start = booked_end
    end = start + duration
    if not math.isfinite(end) or end <= start:
        return None
    return start, end


class RoleResourceScheduler:
    """Thread-safe booking calendars with a sequential cursor for each case.

    ``resources`` maps each configured role to a positive integer slot count.
    Missing roles fail explicitly. Each reservation is atomic across its role pool
    and case cursor. Different cases/roles may overlap; one case cannot overlap its
    own activities, including concurrent calls with stale ``earliest_s`` values.

    Requests are accepted in lock-acquisition order and never move earlier bookings.
    Gap filling is supported; this is neither global FCFS by simulation time nor a
    guarantee of repeatable ordering across thread schedules. Fix dispatch order in
    the caller when reproducible queue assignments are needed. No actor/session is
    created or shared here; actor identity stays in the separate configuration map.
    """

    def __init__(self, resources: Mapping[str, int]):
        if not isinstance(resources, Mapping):
            raise ValueError("resources must map roles to positive integer capacities")
        self._pools: dict[str, list[list[tuple[float, float]]]] = {}
        for role, count in resources.items():
            _identifier(role, "role")
            if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                raise ValueError("resource capacities must be positive integers")
            self._pools[role] = [[] for _ in range(count)]
        self._case_times: dict[str, float] = {}
        self._lock = Lock()

    def reserve(self, case_id: str, role: str, active_seconds: float, *,
                earliest_s: float = 0) -> dict:
        """Book active work after both ``earliest_s`` and the case's previous end.

        Pass the runtime's current simulation time as ``earliest_s`` (including
        transcript availability or other external delays). All returned time fields
        are seconds. ``waiting_s`` counts only resource delay after ``case_ready_s``;
        it does not add to ``active_seconds``. Zero work advances the case cursor if
        needed, takes no slot, and returns ``resource_id=None`` without queue delay.
        Invalid input or unrepresentable end times leave the schedule unchanged.
        """
        _identifier(case_id, "case_id")
        _identifier(role, "role")
        duration = _seconds(active_seconds, "active_seconds")
        earliest = _seconds(earliest_s, "earliest_s")
        if role not in self._pools:
            raise ValueError("role has no configured resource capacity")
        with self._lock:
            ready = max(earliest, self._case_times.get(case_id, 0.0))
            resource_id = None
            start = end = ready
            if duration > 0:
                candidates = []
                for index, calendar in enumerate(self._pools[role]):
                    gap = _first_gap(calendar, ready, duration)
                    if gap is not None:
                        candidates.append((gap[0], index, gap[1]))
                if not candidates:
                    raise ValueError("reservation end time is not representable")
                start, index, end = min(candidates)
                insort(self._pools[role][index], (start, end))
                resource_id = f"{role}:slot-{index + 1}"
            self._case_times[case_id] = end
            return {
                "case_id": case_id, "role": role, "active_seconds": duration,
                "earliest_s": earliest, "case_ready_s": ready,
                "scheduled_start": start, "scheduled_end": end,
                "waiting_s": start - ready, "resource_id": resource_id,
            }

    def case_time(self, case_id: str) -> float:
        """Return the previous reservation end, or zero for an unseen case."""
        _identifier(case_id, "case_id")
        with self._lock:
            return self._case_times.get(case_id, 0.0)
