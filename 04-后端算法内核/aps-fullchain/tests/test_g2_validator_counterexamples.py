"""test_g2_validator_counterexamples.py - G2 Gate: Counterexamples and Corruption Rejection.

Verifies that validate_trace independently detects and rejects:
1. Negative material inventory.
2. Buffer overflow beyond capacity.
3. Overlapping jobs on single-capacity machines.
4. Illegal physical timing F < C.
5. Pullout vehicle illegally executing Assembly (Stage A).
6. Inter-stage precedence violation (Stage P starting before Stage W freeing).
"""

import unittest
import copy
from aps_fullchain.fixtures.scenarios import create_micro_scenario
from aps_fullchain.simulator import ExecutionWorld
from aps_fullchain.policies import get_strategy_a
from aps_fullchain.validate import validate_trace
from aps_fullchain.schema import EventRecord


class TestG2ValidatorCounterexamples(unittest.TestCase):

    def setUp(self):
        self.scenario = create_micro_scenario("BASE_VALID")
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
        # Verify valid trace passes validation
        rep = validate_trace(self.scenario, self.valid_trace)
        self.assertTrue(rep.is_valid, f"Base trace should be valid, got errors: {rep.errors}")

    def test_reject_f_less_than_c(self):
        """Rejects trace where F < C (freeing machine before completing processing)."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        # Corrupt VIN_01 Stage W: set F to 10 when C is 15
        corrupt_trace.stage_node_times["VIN_01"]["W"]["F"] = 5
        corrupt_trace.stage_node_times["VIN_01"]["W"]["C"] = 15

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("F < C" in err or "timing violation" in err for err in report.errors))

    def test_reject_machine_job_overlap(self):
        """Rejects trace where two vehicles occupy a single-capacity machine simultaneously."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        # Forge an overlapping record on W_LINE
        corrupt_rec = EventRecord(
            vin="VIN_OVERLAP",
            order_id="VIN_OVERLAP",
            stage="W",
            node="B",
            timestamp_min=5,  # Overlaps with VIN_01 which is running [0, 15)
            resource_id="W_LINE"
        )
        corrupt_trace.records.append(corrupt_rec)
        corrupt_trace.stage_node_times["VIN_OVERLAP"] = {"W": {"B": 5, "S": 5, "C": 20, "F": 20}}

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("capacity exceeded" in err for err in report.errors))

    def test_reject_buffer_overflow(self):
        """Rejects trace where buffer capacity (2) is exceeded."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        # Set BUF_WP (cap 2) to hold 3 vehicles simultaneously by prolonging stay in buffer
        corrupt_trace.stage_node_times["VIN_01"]["P"]["B"] = 200
        corrupt_trace.stage_node_times["VIN_02"]["P"]["B"] = 200
        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("overflow" in err for err in report.errors))
        self.assertTrue(any("overflow" in err for err in report.errors))

    def test_reject_pullout_executing_stage_a(self):
        """Rejects trace where pullout car enters Stage A."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        corrupt_trace.stage_node_times["VIN_04_PULLOUT"]["A"] = {"B": 100, "S": 100, "C": 120, "F": 120}

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("Pullout vehicle" in err and "Stage A" in err for err in report.errors))

    def test_reject_interstage_precedence_violation(self):
        """Rejects trace where Stage P starts B before Stage W is freed (F)."""
        corrupt_trace = copy.deepcopy(self.valid_trace)
        # Set Stage W.F to 30, but Stage P.B to 20
        corrupt_trace.stage_node_times["VIN_01"]["W"]["F"] = 30
        corrupt_trace.stage_node_times["VIN_01"]["P"]["B"] = 20

        report = validate_trace(self.scenario, corrupt_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("inter-stage overlap" in err for err in report.errors))

    def test_reject_negative_material(self):
        """Rejects scenario where required material is exhausted (negative inventory)."""
        corrupt_scenario = copy.deepcopy(self.scenario)
        # Set initial stock of STEEL_BODY to 0, no delivery
        corrupt_scenario.initial_inventory["STEEL_BODY"] = 0
        corrupt_scenario.deliveries = []

        report = validate_trace(corrupt_scenario, self.valid_trace)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("Negative material balance" in err for err in report.errors))


if __name__ == "__main__":
    unittest.main()
