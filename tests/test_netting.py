import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


def base_data(**overrides):
    data = {'instrument': 'ACME', 'side': 'buy', 'quantity': 100, 'price': 12.0, 'fees': 0.0,
            'currency': 'CNY', 'settlement_day': 2, 'counterparty': 'CCP1',
            'corporate_action': 'none', 'action_ratio': 1.0}
    data.update(overrides)
    return data


OFFICER = Actor("officer", "settlement_officer")
CA_OFFICER = Actor("ca", "corporate_actions")
TRADER = Actor("trader1", "trader")


class NettingBatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.seq = 0

    def tearDown(self):
        self.temp.cleanup()

    def make_instruction(self, side, quantity, price, **overrides):
        self.seq += 1
        record = self.service.create(TRADER, "TRD-N%03d" % self.seq, base_data(side=side, quantity=quantity, price=price, **overrides))
        return self.service.act(OFFICER, record["id"], record["version"], "approve", {})

    def make_split_instruction(self, side, quantity, price, ratio, applied):
        self.seq += 1
        record = self.service.create(TRADER, "TRD-N%03d" % self.seq,
                                     base_data(side=side, quantity=quantity, price=price, corporate_action='split', action_ratio=ratio))
        if applied:
            record = self.service.act(CA_OFFICER, record["id"], record["version"], "apply_corporate", {})
        return self.service.act(OFFICER, record["id"], record["version"], "approve", {})

    def batch_key(self, **overrides):
        key = {'counterparty': 'CCP1', 'currency': 'CNY', 'settlement_day': 2, 'member_ids': []}
        key.update(overrides)
        return key

    def test_net_quantity_and_amount_buy_and_sell(self):
        buy = self.make_instruction('buy', 1000, 10.0)
        sell = self.make_instruction('sell', 600, 10.0)
        batch = self.service.create_batch(TRADER, "NB-001", self.batch_key(member_ids=[buy["id"], sell["id"]]))
        self.assertEqual(batch["state"], "reviewing")
        self.assertEqual(batch["payload"]["net_side"], "buy")
        self.assertEqual(batch["payload"]["net_quantity"], 400)
        self.assertEqual(batch["payload"]["net_amount"], 4000.0)
        self.assertEqual(batch["payload"]["included_count"], 2)

    def test_split_quantity_is_used_after_application(self):
        applied = self.make_split_instruction('buy', 1000, 10.0, 2.0, True)
        self.assertEqual(applied["payload"]["effective_quantity"], 2000)
        other = self.make_instruction('sell', 500, 11.0)
        batch = self.service.create_batch(TRADER, "NB-002", self.batch_key(member_ids=[applied["id"], other["id"]]))
        self.assertEqual(batch["payload"]["net_quantity"], 1500)

    def test_unapplied_corporate_action_member_keeps_original_order(self):
        ready = self.make_split_instruction('buy', 1000, 10.0, 2.0, True)
        pending = self.make_split_instruction('sell', 1000, 13.0, 2.0, False)
        batch = self.service.create_batch(TRADER, "NB-003", self.batch_key(member_ids=[ready["id"], pending["id"]]))
        self.assertEqual(batch["state"], "pending")
        members = {m["record_id"]: m for m in batch["payload"]["members"]}
        self.assertTrue(members[ready["id"]]["included_in_net"])
        self.assertFalse(members[pending["id"]]["included_in_net"])
        self.assertEqual(members[pending["id"]]["flags"], ["corporate_action_pending"])
        self.assertEqual(batch["payload"]["net_quantity"], 2000)
        with self.assertRaises(Conflict):
            self.service.submit_batch(TRADER, batch["id"], batch["version"])
        timeline = self.service.batch_timeline(TRADER, batch["id"])
        self.assertEqual(timeline[-1]["action"], "flag_members")
        self.assertEqual(timeline[-1]["details"]["record_ids"], [str(pending["id"])])

    def test_update_batch_after_fix_goes_to_reviewing(self):
        ready = self.make_split_instruction('buy', 1000, 10.0, 2.0, True)
        pending = self.make_split_instruction('sell', 1000, 13.0, 2.0, False)
        batch = self.service.create_batch(TRADER, "NB-004", self.batch_key(member_ids=[ready["id"], pending["id"]]))
        self.assertEqual(batch["state"], "pending")
        replacement = self.make_split_instruction('sell', 500, 14.0, 2.0, True)
        batch = self.service.update_batch(TRADER, batch["id"], batch["version"],
                                          self.batch_key(member_ids=[ready["id"], replacement["id"]]))
        self.assertEqual(batch["state"], "reviewing")
        self.assertEqual(batch["payload"]["net_quantity"], 1000)

    def test_batch_key_mismatch_is_flagged(self):
        wrong = self.make_instruction('buy', 10, 10.0, currency='USD')
        batch = self.service.create_batch(TRADER, "NB-005", self.batch_key(member_ids=[wrong["id"]]))
        self.assertEqual(batch["state"], "pending")
        self.assertEqual(batch["payload"]["members"][0]["flags"], ["batch_key_mismatch"])

    def test_complete_settles_every_member_once(self):
        first = self.make_instruction('buy', 1000, 10.0)
        second = self.make_instruction('sell', 600, 15.0)
        batch = self.service.create_batch(TRADER, "NB-006", self.batch_key(member_ids=[first["id"], second["id"]]))
        completed = self.service.complete_batch(OFFICER, batch["id"], batch["version"])
        self.assertEqual(completed["state"], "completed")
        settled_first = self.service.get_record(TRADER, first["id"])
        settled_second = self.service.get_record(TRADER, second["id"])
        self.assertEqual(settled_first["state"], "settled")
        self.assertEqual(settled_second["state"], "settled")
        self.assertEqual(settled_first["payload"]["delivered_quantity"], 1000)
        self.assertEqual(settled_second["payload"]["delivered_quantity"], 600)
        self.assertEqual(settled_second["payload"]["cash_paid"], 9000.0)
        actions = [event["action"] for event in self.service.timeline(TRADER, first["id"])]
        self.assertEqual(actions, ["created", "approve", "settle"])
        batch_actions = [event["action"] for event in self.service.batch_timeline(TRADER, batch["id"])]
        self.assertEqual(batch_actions, ["created", "complete_batch"])

    def test_member_reversal_returns_batch_to_reviewing(self):
        first = self.make_instruction('buy', 1000, 10.0)
        second = self.make_instruction('sell', 600, 15.0)
        batch = self.service.create_batch(TRADER, "NB-007", self.batch_key(member_ids=[first["id"], second["id"]]))
        batch = self.service.complete_batch(OFFICER, batch["id"], batch["version"])
        settled_first = self.service.get_record(TRADER, first["id"])
        self.service.act(OFFICER, settled_first["id"], settled_first["version"], "reverse", {"reverse_reason": "数据错误"})
        batch = self.service.get_batch(TRADER, batch["id"])
        self.assertEqual(batch["state"], "reviewing")
        members = {m["record_id"]: m for m in batch["payload"]["members"]}
        self.assertFalse(members[first["id"]]["included_in_net"])
        self.assertEqual(members[first["id"]]["flags"], ["member_reversed"])
        self.assertTrue(members[second["id"]]["included_in_net"])
        self.assertEqual(batch["payload"]["net_quantity"], -600)
        with self.assertRaises(Conflict):
            self.service.complete_batch(OFFICER, batch["id"], batch["version"])
        last = self.service.batch_timeline(TRADER, batch["id"])[-1]
        self.assertEqual(last["action"], "member_reversed")

    def test_return_and_resubmit_flow(self):
        member = self.make_instruction('buy', 10, 10.0)
        batch = self.service.create_batch(TRADER, "NB-008", self.batch_key(member_ids=[member["id"]]))
        self.assertEqual(batch["state"], "reviewing")
        batch = self.service.return_batch(OFFICER, batch["id"], batch["version"], {"reason": "再核一遍"})
        self.assertEqual(batch["state"], "pending")
        batch = self.service.submit_batch(TRADER, batch["id"], batch["version"])
        self.assertEqual(batch["state"], "reviewing")
        batch = self.service.complete_batch(OFFICER, batch["id"], batch["version"])
        self.assertEqual(batch["state"], "completed")

    def test_member_cannot_join_two_active_batches_or_settle_individually(self):
        member = self.make_instruction('buy', 10, 10.0)
        self.service.create_batch(TRADER, "NB-009", self.batch_key(member_ids=[member["id"]]))
        other = self.make_instruction('buy', 5, 16.0)
        with self.assertRaises(Conflict):
            self.service.create_batch(TRADER, "NB-010", self.batch_key(member_ids=[member["id"], other["id"]]))
        with self.assertRaises(Conflict):
            self.service.act(OFFICER, member["id"], member["version"], "settle",
                             {"delivered_quantity": 10, "cash_paid": 100.0})

    def test_permissions_and_version_conflict(self):
        member = self.make_instruction('buy', 10, 10.0)
        with self.assertRaises(PermissionDenied):
            self.service.create_batch(Actor("outsider", "outsider"), "NB-X", self.batch_key(member_ids=[member["id"]]))
        batch = self.service.create_batch(TRADER, "NB-011", self.batch_key(member_ids=[member["id"]]))
        with self.assertRaises(PermissionDenied):
            self.service.complete_batch(TRADER, batch["id"], batch["version"])
        with self.assertRaises(Conflict):
            self.service.complete_batch(OFFICER, batch["id"], batch["version"] - 1)

    def test_validation_requires_members(self):
        with self.assertRaises(ValidationError):
            self.service.create_batch(TRADER, "NB-012", self.batch_key(member_ids=[]))


if __name__ == "__main__":
    unittest.main()
