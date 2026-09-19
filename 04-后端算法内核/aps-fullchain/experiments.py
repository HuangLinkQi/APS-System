"""experiments.py - Fullchain Experiment Runner and TraceBundle Exporter.

Executes the entire closed-loop lifecycle:
1. Scenario & Future Isolation Snapshot (t=0)
2. Demand Netting & Conservation Verification (Firm + Synthetic orders)
3. Aggregate Planning with ID-level Conservation
4. Weekly/Daily Vehicle Allocation & Cross-day Migration
5. Strategy Benchmarking (Strategy A, B, C) and Evaluation
6. Fail-Closed Candidate Selection (hard gate: reject invalid plans)
7. Plan Approval, Shop Floor Dispatch, and Idempotent Receipt
8. Physical DES Execution with Positive Blocking & Pullout Vehicle Handling
9. Dynamic Disruption Event & Rolling Re-plan with WIP Protection and Lifecycle Receipt
10. Final Independent Trace Validation & Comprehensive TraceBundle Export
"""

from __future__ import annotations
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import asdict
import json
import time
import copy
from .fixtures.scenarios import create_micro_scenario, get_scenario, list_all_scenarios
from .schema import Event, VehicleStatus, Scenario, compute_object_hash
from .demand import net_demand
from .aggregate import (
    plan_aggregate,
    plan_n_plus_6,
    plan_n_plus_3,
    verify_overlap_consistency,
    generate_variance_replan_aggregate
)
from .allocation import allocate_orders
from .policies import generate_candidate_order_plans, get_strategy_a, get_strategy_b, get_strategy_c
from .lifecycle import LifecycleManager
from .simulator import ExecutionWorld, simulate
from .rolling import make_snapshot, trigger_rolling_replan
from .validate import validate_trace
from .evaluate import evaluate_schedule


def execute_strategy_closed_loop(
    scenario_in: Scenario,
    strategy_key: str,
    seed: int = 42
) -> Dict[str, Any]:
    """Executes a single strategy through the complete closed loop on scenario_in.
    Implements true rolling re-planning with:
    - Pre-execution demand netting and conservation assertion
    - Active N+6 / N+3 aggregate planning and overlap consistency verification
    - Decision barriers at known_at_min epochs (pause DES before Node B dispatch)
    - Re-snapshotting, re-netting, aggregate and daily replanning
    - Strict lifecycle approval and idempotent dispatch receipt ("无接受不执行")
    - WIP protection: in-process vehicles strictly preserved, only unstarted modified
    - Same ExecutionWorld continued to horizon end with unbroken prefix history
    """
    scenario = copy.deepcopy(scenario_in)

    # 1. World at t=0 to extract isolated t0 snapshot
    init_world = ExecutionWorld(
        calendar=scenario.calendar,
        configurations=scenario.configurations,
        resources=scenario.resources,
        buffers=scenario.buffers,
        labor=scenario.labor,
        initial_inventory=scenario.initial_inventory,
        deliveries=scenario.deliveries,
        orders={o.vin: o.clone() for o in scenario.orders},
        stage_policies=get_strategy_a(),
        horizon_end_min=scenario.calendar.horizon_end_min
    )
    snapshot_t0 = make_snapshot(init_world, 0, scenario.events, scenario.deliveries, scenario.forecasts)

    # 2. Demand netting: enforce conservation
    ledger = net_demand(snapshot_t0)
    assert ledger.balance_verified, "Demand netting balance conservation failed at t=0"
    all_netted_orders = [o.clone() for o in ledger.firm_orders + ledger.synthetic_orders]
    snapshot_t0.orders = [o.clone() for o in all_netted_orders]
    scenario.orders = [o.clone() for o in all_netted_orders]

    # 3. Active N+6 / N+3 aggregate planning & overlap consistency verification
    n6_plan = plan_n_plus_6(snapshot_t0, ledger)
    n3_plan = plan_n_plus_3(snapshot_t0, ledger, n6_plan=n6_plan)
    is_consistent, diffs = verify_overlap_consistency(n3_plan, n6_plan)
    assert is_consistent, f"Initial N+6/N+3 overlap consistency failed: {diffs}"

    # 4. Strategy-specific order allocation & dispatch bundle
    if "Strategy_A" in strategy_key:
        policy_str = "STRATEGY_A_EDD"
        order_plan, _ = allocate_orders(snapshot_t0, n3_plan, policy_name=policy_str)
        bundle = get_strategy_a()
    elif "Strategy_B" in strategy_key:
        policy_str = "STRATEGY_B_COLOR_AWARE"
        order_plan, _ = allocate_orders(snapshot_t0, n3_plan, policy_name=policy_str)
        bundle = get_strategy_b()
    else:  # Strategy_C_FixedNeighborhood
        policy_str = "STRATEGY_B_COLOR_AWARE"
        plan_b, _ = allocate_orders(snapshot_t0, n3_plan, policy_name=policy_str)
        cands = generate_candidate_order_plans(snapshot_t0, n3_plan, budget_evals=3)
        c_matches = [c for c in cands if "Strategy_C" in c[0]]
        if c_matches:
            _, order_plan, bundle = c_matches[0]
        else:
            order_plan = plan_b
            bundle = get_strategy_c([])

    order_plan.version = 1
    order_plan.parent_plan_id = None

    # 5. Lifecycle baseline submission, approval, and receipt ("无接受不执行")
    lifecycle = LifecycleManager()
    baseline = lifecycle.submit_plan(order_plan, author=f"PLANNER_{strategy_key}", at_min=0)
    lifecycle.approve_plan(baseline.baseline_id, approver="CHIEF_DISPATCHER", at_min=0)
    receipt_ok, receipt_id = lifecycle.dispatch_and_receive(baseline.baseline_id, "ALL", at_min=0)
    if not receipt_ok:
        raise RuntimeError(f"Shop floor dispatch receipt failed for baseline {baseline.baseline_id}")

    # 6. Physical execution across horizon with rolling re-plan at event epochs
    exec_world = ExecutionWorld(
        calendar=scenario.calendar,
        configurations=scenario.configurations,
        resources=scenario.resources,
        buffers=scenario.buffers,
        labor=scenario.labor,
        initial_inventory=scenario.initial_inventory,
        deliveries=scenario.deliveries,
        orders={o.vin: o.clone() for o in all_netted_orders},
        stage_policies=bundle,
        horizon_end_min=scenario.calendar.horizon_end_min,
        external_events=scenario.events,
        order_plan=order_plan
    )

    event_epochs = sorted(list(set(
        ev.known_at_min for ev in scenario.events
        if ev.known_at_min > 0 and ev.known_at_min < scenario.calendar.horizon_end_min
    )))

    current_order_plan = order_plan
    current_n6_plan = n6_plan
    current_n3_plan = n3_plan
    current_version = 1
    plan_versions_chain = [order_plan]
    receipts_chain = [{"version": 1, "baseline_id": baseline.baseline_id, "receipt_id": receipt_id, "at_min": 0}]
    snapshots_chain = [{"snapshot_id": snapshot_t0.snapshot_id, "now_min": 0}]
    replan_records: List[Dict[str, Any]] = []

    for t_epoch in event_epochs:
        # Pause before Phase 2 dispatching at t_epoch (Decision Barrier)
        exec_world.run(stop_at_min=t_epoch, pause_before_dispatch_at_stop=True)

        prefix_count = len(exec_world.trace.records)
        prefix_hash = compute_object_hash([r.to_dict() for r in exec_world.trace.records])
        trigger_events = [ev for ev in scenario.events if ev.known_at_min == t_epoch]
        trigger_event_ids = [ev.event_id for ev in trigger_events]

        # Snapshot reflecting world state and known events up to t_epoch
        snapshot_epoch = make_snapshot(exec_world, t_epoch, scenario.events, scenario.deliveries, scenario.forecasts)
        snapshots_chain.append({"snapshot_id": snapshot_epoch.snapshot_id, "now_min": t_epoch})

        # Re-net demand
        replan_ledger = net_demand(snapshot_epoch)
        assert replan_ledger.balance_verified, f"Replan demand balance failed at t={t_epoch}"

        # Re-plan aggregate layers incorporating execution feedback
        new_completions: Dict[str, List[str]] = {}
        for vin in exec_world.completed_vins:
            o = exec_world.orders[vin]
            m = o.forecast_bucket.split(":")[0] if ":" in o.forecast_bucket else "2026-10"
            new_completions.setdefault(m, []).append(vin)

        new_locked: Dict[str, List[str]] = {}
        for vin, o in exec_world.orders.items():
            if o.status in [
                VehicleStatus.SETUP,
                VehicleStatus.PROCESSING,
                VehicleStatus.BLOCKED,
                VehicleStatus.BUFFERED,
                VehicleStatus.IN_TRANSIT
            ]:
                m = o.forecast_bucket.split(":")[0] if ":" in o.forecast_bucket else "2026-10"
                new_locked.setdefault(m, []).append(vin)

        exec_variance = {"new_completions": new_completions, "new_locked": new_locked}
        replan_n6 = generate_variance_replan_aggregate(snapshot_epoch, current_n6_plan, exec_variance)
        replan_n3 = plan_n_plus_3(
            snapshot_epoch,
            replan_ledger,
            n6_plan=replan_n6,
            version=current_version + 1,
            parent_plan_id=current_n3_plan.plan_id
        )
        is_cons, diffs = verify_overlap_consistency(replan_n3, replan_n6)
        assert is_cons, f"Replan N+6/N+3 overlap consistency failed at t={t_epoch}: {diffs}"

        # Re-allocate orders
        revised_order_plan, replan_unmet = allocate_orders(
            snapshot_epoch,
            replan_n3,
            policy_name=policy_str
        )
        revised_order_plan.version = current_version + 1
        revised_order_plan.parent_plan_id = current_order_plan.plan_id

        # Lifecycle approval and dispatch receipt ("无接受不执行")
        child_baseline = lifecycle.submit_plan(
            revised_order_plan,
            author=f"ROLLING_REPLANNER_{strategy_key}",
            at_min=t_epoch
        )
        approved = lifecycle.approve_plan(
            child_baseline.baseline_id,
            approver="CHIEF_DISPATCHER",
            at_min=t_epoch
        )
        if not approved:
            raise RuntimeError(f"Replan plan {child_baseline.baseline_id} rejected by approver")
        receipt_ok, child_receipt_id = lifecycle.dispatch_and_receive(
            child_baseline.baseline_id,
            "ALL",
            at_min=t_epoch
        )
        if not receipt_ok:
            raise RuntimeError(f"Replan dispatch receipt failed for {child_baseline.baseline_id}")

        receipts_chain.append({
            "version": revised_order_plan.version,
            "baseline_id": child_baseline.baseline_id,
            "receipt_id": child_receipt_id,
            "at_min": t_epoch
        })
        plan_versions_chain.append(revised_order_plan)

        # Apply strictly to unstarted orders in active world; WIP protected; prefix untouched
        # Scope replacement semantics: revised_order_plan fully replaces the authorized set of unstarted vehicles!
        new_alloc_map: Dict[str, int] = {}
        for day, vins in revised_order_plan.daily_allocations.items():
            for v in vins:
                new_alloc_map[v] = day

        unstarted_adjustments = []
        wip_protected = []
        for v, o in exec_world.orders.items():
            if o.status in [VehicleStatus.UNRELEASED, VehicleStatus.READY]:
                if v in new_alloc_map:
                    new_day = new_alloc_map[v]
                    old_day = o.assigned_day
                    if old_day != new_day:
                        unstarted_adjustments.append({"vin": v, "old_day": old_day, "new_day": new_day})
                    o.assigned_day = new_day
                else:
                    # Excluded from new plan version: revoke authorization, vehicle enters held state!
                    if o.assigned_day is not None:
                        unstarted_adjustments.append({"vin": v, "old_day": o.assigned_day, "new_day": None})
                    o.assigned_day = None
            elif o.status in [
                VehicleStatus.SETUP,
                VehicleStatus.PROCESSING,
                VehicleStatus.BLOCKED,
                VehicleStatus.BUFFERED,
                VehicleStatus.IN_TRANSIT
            ]:
                wip_protected.append(v)

        exec_world.authorized_vins = set(new_alloc_map.keys())

        replan_records.append({
            "epoch_min": t_epoch,
            "trigger_event_ids": trigger_event_ids,
            "trigger_event_types": [ev.event_type for ev in trigger_events],
            "prefix_records_count": prefix_count,
            "prefix_hash": prefix_hash,
            "parent_plan_version": current_order_plan.version,
            "parent_plan_id": current_order_plan.plan_id,
            "child_plan_version": revised_order_plan.version,
            "child_plan_id": revised_order_plan.plan_id,
            "receipt_id": child_receipt_id,
            "unstarted_adjustments": unstarted_adjustments,
            "wip_protected_vins": sorted(list(set(wip_protected))),
            "replan_unmet_vins": replan_unmet
        })

        current_order_plan = revised_order_plan
        current_n6_plan = replan_n6
        current_n3_plan = replan_n3
        current_version += 1

    # Run remaining execution to horizon end
    final_trace = exec_world.run(stop_at_min=scenario.calendar.horizon_end_min)

    # 7. Independent validation & metrics
    val_report = validate_trace(scenario, final_trace)
    metrics = evaluate_schedule(
        scenario,
        final_trace,
        current_order_plan,
        execution_orders=exec_world.orders,
        baseline_plan=order_plan
    )

    return {
        "strategy_name": strategy_key,
        "scenario_id": scenario.scenario_id,
        "input_hash": scenario.compute_hash(),
        "is_valid": val_report.is_valid,
        "validation_errors": val_report.errors,
        "service_rate": metrics["service_rate"],
        "true_weighted_tardiness_min": metrics["true_weighted_tardiness_min"],
        "raw_completed_tardiness_min": metrics["raw_completed_tardiness_min"],
        "total_blocked_min": metrics["total_blocked_min"],
        "color_switches": metrics["color_switches"],
        "makespan_min": metrics["makespan_min"],
        "stability_penalty": metrics["stability_penalty"],
        "inversion_count": metrics["inversion_count"],
        "day_shift_count": metrics["day_shift_count"],
        "unmet_count": metrics["unmet_count"],
        "cancelled_unstarted_count": metrics["cancelled_unstarted_count"],
        "active_demand_count": metrics["active_demand_count"],
        "completed_count": metrics["completed_count"],
        "lexicographical_rank": metrics["lexicographical_rank"],
        "thesis_lexicographical_rank": metrics["thesis_lexicographical_rank"],
        "composite_score": metrics["composite_score"],
        "events_count": len(final_trace.records),
        "positive_blocking_count": len(final_trace.blocked_durations),
        "pullout_completions": final_trace.pullout_completions,
        "baseline_id": baseline.baseline_id,
        "receipt_id": receipt_id,
        "replan_triggered": len(replan_records) > 0,
        "replan_count": len(replan_records),
        "replan_records": replan_records,
        "plan_versions": [p.plan_id for p in plan_versions_chain],
        "receipts": receipts_chain,
        "snapshots": snapshots_chain,
        "material_trajectory": [dict(s) for s in final_trace.inventory_snapshots],
        "labor_trajectory": [dict(s) for s in final_trace.labor_snapshots],
        "buffer_trajectory": [dict(s) for s in final_trace.buffer_snapshots],
        "full_trace_records": [r.to_dict() for r in final_trace.records]
    }


def run_scenario_matrix(scenario_id: str, seed: int = 42) -> Dict[str, Any]:
    """Runs all 3 strategies (A, B, C) on the exact same scenario and demand,
    producing a complete comparison matrix.
    """
    sc = get_scenario(scenario_id)
    strategies = [
        "Strategy_A_EDD",
        "Strategy_B_ColorAware",
        "Strategy_C_FixedNeighborhood"
    ]

    results: Dict[str, Dict[str, Any]] = {}
    best_strategy = ""
    best_rank = (999, 999999, 999999, 999999, 999999, 999999)

    for strat in strategies:
        res = execute_strategy_closed_loop(sc, strat, seed=seed)
        results[strat] = res
        if res["is_valid"] and res["lexicographical_rank"] < best_rank:
            best_rank = res["lexicographical_rank"]
            best_strategy = strat

    return {
        "scenario_id": scenario_id,
        "results": results,
        "best_strategy": best_strategy,
        "best_rank": best_rank
    }


def run_all_scenarios_matrix(seed: int = 42) -> Dict[str, Any]:
    """Executes the complete 7 Scenarios x 3 Strategies closed-loop matrix."""
    all_scenarios = list_all_scenarios()
    matrix_results: Dict[str, Any] = {}
    summary_table: List[Dict[str, Any]] = []

    for sc_id in all_scenarios:
        sc_matrix = run_scenario_matrix(sc_id, seed=seed)
        matrix_results[sc_id] = sc_matrix
        for strat_name, r in sc_matrix["results"].items():
            summary_table.append({
                "scenario": sc_id,
                "strategy": strat_name,
                "is_valid": r["is_valid"],
                "active_orders": r["active_demand_count"],
                "completed": r["completed_count"],
                "unmet": r["unmet_count"],
                "service_rate": f"{r['service_rate']*100:.1f}%" if r["service_rate"] is not None else "N/A",
                "twt_min": r["true_weighted_tardiness_min"],
                "blocked_min": r["total_blocked_min"],
                "color_switches": r["color_switches"],
                "makespan_min": r["makespan_min"],
                "stability_penalty": r["stability_penalty"],
                "replan_count": r["replan_count"],
                "rank": r["lexicographical_rank"]
            })

    return {
        "scenarios_count": len(all_scenarios),
        "matrix": matrix_results,
        "summary_table": summary_table
    }


def run_experiment(scenario_id: str = "MICRO_BENCHMARK_12", seed: int = 42) -> Dict[str, Any]:
    start_wall_time = time.time()
    scenario = get_scenario(scenario_id)

    # 1. Physical world initialization & initial snapshot at t=0
    init_world = ExecutionWorld(
        calendar=scenario.calendar,
        configurations=scenario.configurations,
        resources=scenario.resources,
        buffers=scenario.buffers,
        labor=scenario.labor,
        initial_inventory=scenario.initial_inventory,
        deliveries=scenario.deliveries,
        orders={o.vin: o.clone() for o in scenario.orders},
        stage_policies=get_strategy_a(),
        horizon_end_min=scenario.calendar.horizon_end_min
    )
    snapshot_t0 = make_snapshot(init_world, 0, scenario.events, scenario.deliveries, scenario.forecasts)

    # 2. Demand Netting: Enforce conservation and include synthetic orders in candidate/execution set
    ledger = net_demand(snapshot_t0)
    all_netted_orders = [o.clone() for o in ledger.firm_orders + ledger.synthetic_orders]
    snapshot_t0.orders = [o.clone() for o in all_netted_orders]
    scenario.orders = [o.clone() for o in all_netted_orders]

    # 3. Aggregate Plan with strict ID-level allocation
    aggr_plan = plan_aggregate(snapshot_t0, ledger)

    # 4. Multi-candidate generation & evaluation across 3 distinct strategies (A, B, C)
    # Note: Strategy C evaluates a deterministic budget of 2-3 fixed 2-opt neighborhood variants,
    # not an iterative metaheuristic (VNS/ALNS).
    candidates = generate_candidate_order_plans(snapshot_t0, aggr_plan, budget_evals=4)
    candidate_evaluations: List[Dict[str, Any]] = []
    selected_name: str = ""
    best_rank = (999, 999999, 999999, 999999, 999999, 999999)
    selected_plan = None
    selected_bundle = None

    for name, plan, bundle in candidates:
        sim_trace = simulate(snapshot_t0, plan, bundle, stop_at_min=scenario.calendar.horizon_end_min)
        val_rep = validate_trace(scenario, sim_trace)
        metrics = evaluate_schedule(scenario, sim_trace, plan)
        candidate_evaluations.append({
            "name": name,
            "metrics": metrics,
            "is_valid": val_rep.is_valid
        })
        if val_rep.is_valid and metrics["lexicographical_rank"] < best_rank:
            best_rank = metrics["lexicographical_rank"]
            selected_name = name
            selected_plan = plan
            selected_bundle = bundle

    # Hard Gate: Fail-closed if no valid candidate meets constraints (Audit Point 4)
    if selected_plan is None:
        raise RuntimeError("Fail closed: No valid candidate order plan satisfied all hard validation checks.")

    # 5. Baseline Approval & Dispatch Receipt
    lifecycle = LifecycleManager()
    baseline = lifecycle.submit_plan(selected_plan, author="AUTONOMOUS_PLANNER", at_min=0)
    lifecycle.approve_plan(baseline.baseline_id, approver="CHIEF_DISPATCHER", at_min=0)
    receipt_ok, receipt_id = lifecycle.dispatch_and_receive(baseline.baseline_id, "ALL", at_min=0)
    if not receipt_ok:
        raise RuntimeError(f"Shop floor dispatch receipt failed for baseline {baseline.baseline_id}: {receipt_id}")

    # 6. Physical Execution to tau = 100 with all netted orders (firm + synthetic)
    exec_world = ExecutionWorld(
        calendar=scenario.calendar,
        configurations=scenario.configurations,
        resources=scenario.resources,
        buffers=scenario.buffers,
        labor=scenario.labor,
        initial_inventory=scenario.initial_inventory,
        deliveries=scenario.deliveries,
        orders={o.vin: o.clone() for o in all_netted_orders},
        stage_policies=selected_bundle,
        horizon_end_min=scenario.calendar.horizon_end_min
    )
    for day, vins in selected_plan.daily_allocations.items():
        for v in vins:
            if v in exec_world.orders:
                exec_world.orders[v].assigned_day = day

    trace_to_tau = exec_world.run(stop_at_min=100)
    prefix_events_count = len(exec_world.trace.records)

    # 7. Snapshot at tau = 100 before disruption and Rolling Re-plan with WIP Protection
    snapshot_tau100 = make_snapshot(exec_world, 100, scenario.events, scenario.deliveries, scenario.forecasts)

    disruption = Event(
        event_id="EV_DISRUPT_100",
        occurred_at_min=100,
        known_at_min=100,
        event_type="ORDER_CANCEL",
        payload={"vin": "VIN_11"}
    )
    revised_plan, final_trace = trigger_rolling_replan(
        world=exec_world,
        now_min=100,
        disruption_event=disruption,
        all_external_events=scenario.events,
        all_planned_deliveries=scenario.deliveries,
        all_forecasts=scenario.forecasts,
        policy_bundle=selected_bundle,
        lifecycle=lifecycle
    )

    # 8. Final Independent Validation & Evaluation (with execution state aligned)
    final_validation = validate_trace(scenario, final_trace)
    final_metrics = evaluate_schedule(scenario, final_trace, revised_plan, execution_orders=exec_world.orders)
    elapsed_wall_sec = round(time.time() - start_wall_time, 4)

    # 9. Format Comprehensive TraceBundle with physical evidence
    snapshot_t0_dict = {
        "snapshot_id": snapshot_t0.snapshot_id,
        "now_min": snapshot_t0.now_min,
        "current_inventory": dict(snapshot_t0.current_inventory),
        "known_deliveries": [asdict(d) for d in snapshot_t0.known_deliveries],
        "received_deliveries": [asdict(d) for d in snapshot_t0.received_deliveries],
        "active_jobs": snapshot_t0.wip_vin_states,
        "orders_count": len(snapshot_t0.orders)
    }
    snapshot_tau100_dict = {
        "snapshot_id": snapshot_tau100.snapshot_id,
        "now_min": snapshot_tau100.now_min,
        "current_inventory": dict(snapshot_tau100.current_inventory),
        "known_deliveries": [asdict(d) for d in snapshot_tau100.known_deliveries],
        "received_deliveries": [asdict(d) for d in snapshot_tau100.received_deliveries],
        "active_jobs": snapshot_tau100.wip_vin_states,
        "orders_count": len(snapshot_tau100.orders)
    }

    trace_bundle = {
        "scenario_id": scenario_id,
        "input_hash": scenario.compute_hash(),
        "elapsed_wall_sec": elapsed_wall_sec,
        "demand_netting": {
            "balance_verified": ledger.balance_verified,
            "original_forecast": ledger.original_forecast_total,
            "consumed_forecast": ledger.netted_forecast_total,
            "residual_forecast": ledger.remaining_forecast_total,
            "firm_orders": ledger.firm_orders_total,
            "net_demand": ledger.net_demand_total,
            "audit_notes": ledger.audit_notes
        },
        "aggregate_plan": {
            "plan_id": aggr_plan.plan_id,
            "is_feasible": aggr_plan.is_feasible,
            "monthly_allocations": aggr_plan.monthly_allocations,
            "allocated_order_ids": aggr_plan.allocated_order_ids,
            "unmet_order_ids": aggr_plan.unmet_order_ids
        },
        "candidate_evaluations": candidate_evaluations,
        "selected_strategy": selected_name,
        "lifecycle": {
            "baseline_id": baseline.baseline_id,
            "status": baseline.status,
            "receipt_id": receipt_id,
            "receipt_ok": receipt_ok,
            "active_baseline_id": lifecycle.active_baseline_id,
            "audit_log": lifecycle.audit_log
        },
        "snapshots": {
            "t0": snapshot_t0_dict,
            "tau100": snapshot_tau100_dict
        },
        "events": [r.to_dict() for r in final_trace.records],
        "inventory_trajectory": final_trace.inventory_snapshots,
        "labor_trajectory": final_trace.labor_snapshots,
        "buffer_trajectory": final_trace.buffer_snapshots,
        "execution_summary": {
            "prefix_events_at_tau100": prefix_events_count,
            "total_final_events": len(final_trace.records),
            "positive_blocking_occurrences": len(final_trace.blocked_durations),
            "blocked_details": final_trace.blocked_durations,
            "pullout_completions": final_trace.pullout_completions,
            "original_order_count": final_metrics["original_order_count"],
            "cancelled_unstarted_count": final_metrics["cancelled_unstarted_count"],
            "active_demand_count": final_metrics["active_demand_count"],
            "completed_vins_count": final_metrics["completed_count"],
            "unmet_vins_count": final_metrics["unmet_count"],
            "service_rate": final_metrics["service_rate"],
            "makespan_min": final_metrics["makespan_min"],
            "true_weighted_tardiness_min": final_metrics["true_weighted_tardiness_min"],
            "raw_completed_tardiness_min": final_metrics["raw_completed_tardiness_min"],
            "total_blocked_min": final_metrics["total_blocked_min"],
            "color_switches": final_metrics["color_switches"]
        },
        "validation_report": {
            "is_valid": final_validation.is_valid,
            "errors": final_validation.errors,
            "warnings": final_validation.warnings,
            "checks_performed": final_validation.checks_performed
        },
        "closed_loop_matrix": run_scenario_matrix(scenario_id, seed=seed)
    }
    return trace_bundle
