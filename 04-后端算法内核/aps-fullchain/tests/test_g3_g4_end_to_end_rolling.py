"""test_g3_g4_end_to_end_rolling.py - G3 & G4 Gate: Full End-to-End Chain and Rolling Re-plan with WIP Protection.

Verifies:
- G3: True execution across forecast -> month -> week -> sequence -> simulation -> approve -> receipt -> execution -> event -> replan.
- G4: Execution prefix [0, tau] is invariant under re-planning; future events strictly hidden prior to known_at; WIP protected.
"""

import unittest
import copy
from aps_fullchain.fixtures.scenarios import create_micro_scenario
from aps_fullchain.schema import (
    PlanningSnapshot,
    Event,
    VehicleStatus
)
from aps_fullchain.demand import net_demand
from aps_fullchain.aggregate import plan_aggregate
from aps_fullchain.allocation import allocate_orders
from aps_fullchain.policies import get_strategy_a, get_strategy_b
from aps_fullchain.lifecycle import LifecycleManager
from aps_fullchain.simulator import ExecutionWorld
from aps_fullchain.rolling import make_snapshot, trigger_rolling_replan
from aps_fullchain.validate import validate_trace


class TestG3G4EndToEndRolling(unittest.TestCase):

    def test_full_chain_and_rolling_replan_with_wip_protection(self):
        scenario = create_micro_scenario("E2E_SCENARIO")

        # --- Layer 1: Snapshot at t=0 (Strictly isolates future events) ---
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

        # Snapshot at t=0
        snap_0 = make_snapshot(init_world, now_min=0, all_external_events=scenario.events, all_planned_deliveries=scenario.deliveries, all_forecasts=scenario.forecasts)
        # Verify future events at t=50 and t=200 are NOT visible at t=0
        self.assertEqual(len(snap_0.known_events), 0, "Future events must be invisible at t=0")

        # --- Layer 2: Demand Netting ---
        ledger = net_demand(snap_0)
        self.assertTrue(ledger.balance_verified, "Demand netting conservation law must hold")
        self.assertEqual(
            ledger.original_forecast_total,
            ledger.netted_forecast_total + ledger.remaining_forecast_total
        )
        self.assertEqual(
            ledger.net_demand_total,
            ledger.firm_orders_total + ledger.remaining_forecast_total
        )

        # --- Layer 3: Aggregate Plan (N+6 / N+3 / Monthly) ---
        aggr = plan_aggregate(snap_0, ledger)
        self.assertTrue(aggr.is_feasible, "Aggregate plan must be feasible under initial capacity & supply")
        self.assertIn("2026-10", aggr.monthly_allocations)

        # --- Layer 4: Weekly / Daily Vehicle Allocation ---
        order_plan, unmet = allocate_orders(snap_0, aggr, policy_name="STRATEGY_A_EDD")
        self.assertGreater(len(order_plan.daily_allocations[0]), 0)
        self.assertGreater(len(order_plan.daily_allocations[1]), 0, "Must observe cross-day allocation to Day 1")

        # --- Layer 5: Plan Approval & Idempotent Receipt ---
        lifecycle = LifecycleManager()
        baseline = lifecycle.submit_plan(order_plan, author="SCHEDULER_01", at_min=0)
        approved = lifecycle.approve_plan(baseline.baseline_id, approver="PLANT_MANAGER", at_min=0)
        self.assertTrue(approved)

        ok1, rec_id1 = lifecycle.dispatch_and_receive(baseline.baseline_id, "ALL", at_min=0)
        self.assertTrue(ok1)
        # Verify receipt idempotency
        ok2, rec_id2 = lifecycle.dispatch_and_receive(baseline.baseline_id, "ALL", at_min=0)
        self.assertTrue(ok2)
        self.assertEqual(rec_id1, rec_id2, "Receipt acknowledgment must be idempotent")

        # --- Layer 6: Execution up to tau=100 ---
        world = ExecutionWorld(
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
        # Apply order plan
        for d, vins in order_plan.daily_allocations.items():
            for v in vins:
                if v in world.orders:
                    world.orders[v].assigned_day = d

        # Run up to tau = 100
        trace_prefix = world.run(stop_at_min=100)
        prefix_records_count = len(world.trace.records)
        self.assertGreater(prefix_records_count, 0)
        prefix_events_snapshot = copy.deepcopy(world.trace.records)

        # Inspect WIP at tau=100
        snap_100 = make_snapshot(world, now_min=100, all_external_events=scenario.events, all_planned_deliveries=scenario.deliveries, all_forecasts=scenario.forecasts)
        # At tau=100, event at t=50 is now known! But event at t=200 is still invisible!
        known_types = [e.event_type for e in snap_100.known_events]
        self.assertIn("MATERIAL_DELAY", known_types)
        self.assertNotIn("ORDER_CANCEL", known_types)

        # --- Layer 7: Disruption Event at tau=100 & Rolling Re-plan ---
        disruption = Event(
            event_id="EV_DISRUPT_100",
            occurred_at_min=100,
            known_at_min=100,
            event_type="ORDER_CANCEL",
            payload={"vin": "VIN_11"}
        )

        revised_plan, final_trace = trigger_rolling_replan(
            world=world,
            now_min=100,
            disruption_event=disruption,
            all_external_events=scenario.events,
            all_planned_deliveries=scenario.deliveries,
            all_forecasts=scenario.forecasts,
            policy_bundle=get_strategy_b()
        )

        # --- Layer 8: Verification of Invariants ---
        # 1. Prefix invariant: the first prefix_records_count records must match EXACTLY
        self.assertGreaterEqual(len(final_trace.records), prefix_records_count)
        for i in range(prefix_records_count):
            orig_rec = prefix_events_snapshot[i]
            resumed_rec = final_trace.records[i]
            self.assertEqual(orig_rec.vin, resumed_rec.vin, f"Mismatch at index {i} vin")
            self.assertEqual(orig_rec.node, resumed_rec.node, f"Mismatch at index {i} node")
            self.assertEqual(orig_rec.stage, resumed_rec.stage, f"Mismatch at index {i} stage")
            self.assertEqual(orig_rec.timestamp_min, resumed_rec.timestamp_min, f"Mismatch at index {i} timestamp")

        # 2. In-progress protection: cancelled order VIN_11 was not started, so cancelled
        self.assertEqual(world.orders["VIN_11"].status, VehicleStatus.CANCELLED)

        # 3. Independent trace validation of final trajectory
        report = validate_trace(scenario, final_trace)
        self.assertTrue(report.is_valid, f"Resumed execution trace must be valid, got errors: {report.errors}")


if __name__ == "__main__":
    unittest.main()
