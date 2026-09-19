"""test_seven_scenarios_and_extensions.py - Comprehensive verification of:
1. N+6 vs N+3 plan objects, ID linkage, overlap consistency, locked/completed inheritance,
   cross-month attribution, and execution variance feedback generating new plan versions.
2. Named skill pools, time-bucketed labor reservation, and labor reduction disruption.
3. Full closed-loop physical execution of 7 base scenarios + 2 stress variants (27 runs).
4. Independent validator zero-violation enforcement across all runs.
5. Physical delay absorption (TWT=0) vs stress delay starvation (TWT>0).
6. Rush order forecast netting conservation vs extra demand with tight due date.
7. Cross-month 19 demand completing 17 with specific root cause (STEEL_BODY cumulative shortage).
8. Stability calculation measuring common unstarted vehicle inversions and day displacements.
9. Zero-denominator service_rate = None when active demand is 0.
10. Strategy C explicit Fixed Neighborhood designation.
"""

from __future__ import annotations
import unittest
import copy
from aps_fullchain.schema import (
    Calendar,
    CalendarShift,
    Configuration,
    Resource,
    Buffer,
    Labor,
    LaborSkillPool,
    SupplyDelivery,
    Order,
    Forecast,
    Event,
    Scenario,
    VehicleStatus,
    MachineStatus,
    PlanHorizonType,
    AggregatePlan
)
from aps_fullchain.fixtures.scenarios import (
    create_micro_scenario,
    get_scenario,
    list_all_scenarios
)
from aps_fullchain.demand import net_demand
from aps_fullchain.aggregate import (
    plan_aggregate,
    plan_n_plus_6,
    plan_n_plus_3,
    verify_overlap_consistency,
    generate_variance_replan_aggregate
)
from aps_fullchain.allocation import allocate_orders
from aps_fullchain.policies import (
    get_strategy_a,
    get_strategy_b,
    get_strategy_c,
    generate_candidate_order_plans
)
from aps_fullchain.simulator import ExecutionWorld
from aps_fullchain.rolling import make_snapshot
from aps_fullchain.validate import validate_trace
from aps_fullchain.evaluate import evaluate_schedule, get_priority_weight
from aps_fullchain.experiments import (
    execute_strategy_closed_loop,
    run_scenario_matrix,
    run_all_scenarios_matrix
)


class TestSevenScenariosAndExtensions(unittest.TestCase):

    def setUp(self):
        self.base_sc = create_micro_scenario("MICRO_BENCHMARK_12")

    def test_n6_n3_objects_linkage_and_overlap_consistency(self):
        """1. Independent N+6 and N+3 objects, linkage, and overlap consistency."""
        world = ExecutionWorld(
            self.base_sc.calendar, self.base_sc.configurations, self.base_sc.resources,
            self.base_sc.buffers, self.base_sc.labor, self.base_sc.initial_inventory,
            self.base_sc.deliveries, {o.vin: o.clone() for o in self.base_sc.orders},
            get_strategy_a()
        )
        snap = make_snapshot(world, 0, self.base_sc.events, self.base_sc.deliveries, self.base_sc.forecasts)
        ledger = net_demand(snap)

        n6_plan = plan_n_plus_6(snap, ledger)
        self.assertEqual(n6_plan.plan_type, "N+6")
        self.assertEqual(len(n6_plan.horizon_months), 6)
        self.assertEqual(n6_plan.version, 1)

        n3_plan = plan_n_plus_3(snap, ledger, n6_plan=n6_plan)
        self.assertEqual(n3_plan.plan_type, "N+3")
        self.assertEqual(len(n3_plan.horizon_months), 3)
        self.assertEqual(n3_plan.version, 1)

        consistent, issues = verify_overlap_consistency(n3_plan, n6_plan)
        self.assertTrue(consistent, f"Overlap consistency failed: {issues}")
        self.assertEqual(len(issues), 0)

    def test_locked_completed_inheritance_and_variance_feedback(self):
        """2. Locked and completed order inheritance and execution variance feedback."""
        world = ExecutionWorld(
            self.base_sc.calendar, self.base_sc.configurations, self.base_sc.resources,
            self.base_sc.buffers, self.base_sc.labor, self.base_sc.initial_inventory,
            self.base_sc.deliveries, {o.vin: o.clone() for o in self.base_sc.orders},
            get_strategy_a()
        )
        snap = make_snapshot(world, 0, self.base_sc.events, self.base_sc.deliveries, self.base_sc.forecasts)
        ledger = net_demand(snap)
        plan_v1 = plan_aggregate(snap, ledger, version=1)
        self.assertEqual(plan_v1.version, 1)

        variance = {
            "new_completions": {"2026-10": ["VIN_01", "VIN_02"]},
            "new_locked": {"2026-10": ["VIN_03", "VIN_04_PULLOUT"]}
        }
        plan_v2 = generate_variance_replan_aggregate(snap, plan_v1, variance)
        self.assertEqual(plan_v2.version, 2)
        self.assertEqual(plan_v2.parent_plan_id, plan_v1.plan_id)
        self.assertIn("VIN_01", plan_v2.completed_order_ids.get("2026-10", []))
        self.assertIn("VIN_03", plan_v2.locked_order_ids.get("2026-10", []))

    def test_cross_month_attribution_and_zero_capacity_enforcement(self):
        """3. Cross-month attribution tracking and strict zero-capacity month enforcement."""
        sc_m = get_scenario("SCENARIO_7_CROSS_MONTH")
        world = ExecutionWorld(
            sc_m.calendar, sc_m.configurations, sc_m.resources,
            sc_m.buffers, sc_m.labor, sc_m.initial_inventory,
            sc_m.deliveries, {o.vin: o.clone() for o in sc_m.orders},
            get_strategy_a(), horizon_end_min=sc_m.calendar.horizon_end_min
        )
        snap = make_snapshot(world, 0, sc_m.events, sc_m.deliveries, sc_m.forecasts)
        ledger = net_demand(snap)

        plan = plan_aggregate(
            snap,
            ledger,
            horizon_months=["2026-10", "2026-11", "2026-12"]
        )
        # Month 2026-12 has 0 shifts -> capacity must be strictly 0
        for stg, cap in plan.capacity_limit["2026-12"].items():
            self.assertEqual(cap, 0, f"Month 2026-12 stage {stg} capacity should be 0")

        self.assertEqual(sum(plan.monthly_allocations["2026-12"].values()), 0)
        self.assertGreater(len(plan.cross_month_attributions), 0)

        # Confirm exact root cause of unmet demand: STEEL_BODY shortage in 2026-11
        self.assertIn("2026-11", plan.unmet_demand)
        self.assertEqual(plan.material_shortages["2026-11"].get("STEEL_BODY"), 2)
        self.assertEqual(len(plan.unmet_order_ids["2026-11"]), 2)

    def test_named_skill_pools_and_labor_reduction(self):
        """4. Named skill pool capacity checks and dynamic labor reduction."""
        labor = Labor(
            pool_id="CREW_01",
            max_workers=3,
            skill_pools={
                "WELD_SKILL": LaborSkillPool("POOL_W", "WELD_SKILL", max_workers=2),
                "PAINT_SKILL": LaborSkillPool("POOL_P", "PAINT_SKILL", max_workers=1),
                "ASSY_SKILL": LaborSkillPool("POOL_A", "ASSY_SKILL", max_workers=2),
                "GENERAL": LaborSkillPool("POOL_G", "GENERAL", max_workers=3),
            }
        )
        self.assertEqual(labor.get_skill_capacity("PAINT_SKILL", 0), 1)
        self.assertEqual(labor.get_skill_capacity("WELD_SKILL", 0), 2)
        self.assertEqual(labor.get_total_capacity(0), 3)

        labor.skill_pools["PAINT_SKILL"].time_windows.append((100, 200, 0))
        self.assertEqual(labor.get_skill_capacity("PAINT_SKILL", 150), 0)
        self.assertEqual(labor.get_skill_capacity("PAINT_SKILL", 250), 1)

    def test_zero_denominator_service_rate_is_none(self):
        """5. Zero denominator: service_rate is None (not 1.0) when active demand is 0."""
        sc = copy.deepcopy(self.base_sc)
        trace = ExecutionWorld(
            sc.calendar, sc.configurations, sc.resources, sc.buffers, sc.labor,
            sc.initial_inventory, sc.deliveries, {o.vin: o.clone() for o in sc.orders},
            get_strategy_a()
        ).trace

        order_plan, _ = allocate_orders(
            make_snapshot(ExecutionWorld(
                sc.calendar, sc.configurations, sc.resources, sc.buffers, sc.labor,
                sc.initial_inventory, sc.deliveries, {o.vin: o.clone() for o in sc.orders},
                get_strategy_a()
            ), 0, [], sc.deliveries),
            plan_aggregate(
                make_snapshot(ExecutionWorld(
                    sc.calendar, sc.configurations, sc.resources, sc.buffers, sc.labor,
                    sc.initial_inventory, sc.deliveries, {o.vin: o.clone() for o in sc.orders},
                    get_strategy_a()
                ), 0, [], sc.deliveries),
                net_demand(make_snapshot(ExecutionWorld(
                    sc.calendar, sc.configurations, sc.resources, sc.buffers, sc.labor,
                    sc.initial_inventory, sc.deliveries, {o.vin: o.clone() for o in sc.orders},
                    get_strategy_a()
                ), 0, [], sc.deliveries))
            )
        )

        exec_orders = {o.vin: o.clone() for o in sc.orders}
        for o in exec_orders.values():
            o.status = VehicleStatus.CANCELLED

        metrics = evaluate_schedule(sc, trace, order_plan, execution_orders=exec_orders)
        self.assertEqual(metrics["active_demand_count"], 0)
        self.assertIsNone(metrics["service_rate"], "service_rate must be None when active_demand_count is 0")

    def test_initial_color_switch_accounting(self):
        """6. Initial vehicle on resource accounts for switch against resource initial attribute."""
        sc = copy.deepcopy(self.base_sc)
        sc.resources["P_LINE"].last_color = "BLACK"
        sc.resources["P_LINE"].initial_last_color = "BLACK"

        res = execute_strategy_closed_loop(sc, "Strategy_A_EDD")
        self.assertTrue(res["is_valid"])
        self.assertGreaterEqual(res["color_switches"], 1)

    def test_strategy_c_explicit_fixed_neighborhood(self):
        """7. Strategy C is explicitly designated as Fixed Neighborhood, not VNS."""
        bundle = get_strategy_c(["V1", "V2"])
        self.assertEqual(bundle.name, "STRATEGY_C_FIXED_NEIGHBORHOOD")
        self.assertNotIn("VNS", bundle.name)
        self.assertNotIn("ALNS", bundle.name)

    def test_supply_delay_buffer_absorption_vs_stress_starvation(self):
        """8. Supply Delay: Safe buffer absorption (TWT=0) vs Stress Starvation (TWT>0)."""
        # Baseline delay: 8 initial batteries safely absorb delay to t=250 -> TWT = 0
        sc_base = get_scenario("SCENARIO_2_SUPPLY_DELAY")
        res_base = execute_strategy_closed_loop(sc_base, "Strategy_A_EDD")
        self.assertTrue(res_base["is_valid"])
        self.assertEqual(res_base["true_weighted_tardiness_min"], 0, "Buffer inventory safely absorbs delay")

        # Stress delay: 2 initial batteries, delay to t=250 causes Assembly starvation & tardiness
        sc_stress = get_scenario("SCENARIO_2_SUPPLY_DELAY_STRESS")
        res_stress = execute_strategy_closed_loop(sc_stress, "Strategy_A_EDD")
        self.assertTrue(res_stress["is_valid"])
        self.assertGreater(res_stress["true_weighted_tardiness_min"], 0, "Starvation must cause measurable TWT")

    def test_rush_order_netting_vs_tight_due_tardiness(self):
        """9. Rush Order: Forecast netting conservation vs extra tight demand tardiness."""
        # Baseline rush order: nets against unstarted forecast in same bucket -> 16 active orders
        sc_rush = get_scenario("SCENARIO_4_RUSH_ORDER")
        res_rush = execute_strategy_closed_loop(sc_rush, "Strategy_A_EDD")
        self.assertTrue(res_rush["is_valid"])
        self.assertEqual(res_rush["active_demand_count"], 16, "Netted against unstarted forecast car")
        self.assertEqual(res_rush["completed_count"], 16)

        # Stress rush order: extra unforecasted emergency car with tight due date
        sc_tight = get_scenario("SCENARIO_4_RUSH_TIGHT_DUE")
        res_tight = execute_strategy_closed_loop(sc_tight, "Strategy_A_EDD")
        self.assertTrue(res_tight["is_valid"])
        self.assertEqual(res_tight["active_demand_count"], 17, "Expands demand by 1 emergency vehicle")
        self.assertEqual(res_tight["completed_count"], 17)
        self.assertGreater(res_tight["true_weighted_tardiness_min"], 0, "Tight due date causes positive TWT")

    def test_stability_kendall_tau_and_displacement_filtering(self):
        """10. Stability metric measures common unstarted vehicle displacement and inversions."""
        sc = copy.deepcopy(self.base_sc)
        order_plan, _ = allocate_orders(
            make_snapshot(ExecutionWorld(
                sc.calendar, sc.configurations, sc.resources, sc.buffers, sc.labor,
                sc.initial_inventory, sc.deliveries, {o.vin: o.clone() for o in sc.orders},
                get_strategy_a()
            ), 0, [], sc.deliveries),
            plan_aggregate(
                make_snapshot(ExecutionWorld(
                    sc.calendar, sc.configurations, sc.resources, sc.buffers, sc.labor,
                    sc.initial_inventory, sc.deliveries, {o.vin: o.clone() for o in sc.orders},
                    get_strategy_a()
                ), 0, [], sc.deliveries),
                net_demand(make_snapshot(ExecutionWorld(
                    sc.calendar, sc.configurations, sc.resources, sc.buffers, sc.labor,
                    sc.initial_inventory, sc.deliveries, {o.vin: o.clone() for o in sc.orders},
                    get_strategy_a()
                ), 0, [], sc.deliveries))
            )
        )

        perturbed_plan = copy.deepcopy(order_plan)
        # Invert two adjacent orders in stage W dispatch
        w_seq = perturbed_plan.stage_dispatch_orders["W"]
        if len(w_seq) >= 2:
            w_seq[0], w_seq[1] = w_seq[1], w_seq[0]

        world = ExecutionWorld(
            sc.calendar, sc.configurations, sc.resources, sc.buffers, sc.labor,
            sc.initial_inventory, sc.deliveries, {o.vin: o.clone() for o in sc.orders},
            get_strategy_a()
        )
        trace = world.run()
        metrics = evaluate_schedule(sc, trace, perturbed_plan, baseline_plan=order_plan)
        self.assertEqual(metrics["inversion_count"], 1, "Exactly 1 pairwise inversion between common unstarted vehicles")
        self.assertEqual(metrics["stability_penalty"], 1)

    def test_all_scenarios_matrix_including_stress_variants(self):
        """11. Matrix of all scenarios (7 base + 2 stress = 9 scenarios x 3 strategies = 27 runs)."""
        all_scenarios = list_all_scenarios()
        self.assertEqual(len(all_scenarios), 9)

        matrix = run_all_scenarios_matrix(seed=42)
        self.assertEqual(matrix["scenarios_count"], 9)
        self.assertEqual(len(matrix["summary_table"]), 27)

        for row in matrix["summary_table"]:
            self.assertTrue(
                row["is_valid"],
                f"Scenario {row['scenario']} with strategy {row['strategy']} failed validation!"
            )


if __name__ == "__main__":
    unittest.main()
