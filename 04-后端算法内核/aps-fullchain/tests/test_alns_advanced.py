"""test_alns_advanced.py - Unit and Integration Tests for ALNS & Workload Smoothing.

Tests:
1. SmoothingAssemblyDispatchPolicy: high/low workload prioritization and Heijunka line leveling.
2. Destroy Operators: due-date, high color-switch, and random destroy conservation and ranking.
3. Repair Operators: greedy minimal-switch and regret-2 insertion batching and conservation.
4. ALNS Search Engine: discrete-event physical simulation feasibility (is_valid),
   thesis Chapter 3 lexicographical evaluation, simulated annealing acceptance,
   and adaptive operator weight evolution.
5. Multi-candidate benchmarking: EDD vs ColorAware vs FixedNbr vs Smoothing vs ALNS.
6. Determinism and time budget constraint enforcement.
"""

import unittest
import random
import time

from aps_fullchain.schema import (
    Order,
    Configuration,
    Resource,
    compute_object_hash
)
from aps_fullchain.fixtures.scenarios import (
    create_micro_scenario,
    create_scenario_2_supply_delay_stress
)
from aps_fullchain.demand import net_demand
from aps_fullchain.aggregate import plan_aggregate
from aps_fullchain.allocation import allocate_orders
from aps_fullchain.rolling import make_snapshot
from aps_fullchain.simulator import ExecutionWorld, simulate
from aps_fullchain.validate import validate_trace
from aps_fullchain.evaluate import evaluate_schedule
from aps_fullchain.policies import get_strategy_a, get_strategy_b
from aps_fullchain.policies_advanced import (
    SmoothingAssemblyDispatchPolicy,
    due_date_destroy,
    high_switch_destroy,
    random_destroy,
    greedy_min_switch_repair,
    regret2_repair,
    run_alns,
    ALNSScheduler,
    get_strategy_smoothing,
    get_strategy_alns,
    generate_advanced_candidate_plans
)


class TestALNSAdvanced(unittest.TestCase):

    def setUp(self):
        self.configs = {
            "SUV_CFG": Configuration(
                config_id="SUV_CFG",
                model_type="SUV",
                color="WHITE",
                material_type="STEEL",
                stages=["W", "P", "A"],
                process_times={"W": 15, "P": 35, "A": 35}
            ),
            "SEDAN_CFG": Configuration(
                config_id="SEDAN_CFG",
                model_type="SEDAN",
                color="RED",
                material_type="STEEL",
                stages=["W", "P", "A"],
                process_times={"W": 15, "P": 35, "A": 20}
            ),
        }
        self.orders = {
            "V_SUV_1": Order(
                order_id="V_SUV_1",
                vin="V_SUV_1",
                config_id="SUV_CFG",
                release_at_min=0,
                due_at_min=500,
                priority=1,
                route=["W", "P", "A"]
            ),
            "V_SUV_2": Order(
                order_id="V_SUV_2",
                vin="V_SUV_2",
                config_id="SUV_CFG",
                release_at_min=0,
                due_at_min=400,
                priority=1,
                route=["W", "P", "A"]
            ),
            "V_SEDAN_1": Order(
                order_id="V_SEDAN_1",
                vin="V_SEDAN_1",
                config_id="SEDAN_CFG",
                release_at_min=0,
                due_at_min=600,
                priority=2,
                route=["W", "P", "A"]
            ),
        }
        self.resource_a = Resource(resource_id="A_LINE", stage="A", capacity=1)

    def test_smoothing_assembly_dispatch_policy(self):
        """Validates that SmoothingAssemblyDispatchPolicy suppresses consecutive high-workload clustering."""
        policy = SmoothingAssemblyDispatchPolicy(
            high_workload_models={"SUV"},
            low_workload_models={"SEDAN"},
            prefer_alternation=True
        )

        # 1. When preceding vehicle was SUV (high-workload), policy must select SEDAN (low-workload)
        self.resource_a.last_model_type = "SUV"
        selected = policy.select_next(
            stage="A",
            eligible_vins=["V_SUV_1", "V_SUV_2", "V_SEDAN_1"],
            orders_map=self.orders,
            configs_map=self.configs,
            resource=self.resource_a,
            current_time_min=100
        )
        self.assertEqual(
            selected, "V_SEDAN_1",
            "Smoothing policy should prioritize low-workload SEDAN when previous vehicle was SUV"
        )

        # 2. When preceding vehicle was SEDAN (low-workload), policy balances takt by picking SUV
        self.resource_a.last_model_type = "SEDAN"
        selected_after_sedan = policy.select_next(
            stage="A",
            eligible_vins=["V_SUV_1", "V_SUV_2", "V_SEDAN_1"],
            orders_map=self.orders,
            configs_map=self.configs,
            resource=self.resource_a,
            current_time_min=100
        )
        # Between V_SUV_1 (due 500) and V_SUV_2 (due 400), earlier due date V_SUV_2 must win
        self.assertEqual(
            selected_after_sedan, "V_SUV_2",
            "Smoothing policy should select earliest-due SUV when previous was SEDAN"
        )

        # 3. Test auto-detection of workload when high_workload_models is not explicitly supplied
        auto_policy = SmoothingAssemblyDispatchPolicy()
        self.resource_a.last_model_type = "SUV"
        auto_selected = auto_policy.select_next(
            stage="A",
            eligible_vins=["V_SUV_1", "V_SEDAN_1"],
            orders_map=self.orders,
            configs_map=self.configs,
            resource=self.resource_a,
            current_time_min=100
        )
        self.assertEqual(auto_selected, "V_SEDAN_1")

    def test_destroy_operators_contract_and_conservation(self):
        """Verifies destroy operators preserve vehicle conservation and partition correctly."""
        seq = ["V_SUV_1", "V_SUV_2", "V_SEDAN_1"]
        rng = random.Random(42)

        # 1. Due date destroy
        part_due, rem_due = due_date_destroy(seq, 2, self.orders, self.configs, rng=rng)
        self.assertEqual(len(rem_due), 2)
        self.assertEqual(len(part_due), 1)
        self.assertEqual(set(part_due) | set(rem_due), set(seq))
        self.assertTrue(set(part_due).isdisjoint(set(rem_due)))

        # 2. High switch destroy
        part_sw, rem_sw = high_switch_destroy(seq, 1, self.orders, self.configs, rng=rng)
        self.assertEqual(len(rem_sw), 1)
        self.assertEqual(len(part_sw), 2)
        self.assertEqual(set(part_sw) | set(rem_sw), set(seq))
        self.assertTrue(set(part_sw).isdisjoint(set(rem_sw)))

        # 3. Random destroy
        part_rnd, rem_rnd = random_destroy(seq, 2, rng=rng)
        self.assertEqual(len(rem_rnd), 2)
        self.assertEqual(len(part_rnd), 1)
        self.assertEqual(set(part_rnd) | set(rem_rnd), set(seq))
        self.assertTrue(set(part_rnd).isdisjoint(set(rem_rnd)))

        # Edge cases: q = 0 and q > len
        p0, r0 = random_destroy(seq, 0, rng=rng)
        self.assertEqual(p0, seq)
        self.assertEqual(r0, [])

    def test_repair_operators_contract_and_clustering(self):
        """Verifies repair operators restore full sequence and effectively cluster identical colors."""
        test_orders = {
            "W1": Order(order_id="W1", vin="W1", config_id="SUV_CFG", release_at_min=0, due_at_min=100, priority=1, route=["W","P","A"]),
            "W2": Order(order_id="W2", vin="W2", config_id="SUV_CFG", release_at_min=0, due_at_min=200, priority=1, route=["W","P","A"]),
            "R1": Order(order_id="R1", vin="R1", config_id="SEDAN_CFG", release_at_min=0, due_at_min=150, priority=1, route=["W","P","A"]),
            "R2": Order(order_id="R2", vin="R2", config_id="SEDAN_CFG", release_at_min=0, due_at_min=250, priority=1, route=["W","P","A"]),
        }
        partial = ["W1", "R1"]
        removed = ["W2", "R2"]

        # Greedy minimal switch repair
        repaired_greedy = greedy_min_switch_repair(partial, removed, test_orders, self.configs)
        self.assertEqual(len(repaired_greedy), 4)
        self.assertEqual(set(repaired_greedy), set(test_orders.keys()))

        # Regret-2 repair
        repaired_regret = regret2_repair(partial, removed, test_orders, self.configs)
        self.assertEqual(len(repaired_regret), 4)
        self.assertEqual(set(repaired_regret), set(test_orders.keys()))

        # Color clustering check: cars of the same color should be placed together
        w_indices = [i for i, v in enumerate(repaired_regret) if v in ["W1", "W2"]]
        self.assertEqual(abs(w_indices[0] - w_indices[1]), 1, "Same color vehicles should be adjacent")

    def test_alns_optimization_on_benchmark(self):
        """Tests that ALNS finds compliant and superior schedules on benchmark scenario."""
        scenario = create_micro_scenario("BENCHMARK_G5")
        init_world = ExecutionWorld(
            calendar=scenario.calendar,
            configurations=scenario.configurations,
            resources=scenario.resources,
            buffers=scenario.buffers,
            labor=scenario.labor,
            initial_inventory=scenario.initial_inventory,
            deliveries=scenario.deliveries,
            orders={o.vin: o.clone() for o in scenario.orders},
            stage_policies=None,
            horizon_end_min=scenario.calendar.horizon_end_min
        )
        snapshot = make_snapshot(init_world, 0, scenario.events, scenario.deliveries, all_forecasts=scenario.forecasts)
        ledger = net_demand(snapshot)
        aggr = plan_aggregate(snapshot, ledger)
        plan_b, _ = allocate_orders(snapshot, aggr, policy_name="STRATEGY_B_COLOR_AWARE")

        # Run ALNS for 20 iterations
        result = run_alns(
            snapshot=snapshot,
            scenario=scenario,
            base_plan=plan_b,
            max_iterations=20,
            seed=42,
            destroy_rate=0.25,
            use_smoothing_assembly=True
        )

        # 1. Verify physical feasibility of best result
        val = validate_trace(scenario, result.best_trace)
        self.assertTrue(val.is_valid, f"ALNS produced invalid trace: {val.errors}")
        self.assertEqual(result.best_rank[0], 0, "Level 0 must be 0 (physically feasible)")

        # 2. Verify lexicographical rank non-worsening / improvement
        self.assertLessEqual(
            result.best_rank, result.initial_rank,
            "ALNS best rank must be lexicographically better than or equal to initial rank"
        )

        # 3. Verify color switches reduction
        self.assertLessEqual(
            result.best_metrics["color_switches"],
            result.initial_metrics["color_switches"],
            "ALNS must achieve equal or fewer color switches than initial baseline"
        )

        # 4. Check search dynamics
        self.assertGreater(len(result.iteration_history), 0)
        self.assertIn("usage", result.operator_stats)
        self.assertIn("success", result.operator_stats)
        self.assertTrue(all(cnt >= 0 for cnt in result.operator_stats["usage"].values()))

    def test_alns_on_supply_delay_stress_scenario(self):
        """Tests that ALNS successfully reduces tardiness under supply delay disruption."""
        scenario = create_scenario_2_supply_delay_stress()
        init_world = ExecutionWorld(
            calendar=scenario.calendar,
            configurations=scenario.configurations,
            resources=scenario.resources,
            buffers=scenario.buffers,
            labor=scenario.labor,
            initial_inventory=scenario.initial_inventory,
            deliveries=scenario.deliveries,
            orders={o.vin: o.clone() for o in scenario.orders},
            stage_policies=None,
            horizon_end_min=scenario.calendar.horizon_end_min
        )
        snapshot = make_snapshot(init_world, 0, scenario.events, scenario.deliveries, all_forecasts=scenario.forecasts)
        ledger = net_demand(snapshot)
        aggr = plan_aggregate(snapshot, ledger)
        plan_a, _ = allocate_orders(snapshot, aggr, policy_name="STRATEGY_A_EDD")

        result = run_alns(
            snapshot=snapshot,
            scenario=scenario,
            base_plan=plan_a,
            max_iterations=15,
            seed=42,
            use_smoothing_assembly=True
        )

        val = validate_trace(scenario, result.best_trace)
        self.assertTrue(val.is_valid)

        # Verify tardiness is reduced or equal to baseline EDD
        self.assertLessEqual(
            result.best_metrics["true_weighted_tardiness_min"],
            result.initial_metrics["true_weighted_tardiness_min"],
            "ALNS should reduce or match tardiness in stress scenario"
        )
        self.assertLessEqual(
            result.best_metrics["color_switches"],
            result.initial_metrics["color_switches"]
        )

    def test_multi_candidate_generation_with_alns(self):
        """Validates that generate_advanced_candidate_plans produces all 5 valid strategies."""
        scenario = create_micro_scenario("BENCHMARK_G5")
        init_world = ExecutionWorld(
            calendar=scenario.calendar,
            configurations=scenario.configurations,
            resources=scenario.resources,
            buffers=scenario.buffers,
            labor=scenario.labor,
            initial_inventory=scenario.initial_inventory,
            deliveries=scenario.deliveries,
            orders={o.vin: o.clone() for o in scenario.orders},
            stage_policies=None,
            horizon_end_min=scenario.calendar.horizon_end_min
        )
        snapshot = make_snapshot(init_world, 0, scenario.events, scenario.deliveries, all_forecasts=scenario.forecasts)
        ledger = net_demand(snapshot)
        aggr = plan_aggregate(snapshot, ledger)

        candidates = generate_advanced_candidate_plans(
            snapshot=snapshot,
            scenario=scenario,
            aggregate_plan=aggr,
            alns_iterations=15,
            seed=42
        )

        self.assertEqual(len(candidates), 5, "Must generate exactly 5 strategies (A, B, C, D, E)")

        names = [c[0] for c in candidates]
        self.assertIn("Strategy_A_EDD", names)
        self.assertIn("Strategy_B_ColorAware", names)
        self.assertIn("Strategy_C_FixedNbr", names)
        self.assertIn("Strategy_D_Smoothing", names)
        self.assertIn("Strategy_E_ALNS", names)

        results = {}
        for name, plan, bundle in candidates:
            trace = simulate(snapshot, plan, bundle, stop_at_min=scenario.calendar.horizon_end_min)
            rep = validate_trace(scenario, trace)
            self.assertTrue(rep.is_valid, f"Candidate {name} failed validation: {rep.errors}")
            metrics = evaluate_schedule(scenario, trace, plan)
            results[name] = metrics

        # ALNS should achieve fewer color switches than EDD
        self.assertLessEqual(
            results["Strategy_E_ALNS"]["color_switches"],
            results["Strategy_A_EDD"]["color_switches"],
            "Strategy E (ALNS) must have <= color switches than Strategy A (EDD)"
        )

    def test_alns_determinism_and_time_budget(self):
        """Verifies ALNS produces reproducible results with identical seed and respects time budget."""
        scenario = create_micro_scenario("BENCHMARK_G5")
        init_world = ExecutionWorld(
            calendar=scenario.calendar,
            configurations=scenario.configurations,
            resources=scenario.resources,
            buffers=scenario.buffers,
            labor=scenario.labor,
            initial_inventory=scenario.initial_inventory,
            deliveries=scenario.deliveries,
            orders={o.vin: o.clone() for o in scenario.orders},
            stage_policies=None,
            horizon_end_min=scenario.calendar.horizon_end_min
        )
        snapshot = make_snapshot(init_world, 0, scenario.events, scenario.deliveries, all_forecasts=scenario.forecasts)
        ledger = net_demand(snapshot)
        aggr = plan_aggregate(snapshot, ledger)
        plan_b, _ = allocate_orders(snapshot, aggr, policy_name="STRATEGY_B_COLOR_AWARE")

        # Run 1
        res1 = run_alns(snapshot, scenario, plan_b, max_iterations=10, seed=123)
        # Run 2
        res2 = run_alns(snapshot, scenario, plan_b, max_iterations=10, seed=123)

        self.assertEqual(res1.best_rank, res2.best_rank, "ALNS must be deterministic with same seed")
        self.assertEqual(
            res1.best_plan.stage_dispatch_orders["W"],
            res2.best_plan.stage_dispatch_orders["W"],
            "ALNS must produce identical dispatch sequence with same seed"
        )

        # Time budget enforcement
        start_t = time.monotonic()
        res_budget = run_alns(
            snapshot, scenario, plan_b,
            max_iterations=1000,
            time_budget_sec=0.1,
            seed=42
        )
        duration = time.monotonic() - start_t
        self.assertLess(duration, 2.0, "Search must terminate promptly when time budget expires")
        self.assertGreater(len(res_budget.iteration_history), 0)


if __name__ == "__main__":
    unittest.main()
