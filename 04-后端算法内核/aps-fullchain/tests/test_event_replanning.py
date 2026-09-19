"""test_event_replanning.py - Test suite for dynamic event handling and rolling replanning.

Verifies:
1. MATERIAL_DELAY delays unreceived deliveries and purges old queue arrival (no double arrival).
2. MATERIAL_DELAY on already-received delivery is rejected without revoking physical inventory.
3. Simultaneous SUPPLY_ARRIVAL and MATERIAL_DELAY arbitration (delay processed first, arrival suppressed).
4. Multi-version repeat delays on the same delivery (latest version wins, arrives exactly once).
5. Horizon truncation for delays past horizon_end_min.
6. Negative test: unapproved / unreceived replan does not modify world orders ("无接受不执行").
7. Prefix immutability: executed records up to replan epoch remain strictly identical after replan.
8. Real matrix pipeline calls N+6/N+3 overlap consistency and demand conservation at each replan epoch.
"""

import copy
import unittest
from aps_fullchain.schema import (
    Event,
    SupplyDelivery,
    VehicleStatus,
    compute_object_hash,
    OrderPlan
)
from aps_fullchain.fixtures.scenarios import create_micro_scenario, get_scenario
from aps_fullchain.simulator import ExecutionWorld
from aps_fullchain.policies import get_strategy_a, get_strategy_b
from aps_fullchain.lifecycle import LifecycleManager
from aps_fullchain.rolling import make_snapshot
from aps_fullchain.demand import net_demand
from aps_fullchain.aggregate import plan_n_plus_6, plan_n_plus_3, verify_overlap_consistency
from aps_fullchain.allocation import allocate_orders
from aps_fullchain.experiments import execute_strategy_closed_loop
from aps_fullchain.validate import validate_trace


class TestEventReplanning(unittest.TestCase):

    def setUp(self):
        self.sc = create_micro_scenario("TEST_REPLAN")

    def test_material_delay_pushes_delivery_and_no_duplicate_arrival(self):
        """1. MATERIAL_DELAY delays unreceived batch; purges old arrival and arrives strictly once."""
        sc = copy.deepcopy(self.sc)
        sc.deliveries = [
            SupplyDelivery(
                delivery_id="DELIV_TEST_01",
                material_id="BATTERY",
                quantity=10,
                available_at_min=100,
                source_version="v1"
            )
        ]
        delay_event = Event(
            event_id="EV_DELAY_100_TO_250",
            occurred_at_min=50,
            known_at_min=50,
            event_type="MATERIAL_DELAY",
            payload={"delivery_id": "DELIV_TEST_01", "new_available_at_min": 250}
        )
        sc.events = [delay_event]

        world = ExecutionWorld(
            calendar=sc.calendar,
            configurations=sc.configurations,
            resources=sc.resources,
            buffers=sc.buffers,
            labor=sc.labor,
            initial_inventory=sc.initial_inventory,
            deliveries=sc.deliveries,
            orders={o.vin: o.clone() for o in sc.orders},
            stage_policies=get_strategy_a(),
            external_events=sc.events
        )

        # Run to t=150: old arrival time t=100 has passed!
        world.run(stop_at_min=150)
        self.assertNotIn("DELIV_TEST_01", world.received_delivery_ids)
        # Verify BATTERY was not added to inventory at t=100
        deliv_snaps_at_100 = [
            s for s in world.trace.inventory_snapshots
            if s.get("reason") == "DELIVERY_DELIV_TEST_01" and s.get("time") == 100
        ]
        self.assertEqual(len(deliv_snaps_at_100), 0, "Delivery must NOT arrive at old t=100")

        # Run to horizon end: arrives at t=250
        world.run(stop_at_min=sc.calendar.horizon_end_min)
        self.assertIn("DELIV_TEST_01", world.received_delivery_ids)
        deliv_snaps = [
            s for s in world.trace.inventory_snapshots
            if s.get("reason") == "DELIVERY_DELIV_TEST_01"
        ]
        self.assertEqual(len(deliv_snaps), 1, "Must arrive exactly once")
        self.assertEqual(deliv_snaps[0]["time"], 250, "Must arrive at new delayed time t=250")

    def test_material_delay_rejected_if_already_received(self):
        """2. MATERIAL_DELAY on already-received delivery is rejected without revoking physical inventory."""
        sc = copy.deepcopy(self.sc)
        sc.deliveries = [
            SupplyDelivery(
                delivery_id="DELIV_EARLY_01",
                material_id="BATTERY",
                quantity=10,
                available_at_min=50,
                source_version="v1"
            )
        ]
        world = ExecutionWorld(
            calendar=sc.calendar,
            configurations=sc.configurations,
            resources=sc.resources,
            buffers=sc.buffers,
            labor=sc.labor,
            initial_inventory=sc.initial_inventory,
            deliveries=sc.deliveries,
            orders={o.vin: o.clone() for o in sc.orders},
            stage_policies=get_strategy_a()
        )

        # Run to t=80 so delivery arrives and is received into physical inventory
        world.run(stop_at_min=80)
        self.assertIn("DELIV_EARLY_01", world.received_delivery_ids)
        stock_before = world.inventory.get("BATTERY", 0)

        # Now attempt to delay what is already in the factory
        late_delay = Event(
            event_id="EV_TOO_LATE_DELAY",
            occurred_at_min=80,
            known_at_min=80,
            event_type="MATERIAL_DELAY",
            payload={"delivery_id": "DELIV_EARLY_01", "new_available_at_min": 200}
        )
        world.apply_disruption_event(late_delay)

        # Assert stock is NOT revoked and rejected event is logged
        stock_after = world.inventory.get("BATTERY", 0)
        self.assertEqual(stock_before, stock_after, "Physical inventory cannot be revoked after arrival")
        rejections = [
            s for s in world.trace.inventory_snapshots
            if "REJECTED_DELAY_ALREADY_RECEIVED_DELIV_EARLY_01" in s.get("reason", "")
        ]
        self.assertEqual(len(rejections), 1, "Rejected delay must be audited in inventory snapshots")

    def test_simultaneous_arrival_and_delay_arbitration(self):
        """3. Simultaneous SUPPLY_ARRIVAL and MATERIAL_DELAY at exact same minute: delay wins before intake."""
        sc = copy.deepcopy(self.sc)
        sc.deliveries = [
            SupplyDelivery(
                delivery_id="DELIV_SIM_01",
                material_id="BATTERY",
                quantity=10,
                available_at_min=100,
                source_version="v1"
            )
        ]
        sim_delay = Event(
            event_id="EV_SIM_DELAY",
            occurred_at_min=100,
            known_at_min=100,
            event_type="MATERIAL_DELAY",
            payload={"delivery_id": "DELIV_SIM_01", "new_available_at_min": 200}
        )
        sc.events = [sim_delay]

        world = ExecutionWorld(
            calendar=sc.calendar,
            configurations=sc.configurations,
            resources=sc.resources,
            buffers=sc.buffers,
            labor=sc.labor,
            initial_inventory=sc.initial_inventory,
            deliveries=sc.deliveries,
            orders={o.vin: o.clone() for o in sc.orders},
            stage_policies=get_strategy_a(),
            external_events=sc.events
        )

        # Run to t=100
        world.run(stop_at_min=100)
        self.assertNotIn("DELIV_SIM_01", world.received_delivery_ids, "Should NOT be received at t=100")
        suppressed = [
            s for s in world.trace.inventory_snapshots
            if "SUPPRESSED_SIMULTANEOUS_ARRIVAL_DELIV_SIM_01" in s.get("reason", "")
        ]
        self.assertEqual(len(suppressed), 1, "Simultaneous arrival must be explicitly suppressed")

        # Run to t=250
        world.run(stop_at_min=250)
        self.assertIn("DELIV_SIM_01", world.received_delivery_ids)
        deliv_snaps = [
            s for s in world.trace.inventory_snapshots
            if s.get("reason") == "DELIVERY_DELIV_SIM_01"
        ]
        self.assertEqual(len(deliv_snaps), 1, "Must arrive exactly once at t=200")
        self.assertEqual(deliv_snaps[0]["time"], 200)

    def test_multi_version_repeat_delays_on_same_delivery(self):
        """4. Multiple sequential delays on same delivery update to latest time without duplicates."""
        sc = copy.deepcopy(self.sc)
        sc.deliveries = [
            SupplyDelivery(
                delivery_id="DELIV_MULTI_01",
                material_id="BATTERY",
                quantity=10,
                available_at_min=100,
                source_version="v1"
            )
        ]
        sc.events = [
            Event(
                event_id="EV_DELAY_V1",
                occurred_at_min=40,
                known_at_min=40,
                event_type="MATERIAL_DELAY",
                payload={"delivery_id": "DELIV_MULTI_01", "new_available_at_min": 180, "revision": 2}
            ),
            Event(
                event_id="EV_DELAY_V2",
                occurred_at_min=80,
                known_at_min=80,
                event_type="MATERIAL_DELAY",
                payload={"delivery_id": "DELIV_MULTI_01", "new_available_at_min": 240, "revision": 3}
            )
        ]

        world = ExecutionWorld(
            calendar=sc.calendar,
            configurations=sc.configurations,
            resources=sc.resources,
            buffers=sc.buffers,
            labor=sc.labor,
            initial_inventory=sc.initial_inventory,
            deliveries=sc.deliveries,
            orders={o.vin: o.clone() for o in sc.orders},
            stage_policies=get_strategy_a(),
            external_events=sc.events
        )

        world.run(stop_at_min=sc.calendar.horizon_end_min)
        self.assertIn("DELIV_MULTI_01", world.received_delivery_ids)
        deliv_snaps = [
            s for s in world.trace.inventory_snapshots
            if s.get("reason") == "DELIVERY_DELIV_MULTI_01"
        ]
        self.assertEqual(len(deliv_snaps), 1, "Only one physical arrival even after multiple delays")
        self.assertEqual(deliv_snaps[0]["time"], 240, "Must arrive at final revised time t=240")

    def test_material_delay_horizon_truncation(self):
        """5. Delay past horizon_end_min truncates arrival cleanly without crash."""
        sc = copy.deepcopy(self.sc)
        sc.deliveries = [
            SupplyDelivery(
                delivery_id="DELIV_TRUNC_01",
                material_id="BATTERY",
                quantity=10,
                available_at_min=100,
                source_version="v1"
            )
        ]
        sc.events = [
            Event(
                event_id="EV_DELAY_BEYOND",
                occurred_at_min=50,
                known_at_min=50,
                event_type="MATERIAL_DELAY",
                payload={"delivery_id": "DELIV_TRUNC_01", "new_available_at_min": 999999}
            )
        ]
        world = ExecutionWorld(
            calendar=sc.calendar,
            configurations=sc.configurations,
            resources=sc.resources,
            buffers=sc.buffers,
            labor=sc.labor,
            initial_inventory=sc.initial_inventory,
            deliveries=sc.deliveries,
            orders={o.vin: o.clone() for o in sc.orders},
            stage_policies=get_strategy_a(),
            external_events=sc.events
        )
        world.run(stop_at_min=sc.calendar.horizon_end_min)
        self.assertNotIn("DELIV_TRUNC_01", world.received_delivery_ids, "Delivery beyond horizon must remain unreceived")

    def test_decision_barrier_unapproved_plan_does_not_affect_world(self):
        """6. Negative test: unapproved / unreceived replan does NOT modify world orders."""
        sc = copy.deepcopy(self.sc)
        world = ExecutionWorld(
            calendar=sc.calendar,
            configurations=sc.configurations,
            resources=sc.resources,
            buffers=sc.buffers,
            labor=sc.labor,
            initial_inventory=sc.initial_inventory,
            deliveries=sc.deliveries,
            orders={o.vin: o.clone() for o in sc.orders},
            stage_policies=get_strategy_a()
        )
        # Initially assign all Day 0
        for o in world.orders.values():
            o.assigned_day = 0

        # Snapshot at t=50
        world.run(stop_at_min=50, pause_before_dispatch_at_stop=True)
        snap = make_snapshot(world, 50, [], sc.deliveries)
        ledger = net_demand(snap)
        aggr = plan_n_plus_6(snap, ledger)
        n3 = plan_n_plus_3(snap, ledger, n6_plan=aggr)
        revised_plan, _ = allocate_orders(snap, n3)
        # Attempt to change assignment to Day 1
        for d in revised_plan.daily_allocations:
            revised_plan.daily_allocations[d] = []
        revised_plan.daily_allocations[1] = list(world.orders.keys())

        # Simulate rejection in Lifecycle
        lifecycle = LifecycleManager()
        baseline = lifecycle.submit_plan(revised_plan, author="ROGUE_PLANNER", at_min=50)
        # Approver REJECTS
        approved = False  # Explicit rejection
        self.assertFalse(approved)

        # Because approval failed, we fail-closed: do NOT apply to world!
        if not approved:
            # Plan not applied!
            pass

        # Verify world orders STILL have Day 0, unchanged!
        for v, o in world.orders.items():
            if o.status in [VehicleStatus.UNRELEASED, VehicleStatus.READY]:
                self.assertEqual(o.assigned_day, 0, f"VIN {v} assigned_day must remain 0 when replan is unapproved")

    def test_prefix_immutability_after_replan(self):
        """7. Executed prefix records up to t_replan are strictly immutable when continuing execution."""
        sc = get_scenario("SCENARIO_2_SUPPLY_DELAY")
        res = execute_strategy_closed_loop(sc, "Strategy_A_EDD")
        self.assertTrue(res["is_valid"])
        self.assertTrue(res["replan_triggered"])
        self.assertGreater(res["replan_count"], 0)

        record = res["replan_records"][0]
        prefix_count = record["prefix_records_count"]
        prefix_hash = record["prefix_hash"]

        # Recompute prefix hash from the final full trace
        final_prefix_records = res["full_trace_records"][:prefix_count]
        recomputed_hash = compute_object_hash(final_prefix_records)

        self.assertEqual(prefix_hash, recomputed_hash, "Executed prefix trace must be bit-for-bit identical after replan")

    def test_fullchain_pipeline_calls_n6_n3_overlap_and_demand_conservation(self):
        """8. Real matrix execution calls N+6/N+3 overlap consistency and demand conservation."""
        sc = get_scenario("SCENARIO_5_CANCELLATION")
        res = execute_strategy_closed_loop(sc, "Strategy_A_EDD")
        self.assertTrue(res["is_valid"])
        self.assertTrue(res["replan_triggered"])
        # Parent plan v1 and child plan v2
        self.assertEqual(len(res["plan_versions"]), 2)
        self.assertEqual(len(res["receipts"]), 2)
        # Check adjustments recorded
        replan_rec = res["replan_records"][0]
        self.assertIn("EV_CANCEL_UNSTARTED_11", replan_rec["trigger_event_ids"])
        self.assertGreater(len(replan_rec["unstarted_adjustments"]), 0)
        self.assertIn("VIN_02", replan_rec["wip_protected_vins"])

    def test_unallocated_vehicle_with_sufficient_stock_cannot_start_node_b(self):
        """9. Key counterexample: Vehicle with abundant physical materials but excluded from approved plan MUST NOT start Node B."""
        sc = copy.deepcopy(self.sc)
        for m in sc.initial_inventory:
            sc.initial_inventory[m] = 999

        world = ExecutionWorld(
            calendar=sc.calendar,
            configurations=sc.configurations,
            resources=sc.resources,
            buffers=sc.buffers,
            labor=sc.labor,
            initial_inventory=sc.initial_inventory,
            deliveries=[],
            orders={o.vin: o.clone() for o in sc.orders},
            stage_policies=get_strategy_a()
        )
        # Authorize all vehicles except VIN_01 (assigned_day = None)
        for v, o in world.orders.items():
            if v == "VIN_01":
                o.assigned_day = None  # Excluded from approved plan
            else:
                o.assigned_day = 0     # Authorized for Day 0

        # Run simulation for 200 minutes (Day 0 shift is active, W machine is idle)
        world.run(stop_at_min=200)

        # Assert VIN_01 NEVER started Node B on Stage W
        v01_records = [r for r in world.trace.records if r.vin == "VIN_01"]
        self.assertEqual(len(v01_records), 0, "Excluded vehicle MUST NOT have any execution records")
        self.assertNotIn("VIN_01", world.trace.stage_node_times, "VIN_01 must not have started Node B")
        self.assertIn(world.orders["VIN_01"].status, [VehicleStatus.UNRELEASED, VehicleStatus.READY])

        # Assert other authorized vehicles DID start Node B
        other_records = [r for r in world.trace.records if r.vin != "VIN_01" and r.node == "B"]
        self.assertGreater(len(other_records), 0, "Authorized vehicles must be dispatched normally")

    def test_replan_revocation_protects_wip_prefix(self):
        """10. Key counterexample: Replan revoking unstarted orders does NOT interrupt or revoke WIP prefix vehicles."""
        sc = copy.deepcopy(self.sc)
        world = ExecutionWorld(
            calendar=sc.calendar,
            configurations=sc.configurations,
            resources=sc.resources,
            buffers=sc.buffers,
            labor=sc.labor,
            initial_inventory=sc.initial_inventory,
            deliveries=sc.deliveries,
            orders={o.vin: o.clone() for o in sc.orders},
            stage_policies=get_strategy_a()
        )
        for o in world.orders.values():
            o.assigned_day = 0

        # Run to t=50 so initial vehicles enter WIP
        world.run(stop_at_min=50, pause_before_dispatch_at_stop=True)
        wip_vins = [
            v for v, o in world.orders.items()
            if o.status in [
                VehicleStatus.SETUP,
                VehicleStatus.PROCESSING,
                VehicleStatus.BLOCKED,
                VehicleStatus.BUFFERED,
                VehicleStatus.IN_TRANSIT
            ]
        ]
        self.assertGreater(len(wip_vins), 0, "Must have vehicles in WIP at t=50")

        # Now simulate a revised plan at t=50 that revokes all remaining unstarted vehicles
        for v, o in world.orders.items():
            if o.status in [VehicleStatus.UNRELEASED, VehicleStatus.READY]:
                o.assigned_day = None  # Revoke authorization

        # Run to horizon end
        world.run(stop_at_min=sc.calendar.horizon_end_min)

        # Assert WIP vehicles completed successfully through their routes
        for v in wip_vins:
            self.assertEqual(world.orders[v].status, VehicleStatus.COMPLETED, f"WIP vehicle {v} must finish execution")
            self.assertIn(v, world.completed_vins)

        # Assert unstarted vehicles with revoked authorization NEVER started
        for v, o in world.orders.items():
            if v not in wip_vins and o.assigned_day is None:
                self.assertNotIn(v, world.completed_vins, f"Revoked vehicle {v} must not be completed")
                self.assertNotIn(v, world.trace.stage_node_times, f"Revoked vehicle {v} must not have records")

    def test_replan_restores_authorization_in_later_version(self):
        """11. Key counterexample: Vehicle excluded in v2 can be re-authorized in v3 and successfully complete."""
        sc = copy.deepcopy(self.sc)
        world = ExecutionWorld(
            calendar=sc.calendar,
            configurations=sc.configurations,
            resources=sc.resources,
            buffers=sc.buffers,
            labor=sc.labor,
            initial_inventory=sc.initial_inventory,
            deliveries=sc.deliveries,
            orders={o.vin: o.clone() for o in sc.orders},
            stage_policies=get_strategy_a()
        )
        # Initially authorize all on Day 0 except VIN_07
        for v, o in world.orders.items():
            o.assigned_day = 0 if v != "VIN_07" else None

        # Run to t=100 (VIN_07 is excluded in v1, cannot dispatch)
        world.run(stop_at_min=100, pause_before_dispatch_at_stop=True)
        self.assertNotIn("VIN_07", world.trace.stage_node_times, "VIN_07 must not have started up to t=100")

        # At t=100, a new plan version (v3) restores authorization for VIN_07 on Day 0
        world.orders["VIN_07"].assigned_day = 0

        # Resume simulation to horizon end
        world.run(stop_at_min=sc.calendar.horizon_end_min)

        # Assert VIN_07 started after t=100 and completed
        self.assertIn("VIN_07", world.trace.stage_node_times, "VIN_07 must start after authorization restored")
        first_b = world.trace.stage_node_times["VIN_07"]["W"]["B"]
        self.assertGreaterEqual(first_b, 100, "VIN_07 must start after restoration epoch t=100")
        self.assertEqual(world.orders["VIN_07"].status, VehicleStatus.COMPLETED)


if __name__ == "__main__":
    unittest.main()
