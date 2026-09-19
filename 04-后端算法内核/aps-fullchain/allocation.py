"""allocation.py - Weekly / Daily Vehicle-Level Allocation and Cross-Day Migration.

Implements Section 4.3 of APS Fullchain Specification:
- Directly constrained by AggregatePlan monthly quotas and ID-level allocations.
- Evaluates individual vehicles (no mandatory 4-car batching).
- Enables cross-day migration for orders when daily capacity/material is saturated.
- Respects order.release_at_min and current snapshot time as hard lower bounds on day assignment.
- Strictly freezes WIP vehicles (SETUP, PROCESSING, BLOCKED, BUFFERED, IN_TRANSIT) in place.
- Verifies strict conservation: input candidate orders = assigned orders + unmet orders.
"""

from __future__ import annotations
from typing import Dict, List, Tuple, Any, Optional, Set
import copy
from .schema import (
    PlanningSnapshot,
    AggregatePlan,
    OrderPlan,
    Order,
    VehicleStatus,
    compute_object_hash
)


def allocate_orders(
    snapshot: PlanningSnapshot,
    aggregate_plan: AggregatePlan,
    policy_name: str = "DEFAULT"
) -> Tuple[OrderPlan, List[str]]:
    """Allocates candidate orders to specific days, strictly constrained by AggregatePlan quotas.
    Returns (OrderPlan, unmet_vins).
    """
    m0 = aggregate_plan.horizon_months[0] if aggregate_plan.horizon_months else "2026-10"
    all_allocated_ids = set()
    order_allocated_month: Dict[str, str] = {}
    for m, ids in aggregate_plan.allocated_order_ids.items():
        all_allocated_ids.update(ids)
        for vid in ids:
            order_allocated_month[vid] = m

    monthly_quotas = copy.deepcopy(aggregate_plan.monthly_allocations)

    # 1. Determine available days and daily stage capacity (in minutes)
    calendar = snapshot.calendar
    day_shifts: Dict[int, int] = {}  # day -> total work minutes
    for s in calendar.shifts:
        day_shifts[s.day_idx] = day_shifts.get(s.day_idx, 0) + s.duration_min

    # Ensure all days up to horizon end are indexed
    max_day = calendar.minutes_to_day(calendar.horizon_end_min - 1)
    for d in range(max_day + 1):
        if d not in day_shifts:
            day_shifts[d] = 0

    w_capacity_min_per_day: Dict[int, int] = {}
    for d, mins in day_shifts.items():
        w_res = snapshot.resources.get("W_LINE") or next((r for r in snapshot.resources.values() if r.stage == "W"), None)
        cap = w_res.capacity if w_res else 1
        w_capacity_min_per_day[d] = mins * cap

    # 2. Identify candidate orders
    # Active orders include firm orders and synthetic orders
    candidate_orders: List[Order] = []
    for o in snapshot.orders:
        if o.status in [VehicleStatus.COMPLETED, VehicleStatus.CANCELLED]:
            continue
        candidate_orders.append(o)

    # Sort candidates by policy preference
    if "COLOR" in policy_name:
        candidate_orders.sort(key=lambda o: (
            o.priority,
            calendar.minutes_to_day(o.release_at_min),
            snapshot.configurations[o.config_id].color if o.config_id in snapshot.configurations else "",
            o.due_at_min,
            o.vin
        ))
    else:
        candidate_orders.sort(key=lambda o: (o.priority, o.due_at_min, o.release_at_min, o.vin))

    daily_allocations: Dict[int, List[str]] = {d: [] for d in range(max_day + 1)}
    daily_w_used: Dict[int, int] = {d: 0 for d in range(max_day + 1)}
    unmet_vins: List[str] = []

    # Track daily cumulative material limits
    # On-hand inventory + unreceived deliveries available by day start
    daily_supply: Dict[int, Dict[str, int]] = {d: dict(snapshot.current_inventory) for d in range(max_day + 1)}
    for d in range(max_day + 1):
        day_start_min = calendar.day_to_start_min(d)
        for deliv in snapshot.known_deliveries:
            if deliv.available_at_min <= day_start_min:
                daily_supply[d][deliv.material_id] = daily_supply[d].get(deliv.material_id, 0) + deliv.quantity

    cum_material_used: Dict[str, int] = {}
    current_day = calendar.minutes_to_day(snapshot.now_min)

    for order in candidate_orders:
        cid = order.config_id
        cfg = snapshot.configurations.get(cid)
        if not cfg:
            unmet_vins.append(order.vin)
            continue

        # Check if order is in WIP (Frozen Zone protection)
        is_wip = order.status in [
            VehicleStatus.SETUP,
            VehicleStatus.PROCESSING,
            VehicleStatus.BLOCKED,
            VehicleStatus.BUFFERED,
            VehicleStatus.IN_TRANSIT
        ]
        if is_wip:
            # Locked in frozen zone: preserve existing day or assign current_day
            locked_day = order.assigned_day if order.assigned_day is not None else current_day
            locked_day = min(locked_day, max_day)
            daily_allocations[locked_day].append(order.vin)
            alloc_m = order_allocated_month.get(order.vin)
            if alloc_m and alloc_m in monthly_quotas:
                monthly_quotas[alloc_m][cid] = max(0, monthly_quotas[alloc_m].get(cid, 0) - 1)
            continue

        # Check AggregatePlan admission: ID-level or quota
        if all_allocated_ids:
            if order.vin not in all_allocated_ids:
                unmet_vins.append(order.vin)
                continue
        else:
            has_quota = any(q.get(cid, 0) > 0 for q in monthly_quotas.values())
            if not has_quota:
                unmet_vins.append(order.vin)
                continue

        w_time = cfg.process_times.get("W", 0)
        earliest_day = max(calendar.minutes_to_day(order.release_at_min), current_day)

        assigned_day: Optional[int] = None

        # Cross-day migration search: try earliest_day, and if full, migrate to subsequent days
        for try_day in range(earliest_day, max_day + 1):
            if day_shifts.get(try_day, 0) == 0:
                continue

            # Check welding capacity on try_day
            if daily_w_used[try_day] + w_time > w_capacity_min_per_day.get(try_day, 0):
                continue  # Saturated on this day, migrate to next day

            # Check material availability on try_day
            mat_ok = True
            for stage, boms in cfg.process_boms.items():
                for m_id, q_req in boms.items():
                    cur_used = cum_material_used.get(m_id, 0)
                    avail = daily_supply[try_day].get(m_id, 0)
                    if cur_used + q_req > avail:
                        mat_ok = False
                        break
                if not mat_ok:
                    break

            if not mat_ok:
                continue  # Materials not ready on this day, migrate to next day

            assigned_day = try_day
            break

        if assigned_day is not None:
            daily_allocations[assigned_day].append(order.vin)
            daily_w_used[assigned_day] += w_time
            alloc_m = order_allocated_month.get(order.vin)
            if alloc_m and alloc_m in monthly_quotas:
                monthly_quotas[alloc_m][cid] = max(0, monthly_quotas[alloc_m].get(cid, 0) - 1)
            order.assigned_day = assigned_day
            # Deduct material estimate
            for stage, boms in cfg.process_boms.items():
                for m_id, q_req in boms.items():
                    cum_material_used[m_id] = cum_material_used.get(m_id, 0) + q_req
        else:
            unmet_vins.append(order.vin)

    # Verify strict conservation
    total_assigned = sum(len(vins) for vins in daily_allocations.values())
    total_unmet = len(unmet_vins)
    assert len(candidate_orders) == total_assigned + total_unmet, (
        f"Order allocation conservation failed: candidates={len(candidate_orders)} != "
        f"assigned={total_assigned} + unmet={total_unmet}"
    )

    # 3. Create stage dispatch priority preferences
    stage_dispatch_orders: Dict[str, List[str]] = {
        "W": [],
        "P": [],
        "A": []
    }
    for d in range(max_day + 1):
        for v in daily_allocations[d]:
            stage_dispatch_orders["W"].append(v)
            stage_dispatch_orders["P"].append(v)
            stage_dispatch_orders["A"].append(v)

    in_hash = compute_object_hash({
        "m0": m0,
        "policy": policy_name,
        "daily_allocations": daily_allocations,
        "unmet": unmet_vins
    })

    plan = OrderPlan(
        plan_id=f"ORDPLAN_{in_hash}",
        version=1,
        policy_name=policy_name,
        daily_allocations=daily_allocations,
        stage_dispatch_orders=stage_dispatch_orders,
        input_hash=in_hash
    )
    return plan, unmet_vins
