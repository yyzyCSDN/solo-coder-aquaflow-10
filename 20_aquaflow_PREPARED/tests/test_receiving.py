import unittest
from decimal import Decimal

from aquaflow.api import AquaFlowService
from aquaflow.chemicals.receiving import ReceivingDock, Shipment, Tank

D = Decimal

TANKS = [Tank("T1", frozenset({"naocl", "hcl"})), Tank("T2", frozenset({"alum"}))]
PAIRS = [("naocl", "hcl")]
REVIEWERS = ["qsmith"]
SPECS = {"naocl": D("0.10"), "alum": D("0.40")}


def make_dock():
    return ReceivingDock(TANKS, PAIRS, REVIEWERS, SPECS)


def naocl(ref, lot="L1", tank="T1", gross="1000", conc="0.12"):
    return Shipment(ref, "naocl", lot, tank, D(gross), D(conc) if conc is not None else None)


class ReceivingTest(unittest.TestCase):
    def test_clean_receipt_enters_inventory(self):
        d = make_dock()
        r = d.receive(naocl("A1"))
        self.assertEqual(r.status, "received")
        self.assertEqual(r.issues, ())
        self.assertEqual(r.gross_kg, D("1000"))
        self.assertEqual(r.effective_kg, D("120.00"))
        self.assertTrue(d.available("A1"))
        self.assertEqual(d.totals(), {"gross_kg": D("1000"), "effective_kg": D("120.00")})

    def test_same_chemical_restock_accumulates(self):
        d = make_dock()
        self.assertEqual(d.receive(naocl("A2")).status, "received")
        self.assertEqual(d.receive(naocl("A3", lot="L2")).status, "received")
        self.assertEqual(d.totals(), {"gross_kg": D("2000"), "effective_kg": D("240.00")})

    def test_diluted_batch_is_staged_not_stocked(self):
        d = make_dock()
        r = d.receive(naocl("A4", conc="0.05"))
        self.assertEqual(r.status, "staged")
        self.assertIn("diluted", r.issues)
        self.assertFalse(d.available("A4"))
        self.assertIn("A4", d.staged())
        self.assertEqual(d.totals(), {"gross_kg": D(0), "effective_kg": D(0)})

    def test_invalid_concentration_is_staged(self):
        d = make_dock()
        r = d.receive(naocl("A5", conc="1.5"))
        self.assertEqual(r.status, "staged")
        self.assertIn("invalid-concentration", r.issues)

    def test_tank_incompatible_is_staged(self):
        d = make_dock()
        r = d.receive(Shipment("A6", "alum", "L9", "T1", D("500"), D("0.44")))
        self.assertEqual(r.status, "staged")
        self.assertIn("tank-incompatible", r.issues)
        self.assertFalse(d.available("A6"))
        self.assertEqual(d.totals(), {"gross_kg": D(0), "effective_kg": D(0)})

    def test_unknown_tank_is_staged(self):
        d = make_dock()
        r = d.receive(naocl("A7", tank="T9"))
        self.assertEqual(r.status, "staged")
        self.assertIn("unknown-tank", r.issues)

    def test_mixture_incompatible_is_staged(self):
        d = make_dock()
        self.assertEqual(d.receive(naocl("A8")).status, "received")
        r = d.receive(Shipment("A9", "hcl", "L2", "T1", D("200"), D("0.30")))
        self.assertEqual(r.status, "staged")
        self.assertIn("mixture-incompatible:naocl", r.issues)
        self.assertFalse(d.available("A9"))
        self.assertEqual(d.totals()["gross_kg"], D("1000"))

    def test_incomplete_information_is_staged(self):
        d = make_dock()
        r = d.receive(Shipment("A10", "naocl", "", "T1", D("100"), None))
        self.assertEqual(r.status, "staged")
        self.assertIn("missing-lot", r.issues)
        self.assertIn("missing-concentration", r.issues)
        self.assertFalse(d.available("A10"))
        self.assertEqual(d.totals(), {"gross_kg": D(0), "effective_kg": D(0)})

    def test_duplicate_arrival_ref_is_not_double_counted(self):
        d = make_dock()
        d.receive(naocl("A11"))
        r = d.receive(naocl("A11", lot="L2"))
        self.assertEqual(r.status, "duplicate")
        self.assertIn("duplicate-arrival-ref", r.issues)
        self.assertEqual(d.totals(), {"gross_kg": D("1000"), "effective_kg": D("120.00")})

    def test_duplicate_of_staged_ref_stays_staged_once(self):
        d = make_dock()
        d.receive(naocl("A12", conc="0.05"))
        r = d.receive(naocl("A12", conc="0.05"))
        self.assertEqual(r.status, "duplicate")
        self.assertEqual(list(d.staged()), ["A12"])
        self.assertEqual(d.totals(), {"gross_kg": D(0), "effective_kg": D(0)})

    def test_authorized_release_adds_to_inventory(self):
        d = make_dock()
        d.receive(naocl("A13", conc="0.05"))
        r = d.release("A13", "qsmith", "lab retest confirms trade strength")
        self.assertEqual(r.status, "released")
        self.assertIn("diluted", r.issues)
        self.assertTrue(d.available("A13"))
        self.assertEqual(d.staged(), {})
        self.assertEqual(d.totals(), {"gross_kg": D("1000"), "effective_kg": D("50.00")})
        (review,) = d.reviews()
        self.assertEqual((review.action, review.reviewer), ("released", "qsmith"))
        self.assertEqual(review.reason, "lab retest confirms trade strength")

    def test_unauthorized_reviewer_cannot_release(self):
        d = make_dock()
        d.receive(naocl("A14", conc="0.05"))
        with self.assertRaises(PermissionError):
            d.release("A14", "intruder", "let it through")
        self.assertFalse(d.available("A14"))
        self.assertIn("A14", d.staged())
        self.assertEqual(d.reviews(), ())

    def test_release_requires_reason(self):
        d = make_dock()
        d.receive(naocl("A15", conc="0.05"))
        with self.assertRaises(ValueError):
            d.release("A15", "qsmith", "  ")
        self.assertIn("A15", d.staged())

    def test_reject_removes_shipment_from_consideration(self):
        d = make_dock()
        d.receive(naocl("A16", conc="0.05"))
        r = d.reject("A16", "qsmith", "supplier diluted load")
        self.assertEqual(r.status, "rejected")
        self.assertFalse(d.available("A16"))
        self.assertEqual(d.staged(), {})
        self.assertEqual(d.totals(), {"gross_kg": D(0), "effective_kg": D(0)})
        with self.assertRaises(ValueError):
            d.release("A16", "qsmith", "second thoughts")

    def test_review_of_unknown_ref_fails(self):
        d = make_dock()
        with self.assertRaises(ValueError):
            d.release("NOPE", "qsmith", "nothing there")

    def test_staged_never_counts_alongside_stock(self):
        d = make_dock()
        d.receive(naocl("A17"))
        d.receive(naocl("A18", conc="0.05"))
        self.assertEqual(d.totals(), {"gross_kg": D("1000"), "effective_kg": D("120.00")})


class ReceivingWorkflowTest(unittest.TestCase):
    def payload(self, **shipment):
        base = {"arrival_ref": "A1", "chemical": "naocl", "lot": "L1", "tank": "T1",
                "gross_kg": "1000", "concentration": "0.12"}
        base.update(shipment)
        return {"shipment": base,
                "tanks": [{"id": "T1", "allowed": ["naocl", "hcl"]}],
                "specs": {"naocl": "0.10"}}

    def test_workflow_receive(self):
        r = AquaFlowService().run_workflow("chemical_receiving", self.payload())
        self.assertEqual(r["decision"], "receive")
        self.assertEqual(r["value"], [])
        self.assertEqual(r["evidence"]["gross_kg"], D("1000"))
        self.assertEqual(r["evidence"]["effective_kg"], D("120.00"))

    def test_workflow_stage_diluted(self):
        r = AquaFlowService().run_workflow("chemical_receiving", self.payload(concentration="0.05"))
        self.assertEqual(r["decision"], "stage")
        self.assertIn("diluted", r["value"])

    def test_workflow_stage_tank_incompatible(self):
        r = AquaFlowService().run_workflow("chemical_receiving", self.payload(chemical="alum"))
        self.assertEqual(r["decision"], "stage")
        self.assertIn("tank-incompatible", r["value"])

    def test_workflow_stage_mixture_incompatible(self):
        p = self.payload(chemical="hcl", concentration="0.30")
        p["contents"] = {"T1": ["naocl"]}
        p["incompatible"] = [["naocl", "hcl"]]
        r = AquaFlowService().run_workflow("chemical_receiving", p)
        self.assertEqual(r["decision"], "stage")
        self.assertIn("mixture-incompatible:naocl", r["value"])

    def test_workflow_stage_incomplete(self):
        p = self.payload()
        del p["shipment"]["concentration"]
        p["shipment"]["lot"] = ""
        r = AquaFlowService().run_workflow("chemical_receiving", p)
        self.assertEqual(r["decision"], "stage")
        self.assertIn("missing-concentration", r["value"])
        self.assertIn("missing-lot", r["value"])

    def test_workflow_duplicate(self):
        p = self.payload()
        p["seen_refs"] = ["A1"]
        r = AquaFlowService().run_workflow("chemical_receiving", p)
        self.assertEqual(r["decision"], "duplicate")


if __name__ == "__main__":
    unittest.main()
