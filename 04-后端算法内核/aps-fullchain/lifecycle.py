"""lifecycle.py - Plan Approval, Shop Floor Dispatch, Receipt Idempotency, and Baseline Versioning.

Implements Section 6 of APS Fullchain Specification:
- Local simulation of plan approval and business receipt (no external systems needed).
- Baseline cannot become active execution target until approved AND received.
- Scoped dispatch and receipt with idempotent tokens: plan_version + scope.
- Audit ledger tracking plan versions, approval decisions, receipts, and transitions.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import hashlib
from .schema import OrderPlan, compute_object_hash


@dataclass
class ReceiptRecord:
    receipt_id: str
    scope: str                      # e.g., "ALL", "WELDING", "PAINTING", "ASSEMBLY"
    idempotency_key: str            # plan_id + scope
    received_at_min: int
    acknowledged: bool = True


@dataclass
class PlanBaseline:
    baseline_id: str
    plan_id: str
    version: int
    order_plan: OrderPlan
    status: str = "PENDING_APPROVAL" # "PENDING_APPROVAL", "APPROVED", "ACTIVE", "REJECTED", "SUPERSEDED"
    created_at_min: int = 0
    approved_by: Optional[str] = None
    approved_at_min: Optional[int] = None
    rejection_reason: Optional[str] = None
    receipts: Dict[str, ReceiptRecord] = field(default_factory=dict) # scope -> ReceiptRecord


class LifecycleManager:
    """Manages baseline lifecycles, approval gates, and idempotent dispatch receipts."""

    def __init__(self):
        self.baselines: Dict[str, PlanBaseline] = {}
        self.active_baseline_id: Optional[str] = None
        self.audit_log: List[Dict[str, Any]] = []

    def submit_plan(self, order_plan: OrderPlan, author: str, at_min: int) -> PlanBaseline:
        base_id = f"BASE_{order_plan.plan_id}_v{order_plan.version}"
        baseline = PlanBaseline(
            baseline_id=base_id,
            plan_id=order_plan.plan_id,
            version=order_plan.version,
            order_plan=order_plan,
            created_at_min=at_min
        )
        self.baselines[base_id] = baseline
        self._log("SUBMITTED", base_id, at_min, {"author": author})
        return baseline

    def approve_plan(self, baseline_id: str, approver: str, at_min: int) -> bool:
        base = self.baselines.get(baseline_id)
        if not base:
            return False
        if base.status != "PENDING_APPROVAL":
            return False
        base.status = "APPROVED"
        base.approved_by = approver
        base.approved_at_min = at_min
        self._log("APPROVED", baseline_id, at_min, {"approver": approver})
        return True

    def reject_plan(self, baseline_id: str, approver: str, at_min: int, reason: str) -> bool:
        base = self.baselines.get(baseline_id)
        if not base:
            return False
        base.status = "REJECTED"
        base.rejection_reason = reason
        self._log("REJECTED", baseline_id, at_min, {"approver": approver, "reason": reason})
        return True

    def dispatch_and_receive(self, baseline_id: str, scope: str, at_min: int) -> Tuple[bool, str]:
        """Dispatches plan to shop floor scope and records idempotent receipt acknowledgment."""
        base = self.baselines.get(baseline_id)
        if not base:
            return False, "BASELINE_NOT_FOUND"
        if base.status not in ["APPROVED", "ACTIVE"]:
            return False, f"CANNOT_DISPATCH_STATUS_{base.status}"

        # Form idempotent key
        idemp_key = f"{base.plan_id}::{base.version}::{scope}"

        # If already acknowledged for this scope, return success idempotently
        if scope in base.receipts and base.receipts[scope].idempotency_key == idemp_key:
            self._log("IDEMPOTENT_RECEIPT_HIT", baseline_id, at_min, {"scope": scope, "key": idemp_key})
            return True, base.receipts[scope].receipt_id

        # New receipt acknowledgement
        rec_id = f"REC_{hashlib.sha256(idemp_key.encode('utf-8')).hexdigest()[:12]}"
        receipt = ReceiptRecord(
            receipt_id=rec_id,
            scope=scope,
            idempotency_key=idemp_key,
            received_at_min=at_min,
            acknowledged=True
        )
        base.receipts[scope] = receipt

        # If full scope received, mark as ACTIVE and supersede previous
        if scope == "ALL" or len(base.receipts) >= 3:
            if self.active_baseline_id and self.active_baseline_id != baseline_id:
                prev = self.baselines.get(self.active_baseline_id)
                if prev:
                    prev.status = "SUPERSEDED"
            base.status = "ACTIVE"
            self.active_baseline_id = baseline_id

        self._log("RECEIPT_CONFIRMED", baseline_id, at_min, {"scope": scope, "receipt_id": rec_id})
        return True, rec_id

    def _log(self, action: str, baseline_id: str, at_min: int, payload: Dict[str, Any]) -> None:
        self.audit_log.append({
            "action": action,
            "baseline_id": baseline_id,
            "timestamp_min": at_min,
            "payload": payload
        })
