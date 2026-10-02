"""Chemical goods-receiving with compatibility staging.

Every arriving shipment is screened for concentration, batch identity, tank
compatibility, and combination rules with the chemicals already stored in
the target tank.  Shipments that fail screening—or arrive with incomplete
information—are held in a staging queue instead of entering inventory until
an authorized reviewer releases or rejects them with a recorded reason.
Staged and rejected shipments never contribute to usable stock totals.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .inventory import Lot, active_mass

# Shipment and review outcomes.
RECEIVED = "received"
STAGED = "staged"
DUPLICATE = "duplicate"
RELEASED = "released"
REJECTED = "rejected"


@dataclass(frozen=True)
class Tank:
    id: str
    allowed: frozenset[str]


@dataclass(frozen=True)
class Shipment:
    arrival_ref: str
    chemical: str
    lot: str
    tank: str
    gross_kg: Decimal | None = None
    concentration: Decimal | None = None


@dataclass(frozen=True)
class Receipt:
    arrival_ref: str
    status: str
    issues: tuple[str, ...]
    gross_kg: Decimal
    effective_kg: Decimal


@dataclass(frozen=True)
class Review:
    arrival_ref: str
    action: str
    reviewer: str
    reason: str


def _effective(shipment: Shipment) -> Decimal:
    if shipment.gross_kg is None or shipment.concentration is None:
        return Decimal(0)
    return shipment.gross_kg * shipment.concentration


def screen(
    shipment: Shipment,
    tanks: dict[str, Tank],
    contents: dict[str, set[str]],
    incompatible: set[frozenset[str]],
    specs: dict[str, Decimal],
) -> tuple[str, ...]:
    """Return the screening issues for a shipment, in deterministic order."""
    issues = []
    if not shipment.arrival_ref:
        issues.append("missing-arrival-ref")
    if not shipment.chemical:
        issues.append("missing-chemical")
    if not shipment.lot:
        issues.append("missing-lot")
    if not shipment.tank:
        issues.append("missing-tank")
    if shipment.gross_kg is None:
        issues.append("missing-gross-kg")
    elif shipment.gross_kg <= 0:
        issues.append("invalid-gross-kg")
    concentration = shipment.concentration
    if concentration is None:
        issues.append("missing-concentration")
    elif not 0 < concentration <= 1:
        issues.append("invalid-concentration")
    elif shipment.chemical in specs and concentration < specs[shipment.chemical]:
        issues.append("diluted")
    tank = tanks.get(shipment.tank)
    if shipment.tank:
        if tank is None:
            issues.append("unknown-tank")
        elif shipment.chemical and shipment.chemical not in tank.allowed:
            issues.append("tank-incompatible")
    if shipment.chemical:
        for other in sorted(contents.get(shipment.tank, ())):
            if other != shipment.chemical and frozenset((shipment.chemical, other)) in incompatible:
                issues.append("mixture-incompatible:" + other)
    return tuple(issues)


class ReceivingDock:
    """Screen incoming shipments and hold failures in a staging queue."""

    def __init__(
        self,
        tanks: Iterable[Tank],
        incompatible_pairs: Iterable[tuple[str, str]] = (),
        reviewers: Iterable[str] = (),
        specs: dict[str, Decimal] | None = None,
        contents: dict[str, Iterable[str]] | None = None,
    ) -> None:
        self._tanks = {tank.id: tank for tank in tanks}
        self._pairs = {frozenset(pair) for pair in incompatible_pairs}
        self._reviewers = set(reviewers)
        self._specs = dict(specs or {})
        self._contents = {tank: set(chemicals) for tank, chemicals in (contents or {}).items()}
        for tank in self._tanks:
            self._contents.setdefault(tank, set())
        self._lots: dict[str, Shipment] = {}
        self._staged: dict[str, tuple[Shipment, tuple[str, ...]]] = {}
        self._rejected: dict[str, Shipment] = {}
        self._reviews: list[Review] = []
        self._unreferenced = 0

    def receive(self, shipment: Shipment) -> Receipt:
        """Screen a shipment; stock it or hold it in the staging queue."""
        ref = shipment.arrival_ref
        if ref and self._known(ref):
            return Receipt(ref, DUPLICATE, ("duplicate-arrival-ref",), Decimal(0), Decimal(0))
        issues = screen(shipment, self._tanks, self._contents, self._pairs, self._specs)
        if not ref:
            self._unreferenced += 1
            ref = "unreferenced-%d" % self._unreferenced
        if issues:
            self._staged[ref] = (shipment, issues)
            return Receipt(ref, STAGED, issues, shipment.gross_kg or Decimal(0), _effective(shipment))
        self._store(ref, shipment)
        return Receipt(ref, RECEIVED, (), shipment.gross_kg, _effective(shipment))

    def release(self, arrival_ref: str, reviewer: str, reason: str) -> Receipt:
        """Authorized override: move a staged shipment into usable inventory."""
        shipment, issues = self._take(arrival_ref, reviewer, reason)
        self._store(arrival_ref, shipment)
        self._reviews.append(Review(arrival_ref, RELEASED, reviewer, reason.strip()))
        return Receipt(arrival_ref, RELEASED, issues, shipment.gross_kg or Decimal(0), _effective(shipment))

    def reject(self, arrival_ref: str, reviewer: str, reason: str) -> Receipt:
        """Authorized override: refuse a staged shipment; it never enters stock."""
        shipment, issues = self._take(arrival_ref, reviewer, reason)
        self._rejected[arrival_ref] = shipment
        self._reviews.append(Review(arrival_ref, REJECTED, reviewer, reason.strip()))
        return Receipt(arrival_ref, REJECTED, issues, Decimal(0), Decimal(0))

    def totals(self) -> dict[str, Decimal]:
        """Total (gross) and effective (active) mass of usable inventory only."""
        lots = []
        gross = Decimal(0)
        for ref, shipment in self._lots.items():
            if shipment.gross_kg is None or shipment.gross_kg < 0:
                continue
            if shipment.concentration is None or not 0 < shipment.concentration <= 1:
                continue
            gross += shipment.gross_kg
            lots.append(Lot(ref, shipment.gross_kg, shipment.concentration, True))
        return {"gross_kg": gross, "effective_kg": active_mass(lots)}

    def available(self, arrival_ref: str) -> bool:
        """True only when the shipment counts as usable inventory."""
        return arrival_ref in self._lots

    def staged(self) -> dict[str, tuple[str, ...]]:
        return {ref: issues for ref, (_, issues) in self._staged.items()}

    def reviews(self) -> tuple[Review, ...]:
        return tuple(self._reviews)

    def _take(self, arrival_ref: str, reviewer: str, reason: str) -> tuple[Shipment, tuple[str, ...]]:
        if reviewer not in self._reviewers:
            raise PermissionError("unauthorized reviewer")
        if not reason or not reason.strip():
            raise ValueError("reason required")
        try:
            return self._staged.pop(arrival_ref)
        except KeyError:
            raise ValueError("shipment not staged") from None

    def _store(self, ref: str, shipment: Shipment) -> None:
        self._lots[ref] = shipment
        if shipment.tank and shipment.chemical:
            self._contents.setdefault(shipment.tank, set()).add(shipment.chemical)

    def _known(self, ref: str) -> bool:
        return ref in self._lots or ref in self._staged or ref in self._rejected
