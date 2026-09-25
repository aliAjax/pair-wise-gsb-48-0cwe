"""证券结算与企业行动处理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "captured"
CREATE_ROLES = {'trader'}
ACTION_ROLES = {'apply_corporate': {'corporate_actions'}, 'approve': {'settlement_officer'}, 'settle': {'settlement_officer'}, 'fail': {'settlement_officer'}, 'reverse': {'corporate_actions', 'settlement_officer'}}
TRANSITIONS = {'apply_corporate': {'captured': 'adjusted'}, 'approve': {'captured': 'approved', 'adjusted': 'approved'}, 'settle': {'approved': 'settled'}, 'fail': {'approved': 'failed'}, 'reverse': {'settled': 'reversed', 'failed': 'reversed'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "instrument")
        choice(p, "side", ["buy", "sell"])
        integer(p, "quantity", 1)
        number(p, "price", 0.01)
        number(p, "fees", 0)
        choice(p, "currency", ["CNY", "USD", "HKD"])
        integer(p, "settlement_day", 0)
        choice(p, "corporate_action", ["none", "split", "dividend", "merger"])
        number(p, "action_ratio", 0.01)
        optional_text(p, "counterparty")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        gross = float(p["quantity"]) * float(p["price"])
        fee = float(p["fees"])
        p["gross_amount"] = round(gross, 2)
        p["net_amount"] = round(gross + fee if p["side"] == "buy" else gross - fee, 2)
        p["adjusted_quantity"] = p["quantity"]
        p["adjusted_price"] = p["price"]
        if p["corporate_action"] == "split":
            p["adjusted_quantity"] = int(float(p["quantity"]) * float(p["action_ratio"]))
            p["adjusted_price"] = round(float(p["price"]) / float(p["action_ratio"]), 4)
        elif p["corporate_action"] == "dividend":
            p["cash_entitlement"] = round(float(p["quantity"]) * float(p["action_ratio"]), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"settled", "reversed"} and item["payload"].get("instrument") == payload.get("instrument") and item["payload"].get("settlement_day") == payload.get("settlement_day"):
                if item["payload"].get("side") == payload.get("side") and item["payload"].get("quantity") == payload.get("quantity") and item["payload"].get("price") == payload.get("price"):
                    raise Conflict("疑似重复结算指令")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "apply_corporate":
            if p["corporate_action"] == "none":
                raise ValidationError("没有待处理的公司行动")
            changes["corporate_applied"] = True
            changes["effective_quantity"] = p["adjusted_quantity"]
            changes["effective_price"] = p["adjusted_price"]
            summary = "公司行动已应用"
        elif action == "approve":
            changes["approved_amount"] = p["net_amount"]
            summary = "结算指令复核通过"
        elif action == "settle":
            delivered = integer(data, "delivered_quantity", 0)
            paid = number(data, "cash_paid", 0)
            required_quantity = int(p.get("effective_quantity", p["quantity"]))
            if delivered != required_quantity:
                raise ValidationError("交收证券数量不匹配")
            if paid < float(p["net_amount"]):
                raise ValidationError("交收资金不足")
            changes["delivered_quantity"] = delivered
            changes["cash_paid"] = paid
            summary = "交收完成"
        elif action == "fail":
            changes["fail_reason"] = text(data, "fail_reason")
            summary = "交收失败"
        elif action == "reverse":
            changes["reverse_reason"] = text(data, "reverse_reason")
            summary = "交收冲正"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)


NETTING_INITIAL_STATE = "pending"
NETTING_CREATE_ROLES = {'trader'}
NETTING_ACTION_ROLES = {'reconcile': {'trader'}, 'review': {'settlement_officer'}}


class NettingRules:
    """净额批次规则：选入已复核指令、净额计算、核对与复核完成。"""

    INITIAL_STATE = NETTING_INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(NETTING_CREATE_ROLES)
        for roles in NETTING_ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in NETTING_CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in NETTING_ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "counterparty")
        choice(p, "currency", ["CNY", "USD", "HKD"])
        integer(p, "settlement_day", 0)
        member_ids = p.get("member_ids")
        if not isinstance(member_ids, list) or len(member_ids) < 2:
            raise ValidationError("member_ids至少需要2项")
        if any(isinstance(item, bool) or not isinstance(item, int) for item in member_ids):
            raise ValidationError("member_ids必须是整数列表")
        if len(set(member_ids)) != len(member_ids):
            raise ValidationError("member_ids不能重复")
        return p

    def _snapshot(self, record: Dict[str, Any]) -> Dict[str, Any]:
        p = record["payload"]
        corporate_pending = p.get("corporate_action") != "none" and not p.get("corporate_applied", False)
        if record["state"] == "approved":
            flag = "kept_original" if corporate_pending else "included"
        elif record["state"] == "settled":
            flag = "settled"
        elif record["state"] == "reversed":
            flag = "reversed"
        else:
            flag = "excluded_state"
        return {
            "record_id": record["id"],
            "reference": record["reference"],
            "instrument": p.get("instrument", ""),
            "side": p.get("side", ""),
            "quantity": p.get("quantity"),
            "effective_quantity": int(p.get("effective_quantity", p.get("quantity"))),
            "net_amount": p.get("net_amount"),
            "corporate_action": p.get("corporate_action", "none"),
            "corporate_applied": bool(p.get("corporate_applied", False)),
            "flag": flag,
        }

    def _netting(self, members: List[Dict[str, Any]]) -> Tuple[int, float, str]:
        net_quantity = 0
        net_amount = 0.0
        for member in members:
            if member["flag"] != "included":
                continue
            sign = 1 if member["side"] == "buy" else -1
            net_quantity += sign * int(member["effective_quantity"])
            net_amount -= sign * float(member["net_amount"])
        net_amount = round(net_amount, 2)
        direction = "buy" if net_quantity > 0 else ("sell" if net_quantity < 0 else "flat")
        return net_quantity, net_amount, direction

    def _check_members(self, data: Dict[str, Any], records: List[Dict[str, Any]]) -> None:
        for record in records:
            p = record["payload"]
            if record["state"] != "approved":
                raise ValidationError("指令%s不是已复核状态" % record["reference"])
            if p.get("counterparty", "").strip() != str(data["counterparty"]).strip():
                raise ValidationError("指令%s清算对手不一致" % record["reference"])
            if p.get("currency") != data["currency"]:
                raise ValidationError("指令%s币种不一致" % record["reference"])
            if p.get("settlement_day") != data["settlement_day"]:
                raise ValidationError("指令%s交收日不一致" % record["reference"])

    def _fill(self, payload: Dict[str, Any], members: List[Dict[str, Any]]) -> Dict[str, Any]:
        net_quantity, net_amount, direction = self._netting(members)
        payload["members"] = members
        payload["included_count"] = sum(1 for member in members if member["flag"] == "included")
        payload["kept_original_count"] = sum(1 for member in members if member["flag"] == "kept_original")
        payload["net_quantity"] = net_quantity
        payload["net_amount"] = net_amount
        payload["net_direction"] = direction
        return payload

    def prepare_create(self, payload: Dict[str, Any], records: List[Dict[str, Any]]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        self._check_members(p, records)
        members = [self._snapshot(record) for record in records]
        batch = {
            "counterparty": str(p["counterparty"]).strip(),
            "currency": p["currency"],
            "settlement_day": p["settlement_day"],
            "member_ids": list(p["member_ids"]),
            "expected_net_quantity": None,
            "expected_net_amount": None,
            "mismatch_members": [],
            "settled_members": [],
        }
        return self._fill(batch, members)

    def reconcile(self, batch: Dict[str, Any], records: List[Dict[str, Any]], data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        if batch["state"] not in {"pending", "pending_review"}:
            raise Conflict("当前状态不允许执行reconcile")
        expected_quantity = integer(data, "expected_net_quantity")
        expected_amount = round(number(data, "expected_net_amount"), 2)
        members = [self._snapshot(record) for record in records]
        p = self._fill(dict(batch["payload"]), members)
        net_quantity, net_amount = p["net_quantity"], p["net_amount"]
        mismatch = net_quantity != expected_quantity or net_amount != expected_amount
        mismatch_members = []
        if mismatch:
            mismatch_members = [m["record_id"] for m in members if m["flag"] != "included" or m["effective_quantity"] != m["quantity"]]
            if not mismatch_members:
                mismatch_members = [m["record_id"] for m in members]
        p["expected_net_quantity"] = expected_quantity
        p["expected_net_amount"] = expected_amount
        p["mismatch_members"] = mismatch_members
        new_state = "pending" if mismatch else "pending_review"
        summary = "核对不齐，批次停留待处理并标出成员" if mismatch else "核对一致，批次待复核"
        return new_state, p, summary

    def review(self, batch: Dict[str, Any], records: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Any], List[int], str]:
        if batch["state"] != "pending_review":
            raise Conflict("当前状态不允许执行review")
        members = [self._snapshot(record) for record in records]
        if members != batch["payload"].get("members"):
            raise Conflict("成员状态已变化，请重新核对")
        p = dict(batch["payload"])
        settled: List[int] = []
        updated_members = []
        for member in members:
            member = dict(member)
            if member["flag"] == "included":
                member["flag"] = "settled"
                settled.append(member["record_id"])
            updated_members.append(member)
        p["members"] = updated_members
        p["settled_members"] = sorted(set(p.get("settled_members", [])) | set(settled))
        p["mismatch_members"] = []
        summary = "批次复核通过，%s项成员指令一次完成" % len(settled)
        return "completed", p, settled, summary
