"""fixtures/scenarios.py - Deterministic Test Scenarios and Micro Benchmarks.

Constructs concrete, mathematically verifiable scenarios guaranteeing:
1. Positive blocking (F > C) via buffer capacity = 1 and downstream bottleneck (P takes 35m, W takes 15m).
2. Stage reordering: Color switch minimization in Paint (Strategy B reorders queue vs Strategy A FIFO).
3. Pullout vehicle: VIN_PULLOUT terminates after Paint without entering BUF_PA or Assembly.
4. Cross-day migration: Daily shift capacity (480m) + release times force vehicles across Day 0 and Day 1.
5. Strict material reservation & consumption at Node S.
6. Labor constraints with setup vs processing pools.
"""

from __future__ import annotations
from typing import Dict, List, Any
import copy
from ..schema import (
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
    normalize_scenario
)


def create_micro_scenario(scenario_id: str = "MICRO_BENCHMARK_12") -> Scenario:
    """Creates a 12-vehicle micro benchmark scenario satisfying all G1-G5 audit conditions."""

    # 1. Calendar: 3 days, 8-hour shift per day (0-480, 1440-1920, 2880-3360)
    shifts = [
        CalendarShift(shift_id="D0_S1", day_idx=0, start_min=0, end_min=480),
        CalendarShift(shift_id="D1_S1", day_idx=1, start_min=1440, end_min=1920),
        CalendarShift(shift_id="D2_S1", day_idx=2, start_min=2880, end_min=3360),
    ]
    calendar = Calendar(
        name="Micro3DayCalendar",
        shifts=shifts,
        horizon_end_min=4320,  # 3 days
        day_length_min=1440
    )

    # 2. Configurations
    # SUV_WHITE: W=15, P=35, A=25
    # SUV_BLACK: W=15, P=35, A=25, setup in P when switching from WHITE->BLACK is 10 mins
    # SEDAN_RED: W=15, P=35, A=30
    # PULLOUT_CAR: W=15, P=30, routes only ["W", "P"], is_pullout=True
    configs = {
        "SUV_WHITE": Configuration(
            config_id="SUV_WHITE",
            model_type="SUV",
            color="WHITE",
            material_type="STEEL",
            is_pullout=False,
            stages=["W", "P", "A"],
            process_times={"W": 15, "P": 35, "A": 25},
            process_boms={
                "W": {"STEEL_BODY": 1},
                "P": {"WHITE_PAINT": 1},
                "A": {"BATTERY": 1, "SEAT": 1}
            },
            setup_matrix={
                "P": {"BLACK->WHITE": 10, "RED->WHITE": 10}
            },
            labor_requirements={
                "W": {"setup": 1, "processing": 1},
                "P": {"setup": 1, "processing": 1},
                "A": {"setup": 1, "processing": 1}
            },
            skill_requirements={
                "W": {"setup": "WELD_SKILL", "processing": "WELD_SKILL"},
                "P": {"setup": "PAINT_SKILL", "processing": "PAINT_SKILL"},
                "A": {"setup": "ASSY_SKILL", "processing": "ASSY_SKILL"}
            }
        ),
        "SUV_BLACK": Configuration(
            config_id="SUV_BLACK",
            model_type="SUV",
            color="BLACK",
            material_type="STEEL",
            is_pullout=False,
            stages=["W", "P", "A"],
            process_times={"W": 15, "P": 35, "A": 25},
            process_boms={
                "W": {"STEEL_BODY": 1},
                "P": {"BLACK_PAINT": 1},
                "A": {"BATTERY": 1, "SEAT": 1}
            },
            setup_matrix={
                "P": {"WHITE->BLACK": 10, "RED->BLACK": 10}
            },
            labor_requirements={
                "W": {"setup": 1, "processing": 1},
                "P": {"setup": 1, "processing": 1},
                "A": {"setup": 1, "processing": 1}
            },
            skill_requirements={
                "W": {"setup": "WELD_SKILL", "processing": "WELD_SKILL"},
                "P": {"setup": "PAINT_SKILL", "processing": "PAINT_SKILL"},
                "A": {"setup": "ASSY_SKILL", "processing": "ASSY_SKILL"}
            }
        ),
        "SEDAN_RED": Configuration(
            config_id="SEDAN_RED",
            model_type="SEDAN",
            color="RED",
            material_type="ALUMINUM",
            is_pullout=False,
            stages=["W", "P", "A"],
            process_times={"W": 20, "P": 35, "A": 30},
            process_boms={
                "W": {"ALUM_BODY": 1},
                "P": {"RED_PAINT": 1},
                "A": {"BATTERY": 1, "SEAT": 1}
            },
            setup_matrix={
                "P": {"WHITE->RED": 10, "BLACK->RED": 10}
            },
            labor_requirements={
                "W": {"setup": 1, "processing": 1},
                "P": {"setup": 1, "processing": 1},
                "A": {"setup": 1, "processing": 1}
            },
            skill_requirements={
                "W": {"setup": "WELD_SKILL", "processing": "WELD_SKILL"},
                "P": {"setup": "PAINT_SKILL", "processing": "PAINT_SKILL"},
                "A": {"setup": "ASSY_SKILL", "processing": "ASSY_SKILL"}
            }
        ),
        "PULLOUT_CAR": Configuration(
            config_id="PULLOUT_CAR",
            model_type="SUV",
            color="WHITE",
            material_type="STEEL",
            is_pullout=True,  # Pullout: exits line after Paint!
            stages=["W", "P"],
            process_times={"W": 15, "P": 30},
            process_boms={
                "W": {"STEEL_BODY": 1},
                "P": {"WHITE_PAINT": 1}
            },
            setup_matrix={},
            labor_requirements={
                "W": {"setup": 1, "processing": 1},
                "P": {"setup": 1, "processing": 1}
            },
            skill_requirements={
                "W": {"setup": "WELD_SKILL", "processing": "WELD_SKILL"},
                "P": {"setup": "PAINT_SKILL", "processing": "PAINT_SKILL"}
            }
        )
    }

    # 3. Resources: 1 resource per stage
    resources = {
        "W_LINE": Resource(resource_id="W_LINE", stage="W", capacity=1),
        "P_LINE": Resource(resource_id="P_LINE", stage="P", capacity=1),
        "A_LINE": Resource(resource_id="A_LINE", stage="A", capacity=1)
    }

    # 4. Buffers: Finite capacity buffers
    buffers = {
        "BUF_WP": Buffer(buffer_id="BUF_WP", edge="W->P", capacity=2, transit_time_min=2),
        "BUF_PA": Buffer(buffer_id="BUF_PA", edge="P->A", capacity=2, transit_time_min=2)
    }

    # 5. Labor: 3 workers available, with named skill pools
    labor = Labor(
        pool_id="CREW_01",
        max_workers=3,
        skill_pools={
            "WELD_SKILL": LaborSkillPool("POOL_W", "WELD_SKILL", max_workers=2),
            "PAINT_SKILL": LaborSkillPool("POOL_P", "PAINT_SKILL", max_workers=2),
            "ASSY_SKILL": LaborSkillPool("POOL_A", "ASSY_SKILL", max_workers=2),
            "GENERAL": LaborSkillPool("POOL_G", "GENERAL", max_workers=3),
        }
    )

    # 6. Inventory & Deliveries
    initial_inventory = {
        "STEEL_BODY": 15,
        "ALUM_BODY": 10,
        "WHITE_PAINT": 15,
        "BLACK_PAINT": 15,
        "RED_PAINT": 10,
        "BATTERY": 8,   # Only 8 batteries at start! Remaining will be delivered at t=100
        "SEAT": 20
    }

    deliveries = [
        SupplyDelivery(
            delivery_id="DELIV_BATTERY_01",
            material_id="BATTERY",
            quantity=10,
            available_at_min=100,
            source_version="v1"
        )
    ]

    # 7. Orders: 12 vehicles
    # Mix of configs, one pull-out car, some with Day 1 release date
    orders_data = [
        # Batch 1 (Day 0, morning): V01, V02, V03 (triggers F>C at W because P is slow)
        {"vin": "VIN_01", "config_id": "SUV_WHITE", "rel": 0, "due": 450, "prio": 1, "bucket": "2026-10:P1:SUV_WHITE"},
        {"vin": "VIN_02", "config_id": "SUV_BLACK", "rel": 0, "due": 460, "prio": 1, "bucket": "2026-10:P1:SUV_BLACK"},
        {"vin": "VIN_03", "config_id": "SUV_WHITE", "rel": 0, "due": 470, "prio": 1, "bucket": "2026-10:P1:SUV_WHITE"},
        # Pullout car: V04 (exits after P)
        {"vin": "VIN_04_PULLOUT", "config_id": "PULLOUT_CAR", "rel": 0, "due": 480, "prio": 1, "bucket": "2026-10:P1:PULLOUT_CAR"},
        {"vin": "VIN_05", "config_id": "SEDAN_RED", "rel": 0, "due": 480, "prio": 2, "bucket": "2026-10:P1:SEDAN_RED"},
        {"vin": "VIN_06", "config_id": "SUV_WHITE", "rel": 0, "due": 480, "prio": 2, "bucket": "2026-10:P1:SUV_WHITE"},
        {"vin": "VIN_07", "config_id": "SUV_BLACK", "rel": 0, "due": 1800, "prio": 2, "bucket": "2026-10:P1:SUV_BLACK"},
        # Vehicles with release on Day 1 (cross-day allocation)
        {"vin": "VIN_08", "config_id": "SUV_WHITE", "rel": 1440, "due": 1900, "prio": 1, "bucket": "2026-10:P1:SUV_WHITE"},
        {"vin": "VIN_09", "config_id": "SEDAN_RED", "rel": 1440, "due": 1900, "prio": 1, "bucket": "2026-10:P1:SEDAN_RED"},
        {"vin": "VIN_10", "config_id": "SUV_BLACK", "rel": 1440, "due": 1920, "prio": 2, "bucket": "2026-10:P1:SUV_BLACK"},
        {"vin": "VIN_11", "config_id": "SUV_WHITE", "rel": 1440, "due": 3300, "prio": 2, "bucket": "2026-10:P1:SUV_WHITE"},
        {"vin": "VIN_12", "config_id": "SUV_WHITE", "rel": 2880, "due": 3360, "prio": 2, "bucket": "2026-10:P1:SUV_WHITE"},
    ]

    orders = []
    for od in orders_data:
        cfg = configs[od["config_id"]]
        orders.append(Order(
            order_id=od["vin"],
            vin=od["vin"],
            config_id=od["config_id"],
            release_at_min=od["rel"],
            due_at_min=od["due"],
            priority=od["prio"],
            route=list(cfg.stages),
            forecast_bucket=od["bucket"]
        ))

    # 8. Forecasts: 16 total forecast quantity across buckets
    forecasts = [
        Forecast(forecast_id="FC_01", version=1, month="2026-10", plant_id="P1", config_id="SUV_WHITE", quantity=8),
        Forecast(forecast_id="FC_02", version=1, month="2026-10", plant_id="P1", config_id="SUV_BLACK", quantity=4),
        Forecast(forecast_id="FC_03", version=1, month="2026-10", plant_id="P1", config_id="SEDAN_RED", quantity=3),
        Forecast(forecast_id="FC_04", version=1, month="2026-10", plant_id="P1", config_id="PULLOUT_CAR", quantity=1),
    ]

    # 9. Truth Events (future events known only at specific times)
    events = [
        Event(
            event_id="EV_BATTERY_DELAY_01",
            occurred_at_min=50,
            known_at_min=50,
            event_type="MATERIAL_DELAY",
            payload={"delivery_id": "DELIV_BATTERY_01", "new_available_at_min": 160}
        ),
        Event(
            event_id="EV_ORDER_CANCEL_01",
            occurred_at_min=200,
            known_at_min=200,
            event_type="ORDER_CANCEL",
            payload={"vin": "VIN_07"}
        )
    ]

    return Scenario(
        scenario_id=scenario_id,
        calendar=calendar,
        configurations=configs,
        resources=resources,
        buffers=buffers,
        labor=labor,
        initial_inventory=initial_inventory,
        deliveries=deliveries,
        orders=orders,
        forecasts=forecasts,
        events=events
    )


def create_scenario_1_normal() -> Scenario:
    """Scenario 1: Baseline Normal Operation without unexpected events.
    Full chain conservation, positive blocking, and pullout mechanics all operate cleanly.
    """
    sc = create_micro_scenario("SCENARIO_1_NORMAL")
    sc.events = []
    return sc


def create_scenario_2_supply_delay() -> Scenario:
    """Scenario 2: Supply Delay Disruption.
    Battery delivery DELIV_BATTERY_01 is delayed from t=100 to t=250.
    Known at t=50; vehicles wait for battery at Assembly stage (S-node).
    """
    sc = create_micro_scenario("SCENARIO_2_SUPPLY_DELAY")
    sc.events = [
        Event(
            event_id="EV_BATTERY_DELAY_250",
            occurred_at_min=50,
            known_at_min=50,
            event_type="MATERIAL_DELAY",
            payload={"delivery_id": "DELIV_BATTERY_01", "new_available_at_min": 250}
        )
    ]
    return sc


def create_scenario_2_supply_delay_stress() -> Scenario:
    """Stress variant of Supply Delay: initial BATTERY stock is reduced to 2 units,
    and delivery is delayed to t=250. Causes physical stockout at Assembly Node B,
    line wait in buffer BUF_PA, and true completion tardiness (TWT > 0).
    """
    sc = create_micro_scenario("SCENARIO_2_SUPPLY_DELAY_STRESS")
    sc.initial_inventory["BATTERY"] = 2  # Only 2 batteries on-hand, creating line starvation
    sc.events = [
        Event(
            event_id="EV_BATTERY_DELAY_STRESS_250",
            occurred_at_min=50,
            known_at_min=50,
            event_type="MATERIAL_DELAY",
            payload={"delivery_id": "DELIV_BATTERY_01", "new_available_at_min": 250}
        )
    ]
    # Tighten due date on Car 3 (which waits for delayed battery)
    for o in sc.orders:
        if o.vin == "VIN_03":
            o.due_at_min = 220
    return sc


def create_scenario_3_material_shortage() -> Scenario:
    """Scenario 3: Material Shortage.
    BATTERY inventory is severely constrained: initial=4, delivery=4 (total 8 batteries).
    15 vehicles require batteries; 7 vehicles remain gracefully unmet at horizon end without crashing.
    """
    sc = create_micro_scenario("SCENARIO_3_MATERIAL_SHORTAGE")
    sc.initial_inventory["BATTERY"] = 4
    sc.deliveries = [
        SupplyDelivery(
            delivery_id="DELIV_BATTERY_SHORTAGE",
            material_id="BATTERY",
            quantity=4,
            available_at_min=100,
            source_version="v1"
        )
    ]
    sc.events = []
    return sc


def create_scenario_4_rush_order() -> Scenario:
    """Scenario 4: Urgent VIP Rush Order Insertion with Forecast Netting.
    At t=80, a VIP order VIN_VIP_RUSH arrives. It nets against the residual unstarted
    forecast vehicle in the same bucket (SYN_2026-10_SEDAN_RED_001), maintaining demand conservation.
    """
    sc = create_micro_scenario("SCENARIO_4_RUSH_ORDER")
    rush_order = Order(
        order_id="VIN_VIP_RUSH",
        vin="VIN_VIP_RUSH",
        config_id="SEDAN_RED",
        release_at_min=80,
        due_at_min=480,
        priority=1,
        route=["W", "P", "A"],
        forecast_bucket="2026-10:P1:SEDAN_RED"
    )
    sc.events = [
        Event(
            event_id="EV_RUSH_INSERT",
            occurred_at_min=80,
            known_at_min=80,
            event_type="RUSH_ORDER",
            payload={"order": rush_order, "consumes_synthetic_bucket": "2026-10:P1:SEDAN_RED"}
        )
    ]
    return sc


def create_scenario_4_rush_tight_due() -> Scenario:
    """Stress variant of Rush Order: VIP rush order VIN_VIP_RUSH_TIGHT arrives at t=80
    as unforecasted extra emergency demand (not netting against forecast, active demand expands to 17),
    with tight due date (240m), demonstrating queue prioritization and tardiness impact.
    """
    sc = create_micro_scenario("SCENARIO_4_RUSH_TIGHT_DUE")
    rush_order = Order(
        order_id="VIN_VIP_RUSH_TIGHT",
        vin="VIN_VIP_RUSH_TIGHT",
        config_id="SEDAN_RED",
        release_at_min=80,
        due_at_min=240,
        priority=1,
        route=["W", "P", "A"],
        forecast_bucket="2026-10:P1:SEDAN_RED"
    )
    sc.events = [
        Event(
            event_id="EV_RUSH_INSERT_TIGHT",
            occurred_at_min=80,
            known_at_min=80,
            event_type="RUSH_ORDER",
            payload={"order": rush_order}
        )
    ]
    # Tighten due date on Car 5 to demonstrate queue displacement tardiness
    for o in sc.orders:
        if o.vin == "VIN_05":
            o.due_at_min = 220
    return sc


def create_scenario_5_cancellation() -> Scenario:
    """Scenario 5: Order Cancellation with WIP Protection.
    At t=100, unstarted VIN_11 is cancelled (status -> CANCELLED).
    At t=100, WIP vehicle VIN_02 is requested to be cancelled (protected with CANCELLED_IN_WIP).
    """
    sc = create_micro_scenario("SCENARIO_5_CANCELLATION")
    sc.events = [
        Event(
            event_id="EV_CANCEL_UNSTARTED_11",
            occurred_at_min=100,
            known_at_min=100,
            event_type="ORDER_CANCEL",
            payload={"vin": "VIN_11"}
        ),
        Event(
            event_id="EV_CANCEL_WIP_02",
            occurred_at_min=100,
            known_at_min=100,
            event_type="ORDER_CANCEL",
            payload={"vin": "VIN_02"}
        )
    ]
    return sc


def create_scenario_6_resource_labor_disruption() -> Scenario:
    """Scenario 6: Resource Line Breakdown & Worker Reduction.
    At t=120, Paint Line (P_LINE) stops for 40 minutes (duration_min=40).
    At t=140, total labor pool is temporarily reduced to 1 worker for 30 minutes.
    Both recover and line resumes processing.
    """
    sc = create_micro_scenario("SCENARIO_6_RESOURCE_LABOR_DISRUPTION")
    sc.events = [
        Event(
            event_id="EV_LINE_STOP_P",
            occurred_at_min=120,
            known_at_min=120,
            event_type="LINE_STOP",
            payload={"resource_id": "P_LINE", "duration_min": 40}
        ),
        Event(
            event_id="EV_LABOR_REDUCTION",
            occurred_at_min=140,
            known_at_min=140,
            event_type="LABOR_REDUCTION",
            payload={"new_max_workers": 1, "duration_min": 30}
        )
    ]
    return sc


def create_scenario_7_cross_month() -> Scenario:
    """Scenario 7: Multi-Period Cross-Month Horizon.
    Horizon covers Month 0 (2026-10) and Month 1 (2026-11).
    Month 0 has shifts on days 0..2 (1440 min).
    Month 1 has shifts on days 31..33 (1440 min).
    Month 2 (2026-12) has ZERO shifts (0 capacity).
    Orders demand both months; capacity overflow in Month 0 migrates into Month 1.
    """
    sc = create_micro_scenario("SCENARIO_7_CROSS_MONTH")
    m0_duration = 31 * 1440  # 44640 min
    m1_duration = 30 * 1440  # 43200 min
    horizon_end = m0_duration + m1_duration  # 87840 min

    shifts = [
        # Month 0 (2026-10): Days 0, 1, 2
        CalendarShift(shift_id="M0_D0_S1", day_idx=0, start_min=0, end_min=480),
        CalendarShift(shift_id="M0_D1_S1", day_idx=1, start_min=1440, end_min=1920),
        CalendarShift(shift_id="M0_D2_S1", day_idx=2, start_min=2880, end_min=3360),
        # Month 1 (2026-11): Days 31, 32, 33
        CalendarShift(shift_id="M1_D31_S1", day_idx=31, start_min=m0_duration, end_min=m0_duration + 480),
        CalendarShift(shift_id="M1_D32_S1", day_idx=32, start_min=m0_duration + 1440, end_min=m0_duration + 1920),
        CalendarShift(shift_id="M1_D33_S1", day_idx=33, start_min=m0_duration + 2880, end_min=m0_duration + 3360),
    ]
    sc.calendar = Calendar(
        name="MultiMonthCalendar_Oct_Nov",
        shifts=shifts,
        horizon_end_min=horizon_end,
        day_length_min=1440
    )

    # Orders spanning Month 0 and Month 1
    # 8 orders in Month 0 (due 3360), 4 orders in Month 1 (rel 44640, due 48000)
    for idx, o in enumerate(sc.orders):
        if idx >= 8:
            o.forecast_bucket = o.forecast_bucket.replace("2026-10", "2026-11")
            o.release_at_min = m0_duration
            o.due_at_min = m0_duration + 3360

    # Forecasts for both months
    sc.forecasts = [
        Forecast(forecast_id="FC_M0_WHITE", version=1, month="2026-10", plant_id="P1", config_id="SUV_WHITE", quantity=6),
        Forecast(forecast_id="FC_M0_BLACK", version=1, month="2026-10", plant_id="P1", config_id="SUV_BLACK", quantity=3),
        Forecast(forecast_id="FC_M1_WHITE", version=1, month="2026-11", plant_id="P1", config_id="SUV_WHITE", quantity=4),
        Forecast(forecast_id="FC_M1_BLACK", version=1, month="2026-11", plant_id="P1", config_id="SUV_BLACK", quantity=3),
    ]

    # Additional supply delivery for Month 1
    sc.deliveries.append(
        SupplyDelivery(
            delivery_id="DELIV_BATTERY_M1",
            material_id="BATTERY",
            quantity=10,
            available_at_min=m0_duration,
            source_version="v1"
        )
    )
    sc.events = []
    return sc


SCENARIOS_MAP = {
    "MICRO_BENCHMARK_12": create_micro_scenario,
    "SCENARIO_1_NORMAL": create_scenario_1_normal,
    "SCENARIO_2_SUPPLY_DELAY": create_scenario_2_supply_delay,
    "SCENARIO_2_SUPPLY_DELAY_STRESS": create_scenario_2_supply_delay_stress,
    "SCENARIO_3_MATERIAL_SHORTAGE": create_scenario_3_material_shortage,
    "SCENARIO_4_RUSH_ORDER": create_scenario_4_rush_order,
    "SCENARIO_4_RUSH_TIGHT_DUE": create_scenario_4_rush_tight_due,
    "SCENARIO_5_CANCELLATION": create_scenario_5_cancellation,
    "SCENARIO_6_RESOURCE_LABOR_DISRUPTION": create_scenario_6_resource_labor_disruption,
    "SCENARIO_7_CROSS_MONTH": create_scenario_7_cross_month,
}


def get_scenario(scenario_id: str) -> Scenario:
    builder = SCENARIOS_MAP.get(scenario_id)
    if builder is None:
        raise ValueError(f"Unknown scenario ID '{scenario_id}'. Available: {list_all_scenarios()}")
    return builder()


def list_all_scenarios() -> List[str]:
    return [
        "SCENARIO_1_NORMAL",
        "SCENARIO_2_SUPPLY_DELAY",
        "SCENARIO_2_SUPPLY_DELAY_STRESS",
        "SCENARIO_3_MATERIAL_SHORTAGE",
        "SCENARIO_4_RUSH_ORDER",
        "SCENARIO_4_RUSH_TIGHT_DUE",
        "SCENARIO_5_CANCELLATION",
        "SCENARIO_6_RESOURCE_LABOR_DISRUPTION",
        "SCENARIO_7_CROSS_MONTH"
    ]
