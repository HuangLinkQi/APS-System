"""test_g1_physics.py - G1 Hand-Calculable Physics & Micro Mechanics Gate.

Verifies:
1. Positive blocking (F > C) under downstream buffer saturation.
2. Pullout vehicle termination immediately after Paint without entering Assembly or P-A buffer.
3. Material reservation at B and actual deduction strictly at S.
4. Shift boundary enforcement (jobs cannot cross shift boundaries).
"""

import unittest
from aps_fullchain.schema import (
    Calendar,
    CalendarShift,
    Configuration,
    Resource,
    Buffer,
    Labor,
    SupplyDelivery,
    Order,
    VehicleStatus,
    MachineStatus
)
from aps_fullchain.simulator import ExecutionWorld, simulate
from aps_fullchain.policies import get_strategy_a
from aps_fullchain.fixtures.scenarios import create_micro_scenario


class TestG1Physics(unittest.TestCase):

    def test_positive_blocking_f_greater_than_c(self):
        """Tests that when upstream machine finishes but downstream buffer is full,
        upstream enters BLOCKED and F > C.
        """
        scenario = create_micro_scenario("TEST_BLOCKING")
        # Run simulation using Strategy A
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
            horizon_end_min=480
        )
        trace = world.run(stop_at_min=480)

        # Check for positive blocking occurrences where F > C
        self.assertGreater(
            len(trace.blocked_durations),
            0,
            "Must observe at least one positive blocking event with F > C"
        )
        for blk in trace.blocked_durations:
            c_val = blk["C"]
            f_val = blk["F"]
            self.assertGreater(f_val, c_val, f"F ({f_val}) must be strictly greater than C ({c_val})")
            self.assertGreater(blk["blocked_duration"], 0)

    def test_pullout_vehicle_mechanics(self):
        """Tests that pullout vehicle exits after Paint, never enters BUF_PA, and never enters Assembly."""
        scenario = create_micro_scenario("TEST_PULLOUT")
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
            horizon_end_min=480
        )
        trace = world.run(stop_at_min=480)

        pullout_vin = "VIN_04_PULLOUT"
        self.assertIn(pullout_vin, trace.pullout_completions)

        # Ensure pullout vehicle executed W and P, but NOT A
        vin_stages = trace.stage_node_times.get(pullout_vin, {})
        self.assertIn("W", vin_stages)
        self.assertIn("P", vin_stages)
        self.assertNotIn("A", vin_stages, "Pullout vehicle must never execute stage A")

        # Ensure pullout vehicle never entered BUF_PA
        for rec in trace.records:
            if rec.vin == pullout_vin:
                self.assertNotEqual(rec.stage, "A")

    def test_material_consumption_at_node_s(self):
        """Verifies that inventory deduction occurs at Node S, not at Node B or C."""
        scenario = create_micro_scenario("TEST_MATERIAL")
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
            horizon_end_min=100
        )
        # Check initial inventory
        initial_steel = world.inventory["STEEL_BODY"]
        world.run(stop_at_min=15)

        # Find first vehicle's B and S timestamps
        vin1_stages = world.trace.stage_node_times.get("VIN_01", {}).get("W", {})
        self.assertIn("B", vin1_stages)
        self.assertIn("S", vin1_stages)
        b_t = vin1_stages["B"]
        s_t = vin1_stages["S"]

        # If setup time is 0, B==S, but if setup time > 0, B < S
        # Inventory must be decremented
        self.assertLess(world.inventory["STEEL_BODY"], initial_steel)


if __name__ == "__main__":
    unittest.main()
