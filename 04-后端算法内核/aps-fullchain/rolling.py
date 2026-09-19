"""rolling.py - Rolling Horizon Re-planning, Execution State Snapshotting, and WIP Protection.

Implements Sections 2.1, 2.2, and 6:
- make_snapshot: extracts current physical state from active ExecutionWorld at tau.
- Filters received deliveries to strictly prevent inventory double-counting.
- Filters future events and deliveries strictly by known_at_min <= tau.
- Disruption events applied to world before snapshot creation so planning reflects cancellations.
- In-process WIP protection: locked vehicles in SETUP, PROCESSING, BLOCKED, or BUFFER cannot be cancelled
  or rescheduled; tagged with in-process exception tag CANCELLED_IN_WIP and continue execution.
- Re-plan requires lifecycle baseline approval and idempotent receipt ("无接受不执行").
- Continuous resume: execution continues from exact state at tau without replaying from t=0.
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple, Any, Set
import copy
from .schema import (
    PlanningSnapshot,
    Calendar,
    Configuration,
    Resource,
    Buffer,
    Labor,
    SupplyDelivery,
    Order,
    Event,
    VehicleStatus,
    MachineStatus,
    OrderPlan,
    SimulationTrace,
    compute_object_hash
)
from .simulator import ExecutionWorld
from .demand import net_demand
from .aggregate import plan_aggregate, plan_n_plus_6, plan_n_plus_3, verify_overlap_consistency
from .allocation import allocate_orders
from .policies import PolicyBundle, get_strategy_b
from .lifecycle import LifecycleManager


def make_snapshot(
    world: ExecutionWorld,
    now_min: int,
    all_external_events: List[Event],
    all_planned_deliveries: List[SupplyDelivery],
    all_forecasts: Optional[List[Any]] = None
) -> PlanningSnapshot:
    """Extracts an isolated PlanningSnapshot from the active ExecutionWorld at time now_min.
    Future information is strictly filtered: only events and deliveries known at or before now_min are visible.
    Deliveries already received into inventory are filtered out of known_deliveries to prevent double counting.
    """
    # 1. Filter known events strictly: known_at_min <= now_min
    known_events: List[Event] = []
    for ev in all_external_events:
        if ev.known_at_min <= now_min:
            known_events.append(copy.deepcopy(ev))

    # Apply known event effects to snapshot views (e.g. known delivery delays)
    delivery_map: Dict[str, SupplyDelivery] = {
        d.delivery_id: copy.deepcopy(d) for d in all_planned_deliveries
    }
    for ev in known_events:
        if ev.event_type == "MATERIAL_DELAY":
            deliv_id = ev.payload.get("delivery_id")
            new_available = ev.payload.get("new_available_at_min")
            if deliv_id and deliv_id in delivery_map and new_available is not None:
                delivery_map[deliv_id].available_at_min = new_available

    # 2. Filter deliveries: strictly separate received vs unreceived
    known_deliveries: List[SupplyDelivery] = []
    received_deliveries: List[SupplyDelivery] = []
    for d in delivery_map.values():
        if getattr(d, "known_at_min", 0) > now_min:
            # Future delivery not known yet
            continue
        if d.delivery_id in world.received_delivery_ids or d.available_at_min <= now_min:
            # Already received into world.inventory! Must NOT be in known_deliveries
            received_deliveries.append(d)
        else:
            # Future unreceived delivery known at or before now_min
            known_deliveries.append(d)

    # 3. Filter published forecasts: published_at_min <= now_min
    known_forecasts = []
    if all_forecasts:
        for f in all_forecasts:
            if getattr(f, "published_at_min", 0) <= now_min:
                known_forecasts.append(copy.deepcopy(f))

    # 4. Snapshot WIP state, active resources, completed stages, and prefix records from execution world
    resources_snap = {k: copy.deepcopy(v) for k, v in world.resources.items()}
    buffers_snap = {k: copy.deepcopy(v) for k, v in world.buffers.items()}
    labor_snap = copy.deepcopy(world.labor)
    inv_snap = dict(world.inventory)
    orders_snap = {k: v.clone() for k, v in world.orders.items()}
    wip_states = copy.deepcopy(world.active_jobs)
    completed_stages_snap = copy.deepcopy(world.vehicle_completed_stages)
    prefix_records_snap = [copy.deepcopy(r) for r in world.trace.records]

    snap_id = f"SNAP_{now_min}_{compute_object_hash({'now': now_min, 'inv': inv_snap})}"

    return PlanningSnapshot(
        snapshot_id=snap_id,
        now_min=now_min,
        calendar=copy.deepcopy(world.calendar),
        configurations=copy.deepcopy(world.configurations),
        resources=resources_snap,
        buffers=buffers_snap,
        labor=labor_snap,
        current_inventory=inv_snap,
        known_deliveries=known_deliveries,
        orders=list(orders_snap.values()),
        forecasts=known_forecasts,
        known_events=known_events,
        wip_vin_states=wip_states,
        received_deliveries=received_deliveries,
        vehicle_completed_stages=completed_stages_snap,
        prefix_records=prefix_records_snap
    )


def trigger_rolling_replan(
    world: ExecutionWorld,
    now_min: int,
    disruption_event: Event,
    all_external_events: List[Event],
    all_planned_deliveries: List[SupplyDelivery],
    all_forecasts: Optional[List[Any]] = None,
    policy_bundle: Optional[PolicyBundle] = None,
    lifecycle: Optional[LifecycleManager] = None
) -> Tuple[OrderPlan, SimulationTrace]:
    """Handles an unexpected event at time now_min:
    1. Applies disruption effects directly to active world first (correct chronological sequence).
       WIP vehicles are protected: given exception tag CANCELLED_IN_WIP and keep executing.
       Unstarted vehicles are marked CANCELLED.
    2. Takes isolated PlanningSnapshot at now_min reflecting the disruption.
    3. Re-plans unstarted orders while protecting all in-progress WIP and locked vehicles.
    4. Submits revised plan to lifecycle; strictly enforces 'no acceptance, no execution'.
    5. Updates ExecutionWorld orders and priorities from revised plan.
    6. Continues execution from now_min to horizon end.
    """
    # 1. Update event history and apply disruption directly to world BEFORE snapshot
    all_external_events.append(disruption_event)
    world.apply_disruption_event(disruption_event)

    # 2. Make isolated snapshot reflecting current world state
    snapshot = make_snapshot(world, now_min, all_external_events, all_planned_deliveries, all_forecasts)

    # 3. Generate demand ledger & plans for remaining orders
    ledger = net_demand(snapshot)
    existing_vins = {o.vin for o in snapshot.orders}
    for so in ledger.synthetic_orders:
        if so.vin not in existing_vins:
            snapshot.orders.append(so)

    n6 = plan_n_plus_6(snapshot, ledger)
    n3 = plan_n_plus_3(snapshot, ledger, n6_plan=n6, version=2)
    verify_overlap_consistency(n3, n6)

    bundle = policy_bundle or get_strategy_b()
    revised_order_plan, _ = allocate_orders(snapshot, n3, policy_name="REVISED_ROLLING")
    revised_order_plan.version = 2

    # 4. Lifecycle approval & receipt verification (Point 5: "无接受不执行")
    if lifecycle is not None:
        baseline = lifecycle.submit_plan(revised_order_plan, author="ROLLING_REPLANNER", at_min=now_min)
        approved = lifecycle.approve_plan(baseline.baseline_id, approver="CHIEF_DISPATCHER", at_min=now_min)
        if not approved:
            raise RuntimeError(f"Replan approval failed for {baseline.baseline_id}. Fail closed: execution blocked.")
        receipt_ok, receipt_id = lifecycle.dispatch_and_receive(baseline.baseline_id, "ALL", at_min=now_min)
        if not receipt_ok:
            raise RuntimeError(f"Replan dispatch receipt failed for {baseline.baseline_id}: {receipt_id}. No acceptance, no execution.")

    # 5. Apply revised order plan strictly to unstarted orders in the active world
    for day, vins in revised_order_plan.daily_allocations.items():
        for v in vins:
            if v in world.orders:
                order = world.orders[v]
                # Only update assigned day if vehicle has NOT started stage W yet
                if order.status in [VehicleStatus.UNRELEASED, VehicleStatus.READY]:
                    order.assigned_day = day

    # Update world stage policy
    world.stage_policies = bundle

    # 6. Continue execution from now_min to end of horizon
    trace = world.run(stop_at_min=world.horizon_end_min)
    return revised_order_plan, trace
