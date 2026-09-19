"""aggregate.py - N+6 / N+3 / Monthly Aggregate Capacity and Material Planning.

Transparent integer incremental allocation:
- Evaluates demand from DemandLedger (firm orders + residual forecast) across monthly buckets.
- Strictly bounds monthly stage capacity (minutes) and cumulative material availability.
- Explicitly accounts for unmet demand and material/capacity bottlenecks.
- Outputs AggregatePlan that directly constrains downstream weekly/daily allocation.
- Enforces strict conservation down to individual item IDs (firm and synthetic).
- Uses real calendar month mapping and strictly enforces zero capacity when no shifts exist.
- Rejects out-of-horizon demand at admission rather than stuffing into Month 0.
"""

from __future__ import annotations
from typing import Dict, List, Tuple, Any, Optional
import calendar as cal_module
import copy
from .schema import (
    PlanningSnapshot,
    DemandLedger,
    AggregatePlan,
    Configuration,
    SupplyDelivery,
    compute_object_hash
)


def get_month_duration_min(month_str: str) -> int:
    """Calculates exact duration in minutes for a given YYYY-MM month based on real calendar days."""
    try:
        parts = month_str.split("-")
        year = int(parts[0])
        month = int(parts[1])
        days = cal_module.monthrange(year, month)[1]
        return days * 1440
    except Exception:
        return 30 * 1440


def extract_month_from_bucket(bucket: str) -> str:
    parts = bucket.split(":")
    if len(parts) >= 1 and len(parts[0]) == 7 and parts[0][4] == "-":
        return parts[0]
    return "2026-10"


def plan_aggregate(
    snapshot: PlanningSnapshot,
    ledger: DemandLedger,
    horizon_months: Optional[List[str]] = None,
    plan_type: str = "N+6",
    version: int = 1,
    parent_plan_id: Optional[str] = None,
    locked_order_ids: Optional[Dict[str, List[str]]] = None,
    completed_order_ids: Optional[Dict[str, List[str]]] = None,
    existing_attributions: Optional[Dict[str, Dict[str, Any]]] = None
) -> AggregatePlan:
    """Computes monthly aggregate plan under capacity and material constraints with strict ID conservation,
    locked/completed state inheritance, and cross-month attribution tracking.
    """
    if horizon_months is None:
        if plan_type == "N+3":
            horizon_months = ["2026-10", "2026-11", "2026-12"]
        else:
            horizon_months = ["2026-10", "2026-11", "2026-12", "2027-01", "2027-02", "2027-03"]

    # 1. Real calendar month mapping: compute start and end minutes for each horizon month
    month_min_starts: Dict[str, int] = {}
    month_min_ends: Dict[str, int] = {}
    cur_min = 0
    for m in horizon_months:
        dur = get_month_duration_min(m)
        month_min_starts[m] = cur_min
        month_min_ends[m] = cur_min + dur
        cur_min += dur

    # 2. Monthly stage capacity derived strictly from calendar shifts overlapping each month
    # Zero shifts -> Zero capacity. NO arbitrary 20-day fallback!
    monthly_capacity_limit: Dict[str, Dict[str, int]] = {}
    for m in horizon_months:
        m_start = month_min_starts[m]
        m_end = month_min_ends[m]
        m_shift_mins = 0
        for s in snapshot.calendar.shifts:
            overlap_start = max(s.start_min, m_start)
            overlap_end = min(s.end_min, m_end)
            if overlap_end > overlap_start:
                m_shift_mins += (overlap_end - overlap_start)

        monthly_capacity_limit[m] = {}
        for rid, res in snapshot.resources.items():
            monthly_capacity_limit[m][res.stage] = m_shift_mins * res.capacity

    # 3. Cumulative material availability up to each month
    cum_material_supply: Dict[str, Dict[str, int]] = {m: {} for m in horizon_months}
    for m in horizon_months:
        end_min = month_min_ends[m]
        for mat, qty in snapshot.current_inventory.items():
            cum_material_supply[m][mat] = cum_material_supply[m].get(mat, 0) + qty
        for d in snapshot.known_deliveries:
            if d.available_at_min <= end_min:
                cum_material_supply[m][d.material_id] = cum_material_supply[m].get(d.material_id, 0) + d.quantity

    # 4. Initialize tracking structures
    allocations: Dict[str, Dict[str, int]] = {m: {} for m in horizon_months}
    allocated_order_ids: Dict[str, List[str]] = {m: [] for m in horizon_months}
    unmet_demand: Dict[str, Dict[str, int]] = {m: {} for m in horizon_months}
    unmet_order_ids: Dict[str, List[str]] = {m: [] for m in horizon_months}
    capacity_usage: Dict[str, Dict[str, int]] = {
        m: {res.stage: 0 for res in snapshot.resources.values()} for m in horizon_months
    }
    cum_material_consumed: Dict[str, Dict[str, int]] = {m: {} for m in horizon_months}
    material_shortages: Dict[str, Dict[str, int]] = {m: {} for m in horizon_months}
    cross_month_attributions: Dict[str, Dict[str, Any]] = copy.deepcopy(existing_attributions or {})

    # Inherit completed orders
    inherited_completed = copy.deepcopy(completed_order_ids or {})
    for m in horizon_months:
        if m not in inherited_completed:
            inherited_completed[m] = []

    # Inherit locked orders
    inherited_locked = copy.deepcopy(locked_order_ids or {})
    for m in horizon_months:
        if m not in inherited_locked:
            inherited_locked[m] = []

    # 5. Collect demand items to allocate (firm active orders and synthetic forecast orders)
    items_to_plan: List[Dict[str, Any]] = []
    locked_vin_set = set()
    for m, vins in inherited_locked.items():
        locked_vin_set.update(vins)

    for fo in ledger.firm_orders:
        m = extract_month_from_bucket(fo.forecast_bucket)
        is_locked = fo.vin in locked_vin_set
        items_to_plan.append({
            "id": fo.vin,
            "config_id": fo.config_id,
            "target_month": m,
            "is_in_horizon": (m in horizon_months),
            "is_firm": True,
            "priority": -1 if is_locked else fo.priority,
            "is_locked": is_locked,
            "locked_month": next((k for k, v in inherited_locked.items() if fo.vin in v), None)
        })
    for so in ledger.synthetic_orders:
        m = extract_month_from_bucket(so.forecast_bucket)
        items_to_plan.append({
            "id": so.vin,
            "config_id": so.config_id,
            "target_month": m,
            "is_in_horizon": (m in horizon_months),
            "is_firm": False,
            "priority": so.priority,
            "is_locked": False,
            "locked_month": None
        })

    # Sort demand: locked first, then firm orders by priority, then synthetic
    items_to_plan.sort(key=lambda x: (not x["is_locked"], not x["is_firm"], x["priority"], x["id"]))

    total_demand_count = len(items_to_plan)

    # 6. Allocation loop with locked inheritance and cross-month tracking
    for item in items_to_plan:
        cid = item["config_id"]
        item_id = item["id"]
        target_m = item["locked_month"] if item["is_locked"] and item["locked_month"] else item["target_month"]

        if not item["is_in_horizon"]:
            if target_m not in unmet_demand:
                unmet_demand[target_m] = {}
                unmet_order_ids[target_m] = []
            unmet_demand[target_m][cid] = unmet_demand[target_m].get(cid, 0) + 1
            unmet_order_ids[target_m].append(item_id)
            continue

        cfg = snapshot.configurations.get(cid)
        if not cfg:
            unmet_demand[target_m][cid] = unmet_demand[target_m].get(cid, 0) + 1
            unmet_order_ids[target_m].append(item_id)
            continue

        # Stages already completed or in-flight in physical execution have already consumed their
        # materials and machine time; they must NOT double-consume from remaining snapshot inventory!
        already_consumed_stages: Set[str] = set()
        if hasattr(snapshot, "vehicle_completed_stages") and snapshot.vehicle_completed_stages:
            already_consumed_stages.update(snapshot.vehicle_completed_stages.get(item_id, set()))
        if hasattr(snapshot, "wip_vin_states") and snapshot.wip_vin_states:
            for res_id, job in snapshot.wip_vin_states.items():
                if job.get("vin") == item_id and "stage" in job:
                    already_consumed_stages.add(job["stage"])

        target_idx = horizon_months.index(target_m)
        allocated_month: Optional[str] = None

        # If locked, only allocate to locked month
        search_range = [target_idx] if item["is_locked"] else range(target_idx, len(horizon_months))

        for try_idx in search_range:
            candidate_m = horizon_months[try_idx]

            # Check capacity only for remaining unstarted stages
            cap_ok = True
            for stage in cfg.stages:
                if stage in already_consumed_stages:
                    continue
                ptime = cfg.process_times.get(stage, 0)
                used = capacity_usage[candidate_m].get(stage, 0)
                lim = monthly_capacity_limit[candidate_m].get(stage, 0)
                if used + ptime > lim:
                    cap_ok = False
                    break

            if not cap_ok:
                continue

            # Check cumulative material availability only for remaining unconsumed stages
            mat_ok = True
            for stage, boms in cfg.process_boms.items():
                if stage in already_consumed_stages:
                    continue
                for mat, qty in boms.items():
                    for f_idx in range(try_idx, len(horizon_months)):
                        f_month = horizon_months[f_idx]
                        cur_cum = cum_material_consumed[f_month].get(mat, 0)
                        max_cum = cum_material_supply[f_month].get(mat, 0)
                        if cur_cum + qty > max_cum:
                            mat_ok = False
                            material_shortages[candidate_m][mat] = material_shortages[candidate_m].get(mat, 0) + qty
                            break
                    if not mat_ok:
                        break

            if cap_ok and mat_ok:
                allocated_month = candidate_m
                break

        if allocated_month:
            allocations[allocated_month][cid] = allocations[allocated_month].get(cid, 0) + 1
            allocated_order_ids[allocated_month].append(item_id)
            for stage in cfg.stages:
                if stage in already_consumed_stages:
                    continue
                capacity_usage[allocated_month][stage] += cfg.process_times.get(stage, 0)
            start_f_idx = horizon_months.index(allocated_month)
            for f_idx in range(start_f_idx, len(horizon_months)):
                f_m = horizon_months[f_idx]
                for stage, boms in cfg.process_boms.items():
                    if stage in already_consumed_stages:
                        continue
                    for mat, qty in boms.items():
                        cum_material_consumed[f_m][mat] = cum_material_consumed[f_m].get(mat, 0) + qty

            # Cross-month attribution tracking
            delay_months = start_f_idx - target_idx
            cross_month_attributions[item_id] = {
                "original_month": target_m,
                "assigned_month": allocated_month,
                "delay_months": delay_months
            }
        else:
            unmet_demand[target_m][cid] = unmet_demand[target_m].get(cid, 0) + 1
            unmet_order_ids[target_m].append(item_id)
            cross_month_attributions[item_id] = {
                "original_month": target_m,
                "assigned_month": None,
                "delay_months": -1
            }

    # 7. ID-level conservation verification
    total_allocated_ids = sum(len(ids) for ids in allocated_order_ids.values())
    total_unmet_ids = sum(len(ids) for ids in unmet_order_ids.values())
    assert total_demand_count == total_allocated_ids + total_unmet_ids, (
        f"AggregatePlan ID conservation failed: input={total_demand_count} != "
        f"allocated={total_allocated_ids} + unmet={total_unmet_ids}"
    )

    total_unmet = total_unmet_ids
    total_shortages = sum(sum(m_map.values()) for m_map in material_shortages.values())
    is_feasible = (total_unmet == 0) and (total_shortages == 0)

    in_hash = compute_object_hash({
        "snapshot_id": snapshot.snapshot_id,
        "demand_count": total_demand_count,
        "horizon": horizon_months,
        "plan_type": plan_type,
        "version": version
    })

    return AggregatePlan(
        plan_id=f"AGGR_{plan_type}_v{version}_{in_hash}",
        horizon_months=horizon_months,
        monthly_allocations=allocations,
        unmet_demand=unmet_demand,
        capacity_usage=capacity_usage,
        capacity_limit=monthly_capacity_limit,
        material_shortages=material_shortages,
        allocated_order_ids=allocated_order_ids,
        unmet_order_ids=unmet_order_ids,
        plan_type=plan_type,
        version=version,
        parent_plan_id=parent_plan_id,
        locked_order_ids=inherited_locked,
        completed_order_ids=inherited_completed,
        cross_month_attributions=cross_month_attributions,
        is_feasible=is_feasible,
        input_hash=in_hash
    )


def plan_n_plus_6(
    snapshot: PlanningSnapshot,
    ledger: DemandLedger,
    horizon_months: Optional[List[str]] = None,
    **kwargs
) -> AggregatePlan:
    """Convenience entry point for N+6 month aggregate plan."""
    if horizon_months is None:
        horizon_months = ["2026-10", "2026-11", "2026-12", "2027-01", "2027-02", "2027-03"]
    return plan_aggregate(snapshot, ledger, horizon_months=horizon_months, plan_type="N+6", **kwargs)


def plan_n_plus_3(
    snapshot: PlanningSnapshot,
    ledger: DemandLedger,
    horizon_months: Optional[List[str]] = None,
    n6_plan: Optional[AggregatePlan] = None,
    **kwargs
) -> AggregatePlan:
    """Convenience entry point for N+3 month rolling plan.
    If n6_plan is passed, inherits overlapping month commitments to ensure consistency.
    """
    if horizon_months is None:
        horizon_months = ["2026-10", "2026-11", "2026-12"]

    locked_ids = copy.deepcopy(kwargs.pop("locked_order_ids", {}))
    if n6_plan is not None:
        # Pre-lock allocated IDs from N+6 in overlapping months to guarantee consistency
        for m in horizon_months:
            if m in n6_plan.allocated_order_ids:
                if m not in locked_ids:
                    locked_ids[m] = []
                for vid in n6_plan.allocated_order_ids[m]:
                    if vid not in locked_ids[m]:
                        locked_ids[m].append(vid)

    return plan_aggregate(
        snapshot,
        ledger,
        horizon_months=horizon_months,
        plan_type="N+3",
        locked_order_ids=locked_ids,
        **kwargs
    )


def verify_overlap_consistency(n3_plan: AggregatePlan, n6_plan: AggregatePlan) -> Tuple[bool, List[str]]:
    """Verifies that common overlapping months between N+3 and N+6 have consistent allocations."""
    return n3_plan.verify_overlap_consistency(n6_plan)


def generate_variance_replan_aggregate(
    snapshot: PlanningSnapshot,
    previous_plan: AggregatePlan,
    execution_variance: Dict[str, Any]
) -> AggregatePlan:
    """Feedback mechanism: Generates a revised aggregate plan version (v+1) incorporating execution variance,
    such as actual completions, locked commitments, cancelled orders, and capacity shifts.
    """
    new_version = previous_plan.version + 1
    new_completed = copy.deepcopy(previous_plan.completed_order_ids)
    new_locked = copy.deepcopy(previous_plan.locked_order_ids)

    # Incorporate variance updates
    for m, ids in execution_variance.get("new_completions", {}).items():
        if m not in new_completed:
            new_completed[m] = []
        new_completed[m].extend(ids)

    for m, ids in execution_variance.get("new_locked", {}).items():
        if m not in new_locked:
            new_locked[m] = []
        new_locked[m].extend(ids)

    from .demand import net_demand
    ledger = net_demand(snapshot)

    return plan_aggregate(
        snapshot=snapshot,
        ledger=ledger,
        horizon_months=previous_plan.horizon_months,
        plan_type=previous_plan.plan_type,
        version=new_version,
        parent_plan_id=previous_plan.plan_id,
        locked_order_ids=new_locked,
        completed_order_ids=new_completed,
        existing_attributions=previous_plan.cross_month_attributions
    )
