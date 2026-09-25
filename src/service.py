"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional, Tuple

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, text
from .repository import Repository
from .rules import BATCH_COMPLETED, BATCH_PENDING, BATCH_REVIEWING, DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        if action in {"settle", "fail"}:
            active = self.repository.active_batch_for_record(record_id)
            if active is not None:
                raise Conflict("该指令属于净额批次#%s（%s），请按批次处理" % (active["id"], active["state"]))
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        result = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        if action == "reverse":
            self._on_member_reversed(result, actor.user_id, data or {})
        return result

    def _on_member_reversed(self, record: Dict[str, Any], actor_id: str, data: Dict[str, Any]) -> None:
        """成员被冲正：对应批次回到待复核并重算净额。"""
        batch = self.repository.batch_for_record_any_state(record["id"])
        if batch is None or batch["state"] != BATCH_COMPLETED:
            return
        plan, member_ids = self._build_plan(batch["payload"]["counterparty"], batch["payload"]["currency"], batch["payload"]["settlement_day"], batch["member_ids"])
        plan["state"] = BATCH_REVIEWING
        self.repository.mutate_batch(
            batch_id=batch["id"],
            expected_version=batch["version"],
            state=BATCH_REVIEWING,
            payload=plan,
            member_ids=member_ids,
            actor_id=actor_id,
            action="member_reversed",
            details={"summary": "成员%s被冲正，批次回到待复核" % record["reference"], "record_id": record["id"], "reason": data.get("reverse_reason", ""), "from": BATCH_COMPLETED, "to": BATCH_REVIEWING, "net_quantity": plan["net_quantity"], "net_amount": plan["net_amount"]},
        )

    def _build_plan(self, counterparty: str, currency: str, settlement_day: int, member_ids: List[int]) -> Tuple[Dict[str, Any], List[int]]:
        members = []
        for record_id in member_ids:
            members.append(self.repository.get(record_id))
        return self.rules.compute_batch((counterparty, currency, int(settlement_day)), members), member_ids

    def _check_member_selectable(self, record_id: int, old_ids: set, current_batch_id: int = None) -> None:
        active = self.repository.active_batch_for_record(record_id)
        if active is not None and active["id"] != current_batch_id:
            raise Conflict("成员%s已属于未完成批次#%s" % (record_id, active["id"]))
        if record_id not in old_ids:
            record = self.repository.get(record_id)
            if record["state"] in {"settled", "failed"}:
                raise Conflict("成员%s已%s，不能选入净额批次" % (record_id, record["state"]))

    def create_batch(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create_batch(actor.role):
            raise PermissionDenied("角色无权创建净额批次")
        reference = text({"reference": reference}, "reference")
        if self.repository.get_batch_by_reference(reference) is not None:
            raise Conflict("批次reference已存在")
        validated = self.rules.validate_batch_create(payload or {})
        member_ids = validated["member_ids"]
        for record_id in member_ids:
            self._check_member_selectable(record_id, set())
        plan, ordered_ids = self._build_plan(validated["counterparty"], validated["currency"], validated["settlement_day"], member_ids)
        batch = self.repository.create_batch(reference, plan["state"], plan, ordered_ids, actor.user_id)
        self._flag_pending_members(batch, actor.user_id)
        return self.repository.get_batch(batch["id"])

    def _flag_pending_members(self, batch: Dict[str, Any], actor_id: str) -> None:
        flagged = [str(member["record_id"]) for member in batch["payload"]["members"] if not member["included_in_net"]]
        if flagged:
            self.repository.add_batch_audit_only(
                batch["id"], actor_id, "flag_members",
                {"summary": "核对不齐，整批停在待处理并标出成员", "record_ids": flagged, "state": BATCH_PENDING},
            )

    def update_batch(self, actor: Actor, batch_id: int, expected_version: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create_batch(actor.role):
            raise PermissionDenied("角色无权调整净额批次")
        batch = self.repository.get_batch(batch_id)
        if batch["state"] != BATCH_PENDING:
            raise Conflict("仅待处理批次可以调整成员")
        validated = self.rules.validate_batch_create(payload or {})
        member_ids = validated["member_ids"]
        old_ids = set(batch["member_ids"])
        for record_id in member_ids:
            self._check_member_selectable(record_id, old_ids, current_batch_id=batch_id)
        plan, ordered_ids = self._build_plan(validated["counterparty"], validated["currency"], validated["settlement_day"], member_ids)
        result = self.repository.mutate_batch(
            batch_id=batch_id,
            expected_version=int(expected_version),
            state=plan["state"],
            payload=plan,
            member_ids=ordered_ids,
            actor_id=actor.user_id,
            action="update_batch",
            details={"summary": "重新选入成员并重算净额", "from": BATCH_PENDING, "to": plan["state"], "net_quantity": plan["net_quantity"], "net_amount": plan["net_amount"]},
        )
        self._flag_pending_members(result, actor.user_id)
        return self.repository.get_batch(batch_id)

    def submit_batch(self, actor: Actor, batch_id: int, expected_version: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_batch_action(actor.role, "submit_batch"):
            raise PermissionDenied("角色无权提交净额批次")
        batch = self.repository.get_batch(batch_id)
        if batch["state"] != BATCH_PENDING:
            raise Conflict("仅待处理批次可以提交")
        plan, member_ids = self._build_plan(batch["payload"]["counterparty"], batch["payload"]["currency"], batch["payload"]["settlement_day"], batch["member_ids"])
        if plan["state"] != BATCH_REVIEWING:
            flagged = [str(member["record_id"]) for member in plan["members"] if not member["included_in_net"]]
            raise Conflict("核对不齐，无法提交：成员%s" % ",".join(flagged))
        return self.repository.mutate_batch(
            batch_id=batch_id,
            expected_version=int(expected_version),
            state=BATCH_REVIEWING,
            payload=plan,
            member_ids=member_ids,
            actor_id=actor.user_id,
            action="submit_batch",
            details={"summary": "净额批次提交结算专员复核", "from": BATCH_PENDING, "to": BATCH_REVIEWING, "net_quantity": plan["net_quantity"], "net_amount": plan["net_amount"]},
        )

    def return_batch(self, actor: Actor, batch_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_batch_action(actor.role, "complete_batch"):
            raise PermissionDenied("角色无权退回净额批次")
        batch = self.repository.get_batch(batch_id)
        if batch["state"] != BATCH_REVIEWING:
            raise Conflict("仅待复核批次可以退回")
        reason = text({"reason": (data or {}).get("reason", "")}, "reason")
        return self.repository.mutate_batch(
            batch_id=batch_id,
            expected_version=int(expected_version),
            state=BATCH_PENDING,
            payload=batch["payload"],
            member_ids=batch["member_ids"],
            actor_id=actor.user_id,
            action="return_batch",
            details={"summary": "结算专员退回批次", "reason": reason, "from": BATCH_REVIEWING, "to": BATCH_PENDING},
        )

    def complete_batch(self, actor: Actor, batch_id: int, expected_version: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_batch_action(actor.role, "complete_batch"):
            raise PermissionDenied("角色无权完成净额批次")
        batch = self.repository.get_batch(batch_id)
        self.rules.require_batch_transition(batch, "complete_batch")
        plan, member_ids = self._build_plan(batch["payload"]["counterparty"], batch["payload"]["currency"], batch["payload"]["settlement_day"], batch["member_ids"])
        self.rules.validate_complete(plan)
        settlements: Dict[int, Dict[str, Any]] = {}
        for member in plan["members"]:
            record = self.repository.get(member["record_id"])
            if record["state"] == "settled":
                continue
            new_state, new_payload, summary = self.rules.apply_action(record, "settle", {
                "delivered_quantity": member["effective_quantity"],
                "cash_paid": member["net_amount"],
            })
            settlements[member["record_id"]] = {"expected_version": record["version"], "payload": new_payload}
        plan["state"] = BATCH_COMPLETED
        return self.repository.complete_batch(batch_id, int(expected_version), plan, settlements, actor.user_id)

    def list_batches(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches(state=state, limit=limit)

    def get_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_batch(batch_id)

    def batch_timeline(self, actor: Actor, batch_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.batch_timeline(batch_id)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
