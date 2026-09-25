import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


TRADER = Actor("trader1", "trader")
OFFICER = Actor("officer1", "settlement_officer")
CORPORATE = Actor("corp1", "corporate_actions")


def instruction(side="buy", quantity=100, price=10.0, fees=0.0, counterparty="CPTY-A", currency="CNY", day=5, corporate_action="none", ratio=1.0):
    return {
        "instrument": "ACME",
        "side": side,
        "quantity": quantity,
        "price": price,
        "fees": fees,
        "currency": currency,
        "settlement_day": day,
        "corporate_action": corporate_action,
        "action_ratio": ratio,
        "counterparty": counterparty,
    }


class NettingTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.references = 0

    def tearDown(self):
        self.temp.cleanup()

    def make_record(self, data=None, apply_corporate=False, approve=True):
        self.references += 1
        record = self.service.create(TRADER, "TRD-N%03d" % self.references, data or instruction())
        if apply_corporate:
            record = self.service.act(CORPORATE, record["id"], record["version"], "apply_corporate", {})
        if approve:
            record = self.service.act(OFFICER, record["id"], record["version"], "approve", {})
        return record

    def make_batch(self, member_ids, reference="NB-0001", counterparty="CPTY-A", currency="CNY", day=5):
        return self.service.create_batch(TRADER, reference, {"counterparty": counterparty, "currency": currency, "settlement_day": day, "member_ids": member_ids})


class NettingFlowTest(NettingTestBase):
    def test_full_flow_and_reopen_on_reverse(self):
        buy = self.make_record(instruction(side="buy", quantity=100, price=10.0))
        sell = self.make_record(instruction(side="sell", quantity=40, price=10.0))
        batch = self.make_batch([buy["id"], sell["id"]])
        self.assertEqual(batch["state"], "pending")
        self.assertEqual(batch["payload"]["net_quantity"], 60)
        self.assertEqual(batch["payload"]["net_amount"], -600.0)
        self.assertEqual(batch["payload"]["net_direction"], "buy")

        batch = self.service.act_on_batch(TRADER, batch["id"], batch["version"], "reconcile", {"expected_net_quantity": 61, "expected_net_amount": -600.0})
        self.assertEqual(batch["state"], "pending")
        self.assertEqual(batch["payload"]["mismatch_members"], [buy["id"], sell["id"]])

        batch = self.service.act_on_batch(TRADER, batch["id"], batch["version"], "reconcile", {"expected_net_quantity": 60, "expected_net_amount": -600.0})
        self.assertEqual(batch["state"], "pending_review")
        self.assertEqual(batch["payload"]["mismatch_members"], [])

        batch = self.service.act_on_batch(OFFICER, batch["id"], batch["version"], "review", {})
        self.assertEqual(batch["state"], "completed")
        self.assertEqual(sorted(batch["payload"]["settled_members"]), sorted([buy["id"], sell["id"]]))
        for member_id in (buy["id"], sell["id"]):
            self.assertEqual(self.service.get_record(OFFICER, member_id)["state"], "settled")

        reversed_record = self.service.act(OFFICER, sell["id"], self.service.get_record(OFFICER, sell["id"])["version"], "reverse", {"reverse_reason": "对手方违约"})
        self.assertEqual(reversed_record["state"], "reversed")
        batch = self.service.get_batch(TRADER, batch["id"])
        self.assertEqual(batch["state"], "pending_review")
        flags = {member["record_id"]: member["flag"] for member in batch["payload"]["members"]}
        self.assertEqual(flags[sell["id"]], "reversed")

        batch = self.service.act_on_batch(TRADER, batch["id"], batch["version"], "reconcile", {"expected_net_quantity": 0, "expected_net_amount": 0.0})
        self.assertEqual(batch["state"], "pending_review")
        batch = self.service.act_on_batch(OFFICER, batch["id"], batch["version"], "review", {})
        self.assertEqual(batch["state"], "completed")

        timeline = self.service.batch_timeline(TRADER, batch["id"])
        actions = [event["action"] for event in timeline]
        self.assertEqual(actions, ["created", "reconcile", "reconcile", "review", "member_reversed", "reconcile", "review"])

    def test_kept_original_member_stays_untouched(self):
        kept = self.make_record(instruction(side="buy", quantity=100, price=10.0, corporate_action="split", ratio=2.0))
        sell = self.make_record(instruction(side="sell", quantity=40, price=10.0))
        batch = self.make_batch([kept["id"], sell["id"]])
        self.assertEqual(batch["payload"]["kept_original_count"], 1)
        self.assertEqual(batch["payload"]["included_count"], 1)
        self.assertEqual(batch["payload"]["net_quantity"], -40)
        self.assertEqual(batch["payload"]["net_amount"], 400.0)
        self.assertEqual(batch["payload"]["net_direction"], "sell")

        batch = self.service.act_on_batch(TRADER, batch["id"], batch["version"], "reconcile", {"expected_net_quantity": -40, "expected_net_amount": 400.0})
        batch = self.service.act_on_batch(OFFICER, batch["id"], batch["version"], "review", {})
        self.assertEqual(batch["state"], "completed")
        self.assertEqual(self.service.get_record(OFFICER, sell["id"])["state"], "settled")
        self.assertEqual(self.service.get_record(OFFICER, kept["id"])["state"], "approved")

    def test_adjusted_quantity_is_netted_and_settled(self):
        adjusted = self.make_record(instruction(side="buy", quantity=100, price=10.0, corporate_action="split", ratio=2.0), apply_corporate=True)
        sell = self.make_record(instruction(side="sell", quantity=200, price=5.0))
        batch = self.make_batch([adjusted["id"], sell["id"]])
        self.assertEqual(batch["payload"]["net_quantity"], 0)
        self.assertEqual(batch["payload"]["net_amount"], 0.0)
        self.assertEqual(batch["payload"]["net_direction"], "flat")

        batch = self.service.act_on_batch(TRADER, batch["id"], batch["version"], "reconcile", {"expected_net_quantity": 0, "expected_net_amount": 0.0})
        batch = self.service.act_on_batch(OFFICER, batch["id"], batch["version"], "review", {})
        self.assertEqual(batch["state"], "completed")
        settled = self.service.get_record(OFFICER, adjusted["id"])
        self.assertEqual(settled["state"], "settled")
        self.assertEqual(settled["payload"]["delivered_quantity"], 200)

    def test_mismatch_flags_adjusted_member(self):
        adjusted = self.make_record(instruction(side="buy", quantity=100, price=10.0, corporate_action="split", ratio=2.0), apply_corporate=True)
        plain = self.make_record(instruction(side="sell", quantity=50, price=5.0))
        batch = self.make_batch([adjusted["id"], plain["id"]])
        batch = self.service.act_on_batch(TRADER, batch["id"], batch["version"], "reconcile", {"expected_net_quantity": 999, "expected_net_amount": 0.0})
        self.assertEqual(batch["state"], "pending")
        self.assertEqual(batch["payload"]["mismatch_members"], [adjusted["id"]])


class NettingFailureTest(NettingTestBase):
    def test_permission_denied(self):
        buy = self.make_record()
        sell = self.make_record(instruction(side="sell", quantity=40))
        with self.assertRaises(PermissionDenied):
            self.service.create_batch(OFFICER, "NB-P1", {"counterparty": "CPTY-A", "currency": "CNY", "settlement_day": 5, "member_ids": [buy["id"], sell["id"]]})
        batch = self.make_batch([buy["id"], sell["id"]], reference="NB-P2")
        with self.assertRaises(PermissionDenied):
            self.service.act_on_batch(OFFICER, batch["id"], batch["version"], "reconcile", {"expected_net_quantity": 60, "expected_net_amount": -600.0})
        batch = self.service.act_on_batch(TRADER, batch["id"], batch["version"], "reconcile", {"expected_net_quantity": 60, "expected_net_amount": -600.0})
        with self.assertRaises(PermissionDenied):
            self.service.act_on_batch(TRADER, batch["id"], batch["version"], "review", {})

    def test_member_validation(self):
        captured = self.service.create(TRADER, "TRD-V001", instruction(quantity=100))
        approved = self.make_record(instruction(side="sell", quantity=40))
        with self.assertRaises(ValidationError):
            self.make_batch([captured["id"], approved["id"]], reference="NB-V1")
        usd = self.make_record(instruction(quantity=200, currency="USD"))
        with self.assertRaises(ValidationError):
            self.make_batch([approved["id"], usd["id"]], reference="NB-V2")
        other_counterparty = self.make_record(instruction(quantity=300, counterparty="CPTY-B"))
        with self.assertRaises(ValidationError):
            self.make_batch([approved["id"], other_counterparty["id"]], reference="NB-V3")
        with self.assertRaises(ValidationError):
            self.make_batch([approved["id"], approved["id"]], reference="NB-V4")

    def test_member_cannot_join_two_active_batches(self):
        first = self.make_record()
        second = self.make_record(instruction(side="sell", quantity=40))
        third = self.make_record(instruction(side="sell", quantity=10))
        self.make_batch([first["id"], second["id"]], reference="NB-C1")
        with self.assertRaises(Conflict):
            self.make_batch([first["id"], third["id"]], reference="NB-C2")

    def test_stale_version_and_invalid_transition(self):
        buy = self.make_record()
        sell = self.make_record(instruction(side="sell", quantity=40))
        batch = self.make_batch([buy["id"], sell["id"]], reference="NB-S1")
        with self.assertRaises(Conflict):
            self.service.act_on_batch(OFFICER, batch["id"], batch["version"], "review", {})
        with self.assertRaises(Conflict):
            self.service.act_on_batch(TRADER, batch["id"], batch["version"] + 1, "reconcile", {"expected_net_quantity": 60, "expected_net_amount": -600.0})
