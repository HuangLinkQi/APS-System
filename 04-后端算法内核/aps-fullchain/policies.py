"""policies.py - Multi-Stage Independent Dispatching and Candidate Strategies A, B, C.

Implements Sections 4.4 and 5.3:
- Independent dispatching logic per shop floor stage (W, P, A).
- Strategy A: Pure EDD (Earliest Due Date) across all stages.
- Strategy B: Color switch minimization in Paint (P) + Workload balance in Assembly (A).
- Strategy C: Neighborhood Search / Sequence-Guided Dispatch Optimization (Local Search).
  Uses PlanSequenceDispatchPolicy to enforce neighborhood permutations directly on shop-floor dispatch.
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple, Any, Callable
import copy
from .schema import (
    PlanningSnapshot,
    AggregatePlan,
    OrderPlan,
    Order,
    Resource,
    Configuration,
    compute_object_hash
)
from .allocation import allocate_orders


class StageDispatchPolicy:
    """Interface for stage-level independent dispatching."""
    def select_next(
        self,
        stage: str,
        eligible_vins: List[str],
        orders_map: Dict[str, Order],
        configs_map: Dict[str, Configuration],
        resource: Resource,
        current_time_min: int
    ) -> Optional[str]:
        raise NotImplementedError


class EDDDispatchPolicy(StageDispatchPolicy):
    """Strategy A: Selects by priority ascending, then due_at_min, then vin."""
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
        # Sort by priority, due date, vin
        sorted_vins = sorted(
            eligible_vins,
            key=lambda v: (
                orders_map[v].priority if v in orders_map else 99,
                orders_map[v].due_at_min if v in orders_map else 999999,
                v
            )
        )
        return sorted_vins[0]


class ColorAwareDispatchPolicy(StageDispatchPolicy):
    """Strategy B: In Paint (P), prioritizes vehicles matching resource's last color to eliminate setup.
    In Assembly (A), balances model type processing times. In W, follows EDD.
    """
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

        if stage == "P":
            last_col = resource.last_color
            if last_col:
                # Find all eligible VINs with identical color
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

            # Otherwise, sort by priority, due date, color grouping
            return sorted(
                eligible_vins,
                key=lambda v: (
                    orders_map[v].priority if v in orders_map else 99,
                    orders_map[v].due_at_min if v in orders_map else 999999,
                    configs_map[orders_map[v].config_id].color if (v in orders_map and orders_map[v].config_id in configs_map) else "",
                    v
                )
            )[0]

        elif stage == "A":
            # Workload balancing: if last was heavy, prefer light; else EDD
            last_model = resource.last_model_type
            if last_model:
                alternatives = [
                    v for v in eligible_vins
                    if v in orders_map and orders_map[v].config_id in configs_map
                    and configs_map[orders_map[v].config_id].model_type != last_model
                ]
                if alternatives:
                    alternatives.sort(key=lambda v: (
                        orders_map[v].priority if v in orders_map else 99,
                        orders_map[v].due_at_min if v in orders_map else 999999,
                        v
                    ))
                    return alternatives[0]

            return sorted(
                eligible_vins,
                key=lambda v: (
                    orders_map[v].priority if v in orders_map else 99,
                    orders_map[v].due_at_min if v in orders_map else 999999,
                    v
                )
            )[0]

        else:
            # W stage: EDD
            return sorted(
                eligible_vins,
                key=lambda v: (
                    orders_map[v].priority if v in orders_map else 99,
                    orders_map[v].due_at_min if v in orders_map else 999999,
                    v
                )
            )[0]


class PlanSequenceDispatchPolicy(StageDispatchPolicy):
    """Strategy C component: Strictly dispatches vehicles according to a designated planned sequence.
    Enables neighborhood search variations to produce genuine physical differences in DES execution.
    """
    def __init__(self, target_sequence: List[str]):
        self.target_sequence = list(target_sequence)
        self.seq_index = {vin: idx for idx, vin in enumerate(self.target_sequence)}

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
        # Sort eligible VINs strictly by their planned sequence index
        sorted_vins = sorted(
            eligible_vins,
            key=lambda v: (
                self.seq_index.get(v, 999999),
                orders_map[v].priority if v in orders_map else 99,
                v
            )
        )
        return sorted_vins[0]


class PolicyBundle:
    """Holds policy configuration for each stage and order plan generation."""
    def __init__(
        self,
        name: str,
        policies: Dict[str, StageDispatchPolicy]
    ):
        self.name = name
        self.policies = policies  # stage -> StageDispatchPolicy

    def get_stage_policy(self, stage: str) -> StageDispatchPolicy:
        return self.policies.get(stage, EDDDispatchPolicy())


def get_strategy_a() -> PolicyBundle:
    """Strategy A: Pure EDD across all stages."""
    return PolicyBundle(
        name="STRATEGY_A_EDD",
        policies={
            "W": EDDDispatchPolicy(),
            "P": EDDDispatchPolicy(),
            "A": EDDDispatchPolicy(),
        }
    )


def get_strategy_b() -> PolicyBundle:
    """Strategy B: Paint color-aware + Assembly workload balancing."""
    return PolicyBundle(
        name="STRATEGY_B_COLOR_AWARE",
        policies={
            "W": EDDDispatchPolicy(),
            "P": ColorAwareDispatchPolicy(),
            "A": ColorAwareDispatchPolicy(),
        }
    )


def get_strategy_c(w_sequence: List[str]) -> PolicyBundle:
    """Strategy C: Fixed Neighborhood (固定邻域) sequence-guided dispatching with Paint color awareness.
    Explicitly named Fixed Neighborhood: uses deterministic sequence permutations (2-opt swaps, block
    reversals, rotation) without claiming iterative metaheuristic search (VNS/ALNS).
    """
    return PolicyBundle(
        name="STRATEGY_C_FIXED_NEIGHBORHOOD",
        policies={
            "W": PlanSequenceDispatchPolicy(w_sequence),
            "P": ColorAwareDispatchPolicy(),
            "A": ColorAwareDispatchPolicy(),
        }
    )


def generate_candidate_order_plans(
    snapshot: PlanningSnapshot,
    aggregate_plan: AggregatePlan,
    budget_evals: int = 5
) -> List[Tuple[str, OrderPlan, PolicyBundle]]:
    """Generates candidate (name, OrderPlan, PolicyBundle) tuples for evaluation.
    Produces genuinely distinct algorithm candidates:
    1. Strategy A (Pure EDD)
    2. Strategy B (Color & Load Aware)
    3. Strategy C (Fixed Neighborhood / 固定邻域 permutations with sequence-guided dispatch)
    """
    candidates: List[Tuple[str, OrderPlan, PolicyBundle]] = []

    # Candidate 1: Strategy A (EDD)
    plan_a, _ = allocate_orders(snapshot, aggregate_plan, policy_name="STRATEGY_A_EDD")
    candidates.append(("Strategy_A_EDD", plan_a, get_strategy_a()))

    # Candidate 2: Strategy B (Color & Load Aware)
    plan_b, _ = allocate_orders(snapshot, aggregate_plan, policy_name="STRATEGY_B_COLOR_AWARE")
    candidates.append(("Strategy_B_ColorAware", plan_b, get_strategy_b()))

    # Candidate 3+: Strategy C (Fixed Neighborhood moves on sequence)
    # Extracts initial sequence from Plan B and applies fixed 2-opt neighborhood operators
    base_seq: List[str] = list(plan_b.stage_dispatch_orders.get("W", []))
    if not base_seq:
        for d in sorted(plan_b.daily_allocations.keys()):
            base_seq.extend(plan_b.daily_allocations[d])

    max_c_evals = max(1, min(budget_evals - 2, 3))
    for i in range(1, max_c_evals + 1):
        plan_c = copy.deepcopy(plan_b)
        plan_c.policy_name = f"STRATEGY_C_FIXED_NBR_{i}"

        mutated_seq = list(base_seq)
        # Apply fixed neighborhood move (swap adjacent or 2-opt block inversion)
        if len(mutated_seq) >= 2:
            if i == 1:
                # 2-opt swap of first two items
                mutated_seq[0], mutated_seq[1] = mutated_seq[1], mutated_seq[0]
            elif i == 2 and len(mutated_seq) >= 4:
                # 2-opt block reverse
                mutated_seq[1:3] = reversed(mutated_seq[1:3])
            else:
                # Rotate sequence by 1
                mutated_seq = mutated_seq[1:] + mutated_seq[:1]

        # Update stage dispatch orders with mutated sequence
        plan_c.stage_dispatch_orders["W"] = list(mutated_seq)
        plan_c.stage_dispatch_orders["P"] = list(mutated_seq)
        plan_c.stage_dispatch_orders["A"] = list(mutated_seq)

        plan_c.input_hash = compute_object_hash({
            "name": plan_c.policy_name,
            "w_seq": mutated_seq
        })

        # Pair with sequence-guided policy bundle
        bundle_c = get_strategy_c(mutated_seq)
        candidates.append((f"Strategy_C_FixedNbr_{i}", plan_c, bundle_c))

    return candidates
