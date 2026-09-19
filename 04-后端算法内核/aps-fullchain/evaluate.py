"""evaluate.py - Business Objectives, Lexicographical Ranking, and True Weighted Tardiness.

Implements Section 5.4 of APS Fullchain Specification:
- Lexicographical / Hierarchical ordering across candidate schedules:
  Level 1: Hard constraint satisfaction (is_valid == True, 0 violations)
  Level 2: Minimize unmet active demand (uncompleted active orders)
  Level 3: Minimize true weighted tardiness of completed orders (TWT)
  Level 4: Minimize positive blocked duration (F > C)
  Level 5: Minimize paint color switches (setup overhead)
  Level 6: Minimize makespan
- Priority rank vs. Weight consistency:
  priority_rank: 1 (Urgent/VIP, smaller integer is higher priority rank)
  priority_weight: 10 for rank 1, 5 for rank 2, 2 for rank 3, 1 for rank >= 99
  Tardiness penalty strictly weights urgent vehicles heavier.
- Separation of concerns for demand accounting:
  original_order_count = total input orders (firm + synthetic)
  cancelled_unstarted_count = legitimately cancelled before release
  active_demand_count = original_order_count - cancelled_unstarted_count
  completed_count = successfully finished through all stages
  unmet_count = active_demand_count - completed_count
  true_weighted_tardiness_min ONLY sums delay of completed orders; unmet penalty is reported separately.
"""

from __future__ import annotations
from typing import Dict, List, Tuple, Any, Optional
from .schema import Scenario, SimulationTrace, OrderPlan, Order, Configuration, VehicleStatus
from .validate import validate_trace


def get_priority_weight(priority_rank: int) -> int:
    """Maps priority rank (1 = highest rank) to penalty weight (higher = more costly).

    Rank 1 (VIP / Urgent): weight 10
    Rank 2 (Normal): weight 5
    Rank 3 (Low): weight 2
    Rank >= 99 (Synthetic / Forecast Buffer): weight 1
    """
    if priority_rank <= 1:
        return 10
    elif priority_rank == 2:
        return 5
    elif priority_rank == 3:
        return 2
    else:
        return max(1, 10 // priority_rank)


def evaluate_schedule(
    scenario: Scenario,
    trace: SimulationTrace,
    order_plan: OrderPlan,
    execution_orders: Optional[Dict[str, Order]] = None,
    baseline_plan: Optional[OrderPlan] = None
) -> Dict[str, Any]:
    """Evaluates simulation trace against business objectives using thesis-aligned lexicographical ranking."""
    validation = validate_trace(scenario, trace)

    configs = scenario.configurations
    if execution_orders:
        base_orders = {k: v.clone() for k, v in execution_orders.items()}
    else:
        base_orders = {o.vin: o.clone() for o in scenario.orders}

    original_order_count = len(base_orders)

    # 1. Classification of order completion and cancellations
    completed_vins = set()
    for vin, stages in trace.stage_node_times.items():
        if vin in base_orders:
            route = base_orders[vin].route
            last_stage = route[-1]
            if last_stage in stages and "F" in stages[last_stage]:
                completed_vins.add(vin)

    completed_count = len(completed_vins)

    # Identify cancelled unstarted orders from execution state without mutating scenario
    cancelled_unstarted_vins = set()
    if execution_orders:
        for vin, o in execution_orders.items():
            if o.status == VehicleStatus.CANCELLED:
                # Cancelled before any B node occurred in trace
                if vin not in trace.stage_node_times or not trace.stage_node_times[vin]:
                    cancelled_unstarted_vins.add(vin)

    cancelled_unstarted_count = len(cancelled_unstarted_vins)
    active_demand_count = max(0, original_order_count - cancelled_unstarted_count)
    unmet_count = max(0, active_demand_count - completed_count)

    # Handle zero denominator safely: return None instead of misleading 1.0
    service_rate = round(completed_count / active_demand_count, 4) if active_demand_count > 0 else None

    # 2. Makespan
    makespan = 0
    for stages in trace.stage_node_times.values():
        for nodes in stages.values():
            f = nodes.get("F", 0)
            if f > makespan:
                makespan = f

    # 3. True Weighted Tardiness (completed orders only)
    true_weighted_tardiness = 0
    raw_completed_tardiness = 0
    for vin in completed_vins:
        order = base_orders[vin]
        last_stage = order.route[-1]
        comp_t = trace.stage_node_times[vin][last_stage]["F"]
        tardiness = max(0, comp_t - order.due_at_min)
        weight = get_priority_weight(order.priority)
        true_weighted_tardiness += tardiness * weight
        raw_completed_tardiness += tardiness

    # On-time completion rate among completed vehicles (完工车辆中按期比例)
    # 依据与 true_weighted_tardiness 相同的完工时刻 F 与 due_at_min，不引入新口径
    on_time_completed = 0
    for vin in completed_vins:
        order = base_orders[vin]
        last_stage = order.route[-1]
        comp_t = trace.stage_node_times[vin][last_stage]["F"]
        if comp_t <= order.due_at_min:
            on_time_completed += 1
    on_time_rate = round(on_time_completed / completed_count, 4) if completed_count > 0 else None

    # Uncompleted active demand weighted penalty (未排重要度权重核算)
    unmet_weighted_penalty = sum(
        get_priority_weight(base_orders[v].priority)
        for v in base_orders
        if v not in completed_vins and v not in cancelled_unstarted_vins
    )
    unmet_penalty = unmet_weighted_penalty * scenario.calendar.horizon_end_min * 10

    # 4. Stability / perturbation penalty relative to baseline (Kendall-tau inversion + day displacement)
    stability_penalty = 0
    inversion_count = 0
    day_shift_count = 0
    if baseline_plan is not None and baseline_plan is not order_plan:
        base_day_map = {}
        for d, vins in baseline_plan.daily_allocations.items():
            for v in vins:
                base_day_map[v] = d
        curr_day_map = {}
        for d, vins in order_plan.daily_allocations.items():
            for v in vins:
                curr_day_map[v] = d

        common_vins = [v for v in curr_day_map if v in base_day_map]
        for v in common_vins:
            day_shift_count += abs(curr_day_map[v] - base_day_map[v])

        base_seq = [v for v in baseline_plan.stage_dispatch_orders.get("W", []) if v in common_vins]
        curr_seq = [v for v in order_plan.stage_dispatch_orders.get("W", []) if v in common_vins]
        base_pos = {v: idx for idx, v in enumerate(base_seq)}
        for i in range(len(curr_seq)):
            for j in range(i + 1, len(curr_seq)):
                u, w = curr_seq[i], curr_seq[j]
                if u in base_pos and w in base_pos:
                    if base_pos[u] > base_pos[w]:
                        inversion_count += 1

        stability_penalty = day_shift_count + inversion_count

    # 5. Paint Color Switches (Setup), including initial vehicle switch against initial machine attribute
    color_switches = 0
    p_events = [
        rec for rec in trace.records
        if rec.stage == "P" and rec.node == "B"
    ]
    p_events.sort(key=lambda x: (x.timestamp_min, x.vin))

    p_res = scenario.resources.get("P_LINE") or next((r for r in scenario.resources.values() if r.stage == "P"), None)
    prev_color = None
    if p_res:
        prev_color = getattr(p_res, "initial_last_color", None)
        if prev_color is None and hasattr(p_res, "last_color"):
            prev_color = p_res.last_color

    for pev in p_events:
        vin = pev.vin
        if vin in base_orders:
            cfg = configs.get(base_orders[vin].config_id)
            if cfg:
                curr_col = cfg.color
                if prev_color is not None and curr_col != prev_color:
                    color_switches += 1
                prev_color = curr_col

    # 6. Total Blocked Duration (F - C) and Waiting
    total_blocked_time = 0
    for blk in trace.blocked_durations:
        total_blocked_time += blk.get("blocked_duration", 0)

    # 7. Lexicographical / Hierarchical Rank (Strictly aligned with Chapter 3 & QA Audits)
    # Level 0: Hard constraint feasibility (0 = valid, 1 = invalid)
    # Level 1: Unmet active demand count (未排数优先)
    # Level 2: True weighted tardiness of completed vehicles (完成拖期)
    # Level 3: Paint color / setup switches (切换 - 优先于阻塞)
    # Level 4: Positive blocked duration (阻塞等待)
    # Level 5: Makespan (完工时间)
    lexicographical_rank = (
        0 if validation.is_valid else 1,
        unmet_count,
        true_weighted_tardiness,
        color_switches,
        total_blocked_time,
        makespan
    )

    # 8-level thesis rank explicitly containing unmet priority weight and stability
    thesis_lexicographical_rank = (
        0 if validation.is_valid else 1,
        unmet_count,
        unmet_weighted_penalty,
        true_weighted_tardiness,
        stability_penalty,
        color_switches,
        total_blocked_time,
        makespan
    )

    # Reference scalar composite score
    composite_score = (
        (1_000_000 if not validation.is_valid else 0)
        + unmet_penalty
        + true_weighted_tardiness * 10
        + stability_penalty * 20
        + color_switches * 25
        + total_blocked_time * 5
        + makespan
    )

    return {
        "is_valid": validation.is_valid,
        "validation_errors": validation.errors,
        "original_order_count": original_order_count,
        "cancelled_unstarted_count": cancelled_unstarted_count,
        "active_demand_count": active_demand_count,
        "completed_count": completed_count,
        "unmet_count": unmet_count,
        "unmet_weighted_penalty": unmet_weighted_penalty,
        "stability_penalty": stability_penalty,
        "inversion_count": inversion_count,
        "day_shift_count": day_shift_count,
        "service_rate": service_rate,
        "on_time_rate": on_time_rate,
        "on_time_completed_count": on_time_completed,
        "makespan_min": makespan,
        "total_tardiness_min": true_weighted_tardiness,
        "true_weighted_tardiness_min": true_weighted_tardiness,
        "raw_completed_tardiness_min": raw_completed_tardiness,
        "unmet_penalty": unmet_penalty,
        "total_blocked_min": total_blocked_time,
        "color_switches": color_switches,
        "lexicographical_rank": lexicographical_rank,
        "thesis_lexicographical_rank": thesis_lexicographical_rank,
        "composite_score": composite_score
    }
