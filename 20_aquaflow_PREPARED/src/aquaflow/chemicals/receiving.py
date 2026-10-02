"""Chemical arrival receiving with compatibility screening and quarantine.

Each arrival is checked for concentration validity, batch identification,
tank compatibility, and pairwise compatibility with the chemicals already
stored in the target tank.  Arrivals that fail any check are held in a
quarantine queue instead of entering stock; only authorized reviewers may
release or reject a held arrival, and only with a recorded reason.  Held
and rejected arrivals never contribute to inventory mass.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from .inventory import Lot, mass_summary

# Acceptable concentration range (min, max) per chemical; below min is diluted.
SPECS: dict[str, tuple[Decimal, Decimal]] = {
    "sodium_hypochlorite": (Decimal("0.10"), Decimal("0.16")),
    "alum": (Decimal("0.40"), Decimal("0.50")),
    "ferric_chloride": (Decimal("0.35"), Decimal("0.45")),
    "sodium_hydroxide": (Decimal("0.25"), Decimal("0.50")),
}

# Chemical pairs that must never share a tank.
INCOMPATIBLE: frozenset[frozenset[str]] = frozenset({
    frozenset({"sodium_hypochlorite", "hydrochloric_acid"}),
    frozenset({"sodium_hypochlorite", "ammonia"}),
    frozenset({"ferric_chloride", "sodium_hydroxide"}),
})

# Holds caused by defective delivery data cannot be overridden by review;
# such arrivals must be rejected (or redelivered with correct paperwork).
STRUCTURAL = frozenset({"missing_batch", "invalid_gross", "invalid_concentration", "unknown_tank"})


@dataclass(frozen=True)
class Tank:
    id: str
    allowed: frozenset[str]


@dataclass(frozen=True)
class Arrival:
    arrival_id: str
    chemical: str
    batch: str
    tank_id: str
    gross_kg: Decimal
    concentration: Decimal | None


@dataclass(frozen=True)
class Held:
    arrival: Arrival
    reasons: tuple[str, ...]
    status: str = "held"  # held | released | rejected
    reviewer: str | None = None
    review_reason: str | None = None


class ReceivingService:
    """Screen arrivals, quarantine failures, and track usable stock."""

    def __init__(self, tanks, reviewers, specs=None, incompatible=None):
        self._tanks = {t.id: t for t in tanks}
        self._reviewers = frozenset(reviewers)
        self._specs = SPECS if specs is None else specs
        self._incompatible = INCOMPATIBLE if incompatible is None else incompatible
        self._seen: set[str] = set()
        self._held: dict[str, Held] = {}
        self._contents: dict[str, list[tuple[str, Lot]]] = {t.id: [] for t in tanks}

    def receive(self, arrival: Arrival) -> dict:
        reasons = self._check(arrival)
        self._seen.add(arrival.arrival_id)
        if not reasons:
            self._stock(arrival)
            return {"arrival_id": arrival.arrival_id, "status": "received", "reasons": []}
        self._held.setdefault(arrival.arrival_id, Held(arrival, tuple(reasons)))
        return {"arrival_id": arrival.arrival_id, "status": "held", "reasons": reasons}

    def review(self, arrival_id: str, reviewer: str, decision: str, reason: str) -> dict:
        if reviewer not in self._reviewers:
            raise ValueError("unauthorized reviewer")
        if not reason or not reason.strip():
            raise ValueError("reason required")
        held = self._held.get(arrival_id)
        if held is None or held.status != "held":
            raise ValueError("not held")
        if decision == "release":
            if STRUCTURAL & set(held.reasons):
                raise ValueError("not stockable")
            self._stock(held.arrival)
        elif decision != "reject":
            raise ValueError("unknown decision")
        status = "released" if decision == "release" else "rejected"
        self._held[arrival_id] = replace(held, status=status, reviewer=reviewer, review_reason=reason)
        return {"arrival_id": arrival_id, "status": status, "reviewer": reviewer}

    def inventory(self, tank_id: str | None = None) -> dict:
        if tank_id is not None:
            lots = [lot for _, lot in self._contents.get(tank_id, [])]
        else:
            lots = [lot for rows in self._contents.values() for _, lot in rows]
        return mass_summary(lots)

    def quarantine(self) -> list[Held]:
        return [h for h in self._held.values() if h.status == "held"]

    def held(self, arrival_id: str) -> Held | None:
        return self._held.get(arrival_id)

    def _check(self, arrival: Arrival) -> list[str]:
        reasons = []
        if arrival.arrival_id in self._seen:
            reasons.append("duplicate_arrival")
        if not arrival.batch or not arrival.batch.strip():
            reasons.append("missing_batch")
        if arrival.gross_kg is None or arrival.gross_kg < 0:
            reasons.append("invalid_gross")
        c = arrival.concentration
        if c is None or not 0 < c <= 1:
            reasons.append("invalid_concentration")
        else:
            spec = self._specs.get(arrival.chemical)
            if spec is not None and c < spec[0]:
                reasons.append("diluted")
            elif spec is not None and c > spec[1]:
                reasons.append("concentrated")
        tank = self._tanks.get(arrival.tank_id)
        if tank is None:
            reasons.append("unknown_tank")
        else:
            if arrival.chemical not in tank.allowed:
                reasons.append("tank_incompatible")
            stocked = {chemical for chemical, _ in self._contents[tank.id]}
            if any(frozenset({arrival.chemical, other}) in self._incompatible for other in stocked):
                reasons.append("chemical_conflict")
        return reasons

    def _stock(self, arrival: Arrival) -> None:
        lot = Lot(arrival.batch, arrival.gross_kg, arrival.concentration)
        self._contents[arrival.tank_id].append((arrival.chemical, lot))
