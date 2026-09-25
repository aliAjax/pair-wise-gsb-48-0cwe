"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules, NettingRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, netting_rules: NettingRules = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.netting_rules = netting_rules or NettingRules()

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
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        updated = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        if action == "reverse":
            self._reopen_batches_for_member(actor, updated)
        return updated

    def _reopen_batches_for_member(self, actor: Actor, record: Dict[str, Any]) -> None:
        for batch in self.repository.list_batches(state="completed", limit=500):
            payload = batch["payload"]
            if record["id"] not in payload.get("member_ids", []):
                continue
            new_payload = dict(payload)
            members = []
            for member in payload.get("members", []):
                member = dict(member)
                if member["record_id"] == record["id"]:
                    member["flag"] = "reversed"
                members.append(member)
            new_payload["members"] = members
            self.repository.mutate_batch(
                batch_id=batch["id"],
                expected_version=batch["version"],
                state="pending_review",
                payload=new_payload,
                actor_id=actor.user_id,
                action="member_reversed",
                details={"summary": "成员冲正，批次回到待复核", "member_record_id": record["id"], "member_reference": record["reference"], "from": "completed", "to": "pending_review"},
            )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    def create_batch(self, actor: Actor, reference: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.netting_rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建净额批次")
        reference = text({"reference": reference}, "reference")
        checked = self.netting_rules.validate_create(data or {})
        records = [self.repository.get(record_id) for record_id in checked["member_ids"]]
        wanted = set(checked["member_ids"])
        active = self.repository.list_batches(state="pending", limit=500) + self.repository.list_batches(state="pending_review", limit=500)
        for other in active:
            if wanted & set(other["payload"].get("member_ids", [])):
                raise Conflict("指令已在其他净额批次中")
        payload = self.netting_rules.prepare_create(checked, records)
        return self.repository.create_batch(reference, self.netting_rules.INITIAL_STATE, payload, actor.user_id)

    def list_batches(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches(state=state, limit=limit)

    def get_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_batch(batch_id)

    def act_on_batch(self, actor: Actor, batch_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.netting_rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        batch = self.repository.get_batch(batch_id)
        records = [self.repository.get(record_id) for record_id in batch["payload"].get("member_ids", [])]
        data = data or {}
        if action == "reconcile":
            new_state, new_payload, summary = self.netting_rules.reconcile(batch, records, data)
            return self.repository.mutate_batch(
                batch_id=batch_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details={"summary": summary, "input": data, "from": batch["state"], "to": new_state},
            )
        if action == "review":
            new_state, new_payload, settled_ids, summary = self.netting_rules.review(batch, records)
            by_id = {record["id"]: record for record in records}
            member_updates = []
            for record_id in settled_ids:
                record = by_id[record_id]
                p = record["payload"]
                settle_data = {"delivered_quantity": int(p.get("effective_quantity", p["quantity"])), "cash_paid": float(p["net_amount"])}
                member_state, member_payload, _ = self.rules.apply_action(record, "settle", settle_data)
                member_updates.append({
                    "id": record_id,
                    "expected_version": record["version"],
                    "state": member_state,
                    "payload": member_payload,
                    "action": "settle",
                    "details": {"summary": "净额批次%s复核后一次完成" % batch["reference"], "input": settle_data, "batch_id": batch_id, "batch_reference": batch["reference"], "from": record["state"], "to": member_state},
                })
            return self.repository.complete_batch(
                batch_id=batch_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                member_updates=member_updates,
                actor_id=actor.user_id,
                action=action,
                details={"summary": summary, "input": data, "from": batch["state"], "to": new_state, "settled_members": settled_ids},
            )
        raise Conflict("当前状态不允许执行%s" % action)

    def batch_timeline(self, actor: Actor, batch_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.batch_audit_timeline(batch_id)
