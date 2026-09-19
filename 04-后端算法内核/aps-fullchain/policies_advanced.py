"""policies_advanced.py - Adaptive Large Neighborhood Search (ALNS) and Workload Smoothing Dispatch.

Advanced Scheduling Engine extending Section 4.4 and Section 5 of APS Fullchain Specification:
1. SmoothingAssemblyDispatchPolicy:
   Workload leveling (Heijunka) in Assembly (A). Suppresses consecutive high-workload
   clustering (e.g. SUV) by prioritizing lower-workload models (e.g. SEDAN) when the
   preceding vehicle was high-workload.
2. ALNS (Adaptive Large Neighborhood Search) Metaheuristic Framework:
   - Destroy Operators:
     * Due-Date Destroy: Removes vehicles with tardiness or tightest/latest due dates.
     * High Color-Switch Destroy: Removes vehicles causing color transitions in Paint.
     * Random Destroy: Removes vehicles uniformly at random for search diversification.
   - Repair Operators:
     * Greedy Minimal-Switch Insertion: Re-inserts removed vehicles at positions minimizing setup.
     * Regret-2 Insertion: Evaluates regret between best and second-best insertion positions.
   - Simulation Feasibility & Lexicographical Evaluation:
     * Discrete event simulation (simulate) verified at every iteration for physical feasibility (is_valid).
     * Thesis Chapter 3 lexicographical rank:
       (feasibility -> unmet_count -> unmet_weighted -> true_weighted_tardiness -> stability -> color_switches -> total_blocked -> makespan).
     * Simulated Annealing (SA) acceptance criterion driven by composite score / hierarchy.
     * Adaptive operator weight updates based on performance feedback.
   - Deterministic iteration limit, time budget, and random seed.
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple, Any, Set
import copy
import math
import random
import time
from dataclasses import dataclass, field

from .schema import (
    PlanningSnapshot,
    Scenario,
    AggregatePlan,
    OrderPlan,
    Order,
    Resource,
    Configuration,
    SimulationTrace,
    compute_object_hash
)
from .policies import (
    StageDispatchPolicy,
    PolicyBundle,
    EDDDispatchPolicy,
    ColorAwareDispatchPolicy,
    PlanSequenceDispatchPolicy,
    get_strategy_a,
    get_strategy_b,
    get_strategy_c
)
from .simulator import simulate
from .validate import validate_trace
from .evaluate import evaluate_schedule, get_priority_weight
from .allocation import allocate_orders


# ============================================================================
# 1. Total Assembly Workload Smoothing Policy (总装工时平准化派工策略)
# ============================================================================

class SmoothingAssemblyDispatchPolicy(StageDispatchPolicy):
    """Assembly Workload Smoothing Dispatch Policy.

    Suppresses consecutive high-workload clustering in Assembly (Stage A).
    When the preceding vehicle on the assembly line was high-workload (e.g., SUV),
    this policy prioritizes lower-workload models (e.g., SEDAN) among eligible candidates.
    If alternation is enabled, it balances line takt and worker physical fatigue.
    """

    def __init__(
        self,
        high_workload_models: Optional[Set[str]] = None,
        low_workload_models: Optional[Set[str]] = None,
        prefer_alternation: bool = True
    ):
        self.high_workload_models = set(high_workload_models) if high_workload_models is not None else None
        self.low_workload_models = set(low_workload_models) if low_workload_models is not None else None
        self.prefer_alternation = prefer_alternation

    def _classify_model(
        self,
        model_type: Optional[str],
        configs_map: Dict[str, Configuration]
    ) -> str:
        """Classifies a model_type as 'HIGH' or 'LOW' workload."""
        if not model_type:
            return "UNKNOWN"

        if self.high_workload_models is not None:
            if model_type in self.high_workload_models:
                return "HIGH"
            if self.low_workload_models is not None and model_type in self.low_workload_models:
                return "LOW"

        # Auto-detect from configs_map based on stage A processing times
        a_times: Dict[str, List[int]] = {}
        for cfg in configs_map.values():
            m = cfg.model_type
            t = cfg.process_times.get("A", 0)
            a_times.setdefault(m, []).append(t)

        if not a_times or model_type not in a_times:
            # Fallback heuristic: SUV is high, others low
            return "HIGH" if "SUV" in model_type.upper() else "LOW"

        avg_by_model = {m: sum(ts) / len(ts) for m, ts in a_times.items()}
        all_avgs = list(avg_by_model.values())
        min_t, max_t = min(all_avgs), max(all_avgs)

        if max_t > min_t:
            mid = (min_t + max_t) / 2.0
            return "HIGH" if avg_by_model[model_type] >= mid else "LOW"

        # If identical times, fallback by name
        return "HIGH" if "SUV" in model_type.upper() else "LOW"

    def select_next(
        self,
        stage: str,
        eligible_vins: List[str],
        orders_map: Dict[str, Order],
        configs_map: Dict[str, Configuration],
        resource: Resource,
        current_time_min: int
    ) -> Optional[str]:
        if not eligible_vins:
            return None

        if stage == "A":
            last_model = resource.last_model_type
            if not last_model and hasattr(resource, "initial_last_model_type"):
                last_model = getattr(resource, "initial_last_model_type")

            last_class = self._classify_model(last_model, configs_map)

            def sort_key(vin: str) -> Tuple[int, int, int, str]:
                order = orders_map.get(vin)
                priority = order.priority if order else 99
                due_min = order.due_at_min if order else 999999
                cfg = configs_map.get(order.config_id) if order else None
                m_type = cfg.model_type if cfg else ""
                v_class = self._classify_model(m_type, configs_map)

                if last_class == "HIGH":
                    # Preceding was high workload: strictly prioritize LOW workload vehicles
                    workload_tier = 0 if v_class == "LOW" else 1
                elif last_class == "LOW" and self.prefer_alternation:
                    # Preceding was low workload: prioritize HIGH workload vehicles to balance
                    workload_tier = 0 if v_class == "HIGH" else 1
                else:
                    # Initial / neutral state: no bias
                    workload_tier = 0

                return (workload_tier, priority, due_min, vin)

            sorted_vins = sorted(eligible_vins, key=sort_key)
            return sorted_vins[0]

        elif stage == "P":
            # Paint stage: minimize color switch (same as ColorAwareDispatchPolicy)
            last_col = resource.last_color
            if last_col:
                same_color_vins = [
                    v for v in eligible_vins
                    if v in orders_map and orders_map[v].config_id in configs_map
                    and configs_map[orders_map[v].config_id].color == last_col
                ]
                if same_color_vins:
                    same_color_vins.sort(key=lambda v: (
                        orders_map[v].priority if v in orders_map else 99,
                        orders_map[v].due_at_min if v in orders_map else 999999,
                        v
                    ))
                    return same_color_vins[0]

            return sorted(
                eligible_vins,
                key=lambda v: (
                    orders_map[v].priority if v in orders_map else 99,
                    orders_map[v].due_at_min if v in orders_map else 999999,
                    configs_map[orders_map[v].config_id].color if (v in orders_map and orders_map[v].config_id in configs_map) else "",
                    v
                )
            )[0]

        else:
            # Stage W: EDD dispatch
            return sorted(
                eligible_vins,
                key=lambda v: (
                    orders_map[v].priority if v in orders_map else 99,
                    orders_map[v].due_at_min if v in orders_map else 999999,
                    v
                )
            )[0]


# ============================================================================
# 2. ALNS Destroy Operators (破坏算子)
# ============================================================================

def due_date_destroy(
    sequence: List[str],
    q: int,
    orders_map: Dict[str, Order],
    configs_map: Dict[str, Configuration],
    trace: Optional[SimulationTrace] = None,
    rng: Optional[random.Random] = None
) -> Tuple[List[str], List[str]]:
    """Due-Date Destroy (最迟交期破坏).

    Identifies vehicles with positive tardiness in the previous simulation trace,
    or those with the tightest/latest delivery dates, and removes them so they
    can be re-inserted into positions with lower delay.
    """
    if not sequence or q <= 0:
        return list(sequence), []

    q = min(q, len(sequence))
    vins = list(sequence)

    tardiness_map: Dict[str, int] = {}
    if trace and trace.stage_node_times:
        for v in vins:
            ord_obj = orders_map.get(v)
            if not ord_obj:
                continue
            last_stage = ord_obj.route[-1]
            comp_t = trace.stage_node_times.get(v, {}).get(last_stage, {}).get("F", 0)
            tardiness = max(0, comp_t - ord_obj.due_at_min)
            tardiness_map[v] = tardiness

    scores: List[Tuple[float, str]] = []
    for idx, v in enumerate(vins):
        ord_obj = orders_map.get(v)
        due = ord_obj.due_at_min if ord_obj else 999999
        prio = ord_obj.priority if ord_obj else 99
        tard = tardiness_map.get(v, 0)
        # Random noise for exploration
        noise = rng.uniform(0.0, 1.0) if rng else 0.0
        # Score prioritizing tardy vehicles, tight due dates, high priority
        score = (tard * 1000.0) + (10000.0 / (due + 1.0)) + ((10 - prio) * 10.0) + noise
        scores.append((score, v))

    scores.sort(key=lambda x: x[0], reverse=True)
    removed_set = {v for _, v in scores[:q]}

    remaining = [v for v in sequence if v not in removed_set]
    removed = [v for _, v in scores[:q]]
    return remaining, removed


def high_switch_destroy(
    sequence: List[str],
    q: int,
    orders_map: Dict[str, Order],
    configs_map: Dict[str, Configuration],
    rng: Optional[random.Random] = None
) -> Tuple[List[str], List[str]]:
    """High Color-Switch Destroy (高切换破坏).

    Identifies vehicles in the sequence that incur color transitions in Paint,
    particularly isolated colors or color boundaries, and removes them for
    batch-level re-clustering.
    """
    if not sequence or q <= 0:
        return list(sequence), []

    q = min(q, len(sequence))
    n = len(sequence)

    def get_color(v: str) -> str:
        ord_obj = orders_map.get(v)
        if ord_obj and ord_obj.config_id in configs_map:
            return configs_map[ord_obj.config_id].color
        return ""

    colors = [get_color(v) for v in sequence]
    scores: List[Tuple[float, str]] = []

    for i in range(n):
        switches = 0
        cur_c = colors[i]
        if i > 0 and colors[i - 1] != cur_c:
            switches += 1
        if i < n - 1 and colors[i + 1] != cur_c:
            switches += 1

        noise = rng.uniform(0.0, 0.5) if rng else 0.0
        score = float(switches) + noise
        scores.append((score, sequence[i]))

    scores.sort(key=lambda x: x[0], reverse=True)
    removed_set = {v for _, v in scores[:q]}

    remaining = [v for v in sequence if v not in removed_set]
    removed = [v for _, v in scores[:q]]
    return remaining, removed


def random_destroy(
    sequence: List[str],
    q: int,
    rng: Optional[random.Random] = None
) -> Tuple[List[str], List[str]]:
    """Random Destroy (随机破坏).

    Selects q vehicles uniformly at random to prevent the search from being trapped
    in local minima and promote diversified exploration.
    """
    if not sequence or q <= 0:
        return list(sequence), []

    q = min(q, len(sequence))
    vins = list(sequence)

    if rng:
        removed = rng.sample(vins, q)
    else:
        removed = vins[:q]

    removed_set = set(removed)
    remaining = [v for v in sequence if v not in removed_set]
    return remaining, removed


# ============================================================================
# 3. ALNS Repair Operators (修复算子)
# ============================================================================

def _compute_insertion_cost(
    seq: List[str],
    vin: str,
    pos: int,
    orders_map: Dict[str, Order],
    configs_map: Dict[str, Configuration]
) -> float:
    """Computes the marginal insertion cost of placing `vin` at index `pos` in `seq`.
    Primary objective: color switch minimization.
    Secondary objective: due date preservation and priority ordering.
    """
    def get_color(v: str) -> str:
        ord_obj = orders_map.get(v)
        if ord_obj and ord_obj.config_id in configs_map:
            return configs_map[ord_obj.config_id].color
        return ""

    cur_col = get_color(vin)
    n = len(seq)

    # 1. Marginal color switch change
    delta_switches = 0
    if n == 0:
        delta_switches = 0
    elif pos == 0:
        delta_switches = 1 if cur_col != get_color(seq[0]) else 0
    elif pos == n:
        delta_switches = 1 if cur_col != get_color(seq[-1]) else 0
    else:
        left_col = get_color(seq[pos - 1])
        right_col = get_color(seq[pos])
        old_switch = 1 if left_col != right_col else 0
        new_switch = (1 if left_col != cur_col else 0) + (1 if cur_col != right_col else 0)
        delta_switches = new_switch - old_switch

    # 2. Due date alignment penalty
    ord_obj = orders_map.get(vin)
    due_penalty = 0.0
    if ord_obj:
        # Penalize placing early due-date vehicles near the end
        pos_ratio = pos / max(1.0, float(n))
        due_penalty = (pos_ratio * 5.0) + (ord_obj.priority * 0.5)

    return (delta_switches * 100.0) + due_penalty


def greedy_min_switch_repair(
    partial_sequence: List[str],
    removed_vins: List[str],
    orders_map: Dict[str, Order],
    configs_map: Dict[str, Configuration]
) -> List[str]:
    """Greedy Minimal-Switch Insertion (最少切换贪心插入).

    Iteratively selects the (vin, position) pair that incurs the absolute lowest
    marginal insertion cost until all removed vehicles are restored.
    """
    seq = list(partial_sequence)
    pool = list(removed_vins)

    while pool:
        best_cost = float("inf")
        best_vin_idx = -1
        best_pos = 0

        for vin_idx, v in enumerate(pool):
            for pos in range(len(seq) + 1):
                cost = _compute_insertion_cost(seq, v, pos, orders_map, configs_map)
                if cost < best_cost:
                    best_cost = cost
                    best_vin_idx = vin_idx
                    best_pos = pos

        selected_vin = pool.pop(best_vin_idx)
        seq.insert(best_pos, selected_vin)

    return seq


def regret2_repair(
    partial_sequence: List[str],
    removed_vins: List[str],
    orders_map: Dict[str, Order],
    configs_map: Dict[str, Configuration]
) -> List[str]:
    """Regret-2 Insertion (后悔值 Regret-2 插入).

    For each uninserted vehicle, evaluates the difference between its best position
    cost and second-best position cost (regret = c2 - c1). Prioritizes vehicles
    with the largest regret to prevent costly suboptimal forced insertions later.
    """
    seq = list(partial_sequence)
    pool = list(removed_vins)

    while pool:
        if len(pool) == 1:
            v = pool.pop()
            best_cost = float("inf")
            best_pos = 0
            for pos in range(len(seq) + 1):
                cost = _compute_insertion_cost(seq, v, pos, orders_map, configs_map)
                if cost < best_cost:
                    best_cost = cost
                    best_pos = pos
            seq.insert(best_pos, v)
            break

        max_regret = -float("inf")
        best_vin_idx = 0
        target_pos = 0

        for vin_idx, v in enumerate(pool):
            # Compute cost across all positions
            costs = []
            for pos in range(len(seq) + 1):
                cost = _compute_insertion_cost(seq, v, pos, orders_map, configs_map)
                costs.append((cost, pos))

            costs.sort(key=lambda x: x[0])
            c1, p1 = costs[0]
            c2, _ = costs[1] if len(costs) > 1 else (costs[0][0] + 100.0, p1)
            regret = c2 - c1

            if regret > max_regret:
                max_regret = regret
                best_vin_idx = vin_idx
                target_pos = p1

        selected_vin = pool.pop(best_vin_idx)
        seq.insert(target_pos, selected_vin)

    return seq


# ============================================================================
# 4. Policy Bundle Factory for Advanced Candidates
# ============================================================================

def get_strategy_smoothing(
    high_workload_models: Optional[Set[str]] = None,
    prefer_alternation: bool = True
) -> PolicyBundle:
    """Strategy: Assembly workload smoothing with paint color awareness."""
    return PolicyBundle(
        name="STRATEGY_SMOOTHING_ASSEMBLY",
        policies={
            "W": EDDDispatchPolicy(),
            "P": ColorAwareDispatchPolicy(),
            "A": SmoothingAssemblyDispatchPolicy(
                high_workload_models=high_workload_models,
                prefer_alternation=prefer_alternation
            )
        }
    )


def get_strategy_alns(
    w_sequence: List[str],
    use_smoothing_assembly: bool = True
) -> PolicyBundle:
    """Strategy ALNS: Stage W sequence-guided with Smoothing Assembly & ColorAware Paint."""
    assembly_policy = (
        SmoothingAssemblyDispatchPolicy()
        if use_smoothing_assembly
        else ColorAwareDispatchPolicy()
    )
    return PolicyBundle(
        name="STRATEGY_ALNS_OPTIMIZED",
        policies={
            "W": PlanSequenceDispatchPolicy(w_sequence),
            "P": ColorAwareDispatchPolicy(),
            "A": assembly_policy
        }
    )


# ============================================================================
# 5. ALNS Iterative Search Framework & Result Container
# ============================================================================

@dataclass
class ALNSResult:
    """Result returned by ALNS search."""
    best_plan: OrderPlan
    best_bundle: PolicyBundle
    best_metrics: Dict[str, Any]
    best_trace: SimulationTrace
    best_rank: Tuple[Any, ...]
    initial_metrics: Dict[str, Any]
    initial_rank: Tuple[Any, ...]
    iteration_history: List[Dict[str, Any]] = field(default_factory=list)
    operator_stats: Dict[str, Any] = field(default_factory=dict)
    convergence_info: Dict[str, Any] = field(default_factory=dict)


class ALNSScheduler:
    """Adaptive Large Neighborhood Search Engine for Multi-Stage Production Scheduling.

    Combines iterative destruction, heuristic repair, physical discrete event
    simulation (DES), thesis Chapter 3 lexicographical evaluation, simulated
    annealing acceptance, and adaptive weight updating.
    """

    def __init__(
        self,
        max_iterations: int = 30,
        time_budget_sec: float = 10.0,
        seed: int = 42,
        destroy_rate: float = 0.25,
        initial_temp: float = 100.0,
        cooling_rate: float = 0.95,
        use_smoothing_assembly: bool = True,
        reaction_factor: float = 0.3
    ):
        self.max_iterations = max(1, max_iterations)
        self.time_budget_sec = max(0.1, time_budget_sec)
        self.seed = seed
        self.destroy_rate = destroy_rate
        self.initial_temp = initial_temp
        self.cooling_rate = cooling_rate
        self.use_smoothing_assembly = use_smoothing_assembly
        self.reaction_factor = reaction_factor

        self.destroy_operators = ["due_date", "high_switch", "random"]
        self.repair_operators = ["greedy_min_switch", "regret2"]

    def _select_operator(
        self,
        operators: List[str],
        weights: Dict[str, float],
        rng: random.Random
    ) -> str:
        """Selects an operator via roulette wheel selection."""
        total = sum(weights[op] for op in operators)
        r = rng.uniform(0.0, total)
        cum = 0.0
        for op in operators:
            cum += weights[op]
            if r <= cum:
                return op
        return operators[-1]

    def solve(
        self,
        snapshot: PlanningSnapshot,
        scenario: Scenario,
        base_plan: OrderPlan,
        baseline_plan: Optional[OrderPlan] = None
    ) -> ALNSResult:
        """Executes the ALNS optimization process starting from base_plan."""
        start_time = time.monotonic()
        rng = random.Random(self.seed)

        # 1. Initialize operator weights and scores
        destroy_weights = {op: 10.0 for op in self.destroy_operators}
        repair_weights = {op: 10.0 for op in self.repair_operators}
        operator_usage: Dict[str, int] = {op: 0 for op in self.destroy_operators + self.repair_operators}
        operator_success: Dict[str, int] = {op: 0 for op in self.destroy_operators + self.repair_operators}

        orders_map = {o.vin: o for o in scenario.orders}
        configs_map = scenario.configurations
        horizon_end = scenario.calendar.horizon_end_min

        # 2. Extract initial sequence from base_plan
        curr_seq: List[str] = list(base_plan.stage_dispatch_orders.get("W", []))
        if not curr_seq:
            for d in sorted(base_plan.daily_allocations.keys()):
                curr_seq.extend(base_plan.daily_allocations[d])

        # 3. Simulate and evaluate initial solution
        curr_bundle = get_strategy_alns(curr_seq, use_smoothing_assembly=self.use_smoothing_assembly)
        curr_trace = simulate(snapshot, base_plan, curr_bundle, stop_at_min=horizon_end)
        curr_val = validate_trace(scenario, curr_trace)
        curr_metrics = evaluate_schedule(
            scenario, curr_trace, base_plan, baseline_plan=baseline_plan
        )
        curr_rank = curr_metrics["thesis_lexicographical_rank"]
        curr_score = curr_metrics["composite_score"]

        # Global best trackers
        best_plan = copy.deepcopy(base_plan)
        best_bundle = curr_bundle
        best_trace = curr_trace
        best_metrics = copy.deepcopy(curr_metrics)
        best_rank = curr_rank
        best_seq = list(curr_seq)

        initial_metrics = copy.deepcopy(curr_metrics)
        initial_rank = curr_rank

        history: List[Dict[str, Any]] = []
        temperature = self.initial_temp
        q_size = max(1, int(len(curr_seq) * self.destroy_rate))

        # 4. Search Loop
        for it in range(1, self.max_iterations + 1):
            elapsed = time.monotonic() - start_time
            if elapsed >= self.time_budget_sec:
                break

            # Choose operators
            d_op = self._select_operator(self.destroy_operators, destroy_weights, rng)
            r_op = self._select_operator(self.repair_operators, repair_weights, rng)
            operator_usage[d_op] += 1
            operator_usage[r_op] += 1

            # Apply Destroy
            if d_op == "due_date":
                partial, removed = due_date_destroy(curr_seq, q_size, orders_map, configs_map, trace=curr_trace, rng=rng)
            elif d_op == "high_switch":
                partial, removed = high_switch_destroy(curr_seq, q_size, orders_map, configs_map, rng=rng)
            else:
                partial, removed = random_destroy(curr_seq, q_size, rng=rng)

            # Apply Repair
            if r_op == "greedy_min_switch":
                cand_seq = greedy_min_switch_repair(partial, removed, orders_map, configs_map)
            else:
                cand_seq = regret2_repair(partial, removed, orders_map, configs_map)

            # Construct Candidate Plan
            cand_plan = copy.deepcopy(base_plan)
            cand_plan.policy_name = f"ALNS_IT_{it}_{d_op}_{r_op}"
            cand_plan.stage_dispatch_orders["W"] = list(cand_seq)
            cand_plan.stage_dispatch_orders["P"] = list(cand_seq)
            cand_plan.stage_dispatch_orders["A"] = list(cand_seq)
            cand_plan.input_hash = compute_object_hash({
                "policy": cand_plan.policy_name,
                "w_seq": cand_seq
            })

            cand_bundle = get_strategy_alns(cand_seq, use_smoothing_assembly=self.use_smoothing_assembly)

            # Simulate and validate candidate
            cand_trace = simulate(snapshot, cand_plan, cand_bundle, stop_at_min=horizon_end)
            cand_val = validate_trace(scenario, cand_trace)
            cand_metrics = evaluate_schedule(
                scenario, cand_trace, cand_plan, baseline_plan=baseline_plan
            )
            cand_rank = cand_metrics["thesis_lexicographical_rank"]
            cand_score = cand_metrics["composite_score"]

            # Evaluation & SA Acceptance Logic
            accepted = False
            is_global_best = False
            operator_score = 1.0

            # Level 0 physical feasibility guard: never accept infeasible candidate over feasible current
            if cand_rank[0] > 0 and curr_rank[0] == 0:
                accepted = False
                operator_score = 0.5
            elif cand_rank < best_rank:
                # Strictly dominates global best
                accepted = True
                is_global_best = True
                best_plan = cand_plan
                best_bundle = cand_bundle
                best_trace = cand_trace
                best_metrics = cand_metrics
                best_rank = cand_rank
                best_seq = list(cand_seq)
                operator_score = 30.0
                operator_success[d_op] += 1
                operator_success[r_op] += 1
            elif cand_rank < curr_rank:
                # Strictly dominates current solution
                accepted = True
                operator_score = 20.0
                operator_success[d_op] += 1
                operator_success[r_op] += 1
            else:
                # Worse or equal to current solution: Simulated Annealing test
                delta = max(1.0, cand_score - curr_score)
                prob = math.exp(-delta / max(1e-4, temperature))
                if rng.random() < prob:
                    accepted = True
                    operator_score = 10.0
                else:
                    accepted = False
                    operator_score = 1.0

            if accepted:
                curr_seq = cand_seq
                curr_trace = cand_trace
                curr_metrics = cand_metrics
                curr_rank = cand_rank
                curr_score = cand_score

            # Adaptive weight update (exponential smoothing)
            lam = self.reaction_factor
            destroy_weights[d_op] = max(1.0, (1.0 - lam) * destroy_weights[d_op] + lam * operator_score)
            repair_weights[r_op] = max(1.0, (1.0 - lam) * repair_weights[r_op] + lam * operator_score)

            # Cool temperature
            temperature = max(1e-3, temperature * self.cooling_rate)

            # Record iteration history
            history.append({
                "iteration": it,
                "destroy_op": d_op,
                "repair_op": r_op,
                "candidate_rank": cand_rank,
                "current_rank": curr_rank,
                "best_rank": best_rank,
                "accepted": accepted,
                "is_global_best": is_global_best,
                "temperature": round(temperature, 4),
                "destroy_weights": {k: round(v, 2) for k, v in destroy_weights.items()},
                "repair_weights": {k: round(v, 2) for k, v in repair_weights.items()}
            })

        total_time = time.monotonic() - start_time
        convergence_info = {
            "total_iterations": len(history),
            "elapsed_seconds": round(total_time, 4),
            "initial_composite_score": initial_metrics["composite_score"],
            "best_composite_score": best_metrics["composite_score"],
            "initial_color_switches": initial_metrics["color_switches"],
            "best_color_switches": best_metrics["color_switches"],
            "initial_tardiness": initial_metrics["true_weighted_tardiness_min"],
            "best_tardiness": best_metrics["true_weighted_tardiness_min"],
            "improved": best_rank < initial_rank
        }

        return ALNSResult(
            best_plan=best_plan,
            best_bundle=best_bundle,
            best_metrics=best_metrics,
            best_trace=best_trace,
            best_rank=best_rank,
            initial_metrics=initial_metrics,
            initial_rank=initial_rank,
            iteration_history=history,
            operator_stats={
                "usage": operator_usage,
                "success": operator_success,
                "final_destroy_weights": destroy_weights,
                "final_repair_weights": repair_weights
            },
            convergence_info=convergence_info
        )


def run_alns(
    snapshot: PlanningSnapshot,
    scenario: Scenario,
    base_plan: OrderPlan,
    max_iterations: int = 30,
    time_budget_sec: float = 10.0,
    seed: int = 42,
    destroy_rate: float = 0.25,
    initial_temp: float = 100.0,
    cooling_rate: float = 0.95,
    use_smoothing_assembly: bool = True,
    baseline_plan: Optional[OrderPlan] = None
) -> ALNSResult:
    """Convenience functional wrapper for executing ALNS."""
    scheduler = ALNSScheduler(
        max_iterations=max_iterations,
        time_budget_sec=time_budget_sec,
        seed=seed,
        destroy_rate=destroy_rate,
        initial_temp=initial_temp,
        cooling_rate=cooling_rate,
        use_smoothing_assembly=use_smoothing_assembly
    )
    return scheduler.solve(snapshot, scenario, base_plan, baseline_plan=baseline_plan)


# ============================================================================
# 6. Advanced Multi-Strategy Candidate Generation
# ============================================================================

def generate_advanced_candidate_plans(
    snapshot: PlanningSnapshot,
    scenario: Scenario,
    aggregate_plan: AggregatePlan,
    alns_iterations: int = 25,
    seed: int = 42
) -> List[Tuple[str, OrderPlan, PolicyBundle]]:
    """Generates candidate (name, OrderPlan, PolicyBundle) tuples including ALNS.

    Candidates produced:
    1. Strategy A: Pure EDD
    2. Strategy B: Color & Load Aware
    3. Strategy C: Fixed Neighborhood (2-opt permutations)
    4. Strategy D: Smoothing Assembly Dispatch (Heijunka Workload Leveling)
    5. Strategy E: ALNS Iterative Optimization (Iterative Destroy & Repair with DES validation)
    """
    candidates: List[Tuple[str, OrderPlan, PolicyBundle]] = []

    # 1. Strategy A (EDD)
    plan_a, _ = allocate_orders(snapshot, aggregate_plan, policy_name="STRATEGY_A_EDD")
    candidates.append(("Strategy_A_EDD", plan_a, get_strategy_a()))

    # 2. Strategy B (Color Aware)
    plan_b, _ = allocate_orders(snapshot, aggregate_plan, policy_name="STRATEGY_B_COLOR_AWARE")
    candidates.append(("Strategy_B_ColorAware", plan_b, get_strategy_b()))

    # 3. Strategy C (Fixed Neighborhood)
    base_seq: List[str] = list(plan_b.stage_dispatch_orders.get("W", []))
    if not base_seq:
        for d in sorted(plan_b.daily_allocations.keys()):
            base_seq.extend(plan_b.daily_allocations[d])

    mutated_seq = list(base_seq)
    if len(mutated_seq) >= 2:
        mutated_seq[0], mutated_seq[1] = mutated_seq[1], mutated_seq[0]

    plan_c = copy.deepcopy(plan_b)
    plan_c.policy_name = "STRATEGY_C_FIXED_NBR"
    plan_c.stage_dispatch_orders["W"] = list(mutated_seq)
    plan_c.stage_dispatch_orders["P"] = list(mutated_seq)
    plan_c.stage_dispatch_orders["A"] = list(mutated_seq)
    plan_c.input_hash = compute_object_hash({
        "policy": plan_c.policy_name,
        "w_seq": mutated_seq
    })
    candidates.append(("Strategy_C_FixedNbr", plan_c, get_strategy_c(mutated_seq)))

    # 4. Strategy D (Smoothing Assembly Dispatch)
    plan_d = copy.deepcopy(plan_b)
    plan_d.policy_name = "STRATEGY_D_SMOOTHING"
    candidates.append(("Strategy_D_Smoothing", plan_d, get_strategy_smoothing()))

    # 5. Strategy E (ALNS Optimized)
    alns_res = run_alns(
        snapshot=snapshot,
        scenario=scenario,
        base_plan=plan_b,
        max_iterations=alns_iterations,
        seed=seed,
        use_smoothing_assembly=True
    )
    candidates.append(("Strategy_E_ALNS", alns_res.best_plan, alns_res.best_bundle))

    return candidates
