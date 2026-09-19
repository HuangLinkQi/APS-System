"""fixtures/scenarios_advanced.py - Multi-Scale & Compound Disruption Benchmark Scenarios.

Provides mathematically verifiable, production-grade test scenarios across:
1. Multi-scale horizons:
   - 16 vehicles: Micro benchmark (Day 0 & Day 1 two-shift structure)
   - 32 vehicles: Two-day medium scale (Day 0 & Day 1 two-shift structure)
   - 48 vehicles: Three-day extended scale (Day 0, Day 1, Day 2 two-shift structure)
2. Compound & extreme disruptions:
   - Dual critical material delay + multi-station labor temporary reduction
   - High-density tight-deadline VIP batch rush orders + original order cancellation compound shock
   - High-ratio aluminum vehicles & coating multi-color interleaving process bottleneck
3. Real calendar shift mapping:
   - Two 8-hour shifts per day (Shift 1: 08:00-16:00, Shift 2: 17:00-01:00)
   - Realistic BOM delivery schedules and guaranteed physical feasibility
"""

from __future__ import annotations
from typing import Dict, List, Any, Optional
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


def create_advanced_configs(bottleneck: bool = False) -> Dict[str, Configuration]:
    """Creates configurations covering SUV and SEDAN models with STEEL and ALUMINUM body types,
    and 5 coating colors (WHITE, BLACK, SILVER, RED, BLUE).
    In bottleneck mode, paint setup penalty between different colors is increased to 15 min,
    and welding time for aluminum sedans is increased to 25 min.
    """
    colors = ["WHITE", "BLACK", "SILVER", "RED", "BLUE"]
    setup_penalty = 15 if bottleneck else 10
    setup_p: Dict[str, int] = {}
    for c1 in colors:
        for c2 in colors:
            if c1 != c2:
                setup_p[f"{c1}->{c2}"] = setup_penalty

    w_sedan = 25 if bottleneck else 20

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
            setup_matrix={"P": dict(setup_p)},
            labor_requirements={"W": {"setup": 1, "processing": 1}, "P": {"setup": 1, "processing": 1}, "A": {"setup": 1, "processing": 1}},
            skill_requirements={"W": {"setup": "WELD_SKILL", "processing": "WELD_SKILL"}, "P": {"setup": "PAINT_SKILL", "processing": "PAINT_SKILL"}, "A": {"setup": "ASSY_SKILL", "processing": "ASSY_SKILL"}}
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
            setup_matrix={"P": dict(setup_p)},
            labor_requirements={"W": {"setup": 1, "processing": 1}, "P": {"setup": 1, "processing": 1}, "A": {"setup": 1, "processing": 1}},
            skill_requirements={"W": {"setup": "WELD_SKILL", "processing": "WELD_SKILL"}, "P": {"setup": "PAINT_SKILL", "processing": "PAINT_SKILL"}, "A": {"setup": "ASSY_SKILL", "processing": "ASSY_SKILL"}}
        ),
        "SUV_SILVER": Configuration(
            config_id="SUV_SILVER",
            model_type="SUV",
            color="SILVER",
            material_type="STEEL",
            is_pullout=False,
            stages=["W", "P", "A"],
            process_times={"W": 15, "P": 35, "A": 25},
            process_boms={
                "W": {"STEEL_BODY": 1},
                "P": {"SILVER_PAINT": 1},
                "A": {"BATTERY": 1, "SEAT": 1}
            },
            setup_matrix={"P": dict(setup_p)},
            labor_requirements={"W": {"setup": 1, "processing": 1}, "P": {"setup": 1, "processing": 1}, "A": {"setup": 1, "processing": 1}},
            skill_requirements={"W": {"setup": "WELD_SKILL", "processing": "WELD_SKILL"}, "P": {"setup": "PAINT_SKILL", "processing": "PAINT_SKILL"}, "A": {"setup": "ASSY_SKILL", "processing": "ASSY_SKILL"}}
        ),
        "SEDAN_RED": Configuration(
            config_id="SEDAN_RED",
            model_type="SEDAN",
            color="RED",
            material_type="ALUMINUM",
            is_pullout=False,
            stages=["W", "P", "A"],
            process_times={"W": w_sedan, "P": 35, "A": 30},
            process_boms={
                "W": {"ALUM_BODY": 1},
                "P": {"RED_PAINT": 1},
                "A": {"BATTERY": 1, "SEAT": 1}
            },
            setup_matrix={"P": dict(setup_p)},
            labor_requirements={"W": {"setup": 1, "processing": 1}, "P": {"setup": 1, "processing": 1}, "A": {"setup": 1, "processing": 1}},
            skill_requirements={"W": {"setup": "WELD_SKILL", "processing": "WELD_SKILL"}, "P": {"setup": "PAINT_SKILL", "processing": "PAINT_SKILL"}, "A": {"setup": "ASSY_SKILL", "processing": "ASSY_SKILL"}}
        ),
        "SEDAN_BLUE": Configuration(
            config_id="SEDAN_BLUE",
            model_type="SEDAN",
            color="BLUE",
            material_type="ALUMINUM",
            is_pullout=False,
            stages=["W", "P", "A"],
            process_times={"W": w_sedan, "P": 35, "A": 30},
            process_boms={
                "W": {"ALUM_BODY": 1},
                "P": {"BLUE_PAINT": 1},
                "A": {"BATTERY": 1, "SEAT": 1}
            },
            setup_matrix={"P": dict(setup_p)},
            labor_requirements={"W": {"setup": 1, "processing": 1}, "P": {"setup": 1, "processing": 1}, "A": {"setup": 1, "processing": 1}},
            skill_requirements={"W": {"setup": "WELD_SKILL", "processing": "WELD_SKILL"}, "P": {"setup": "PAINT_SKILL", "processing": "PAINT_SKILL"}, "A": {"setup": "ASSY_SKILL", "processing": "ASSY_SKILL"}}
        ),
        "PULLOUT_CAR": Configuration(
            config_id="PULLOUT_CAR",
            model_type="SUV",
            color="WHITE",
            material_type="STEEL",
            is_pullout=True,
            stages=["W", "P"],
            process_times={"W": 15, "P": 30},
            process_boms={
                "W": {"STEEL_BODY": 1},
                "P": {"WHITE_PAINT": 1}
            },
            setup_matrix={"P": dict(setup_p)},
            labor_requirements={"W": {"setup": 1, "processing": 1}, "P": {"setup": 1, "processing": 1}},
            skill_requirements={"W": {"setup": "WELD_SKILL", "processing": "WELD_SKILL"}, "P": {"setup": "PAINT_SKILL", "processing": "PAINT_SKILL"}}
        )
    }
    return configs


def create_two_shift_calendar(days: int = 2) -> Calendar:
    """Creates a real calendar mapping two 8-hour shifts per day:
    Shift 1: 08:00 - 16:00 (480 mins)
    Shift 2: 17:00 - 01:00 (480 mins)
    Inter-shift maintenance: 60 mins. Day length: 1440 mins.
    """
    shifts: List[CalendarShift] = []
    for d in range(days):
        day_base = d * 1440
        shifts.append(CalendarShift(
            shift_id=f"D{d}_S1",
            day_idx=d,
            start_min=day_base + 0,
            end_min=day_base + 480
        ))
        shifts.append(CalendarShift(
            shift_id=f"D{d}_S2",
            day_idx=d,
            start_min=day_base + 540,
            end_min=day_base + 1020
        ))
    return Calendar(
        name=f"Automotive_TwoShift_{days}Day_Calendar",
        shifts=shifts,
        horizon_end_min=days * 1440,
        day_length_min=1440
    )


def create_advanced_resources() -> Dict[str, Resource]:
    return {
        "W_LINE": Resource(resource_id="W_LINE", stage="W", capacity=1),
        "P_LINE": Resource(resource_id="P_LINE", stage="P", capacity=1),
        "A_LINE": Resource(resource_id="A_LINE", stage="A", capacity=1)
    }


def create_advanced_buffers() -> Dict[str, Buffer]:
    return {
        "BUF_WP": Buffer(buffer_id="BUF_WP", edge="W->P", capacity=4, transit_time_min=2),
        "BUF_PA": Buffer(buffer_id="BUF_PA", edge="P->A", capacity=4, transit_time_min=2)
    }


def create_advanced_labor(max_workers: int = 4) -> Labor:
    return Labor(
        pool_id="MAIN_LABOR_POOL",
        max_workers=max_workers,
        skill_pools={
            "WELD_SKILL": LaborSkillPool("POOL_W", "WELD_SKILL", max_workers=2),
            "PAINT_SKILL": LaborSkillPool("POOL_P", "PAINT_SKILL", max_workers=2),
            "ASSY_SKILL": LaborSkillPool("POOL_A", "ASSY_SKILL", max_workers=2),
            "GENERAL": LaborSkillPool("POOL_G", "GENERAL", max_workers=max_workers),
        }
    )


# =========================================================================
# Base Builders for Scales: 16 cars, 32 cars, 48 cars
# =========================================================================

def build_scale_scenario(
    scenario_id: str,
    scale: int,
    bottleneck: bool = False
) -> Scenario:
    """Builds a deterministic multi-scale scenario.
    scale: 16 (2 days), 32 (2 days), 48 (3 days).
    Enforces demand conservation with firm orders + synthetic forecast orders.
    """
    days = 3 if scale >= 48 else 2
    calendar = create_two_shift_calendar(days=days)
    configs = create_advanced_configs(bottleneck=bottleneck)
    resources = create_advanced_resources()
    buffers = create_advanced_buffers()
    labor = create_advanced_labor(max_workers=4)

    # Orders distribution
    # 16 cars: 14 firm + 2 forecast residual
    # 32 cars: 28 firm + 4 forecast residual
    # 48 cars: 42 firm + 6 forecast residual
    firm_count = int(scale * 14 / 16)
    forecast_target = scale

    cars_per_day = firm_count // days
    orders_data: List[Dict[str, Any]] = []

    if bottleneck:
        # High aluminum (SEDAN_RED and SEDAN_BLUE > 50%), interleaved with SUV colors
        color_seq = [
            "SEDAN_RED", "SUV_WHITE", "SEDAN_BLUE", "SUV_BLACK",
            "SEDAN_RED", "SUV_SILVER", "SEDAN_BLUE", "SUV_WHITE",
            "SEDAN_RED", "SUV_BLACK", "SEDAN_BLUE", "SUV_SILVER",
            "SEDAN_RED", "SEDAN_BLUE"
        ]
    else:
        # Balanced mix including pullout vehicles
        color_seq = [
            "SUV_WHITE", "SUV_BLACK", "SEDAN_RED", "SUV_WHITE",
            "PULLOUT_CAR", "SUV_SILVER", "SEDAN_BLUE", "SUV_WHITE",
            "SUV_BLACK", "SEDAN_RED", "SUV_WHITE", "SUV_BLACK",
            "SEDAN_BLUE", "PULLOUT_CAR"
        ]

    for d in range(days):
        day_rel = d * 1440
        day_due = day_base = d * 1440 + 1020
        for i in range(cars_per_day):
            idx = d * cars_per_day + i + 1
            cfg_id = color_seq[i % len(color_seq)]
            vin = f"VIN_{scale}C_D{d}_{i+1:02d}"
            # Even spacing of due dates within shift
            due = day_due - (cars_per_day - 1 - i) * 15
            prio = 1 if i in (0, 1) else 2
            bucket = f"2026-10:P1:{cfg_id}"
            orders_data.append({
                "vin": vin,
                "config_id": cfg_id,
                "rel": day_rel,
                "due": due,
                "prio": prio,
                "bucket": bucket
            })

    orders: List[Order] = []
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

    # Forecast definition ensuring total forecast == forecast_target
    # Sum of firm order counts by config
    firm_counts_by_cfg: Dict[str, int] = {}
    for od in orders_data:
        cid = od["config_id"]
        firm_counts_by_cfg[cid] = firm_counts_by_cfg.get(cid, 0) + 1

    forecast_extras: Dict[str, int] = {cid: 0 for cid in firm_counts_by_cfg}
    cfg_keys = sorted(firm_counts_by_cfg.keys())
    residual_to_distribute = forecast_target - firm_count
    round_robin_idx = 0
    while residual_to_distribute > 0:
        cfg_keys_idx = round_robin_idx % len(cfg_keys)
        forecast_extras[cfg_keys[cfg_keys_idx]] += 1
        residual_to_distribute -= 1
        round_robin_idx += 1

    forecasts: List[Forecast] = []
    fc_idx = 1
    for cid, cnt in sorted(firm_counts_by_cfg.items()):
        extra = forecast_extras[cid]
        forecasts.append(Forecast(
            forecast_id=f"FC_{scale}_{cid}_{fc_idx:02d}",
            version=1,
            month="2026-10",
            plant_id="P1",
            config_id=cid,
            quantity=cnt + extra
        ))
        fc_idx += 1

    # Inventories & Deliveries ensuring physical feasibility
    initial_inventory = {
        "STEEL_BODY": scale,
        "ALUM_BODY": scale,
        "WHITE_PAINT": scale,
        "BLACK_PAINT": scale,
        "SILVER_PAINT": scale,
        "RED_PAINT": scale,
        "BLUE_PAINT": scale,
        "BATTERY": int(scale * 0.4),  # Rest arrives via delivery
        "SEAT": int(scale * 0.4)
    }

    deliveries: List[SupplyDelivery] = []
    for d in range(days):
        deliv_time = d * 1440 + 100
        deliveries.append(SupplyDelivery(
            delivery_id=f"DELIV_BATTERY_D{d}",
            material_id="BATTERY",
            quantity=int(scale * 0.4) + 4,
            available_at_min=deliv_time,
            source_version="v1"
        ))
        deliveries.append(SupplyDelivery(
            delivery_id=f"DELIV_SEAT_D{d}",
            material_id="SEAT",
            quantity=int(scale * 0.4) + 4,
            available_at_min=deliv_time,
            source_version="v1"
        ))

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
        events=[]
    )


# =========================================================================
# Concrete Scenarios: 16 Cars (Micro Benchmark)
# =========================================================================

def create_scenario_16_normal() -> Scenario:
    """16-Vehicle Micro Benchmark Normal Operation."""
    return build_scale_scenario("SCENARIO_16_NORMAL", scale=16, bottleneck=False)


def create_scenario_16_dual_delay_labor_cut() -> Scenario:
    """16-Vehicle: Dual critical material sudden delay + temporary labor reduction.
    - BATTERY delivery delayed from t=100 to t=240 (known at t=50)
    - SEAT delivery delayed from t=100 to t=260 (known at t=60)
    - Labor pool temporarily reduced from 4 to 2 workers for 45 mins at t=140
    """
    sc = build_scale_scenario("SCENARIO_16_DUAL_DELAY_LABOR_CUT", scale=16, bottleneck=False)
    sc.events = [
        Event(
            event_id="EV_16_BATTERY_DELAY",
            occurred_at_min=50,
            known_at_min=50,
            event_type="MATERIAL_DELAY",
            payload={"delivery_id": "DELIV_BATTERY_D0", "new_available_at_min": 240}
        ),
        Event(
            event_id="EV_16_SEAT_DELAY",
            occurred_at_min=60,
            known_at_min=60,
            event_type="MATERIAL_DELAY",
            payload={"delivery_id": "DELIV_SEAT_D0", "new_available_at_min": 260}
        ),
        Event(
            event_id="EV_16_LABOR_CUT",
            occurred_at_min=140,
            known_at_min=140,
            event_type="LABOR_REDUCTION",
            payload={"new_max_workers": 2, "duration_min": 45}
        )
    ]
    return sc


def create_scenario_16_vip_rush_cancel_shock() -> Scenario:
    """16-Vehicle: High-density tight-deadline VIP batch rush insertion + order cancellation.
    - At t=100: VIP Rush Order 1 arrives (priority 1, due 380) netting against residual forecast
    - At t=120: VIP Rush Order 2 arrives (priority 1, due 420) netting against residual forecast
    - At t=100: Unstarted vehicle VIN_16C_D0_07 cancelled
    - At t=120: Unstarted vehicle VIN_16C_D0_06 cancelled
    """
    sc = build_scale_scenario("SCENARIO_16_VIP_RUSH_CANCEL_SHOCK", scale=16, bottleneck=False)
    rush1 = Order(
        order_id="VIP_RUSH_16_01",
        vin="VIP_RUSH_16_01",
        config_id="SEDAN_RED",
        release_at_min=100,
        due_at_min=380,
        priority=1,
        route=["W", "P", "A"],
        forecast_bucket="2026-10:P1:SEDAN_RED"
    )
    rush2 = Order(
        order_id="VIP_RUSH_16_02",
        vin="VIP_RUSH_16_02",
        config_id="SUV_WHITE",
        release_at_min=120,
        due_at_min=420,
        priority=1,
        route=["W", "P", "A"],
        forecast_bucket="2026-10:P1:SUV_WHITE"
    )
    sc.events = [
        Event(
            event_id="EV_16_VIP_RUSH_01",
            occurred_at_min=100,
            known_at_min=100,
            event_type="RUSH_ORDER",
            payload={"order": rush1, "consumes_synthetic_bucket": "2026-10:P1:SEDAN_RED"}
        ),
        Event(
            event_id="EV_16_CANCEL_01",
            occurred_at_min=100,
            known_at_min=100,
            event_type="ORDER_CANCEL",
            payload={"vin": "VIN_16C_D0_07"}
        ),
        Event(
            event_id="EV_16_VIP_RUSH_02",
            occurred_at_min=120,
            known_at_min=120,
            event_type="RUSH_ORDER",
            payload={"order": rush2, "consumes_synthetic_bucket": "2026-10:P1:SUV_WHITE"}
        ),
        Event(
            event_id="EV_16_CANCEL_02",
            occurred_at_min=120,
            known_at_min=120,
            event_type="ORDER_CANCEL",
            payload={"vin": "VIN_16C_D0_06"}
        )
    ]
    return sc


def create_scenario_16_bottleneck_color_alum() -> Scenario:
    """16-Vehicle: High-ratio aluminum vehicles (>50%) & coating multi-color bottleneck.
    Features 15m paint setup penalties and interleaved colors to evaluate color-batching strategies.
    """
    return build_scale_scenario("SCENARIO_16_BOTTLENECK_COLOR_ALUM", scale=16, bottleneck=True)


# =========================================================================
# Concrete Scenarios: 32 Cars (Two-Day Medium Scale)
# =========================================================================

def create_scenario_32_normal() -> Scenario:
    """32-Vehicle Two-Day Medium Scale Normal Operation."""
    return build_scale_scenario("SCENARIO_32_NORMAL", scale=32, bottleneck=False)


def create_scenario_32_dual_delay_labor_cut() -> Scenario:
    """32-Vehicle: Dual material delay + multi-station labor reduction on Day 0."""
    sc = build_scale_scenario("SCENARIO_32_DUAL_DELAY_LABOR_CUT", scale=32, bottleneck=False)
    sc.events = [
        Event(
            event_id="EV_32_BATTERY_DELAY_D0",
            occurred_at_min=50,
            known_at_min=50,
            event_type="MATERIAL_DELAY",
            payload={"delivery_id": "DELIV_BATTERY_D0", "new_available_at_min": 240}
        ),
        Event(
            event_id="EV_32_SEAT_DELAY_D0",
            occurred_at_min=60,
            known_at_min=60,
            event_type="MATERIAL_DELAY",
            payload={"delivery_id": "DELIV_SEAT_D0", "new_available_at_min": 260}
        ),
        Event(
            event_id="EV_32_LABOR_CUT_D0",
            occurred_at_min=150,
            known_at_min=150,
            event_type="LABOR_REDUCTION",
            payload={"new_max_workers": 2, "duration_min": 50}
        )
    ]
    return sc


def create_scenario_32_vip_rush_cancel_shock() -> Scenario:
    """32-Vehicle: High-density tight-deadline VIP batch rush insertion + order cancellation."""
    sc = build_scale_scenario("SCENARIO_32_VIP_RUSH_CANCEL_SHOCK", scale=32, bottleneck=False)
    rush1 = Order(
        order_id="VIP_RUSH_32_01",
        vin="VIP_RUSH_32_01",
        config_id="SEDAN_RED",
        release_at_min=100,
        due_at_min=400,
        priority=1,
        route=["W", "P", "A"],
        forecast_bucket="2026-10:P1:SEDAN_RED"
    )
    rush2 = Order(
        order_id="VIP_RUSH_32_02",
        vin="VIP_RUSH_32_02",
        config_id="SUV_WHITE",
        release_at_min=120,
        due_at_min=450,
        priority=1,
        route=["W", "P", "A"],
        forecast_bucket="2026-10:P1:SUV_WHITE"
    )
    sc.events = [
        Event(
            event_id="EV_32_VIP_RUSH_01",
            occurred_at_min=100,
            known_at_min=100,
            event_type="RUSH_ORDER",
            payload={"order": rush1, "consumes_synthetic_bucket": "2026-10:P1:SEDAN_RED"}
        ),
        Event(
            event_id="EV_32_CANCEL_01",
            occurred_at_min=100,
            known_at_min=100,
            event_type="ORDER_CANCEL",
            payload={"vin": "VIN_32C_D0_13"}
        ),
        Event(
            event_id="EV_32_VIP_RUSH_02",
            occurred_at_min=120,
            known_at_min=120,
            event_type="RUSH_ORDER",
            payload={"order": rush2, "consumes_synthetic_bucket": "2026-10:P1:SUV_WHITE"}
        ),
        Event(
            event_id="EV_32_CANCEL_02",
            occurred_at_min=120,
            known_at_min=120,
            event_type="ORDER_CANCEL",
            payload={"vin": "VIN_32C_D0_14"}
        )
    ]
    return sc


def create_scenario_32_bottleneck_color_alum() -> Scenario:
    """32-Vehicle: High-ratio aluminum vehicles (>50%) & coating multi-color bottleneck."""
    return build_scale_scenario("SCENARIO_32_BOTTLENECK_COLOR_ALUM", scale=32, bottleneck=True)


# =========================================================================
# Concrete Scenarios: 48 Cars (Three-Day Extended Scale)
# =========================================================================

def create_scenario_48_normal() -> Scenario:
    """48-Vehicle Three-Day Extended Scale Normal Operation."""
    return build_scale_scenario("SCENARIO_48_NORMAL", scale=48, bottleneck=False)


def create_scenario_48_dual_delay_labor_cut() -> Scenario:
    """48-Vehicle: Dual material delay + multi-station labor reduction across Day 0 and Day 1."""
    sc = build_scale_scenario("SCENARIO_48_DUAL_DELAY_LABOR_CUT", scale=48, bottleneck=False)
    sc.events = [
        Event(
            event_id="EV_48_BATTERY_DELAY_D0",
            occurred_at_min=50,
            known_at_min=50,
            event_type="MATERIAL_DELAY",
            payload={"delivery_id": "DELIV_BATTERY_D0", "new_available_at_min": 240}
        ),
        Event(
            event_id="EV_48_SEAT_DELAY_D0",
            occurred_at_min=60,
            known_at_min=60,
            event_type="MATERIAL_DELAY",
            payload={"delivery_id": "DELIV_SEAT_D0", "new_available_at_min": 260}
        ),
        Event(
            event_id="EV_48_LABOR_CUT_D0",
            occurred_at_min=150,
            known_at_min=150,
            event_type="LABOR_REDUCTION",
            payload={"new_max_workers": 2, "duration_min": 60}
        ),
        Event(
            event_id="EV_48_BATTERY_DELAY_D1",
            occurred_at_min=1490,
            known_at_min=1490,
            event_type="MATERIAL_DELAY",
            payload={"delivery_id": "DELIV_BATTERY_D1", "new_available_at_min": 1680}
        )
    ]
    return sc


def create_scenario_48_vip_rush_cancel_shock() -> Scenario:
    """48-Vehicle: High-density tight-deadline VIP batch rush insertion + order cancellation."""
    sc = build_scale_scenario("SCENARIO_48_VIP_RUSH_CANCEL_SHOCK", scale=48, bottleneck=False)
    rush1 = Order(
        order_id="VIP_RUSH_48_01",
        vin="VIP_RUSH_48_01",
        config_id="SEDAN_RED",
        release_at_min=100,
        due_at_min=420,
        priority=1,
        route=["W", "P", "A"],
        forecast_bucket="2026-10:P1:SEDAN_RED"
    )
    rush2 = Order(
        order_id="VIP_RUSH_48_02",
        vin="VIP_RUSH_48_02",
        config_id="SUV_WHITE",
        release_at_min=120,
        due_at_min=460,
        priority=1,
        route=["W", "P", "A"],
        forecast_bucket="2026-10:P1:SUV_WHITE"
    )
    sc.events = [
        Event(
            event_id="EV_48_VIP_RUSH_01",
            occurred_at_min=100,
            known_at_min=100,
            event_type="RUSH_ORDER",
            payload={"order": rush1, "consumes_synthetic_bucket": "2026-10:P1:SEDAN_RED"}
        ),
        Event(
            event_id="EV_48_CANCEL_01",
            occurred_at_min=100,
            known_at_min=100,
            event_type="ORDER_CANCEL",
            payload={"vin": "VIN_48C_D0_13"}
        ),
        Event(
            event_id="EV_48_VIP_RUSH_02",
            occurred_at_min=120,
            known_at_min=120,
            event_type="RUSH_ORDER",
            payload={"order": rush2, "consumes_synthetic_bucket": "2026-10:P1:SUV_WHITE"}
        ),
        Event(
            event_id="EV_48_CANCEL_02",
            occurred_at_min=120,
            known_at_min=120,
            event_type="ORDER_CANCEL",
            payload={"vin": "VIN_48C_D0_14"}
        )
    ]
    return sc


def create_scenario_48_bottleneck_color_alum() -> Scenario:
    """48-Vehicle: High-ratio aluminum vehicles (>50%) & coating multi-color bottleneck."""
    return build_scale_scenario("SCENARIO_48_BOTTLENECK_COLOR_ALUM", scale=48, bottleneck=True)


# =========================================================================
# Registry Mapping & Retrieval API
# =========================================================================

ADVANCED_SCENARIOS_MAP = {
    # 16-vehicle scale
    "SCENARIO_16_NORMAL": create_scenario_16_normal,
    "SCENARIO_16_DUAL_DELAY_LABOR_CUT": create_scenario_16_dual_delay_labor_cut,
    "SCENARIO_16_VIP_RUSH_CANCEL_SHOCK": create_scenario_16_vip_rush_cancel_shock,
    "SCENARIO_16_BOTTLENECK_COLOR_ALUM": create_scenario_16_bottleneck_color_alum,
    # 32-vehicle scale
    "SCENARIO_32_NORMAL": create_scenario_32_normal,
    "SCENARIO_32_DUAL_DELAY_LABOR_CUT": create_scenario_32_dual_delay_labor_cut,
    "SCENARIO_32_VIP_RUSH_CANCEL_SHOCK": create_scenario_32_vip_rush_cancel_shock,
    "SCENARIO_32_BOTTLENECK_COLOR_ALUM": create_scenario_32_bottleneck_color_alum,
    # 48-vehicle scale
    "SCENARIO_48_NORMAL": create_scenario_48_normal,
    "SCENARIO_48_DUAL_DELAY_LABOR_CUT": create_scenario_48_dual_delay_labor_cut,
    "SCENARIO_48_VIP_RUSH_CANCEL_SHOCK": create_scenario_48_vip_rush_cancel_shock,
    "SCENARIO_48_BOTTLENECK_COLOR_ALUM": create_scenario_48_bottleneck_color_alum,
}


def get_advanced_scenario(scenario_id: str) -> Scenario:
    """Retrieves an advanced scenario instance by ID."""
    builder = ADVANCED_SCENARIOS_MAP.get(scenario_id)
    if builder is None:
        raise ValueError(
            f"Unknown advanced scenario ID '{scenario_id}'. Available: {list_all_advanced_scenarios()}"
        )
    return builder()


def list_all_advanced_scenarios() -> List[str]:
    """Lists all available advanced scenario IDs."""
    return list(ADVANCED_SCENARIOS_MAP.keys())


def list_scenarios_by_scale(scale: int) -> List[str]:
    """Filters scenario IDs by vehicle scale (16, 32, 48)."""
    prefix = f"SCENARIO_{scale}_"
    return [sid for sid in ADVANCED_SCENARIOS_MAP if sid.startswith(prefix)]


def scenario_to_dict(sc: Scenario) -> Dict[str, Any]:
    """Converts a Scenario dataclass instance into a dictionary suitable for normalize_scenario."""
    from dataclasses import asdict
    d = asdict(sc)
    d["configurations"] = list(d["configurations"].values())
    d["resources"] = list(d["resources"].values())
    d["buffers"] = list(d["buffers"].values())
    return d

