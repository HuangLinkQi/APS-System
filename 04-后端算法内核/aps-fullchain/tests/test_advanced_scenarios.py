"""tests/test_advanced_scenarios.py - Test Suite for Multi-Scale & Compound Disruption Benchmarks.

Verifies:
1. Scenario contract & data normalization (normalize_scenario).
2. Demand netting, forecast consumption, and conservation laws across all scales (16, 32, 48).
3. Closed-loop execution across Strategy A, Strategy B, and Strategy C.
4. Zero physical/operational violations (validate_trace) in DES execution.
5. Algorithmic differentiation under process bottlenecks and compound shocks:
   - Paint color batching and setup reduction (Strategy B & C vs Strategy A)
   - Dual material arrival delay and labor reduction physical absorption
   - High-density VIP rush order prioritization and unstarted order cancellation
6. Automated benchmark pipeline execution and JSON report generation.
"""

from __future__ import annotations
import unittest
import copy
import os
import json

from aps_fullchain.fixtures.scenarios_advanced import (
    get_advanced_scenario,
    list_all_advanced_scenarios,
    list_scenarios_by_scale,
    scenario_to_dict,
    ADVANCED_SCENARIOS_MAP
)
from aps_fullchain.schema import normalize_scenario, VehicleStatus
from aps_fullchain.demand import net_demand
from aps_fullchain.rolling import make_snapshot
from aps_fullchain.simulator import ExecutionWorld
from aps_fullchain.policies import get_strategy_a, get_strategy_b, get_strategy_c
from aps_fullchain.experiments import execute_strategy_closed_loop
from aps_fullchain.experiments_advanced import (
    run_advanced_scenario_matrix,
    run_advanced_benchmark_suite,
    export_benchmark_report,
    extract_scenario_metadata
)


class TestAdvancedScenarios(unittest.TestCase):

    def setUp(self):
        self.all_scenario_ids = list_all_advanced_scenarios()

    def test_all_12_scenarios_registered(self):
        """Verify that exactly 12 scenarios (4 per scale: 16, 32, 48) are registered."""
        self.assertEqual(len(self.all_scenario_ids), 12)
        for scale in [16, 32, 48]:
            scale_scenarios = list_scenarios_by_scale(scale)
            self.assertEqual(len(scale_scenarios), 4, f"Scale {scale} must have 4 scenarios")

    def test_scenario_data_normalization(self):
        """Test that all advanced scenarios pass strict normalize_scenario validation."""
        for sid in self.all_scenario_ids:
            sc = get_advanced_scenario(sid)
            raw_dict = scenario_to_dict(sc)
            normalized = normalize_scenario(raw_dict)
            self.assertEqual(normalized.scenario_id, sid)
            self.assertGreater(len(normalized.configurations), 0)
            self.assertGreater(len(normalized.resources), 0)
            self.assertGreater(len(normalized.buffers), 0)
            self.assertGreater(normalized.labor.max_workers, 0)
            self.assertGreater(len(normalized.calendar.shifts), 0)

    def test_demand_netting_and_forecast_conservation(self):
        """Verify strict demand netting conservation across all 12 scenarios:
        original_forecast == consumed_forecast + residual_forecast.
        """
        for sid in self.all_scenario_ids:
            sc = get_advanced_scenario(sid)
            meta = extract_scenario_metadata(sid)
            expected_scale = meta["scale"]

            world = ExecutionWorld(
                sc.calendar, sc.configurations, sc.resources, sc.buffers, sc.labor,
                sc.initial_inventory, sc.deliveries, {o.vin: o.clone() for o in sc.orders},
                get_strategy_a()
            )
            snap = make_snapshot(world, 0, sc.events, sc.deliveries, sc.forecasts)
            ledger = net_demand(snap)

            self.assertTrue(ledger.balance_verified, f"Netting balance failed for {sid}")
            total_netted_orders = len(ledger.firm_orders) + len(ledger.synthetic_orders)
            self.assertEqual(
                total_netted_orders,
                expected_scale,
                f"Scenario {sid} active netted orders ({total_netted_orders}) must match scale ({expected_scale})"
            )

    def test_closed_loop_scale_16_all_strategies(self):
        """Test full closed-loop execution of all 16-vehicle scenarios under Strategy A, B, and C."""
        scenarios_16 = list_scenarios_by_scale(16)
        strategies = ["Strategy_A_EDD", "Strategy_B_ColorAware", "Strategy_C_FixedNeighborhood"]

        for sid in scenarios_16:
            sc = get_advanced_scenario(sid)
            for strat in strategies:
                res = execute_strategy_closed_loop(sc, strat)
                self.assertTrue(
                    res["is_valid"],
                    f"Physical validation failed for {sid} with {strat}: {res['validation_errors']}"
                )
                self.assertGreaterEqual(res["completed_count"], 14)
                self.assertIsNotNone(res["service_rate"])

    def test_bottleneck_scenario_color_batching_superiority(self):
        """Verify that Strategy B and C achieve fewer color switches than Strategy A
        in the high-aluminum, multi-color bottleneck scenario.
        """
        for scale in [16, 32, 48]:
            sid = f"SCENARIO_{scale}_BOTTLENECK_COLOR_ALUM"
            sc = get_advanced_scenario(sid)

            res_a = execute_strategy_closed_loop(sc, "Strategy_A_EDD")
            res_b = execute_strategy_closed_loop(sc, "Strategy_B_ColorAware")
            res_c = execute_strategy_closed_loop(sc, "Strategy_C_FixedNeighborhood")

            self.assertTrue(res_a["is_valid"])
            self.assertTrue(res_b["is_valid"])
            self.assertTrue(res_c["is_valid"])

            # Strategy B and C should significantly reduce paint color switches vs Strategy A
            self.assertLess(
                res_b["color_switches"],
                res_a["color_switches"],
                f"Scale {scale}: Strategy B ({res_b['color_switches']}) should have fewer switches than Strategy A ({res_a['color_switches']})"
            )
            self.assertLess(
                res_c["color_switches"],
                res_a["color_switches"],
                f"Scale {scale}: Strategy C ({res_c['color_switches']}) should have fewer switches than Strategy A ({res_a['color_switches']})"
            )

    def test_dual_delay_and_labor_cut_resilience(self):
        """Verify closed-loop response to dual material delay and labor reduction."""
        for scale in [16, 32]:
            sid = f"SCENARIO_{scale}_DUAL_DELAY_LABOR_CUT"
            sc = get_advanced_scenario(sid)
            res = execute_strategy_closed_loop(sc, "Strategy_A_EDD")

            self.assertTrue(res["is_valid"])
            self.assertGreater(res["replan_count"], 0, f"{sid} must trigger rolling replanning")
            # All vehicles should complete once delayed materials arrive
            self.assertEqual(res["completed_count"], scale)

    def test_vip_rush_order_insertion_and_cancellation(self):
        """Verify compound shock: VIP rush insertion prioritized and unstarted orders cancelled."""
        for scale in [16, 32]:
            sid = f"SCENARIO_{scale}_VIP_RUSH_CANCEL_SHOCK"
            sc = get_advanced_scenario(sid)
            res = execute_strategy_closed_loop(sc, "Strategy_A_EDD")

            self.assertTrue(res["is_valid"])
            self.assertGreater(res["replan_count"], 0)
            self.assertGreater(res["cancelled_unstarted_count"], 0)
            # Active demand = original - cancelled
            self.assertEqual(
                res["active_demand_count"],
                res["completed_count"] + res["unmet_count"]
            )

    def test_closed_loop_scale_32_medium_scale(self):
        """Test 32-car (two-day) closed-loop execution across all strategies."""
        sc = get_advanced_scenario("SCENARIO_32_NORMAL")
        for strat in ["Strategy_A_EDD", "Strategy_B_ColorAware", "Strategy_C_FixedNeighborhood"]:
            res = execute_strategy_closed_loop(sc, strat)
            self.assertTrue(res["is_valid"])
            self.assertEqual(res["completed_count"], 32)
            self.assertGreater(res["makespan_min"], 1440, "32 vehicles must span across Day 0 and Day 1")

    def test_closed_loop_scale_48_extended_scale(self):
        """Test 48-car (three-day) closed-loop execution across all strategies."""
        sc = get_advanced_scenario("SCENARIO_48_NORMAL")
        for strat in ["Strategy_A_EDD", "Strategy_B_ColorAware", "Strategy_C_FixedNeighborhood"]:
            res = execute_strategy_closed_loop(sc, strat)
            self.assertTrue(res["is_valid"])
            self.assertEqual(res["completed_count"], 48)
            self.assertGreater(res["makespan_min"], 2880, "48 vehicles must span across Day 0, Day 1, and Day 2")

    def test_benchmark_matrix_and_json_export(self):
        """Test automated benchmark suite execution and JSON export."""
        report = run_advanced_benchmark_suite(scales=[16], seed=42)
        self.assertEqual(report["metadata"]["scenarios_count"], 4)
        self.assertEqual(report["metadata"]["total_runs"], 12)
        self.assertIn("by_strategy", report["aggregates"])

        temp_out = "/tmp/test_advanced_benchmark_output.json"
        export_benchmark_report(report, temp_out)
        self.assertTrue(os.path.exists(temp_out))

        with open(temp_out, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        self.assertEqual(loaded["metadata"]["scenarios_count"], 4)
        os.remove(temp_out)


if __name__ == "__main__":
    unittest.main()
