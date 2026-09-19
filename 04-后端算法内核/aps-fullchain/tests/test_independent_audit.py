"""test_independent_audit.py - Independent QA Invariant Audit & Counterexamples Suite.

Verifies that validate_trace independently enforces all physical, operational, and lifecycle invariants:
1. Working window compliance (no work during off-shifts, [B, C) cannot cross shift boundary)
2. Labor capacity & skill pool concurrency bounds
3. Inter-stage transit tau (transfer delay) enforcement
4. True processing duration fidelity (C - S == configured process time)
5. S - B setup duration & labor wait calibration (S >= B + setup_time)
6. Dual-ledger consistency between trace.records and trace.stage_node_times
7. Unknown VIN and invalid configuration rejection
8. Node lifecycle completeness (no skipped predecessor nodes)
9. Continuous machine occupancy hold for in-flight / unfinished jobs
10. Truthful order completion (no false completion claims for unfinished VINs)
11. Material reservation non-negativity at Node B and physical non-negativity at Node S
12. Actual trace inventory snapshot fidelity and dynamic delivery tracking
13. Route conservation (no stage skipping, pullout exclusivity)
14. Missing validation data reporting (empty trace, missing configs)
15. Acceptance of legal in-flight unfinished nodes at stop_at cutoff
"""

import unittest
import copy
from aps_fullchain.fixtures.scenarios import create_micro_scenario
from aps_fullchain.simulator import ExecutionWorld
from aps_fullchain.policies import get_strategy_a
from aps_fullchain.validate import validate_trace
from aps_fullchain.schema import (
    EventRecord,
    VehicleStatus,
    SimulationTrace,
    Order,
    Configuration,
    Labor,
    SupplyDelivery
)


class TestIndependentAudit(unittest.TestCase):

    def setUp(self):
        self.scenario = create_micro_scenario("BASE_AUDIT")
        # Run standard baseline
        world = ExecutionWorld(
            calendar=self.scenario.calendar,
            configurations=self.scenario.configurations,
            resources=self.scenario.resources,
            buffers=self.scenario.buffers,
            labor=self.scenario.labor,
            initial_inventory=self.scenario.initial_inventory,
            deliveries=self.scenario.deliveries,
            orders={o.vin: o.clone() for o in self.scenario.orders},
            stage_policies=get_strategy_a(),
            horizon_end_min=480
        )
        self.valid_trace = world.run(stop_at_min=480)
        rep = validate_trace(self.scenario, self.valid_trace)
        self.assertTrue(rep.is_valid, f"Baseline trace should be valid, got errors: {rep.errors}")

    def test_baseline_valid_trace(self):
        """Baseline uncorrupted trace passes all independent audit checks."""
        report = validate_trace(self.scenario, self.valid_trace)
        self.assertTrue(report.is_valid)
        self.assertEqual(len(report.errors), 0)
        self.assertGreaterEqual(len(report.checks_performed), 10)

    def test_reject_working_window_cross_shift_boundary(self):
        """Rejects active work interval [B, C) that crosses a calendar shift boundary."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        # Shift 0 ends at 480. Tamper VIN_07 Stage A to start at B=460 and finish at C=500
        corrupt_trace.stage_node_times["VIN_07"]["A"]["B"] = 460
        corrupt_trace.stage_node_times["VIN_07"]["A"]["S"] = 460
        corrupt_trace.stage_node_times["VIN_07"]["A"]["C"] = 485  # Crosses 480 boundary
        corrupt_trace.stage_node_times["VIN_07"]["A"]["F"] = 485
        # Keep records in sync to isolate the working window check
        for r in corrupt_trace.records:
            if r.vin == "VIN_07" and r.stage == "A":
                r.timestamp_min = corrupt_trace.stage_node_times["VIN_07"]["A"][r.node]

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("violates calendar shift window" in err for err in report.errors),
            f"Expected shift window violation, got: {report.errors}"
        )

    def test_reject_working_during_non_work_hours(self):
        """Rejects work starting outside working calendar shifts."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        # Shift 0 is [0, 480), shift 1 is [1440, 1920). Minute 600 is off-shift.
        corrupt_trace.stage_node_times["VIN_01"]["W"]["B"] = 600
        corrupt_trace.stage_node_times["VIN_01"]["W"]["S"] = 600
        corrupt_trace.stage_node_times["VIN_01"]["W"]["C"] = 615
        corrupt_trace.stage_node_times["VIN_01"]["W"]["F"] = 615
        for r in corrupt_trace.records:
            if r.vin == "VIN_01" and r.stage == "W":
                r.timestamp_min = corrupt_trace.stage_node_times["VIN_01"]["W"][r.node]

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("outside active working calendar shifts" in err for err in report.errors),
            f"Expected outside active working calendar shifts error, got: {report.errors}"
        )

    def test_reject_processing_duration_mismatch(self):
        """Rejects fabricated processing duration where C - S != configured process time."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        # VIN_01 Stage W process time is 15. Set C = S + 8 = 8 (fabricated faster processing)
        s_val = corrupt_trace.stage_node_times["VIN_01"]["W"]["S"]
        corrupt_trace.stage_node_times["VIN_01"]["W"]["C"] = s_val + 8
        corrupt_trace.stage_node_times["VIN_01"]["W"]["F"] = s_val + 8
        for r in corrupt_trace.records:
            if r.vin == "VIN_01" and r.stage == "W" and r.node in ("C", "F"):
                r.timestamp_min = corrupt_trace.stage_node_times["VIN_01"]["W"][r.node]

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("processing duration mismatch" in err and "C - S = 8" in err for err in report.errors),
            f"Expected processing duration mismatch error, got: {report.errors}"
        )

    def test_reject_premature_processing_before_setup_finished(self):
        """Rejects Node S starting before setup duration completes (S - B < setup_time)."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        # VIN_02 Stage P switches from WHITE to BLACK, requiring 10 min setup.
        # B is 52. Required S is >= 62. Set S=56 (starts before setup finishes).
        corrupt_trace.stage_node_times["VIN_02"]["P"]["S"] = 56
        for r in corrupt_trace.records:
            if r.vin == "VIN_02" and r.stage == "P" and r.node == "S":
                r.timestamp_min = 56

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("before setup finished" in err for err in report.errors),
            f"Expected before setup finished error, got: {report.errors}"
        )

    def test_reject_interstage_transit_tau_violation(self):
        """Rejects downstream stage starting before upstream F + buffer transit time (tau)."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        # VIN_01 Stage W finishes F at 15. BUF_WP transit_time_min = 2.
        # Therefore Stage P.B must be >= 17. Corrupt P.B to 16.
        corrupt_trace.stage_node_times["VIN_01"]["P"]["B"] = 16
        corrupt_trace.stage_node_times["VIN_01"]["P"]["S"] = 16
        for r in corrupt_trace.records:
            if r.vin == "VIN_01" and r.stage == "P" and r.node in ("B", "S"):
                r.timestamp_min = 16

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("inter-stage transit violation" in err for err in report.errors),
            f"Expected transit violation error, got: {report.errors}"
        )

    def test_reject_labor_concurrency_exceeded(self):
        """Rejects schedule where concurrent worker demand exceeds labor pool capacity."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        # In base scenario, labor.max_workers = 3.
        # Forge simultaneous processing on 4 vehicles across stages at t=54.
        # VIN_01 is on A (labor 1), VIN_02 is on P (labor 1).
        # We add VIN_05 and VIN_06 processing concurrently at t=54.
        corrupt_scenario = copy.deepcopy(self.scenario)
        corrupt_scenario.labor.max_workers = 1  # Restrict to 1 worker to trigger violation

        report = validate_trace(corrupt_scenario, self.valid_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("Labor capacity exceeded" in err for err in report.errors),
            f"Expected labor capacity exceeded error, got: {report.errors}"
        )

    def test_reject_records_and_stage_node_times_inconsistent_timestamp(self):
        """Rejects trace where EventRecord timestamp disagrees with stage_node_times."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        # In records, set VIN_01 Stage W Node B timestamp to 5, leaving stage_node_times at 0
        for r in corrupt_trace.records:
            if r.vin == "VIN_01" and r.stage == "W" and r.node == "B":
                r.timestamp_min = 5
                break

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("Inconsistency between records and stage_node_times" in err for err in report.errors),
            f"Expected dual-ledger inconsistency error, got: {report.errors}"
        )

    def test_reject_record_missing_from_stage_node_times(self):
        """Rejects trace where a record exists in records but is missing from stage_node_times."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        del corrupt_trace.stage_node_times["VIN_01"]["W"]["B"]

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("Inconsistency" in err for err in report.errors),
            f"Expected inconsistency error, got: {report.errors}"
        )

    def test_reject_stage_node_time_missing_from_records(self):
        """Rejects trace where stage_node_times contains an entry not present in records."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        corrupt_trace.records = [
            r for r in corrupt_trace.records
            if not (r.vin == "VIN_01" and r.stage == "W" and r.node == "B")
        ]

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("not found in trace records" in err for err in report.errors),
            f"Expected missing record error, got: {report.errors}"
        )

    def test_reject_unknown_vin_in_trace(self):
        """Rejects trace containing records for an unknown VIN not registered in scenario."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        corrupt_trace.stage_node_times["VIN_GHOST_999"] = {"W": {"B": 0, "S": 0, "C": 15, "F": 15}}
        corrupt_trace.records.append(
            EventRecord(vin="VIN_GHOST_999", order_id="ORDER_GHOST", stage="W", node="B", timestamp_min=0, resource_id="W_LINE")
        )

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("Unknown VIN 'VIN_GHOST_999'" in err for err in report.errors),
            f"Expected unknown VIN error, got: {report.errors}"
        )

    def test_reject_skipped_node_sequence(self):
        """Rejects stage execution with skipped predecessor nodes (e.g. S without B)."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        # Remove B from VIN_01 Stage W
        del corrupt_trace.stage_node_times["VIN_01"]["W"]["B"]
        corrupt_trace.records = [
            r for r in corrupt_trace.records
            if not (r.vin == "VIN_01" and r.stage == "W" and r.node == "B")
        ]

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("illegal/skipped node set" in err for err in report.errors),
            f"Expected skipped node set error, got: {report.errors}"
        )

    def test_reject_continuous_machine_hold_overlap_on_unfinished_job(self):
        """Rejects a second job starting on a machine while an unfinished in-flight job still holds it."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        # Simulate an unfinished job on W_LINE: remove C and F for VIN_01 (it started B=0, S=0 but hasn't freed W_LINE)
        del corrupt_trace.stage_node_times["VIN_01"]["W"]["C"]
        del corrupt_trace.stage_node_times["VIN_01"]["W"]["F"]
        corrupt_trace.records = [
            r for r in corrupt_trace.records
            if not (r.vin == "VIN_01" and r.stage == "W" and r.node in ("C", "F"))
        ]
        # VIN_02 starts at B=15 on W_LINE. Since VIN_01 never freed W_LINE, this is a capacity violation!
        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("Resource W_LINE capacity exceeded" in err for err in report.errors),
            f"Expected continuous hold capacity violation, got: {report.errors}"
        )

    def test_reject_false_completion_claim_when_final_stage_incomplete(self):
        """Rejects scenario where an order claims COMPLETED status but final stage F is incomplete."""
        corrupt_scenario = copy.deepcopy(self.scenario)
        # VIN_07 did not complete Stage A in a stop_at=100 prefix run
        corrupt_order = next(o for o in corrupt_scenario.orders if o.vin == "VIN_07")
        corrupt_order.status = VehicleStatus.COMPLETED
        corrupt_order.completed_at_min = 350

        # Create a prefix trace where VIN_07 only reached Stage W
        prefix_trace = copy.deepcopy(self.valid_trace)
        if "A" in prefix_trace.stage_node_times["VIN_07"]:
            del prefix_trace.stage_node_times["VIN_07"]["A"]
        prefix_trace.records = [
            r for r in prefix_trace.records
            if not (r.vin == "VIN_07" and r.stage == "A")
        ]

        report = validate_trace(corrupt_scenario, prefix_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("False completion claim" in err for err in report.errors),
            f"Expected false completion claim error, got: {report.errors}"
        )

    def test_reject_false_pullout_completion_claim(self):
        """Rejects non-pullout vehicle falsely listed in pullout_completions."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        corrupt_trace.pullout_completions.append("VIN_01")  # VIN_01 is standard SUV_WHITE, not pullout

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("False pullout claim" in err for err in report.errors),
            f"Expected false pullout claim error, got: {report.errors}"
        )

    def test_reject_material_reservation_insufficient_at_b(self):
        """Rejects scenario where required material cannot be reserved at Node B due to stock exhaustion."""
        corrupt_scenario = copy.deepcopy(self.scenario)
        # VIN_01 and VIN_02 both require STEEL_BODY at Stage W.
        # Set initial STEEL_BODY to only 1, so VIN_01 reserves it, and VIN_02 cannot reserve it at B=15.
        corrupt_scenario.initial_inventory["STEEL_BODY"] = 1
        corrupt_scenario.deliveries = []

        report = validate_trace(corrupt_scenario, self.valid_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("Insufficient material reservation for STEEL_BODY at Node B" in err for err in report.errors),
            f"Expected insufficient material reservation error, got: {report.errors}"
        )

    def test_reject_negative_stock_in_trace_inventory_snapshots(self):
        """Rejects trace whose inventory snapshots report negative stock."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        corrupt_trace.inventory_snapshots.append({
            "time": 200,
            "material_id": "BATTERY",
            "on_hand": -3,
            "reason": "FORGED_RECORD"
        })

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("Negative material balance for BATTERY in trace snapshot" in err for err in report.errors),
            f"Expected negative balance in trace snapshot error, got: {report.errors}"
        )

    def test_reject_inventory_snapshot_mismatch_with_ledger(self):
        """Rejects trace whose inventory snapshot claims stock contradicting the independent ledger."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        # Forge on_hand = 999 for BATTERY at delivery time t=100
        for snap in corrupt_trace.inventory_snapshots:
            if snap.get("reason") == "DELIVERY_DELIV_BATTERY_01":
                snap["on_hand"] = 999
                break

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("Trace inventory snapshot mismatch for BATTERY" in err for err in report.errors),
            f"Expected snapshot mismatch error, got: {report.errors}"
        )

    def test_reject_route_stage_skipping(self):
        """Rejects trace where a vehicle skips an intermediate stage in its route."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        # Delete Stage P for VIN_01, but keep Stage A (skipping Paint)
        del corrupt_trace.stage_node_times["VIN_01"]["P"]
        corrupt_trace.records = [
            r for r in corrupt_trace.records
            if not (r.vin == "VIN_01" and r.stage == "P")
        ]

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("route violation: started Stage A without executing previous Stage P" in err for err in report.errors),
            f"Expected stage skipping route violation, got: {report.errors}"
        )

    def test_reject_empty_trace_when_scenario_has_orders(self):
        """Rejects empty trace when scenario contains orders, reporting missing validation data."""
        empty_trace = SimulationTrace()
        report = validate_trace(self.scenario, empty_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("Missing validation data: simulation trace is completely empty" in err for err in report.errors),
            f"Expected missing validation data error, got: {report.errors}"
        )

    def test_accept_legal_in_flight_uncompleted_nodes_at_stop_at(self):
        """Accepts legitimate in-flight uncompleted nodes when simulation stops mid-run (e.g. stop_at_min=100)."""
        world = ExecutionWorld(
            calendar=self.scenario.calendar,
            configurations=self.scenario.configurations,
            resources=self.scenario.resources,
            buffers=self.scenario.buffers,
            labor=self.scenario.labor,
            initial_inventory=self.scenario.initial_inventory,
            deliveries=self.scenario.deliveries,
            orders={o.vin: o.clone() for o in self.scenario.orders},
            stage_policies=get_strategy_a(),
            horizon_end_min=480
        )
        # Run up to t=100 (where some vehicles are in SETUP, PROCESSING, or BLOCKED)
        trace_100 = world.run(stop_at_min=100)
        report = validate_trace(self.scenario, trace_100)

        # Must be accepted as valid physical trace with no errors
        self.assertTrue(
            report.is_valid,
            f"Legal in-flight uncompleted trace at stop_at=100 must be valid, got errors: {report.errors}"
        )
        self.assertEqual(len(report.errors), 0)

    def test_reject_first_vehicle_setup_falsification(self):
        """Rejects forged trace where first vehicle setup on machine is falsified/skipped.
        Independent calculation uses scenario.resource immutable initial attribute (e.g. last_color='BLACK')
        and refuses trace self-reported details or 0 setup assumption.
        """
        scenario = copy.deepcopy(self.scenario)
        # Explicit initial attribute on P_LINE: previously ran BLACK vehicle
        scenario.resources["P_LINE"].last_color = "BLACK"
        scenario.resources["P_LINE"].initial_last_color = "BLACK"

        # In valid_trace, VIN_01 has color WHITE. BLACK->WHITE setup is 10 minutes.
        # But valid_trace had B=17, S=17 (0 setup).
        # When evaluated against scenario with initial last_color='BLACK', S must equal B + 10 = 27.
        report = validate_trace(scenario, self.valid_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("S-B timing violation" in err for err in report.errors),
            f"Expected first-vehicle S-B setup timing violation, got: {report.errors}"
        )

    def test_reject_unknown_resource_id(self):
        """Rejects trace referencing an unknown resource ID and refuses to default to cap=1."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        # Inject an unregistered resource ID in an EventRecord
        corrupt_rec = EventRecord(
            vin="VIN_01",
            order_id="VIN_01",
            stage="W",
            node="B",
            timestamp_min=0,
            resource_id="UNREGISTERED_LINE_99"
        )
        corrupt_trace.records.append(corrupt_rec)
        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("Unknown resource 'UNREGISTERED_LINE_99'" in err for err in report.errors),
            f"Expected unknown resource rejection, got: {report.errors}"
        )

    def test_reject_labor_zero_period_or_zero_workers(self):
        """Rejects labor consumption when available labor is zero."""
        scenario = copy.deepcopy(self.scenario)
        scenario.labor.max_workers = 0  # Zero labor window / no available workers

        report = validate_trace(scenario, self.valid_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("Labor capacity exceeded" in err and "available=0" in err for err in report.errors),
            f"Expected zero labor capacity error, got: {report.errors}"
        )

    def test_boundary_positive_different_setup_proc_labor_parallel(self):
        """Positive boundary test: setup labor and processing labor differ, and stages execute
        in parallel within aggregate max_workers without false capacity reduction.
        """
        scenario = copy.deepcopy(self.scenario)
        # Max workers = 3
        scenario.labor.max_workers = 3
        cfg_w = scenario.configurations["SUV_WHITE"]
        cfg_w.labor_requirements["P"] = {"setup": 2, "processing": 1}
        cfg_w.labor_requirements["A"] = {"setup": 1, "processing": 2}
        cfg_w.process_times["P"] = 50
        cfg_w.process_times["A"] = 30
        cfg_w.process_boms["P"] = {}
        cfg_w.process_boms["A"] = {}

        order1 = Order("VIN_P1", "VIN_P1", "SUV_WHITE", release_at_min=0, due_at_min=500, route=["P"])
        order2 = Order("VIN_P2", "VIN_P2", "SUV_WHITE", release_at_min=0, due_at_min=500, route=["A"])
        scenario.orders = [order1, order2]

        trace = SimulationTrace()
        # VIN_P1 in Paint: B=70, S=70, C=120, F=120 (setup=0, proc=50, labor_proc=1)
        # VIN_P2 in Assembly: B=100, S=100, C=130, F=130 (setup=0, proc=30, labor_proc=2)
        # At t=100..120: VIN_P1 proc (1) + VIN_P2 proc (2) = 3 <= 3!
        # Both stages run in parallel and never exceed max_workers (3)
        trace.record_node(EventRecord("VIN_P1", "VIN_P1", "P", "B", 70, "P_LINE"))
        trace.record_node(EventRecord("VIN_P1", "VIN_P1", "P", "S", 70, "P_LINE"))
        trace.record_node(EventRecord("VIN_P1", "VIN_P1", "P", "C", 120, "P_LINE"))
        trace.record_node(EventRecord("VIN_P1", "VIN_P1", "P", "F", 120, "P_LINE"))

        trace.record_node(EventRecord("VIN_P2", "VIN_P2", "A", "B", 100, "A_LINE"))
        trace.record_node(EventRecord("VIN_P2", "VIN_P2", "A", "S", 100, "A_LINE"))
        trace.record_node(EventRecord("VIN_P2", "VIN_P2", "A", "C", 130, "A_LINE"))
        trace.record_node(EventRecord("VIN_P2", "VIN_P2", "A", "F", 130, "A_LINE"))

        report = validate_trace(scenario, trace)
        self.assertTrue(report.is_valid, f"Boundary parallel execution should be valid, got errors: {report.errors}")
        self.assertEqual(len(report.errors), 0)


if __name__ == "__main__":
    unittest.main()
