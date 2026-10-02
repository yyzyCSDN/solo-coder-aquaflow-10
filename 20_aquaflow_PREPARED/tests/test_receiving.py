import unittest
from decimal import Decimal

from aquaflow.chemicals.receiving import Arrival, ReceivingService, Tank

D = Decimal


def service():
    return ReceivingService(
        tanks=[
            Tank("T-HYPO", frozenset({"sodium_hypochlorite"})),
            Tank("T-COAG", frozenset({"alum", "ferric_chloride"})),
        ],
        reviewers={"qs_lead"},
    )


def arrival(**kw):
    base = dict(arrival_id="PO-1", chemical="sodium_hypochlorite", batch="B-100",
                tank_id="T-HYPO", gross_kg=D("1000"), concentration=D("0.12"))
    base.update(kw)
    return Arrival(**base)


class ReceivingTest(unittest.TestCase):
    def test_valid_arrival_stocked_with_both_masses(self):
        s = service()
        r = s.receive(arrival())
        self.assertEqual(r["status"], "received")
        self.assertEqual(r["reasons"], [])
        inv = s.inventory()
        self.assertEqual(inv["total_kg"], D("1000"))
        self.assertEqual(inv["effective_kg"], D("120"))
        self.assertEqual(s.inventory("T-HYPO")["effective_kg"], D("120"))
        self.assertEqual(s.inventory("T-COAG")["total_kg"], D(0))

    def test_diluted_batch_held_then_released_by_reviewer(self):
        s = service()
        r = s.receive(arrival(concentration=D("0.05")))
        self.assertEqual(r["status"], "held")
        self.assertIn("diluted", r["reasons"])
        self.assertEqual(s.inventory()["total_kg"], D(0))
        self.assertEqual(s.inventory()["effective_kg"], D(0))
        out = s.review("PO-1", "qs_lead", "release", "CoA confirms 5% usable, dose adjusted")
        self.assertEqual(out["status"], "released")
        self.assertEqual(s.inventory()["total_kg"], D("1000"))
        self.assertEqual(s.inventory()["effective_kg"], D("50"))
        self.assertEqual(s.quarantine(), [])
        self.assertEqual(s.held("PO-1").review_reason, "CoA confirms 5% usable, dose adjusted")

    def test_diluted_batch_rejected_never_enters_stock(self):
        s = service()
        s.receive(arrival(concentration=D("0.05")))
        s.review("PO-1", "qs_lead", "reject", "too dilute for process")
        self.assertEqual(s.held("PO-1").status, "rejected")
        self.assertEqual(s.inventory()["total_kg"], D(0))
        self.assertEqual(s.inventory()["effective_kg"], D(0))

    def test_tank_incompatible_arrival_held(self):
        s = service()
        r = s.receive(arrival(chemical="alum", tank_id="T-HYPO", concentration=D("0.45")))
        self.assertEqual(r["status"], "held")
        self.assertEqual(r["reasons"], ["tank_incompatible"])
        self.assertEqual(s.inventory()["total_kg"], D(0))

    def test_conflict_with_chemical_already_in_tank(self):
        s = service()
        s.receive(arrival(chemical="ferric_chloride", tank_id="T-COAG", batch="B-1",
                          concentration=D("0.40")))
        r = s.receive(arrival(arrival_id="PO-2", chemical="sodium_hydroxide", batch="B-2",
                              tank_id="T-COAG", concentration=D("0.30")))
        self.assertEqual(r["status"], "held")
        self.assertIn("chemical_conflict", r["reasons"])
        self.assertEqual(s.inventory("T-COAG")["total_kg"], D("1000"))

    def test_duplicate_arrival_reference_held_and_not_double_counted(self):
        s = service()
        s.receive(arrival())
        dup = s.receive(arrival())
        self.assertEqual(dup["status"], "held")
        self.assertIn("duplicate_arrival", dup["reasons"])
        self.assertEqual(s.inventory()["total_kg"], D("1000"))
        self.assertEqual(s.inventory()["effective_kg"], D("120"))
        s.review("PO-1", "qs_lead", "reject", "duplicate transmission of PO-1")
        self.assertEqual(s.inventory()["total_kg"], D("1000"))

    def test_incomplete_delivery_data_held_and_not_releasable(self):
        s = service()
        r = s.receive(arrival(batch="  ", concentration=None))
        self.assertEqual(r["status"], "held")
        self.assertIn("missing_batch", r["reasons"])
        self.assertIn("invalid_concentration", r["reasons"])
        with self.assertRaises(ValueError):
            s.review("PO-1", "qs_lead", "release", "override")
        s.review("PO-1", "qs_lead", "reject", "paperwork incomplete")
        self.assertEqual(s.inventory()["total_kg"], D(0))

    def test_review_requires_authorized_reviewer_and_reason(self):
        s = service()
        s.receive(arrival(concentration=D("0.05")))
        with self.assertRaises(ValueError):
            s.review("PO-1", "operator", "release", "ok")
        with self.assertRaises(ValueError):
            s.review("PO-1", "qs_lead", "release", " ")
        with self.assertRaises(ValueError):
            s.review("PO-9", "qs_lead", "release", "no such arrival")
        with self.assertRaises(ValueError):
            s.review("PO-1", "qs_lead", "dispose", "bad decision")


if __name__ == "__main__":
    unittest.main()
