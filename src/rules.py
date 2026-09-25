"""证券结算与企业行动处理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, integer_list, number, optional_text, text, text_list


INITIAL_STATE = "captured"
CREATE_ROLES = {'trader'}
ACTION_ROLES = {'apply_corporate': {'corporate_actions'}, 'approve': {'settlement_officer'}, 'settle': {'settlement_officer'}, 'fail': {'settlement_officer'}, 'reverse': {'corporate_actions', 'settlement_officer'}}
TRANSITIONS = {'apply_corporate': {'captured': 'adjusted'}, 'approve': {'captured': 'approved', 'adjusted': 'approved'}, 'settle': {'approved': 'settled'}, 'fail': {'approved': 'failed'}, 'reverse': {'settled': 'reversed', 'failed': 'reversed'}}

BATCH_PENDING = "pending"
BATCH_REVIEWING = "reviewing"
BATCH_COMPLETED = "completed"
BATCH_CREATE_ROLES = {'trader'}
BATCH_ACTION_ROLES = {'submit_batch': {'trader'}, 'complete_batch': {'settlement_officer'}}
BATCH_TRANSITIONS = {'submit_batch': {BATCH_PENDING: BATCH_REVIEWING}, 'complete_batch': {BATCH_REVIEWING: BATCH_COMPLETED}}
FLAG_CORPORATE_PENDING = "corporate_action_pending"
FLAG_KEY_MISMATCH = "batch_key_mismatch"
FLAG_NOT_APPROVED = "not_approved"
FLAG_REVERSED = "member_reversed"
MAX_BATCH_MEMBERS = 500


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        for roles in BATCH_ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_create_batch(self, role: str) -> bool:
        return role == "admin" or role in BATCH_CREATE_ROLES

    def role_can_batch_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in BATCH_ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "instrument")
        choice(p, "side", ["buy", "sell"])
        integer(p, "quantity", 1)
        number(p, "price", 0.01)
        number(p, "fees", 0)
        choice(p, "currency", ["CNY", "USD", "HKD"])
        integer(p, "settlement_day", 0)
        optional_text(p, "counterparty")
        choice(p, "corporate_action", ["none", "split", "dividend", "merger"])
        number(p, "action_ratio", 0.01)
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

    def validate_batch_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        text(p, "counterparty")
        choice(p, "currency", ["CNY", "USD", "HKD"])
        integer(p, "settlement_day", 0)
        integer_list(p, "member_ids", 1)
        if len(p["member_ids"]) > MAX_BATCH_MEMBERS:
            raise ValidationError("批次成员不能超过%s条" % MAX_BATCH_MEMBERS)
        return p

    def member_key(self, payload: Dict[str, Any]) -> Tuple[str, str, int]:
        return payload.get("counterparty", ""), payload.get("currency"), int(payload.get("settlement_day"))

    def signed_quantity(self, payload: Dict[str, Any]) -> int:
        quantity = int(payload.get("effective_quantity", payload["adjusted_quantity"]))
        return quantity if payload.get("side") == "buy" else -quantity

    def compute_batch(self, key: Tuple[str, str, int], members: List[Dict[str, Any]]) -> Dict[str, Any]:
        """按对手/币种/交收日轧差。公司行动未应用等异常成员保留原单并标出。"""
        member_snapshots: List[Dict[str, Any]] = []
        net_quantity = 0
        net_amount = 0.0
        flags_present = False
        for seq, member in enumerate(members):
            p = member["payload"]
            flags: List[str] = []
            included = False
            if member["state"] == "reversed":
                flags.append(FLAG_REVERSED)
            elif member["state"] not in {"approved", "settled"}:
                flags.append(FLAG_NOT_APPROVED)
            if not p.get("corporate_applied", False) and p.get("corporate_action", "none") != "none":
                flags.append(FLAG_CORPORATE_PENDING)
            if self.member_key(p) != key:
                flags.append(FLAG_KEY_MISMATCH)
            if not flags:
                included = True
                net_quantity += self.signed_quantity(p)
                net_amount += float(p["net_amount"]) * (1 if p.get("side") == "buy" else -1)
            else:
                flags_present = True
            member_snapshots.append({
                "seq": seq,
                "record_id": member["id"],
                "reference": member["reference"],
                "version": member["version"],
                "state": member["state"],
                "side": p.get("side"),
                "included_in_net": included,
                "flags": flags,
                "signed_quantity": self.signed_quantity(p) if included else 0,
                "signed_amount": round(float(p.get("net_amount", 0.0)) * (1 if p.get("side") == "buy" else -1), 2) if included else 0.0,
                "effective_quantity": int(p.get("effective_quantity", p.get("adjusted_quantity", p.get("quantity")))),
                "net_amount": float(p.get("net_amount", 0.0)),
            })
        net_quantity = int(net_quantity)
        net_amount = round(net_amount, 2)
        if net_quantity > 0:
            net_side = "buy"
        elif net_quantity < 0:
            net_side = "sell"
        else:
            net_side = "flat"
        return {
            "counterparty": key[0],
            "currency": key[1],
            "settlement_day": key[2],
            "state": BATCH_PENDING if flags_present else BATCH_REVIEWING,
            "net_quantity": net_quantity,
            "net_amount": net_amount,
            "net_side": net_side,
            "member_count": len(member_snapshots),
            "included_count": sum(1 for item in member_snapshots if item["included_in_net"]),
            "members": member_snapshots,
        }

    def require_batch_transition(self, batch: Dict[str, Any], action: str) -> str:
        allowed = BATCH_TRANSITIONS.get(action, {}).get(batch["state"])
        if allowed is None:
            raise Conflict("当前批次状态不允许执行%s" % action)
        return allowed

    def validate_complete(self, plan: Dict[str, Any]) -> None:
        for member in plan["members"]:
            if not member["included_in_net"]:
                if FLAG_REVERSED in member["flags"]:
                    raise Conflict("成员已被冲正，请移除后重新提交")
                raise Conflict("批次核对不齐，无法一次完成：成员%s %s" % (member["record_id"], "/".join(member["flags"])))
            if member["state"] not in {"approved", "settled"}:
                raise Conflict("成员%s状态已变化：%s" % (member["record_id"], member["state"]))
