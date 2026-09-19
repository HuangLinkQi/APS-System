"""test_g5_candidates_comparison.py - G5 Gate: Multi-Strategy Benchmarking & Evaluation.

Compares Strategy A (EDD), Strategy B (Color & Load Aware), and Strategy C (Neighborhood Search)
under identical scenario truth, observation cutoff, and independent validation.
"""

import unittest
from aps_fullchain.fixtures.scenarios import create_micro_scenario
from aps_fullchain.policies import generate_candidate_order_plans
from aps_fullchain.demand import net_demand
from aps_fullchain.aggregate import plan_aggregate
from aps_fullchain.rolling import make_snapshot
from aps_fullchain.simulator import simulate, ExecutionWorld
from aps_fullchain.evaluate import evaluate_schedule
from aps_fullchain.validate import validate_trace


class TestG5CandidatesComparison(unittest.TestCase):

    def test_multi_candidate_comparison(self):
        scenario = create_micro_scenario("BENCHMARK_G5")

        # Initial world to take snapshot at t=0
        init_world = ExecutionWorld(
            calendar=scenario.calendar,
            configurations=scenario.configurations,
            resources=scenario.resources,
            buffers=scenario.buffers,
            labor=scenario.labor,
            initial_inventory=scenario.initial_inventory,
            deliveries=scenario.deliveries,
            orders={o.vin: o.clone() for o in scenario.orders},
            stage_policies=None,  # type: ignore
            horizon_end_min=scenario.calendar.horizon_end_min
        )
        snapshot = make_snapshot(init_world, 0, scenario.events, scenario.deliveries, all_forecasts=scenario.forecasts)

        ledger = net_demand(snapshot)
        aggr = plan_aggregate(snapshot, ledger)

        # Generate candidates A, B, C
        candidates = generate_candidate_order_plans(snapshot, aggr, budget_evals=3)
        self.assertGreaterEqual(len(candidates), 3, "Must generate at least 3 candidates (A, B, C)")

        candidate_results = []
        for name, plan, bundle in candidates:
            # Simulate each candidate with exact same simulator and snapshot
            trace = simulate(snapshot, plan, bundle, stop_at_min=scenario.calendar.horizon_end_min)

            # Independent validation
            rep = validate_trace(scenario, trace)
            self.assertTrue(rep.is_valid, f"Candidate {name} produced invalid trace: {rep.errors}")

            # Business metric evaluation
            metrics = evaluate_schedule(scenario, trace, plan)
            candidate_results.append((name, metrics, trace))

        # Check that Strategy B or C achieves fewer color switches or better composite score than Strategy A
        strat_a = next(r for r in candidate_results if "Strategy_A" in r[0])
        strat_b = next(r for r in candidate_results if "Strategy_B" in r[0])

        self.assertLessEqual(
            strat_b[1]["color_switches"],
            strat_a[1]["color_switches"],
            "Strategy B should produce equal or fewer color switches in Paint than EDD Strategy A"
        )


if __name__ == "__main__":
    unittest.main()
