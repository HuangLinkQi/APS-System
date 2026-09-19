"""schema.py - APS Fullchain Core Data Models and Schema aps_fullchain_v1.

Standard library only. Defines immutable and mutable data structures for:
- Calendar & Shifts
- Products & Configurations (W/P/A, BOM, setup matrix, pull-out routes)
- Resources & Buffers
- Labor & Supply
- Forecasts & Orders
- Events, Snapshots, and Scenarios
- Trace & Validation Reports
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional, Tuple, Any, Set
import hashlib
import json
import copy


class VehicleStatus(str, Enum):
    UNRELEASED = "UNRELEASED"
    READY = "READY"
    SETUP = "SETUP"
    PROCESSING = "PROCESSING"
    BLOCKED = "BLOCKED"
    IN_TRANSIT = "IN_TRANSIT"
    BUFFERED = "BUFFERED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class PlanHorizonType(str, Enum):
    N_PLUS_6 = "N+6"
    N_PLUS_3 = "N+3"
    MONTHLY = "MONTHLY"
    WEEKLY_DAILY = "WEEKLY_DAILY"


class MachineStatus(str, Enum):
    OFF = "OFF"
    IDLE = "IDLE"
    SETUP = "SETUP"
    PROCESSING = "PROCESSING"
    BLOCKED = "BLOCKED"
    DOWN = "DOWN"


@dataclass
class CalendarShift:
    shift_id: str
    day_idx: int
    start_min: int  # Absolute minute from sim origin
    end_min: int    # Absolute minute from sim origin

    @property
    def duration_min(self) -> int:
        return self.end_min - self.start_min


@dataclass
class Calendar:
    name: str
    shifts: List[CalendarShift]
    horizon_end_min: int
    day_length_min: int = 1440  # 24 hours

    def is_work_time(self, t: int) -> bool:
        if t >= self.horizon_end_min:
            return False
        for s in self.shifts:
            if s.start_min <= t < s.end_min:
                return True
        return False

    def get_current_shift(self, t: int) -> Optional[CalendarShift]:
        for s in self.shifts:
            if s.start_min <= t < s.end_min:
                return s
        return None

    def get_current_or_next_shift(self, t: int) -> Optional[CalendarShift]:
        for s in self.shifts:
            if s.end_min > t:
                return s
        return None

    def get_next_work_minute(self, t: int) -> int:
        for s in self.shifts:
            if s.start_min <= t < s.end_min:
                return t
            if s.start_min > t:
                return s.start_min
        return self.horizon_end_min

    def can_fit_job(self, start_t: int, duration_min: int) -> bool:
        """Jobs (setup+processing) cannot cross shift boundaries by default."""
        for s in self.shifts:
            if s.start_min <= start_t < s.end_min:
                return (start_t + duration_min) <= s.end_min
        return False

    def find_earliest_fit_minute(self, min_t: int, duration_min: int) -> int:
        """Finds earliest minute >= min_t where duration_min can execute continuously inside a single shift."""
        curr = max(0, min_t)
        for s in self.shifts:
            if s.end_min <= curr:
                continue
            earliest_start = max(curr, s.start_min)
            if earliest_start + duration_min <= s.end_min:
                return earliest_start
            # If it cannot fit in shift s, move to next shift
            curr = s.end_min
        return self.horizon_end_min

    def minutes_to_day(self, t: int) -> int:
        return max(0, t // self.day_length_min)

    def day_to_start_min(self, day: int) -> int:
        return day * self.day_length_min

    def day_to_end_min(self, day: int) -> int:
        return (day + 1) * self.day_length_min


@dataclass
class Configuration:
    config_id: str
    model_type: str                  # e.g., "SUV", "SEDAN"
    color: str                       # e.g., "WHITE", "BLACK", "RED"
    material_type: str               # e.g., "STEEL", "ALUMINUM"
    is_pullout: bool = False         # Pullout vehicle: terminates after Paint (P), no A or P-A buffer
    stages: List[str] = field(default_factory=lambda: ["W", "P", "A"])
    process_times: Dict[str, int] = field(default_factory=dict)  # stage -> minutes
    process_boms: Dict[str, Dict[str, int]] = field(default_factory=dict)  # stage -> {material_id: qty}
    setup_matrix: Dict[str, Dict[str, int]] = field(default_factory=dict)  # stage -> {"prev_val->curr_val": minutes}
    labor_requirements: Dict[str, Dict[str, int]] = field(default_factory=dict)  # stage -> {"setup": 1, "processing": 1}
    skill_requirements: Dict[str, Dict[str, str]] = field(default_factory=dict)  # stage -> {"setup": "PAINTING", "processing": "PAINTING"}

    def get_setup_time(self, stage: str, prev_val: Optional[str], curr_val: str) -> int:
        if prev_val is None or prev_val == curr_val:
            return 0
        mat = self.setup_matrix.get(stage, {})
        key = f"{prev_val}->{curr_val}"
        return mat.get(key, 0)

    def get_labor_req(self, stage: str, phase: str) -> int:
        """phase: 'setup' or 'processing'"""
        reqs = self.labor_requirements.get(stage, {})
        return reqs.get(phase, 1)

    def get_skill_req(self, stage: str, phase: str) -> str:
        """phase: 'setup' or 'processing'"""
        reqs = self.skill_requirements.get(stage, {})
        return reqs.get(phase, "GENERAL")


@dataclass
class Resource:
    resource_id: str
    stage: str
    capacity: int = 1
    state: MachineStatus = MachineStatus.IDLE
    current_vin: Optional[str] = None
    last_color: Optional[str] = None
    last_model_type: Optional[str] = None
    blocked_until_freed: bool = False


@dataclass
class Buffer:
    buffer_id: str
    edge: str                       # "W->P" or "P->A"
    capacity: int                   # strict capacity
    transit_time_min: int = 0
    discipline: str = "FIFO"        # "FIFO", "PRIORITY"
    items: List[str] = field(default_factory=list)  # VINs currently occupying buffer
    in_transit: List[Tuple[str, int]] = field(default_factory=list)  # (VIN, arrival_time)

    @property
    def current_occupancy(self) -> int:
        return len(self.items) + len(self.in_transit)

    def has_space(self) -> bool:
        return self.current_occupancy < self.capacity


@dataclass
class LaborSkillPool:
    pool_id: str
    skill_name: str                  # e.g., "WELDING", "PAINTING", "ASSEMBLY", "GENERAL"
    max_workers: int
    capacity_by_shift: Dict[str, int] = field(default_factory=dict)
    time_windows: List[Tuple[int, int, int]] = field(default_factory=list)  # (start_min, end_min, workers)


@dataclass
class Labor:
    pool_id: str
    max_workers: int
    current_allocated: int = 0
    skill_pools: Dict[str, LaborSkillPool] = field(default_factory=dict)
    time_windows: List[Tuple[int, int, int]] = field(default_factory=list)  # (start_min, end_min, max_workers) overrides

    def get_skill_capacity(self, skill_name: Optional[str] = None, t_min: int = 0, shift_id: Optional[str] = None) -> int:
        if not self.skill_pools:
            return self.get_total_capacity(t_min)
        if skill_name and skill_name in self.skill_pools:
            pool = self.skill_pools[skill_name]
            for st, ed, cap in pool.time_windows:
                if st <= t_min < ed:
                    return cap
            if shift_id and shift_id in pool.capacity_by_shift:
                return pool.capacity_by_shift[shift_id]
            return pool.max_workers
        # Fail-closed: when skill pools are configured, unknown skill is rejected (capacity 0)
        return 0

    def get_total_capacity(self, t_min: int = 0) -> int:
        for st, ed, cap in self.time_windows:
            if st <= t_min < ed:
                return cap
        return self.max_workers


@dataclass
class SupplyDelivery:
    delivery_id: str
    material_id: str
    quantity: int
    available_at_min: int
    source_version: str = "v1"
    known_at_min: int = 0


@dataclass
class Order:
    order_id: str
    vin: str
    config_id: str
    release_at_min: int
    due_at_min: int
    priority: int = 1
    route: List[str] = field(default_factory=lambda: ["W", "P", "A"])
    forecast_bucket: str = ""       # e.g., "2026-10:PLANT1:SUV_WHITE"
    status: VehicleStatus = VehicleStatus.UNRELEASED
    is_virtual: bool = False        # True for forecast netting synthetic vehicle
    assigned_day: Optional[int] = None
    completed_at_min: Optional[int] = None
    exception_tag: Optional[str] = None

    def clone(self) -> Order:
        return copy.deepcopy(self)


@dataclass
class Forecast:
    forecast_id: str
    version: int
    month: str                      # "2026-10"
    plant_id: str
    config_id: str
    quantity: int
    published_at_min: int = 0


@dataclass
class Event:
    event_id: str
    occurred_at_min: int
    known_at_min: int
    event_type: str                 # "MATERIAL_DELAY", "DEMAND_CHANGE", "ORDER_CANCEL", "LINE_STOP"
    payload: Dict[str, Any] = field(default_factory=dict)
    revision: int = 1


@dataclass
class EventRecord:
    vin: str
    order_id: str
    stage: str
    node: str                       # "B", "S", "C", "F"
    timestamp_min: int
    resource_id: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "vin": self.vin,
            "order_id": self.order_id,
            "stage": self.stage,
            "node": self.node,
            "timestamp_min": self.timestamp_min,
            "resource_id": self.resource_id,
            "details": self.details,
        }


@dataclass
class SimulationTrace:
    records: List[EventRecord] = field(default_factory=list)
    stage_node_times: Dict[str, Dict[str, Dict[str, int]]] = field(default_factory=dict)
    # vin -> stage -> {"B": t, "S": t, "C": t, "F": t}
    blocked_durations: List[Dict[str, Any]] = field(default_factory=list)
    # [{"vin": v, "stage": s, "C": c, "F": f, "blocked_duration": f - c}]
    pullout_completions: List[str] = field(default_factory=list)
    inventory_snapshots: List[Dict[str, Any]] = field(default_factory=list)
    labor_snapshots: List[Dict[str, Any]] = field(default_factory=list)
    buffer_snapshots: List[Dict[str, Any]] = field(default_factory=list)
    unmet_vins: List[str] = field(default_factory=list)

    def record_node(self, record: EventRecord) -> None:
        self.records.append(record)
        if record.vin not in self.stage_node_times:
            self.stage_node_times[record.vin] = {}
        if record.stage not in self.stage_node_times[record.vin]:
            self.stage_node_times[record.vin][record.stage] = {}
        self.stage_node_times[record.vin][record.stage][record.node] = record.timestamp_min

    def get_node_time(self, vin: str, stage: str, node: str) -> Optional[int]:
        return self.stage_node_times.get(vin, {}).get(stage, {}).get(node)


@dataclass
class DemandLedger:
    firm_orders: List[Order]
    consumed_forecast_qty: Dict[str, int]   # bucket -> consumed
    residual_forecast_qty: Dict[str, int]   # bucket -> residual
    synthetic_orders: List[Order]           # Virtual orders representing remaining forecast
    original_forecast_total: int
    netted_forecast_total: int
    remaining_forecast_total: int
    firm_orders_total: int
    net_demand_total: int
    balance_verified: bool
    audit_notes: List[str] = field(default_factory=list)


@dataclass
class AggregatePlan:
    plan_id: str
    horizon_months: List[str]
    monthly_allocations: Dict[str, Dict[str, int]]  # month -> {config_id: qty}
    unmet_demand: Dict[str, Dict[str, int]]          # month -> {config_id: qty}
    capacity_usage: Dict[str, Dict[str, int]]        # month -> {stage: minutes_used}
    capacity_limit: Dict[str, Dict[str, int]]        # month -> {stage: minutes_available}
    material_shortages: Dict[str, Dict[str, int]]    # month -> {material_id: shortage}
    allocated_order_ids: Dict[str, List[str]] = field(default_factory=dict)  # month -> [order_id]
    unmet_order_ids: Dict[str, List[str]] = field(default_factory=dict)      # month -> [order_id]
    plan_type: str = "N+6"                          # "N+6", "N+3", "MONTHLY"
    version: int = 1
    parent_plan_id: Optional[str] = None
    locked_order_ids: Dict[str, List[str]] = field(default_factory=dict)     # month -> [order_id]
    completed_order_ids: Dict[str, List[str]] = field(default_factory=dict)  # month -> [order_id]
    cross_month_attributions: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # vin -> {"original_month": m0, "assigned_month": m1, "delay_months": d}
    is_feasible: bool = True
    input_hash: str = ""

    def verify_overlap_consistency(self, other: AggregatePlan) -> Tuple[bool, List[str]]:
        """Verifies that common overlapping months between self and other have consistent allocations."""
        overlap_months = [m for m in self.horizon_months if m in other.horizon_months]
        issues = []
        for m in overlap_months:
            self_vins = set(self.allocated_order_ids.get(m, []))
            other_vins = set(other.allocated_order_ids.get(m, []))
            if self_vins and other_vins:
                diff_self = self_vins - other_vins
                diff_other = other_vins - self_vins
                if diff_self or diff_other:
                    issues.append(
                        f"Month {m} VIN allocation mismatch between {self.plan_type} (v{self.version}) and "
                        f"{other.plan_type} (v{other.version}): extra_in_self={sorted(diff_self)}, extra_in_other={sorted(diff_other)}"
                    )
            self_alloc = self.monthly_allocations.get(m, {})
            other_alloc = other.monthly_allocations.get(m, {})
            all_cfgs = set(self_alloc.keys()) | set(other_alloc.keys())
            for c in all_cfgs:
                q1 = self_alloc.get(c, 0)
                q2 = other_alloc.get(c, 0)
                if q1 != q2:
                    issues.append(
                        f"Month {m} config {c} quota mismatch: {self.plan_type}={q1} != {other.plan_type}={q2}"
                    )
        return len(issues) == 0, issues


@dataclass
class OrderPlan:
    plan_id: str
    version: int
    policy_name: str
    daily_allocations: Dict[int, List[str]]          # day_idx -> [order_id]
    stage_dispatch_orders: Dict[str, List[str]]      # stage -> [order_id] dispatch sequence preference
    input_hash: str = ""


@dataclass
class ValidationReport:
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    checks_performed: List[str] = field(default_factory=list)


@dataclass
class Scenario:
    scenario_id: str
    calendar: Calendar
    configurations: Dict[str, Configuration]
    resources: Dict[str, Resource]
    buffers: Dict[str, Buffer]
    labor: Labor
    initial_inventory: Dict[str, int]
    deliveries: List[SupplyDelivery]
    orders: List[Order]
    forecasts: List[Forecast]
    events: List[Event] = field(default_factory=list)

    def compute_hash(self) -> str:
        s = f"{self.scenario_id}|{len(self.orders)}|{len(self.forecasts)}|{len(self.deliveries)}"
        return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


@dataclass
class PlanningSnapshot:
    """Strictly isolated snapshot at time `now_min`.
    Only contains events, inventory, and machine states KNOWN at or before now_min.
    """
    snapshot_id: str
    now_min: int
    calendar: Calendar
    configurations: Dict[str, Configuration]
    resources: Dict[str, Resource]
    buffers: Dict[str, Buffer]
    labor: Labor
    current_inventory: Dict[str, int]
    known_deliveries: List[SupplyDelivery]
    orders: List[Order]
    forecasts: List[Forecast]
    known_events: List[Event]
    wip_vin_states: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    received_deliveries: List[SupplyDelivery] = field(default_factory=list)
    vehicle_completed_stages: Dict[str, Set[str]] = field(default_factory=dict)
    prefix_records: List[EventRecord] = field(default_factory=list)
    committed_plan_id: Optional[str] = None
    input_hash: str = ""


def compute_object_hash(data: Any) -> str:
    """Deterministic hash of arbitrary JSON-serializable structures."""
    serialized = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def normalize_scenario(data: Dict[str, Any]) -> Scenario:
    """Validates raw dictionary input, rejecting invalid IDs, missing BOM items, or negative stock."""
    # 1. Calendar
    cal_raw = data.get("calendar", {})
    shifts = []
    for s in cal_raw.get("shifts", []):
        shifts.append(CalendarShift(
            shift_id=str(s["shift_id"]),
            day_idx=int(s["day_idx"]),
            start_min=int(s["start_min"]),
            end_min=int(s["end_min"])
        ))
    calendar = Calendar(
        name=str(cal_raw.get("name", "DefaultCalendar")),
        shifts=shifts,
        horizon_end_min=int(cal_raw.get("horizon_end_min", 1440 * 7)),
        day_length_min=int(cal_raw.get("day_length_min", 1440))
    )

    # 2. Configurations
    configs: Dict[str, Configuration] = {}
    all_materials_used: Set[str] = set()
    for cfg in data.get("configurations", []):
        cid = str(cfg["config_id"])
        stages = list(cfg.get("stages", ["W", "P", "A"]))
        ptimes = {k: int(v) for k, v in cfg.get("process_times", {}).items()}
        pboms = {}
        for stg, boms in cfg.get("process_boms", {}).items():
            pboms[stg] = {k: int(v) for k, v in boms.items()}
            for m in pboms[stg]:
                all_materials_used.add(m)
        smat = cfg.get("setup_matrix", {})
        lreq = cfg.get("labor_requirements", {})
        configs[cid] = Configuration(
            config_id=cid,
            model_type=str(cfg.get("model_type", "SUV")),
            color=str(cfg.get("color", "WHITE")),
            material_type=str(cfg.get("material_type", "STEEL")),
            is_pullout=bool(cfg.get("is_pullout", False)),
            stages=stages,
            process_times=ptimes,
            process_boms=pboms,
            setup_matrix=smat,
            labor_requirements=lreq
        )

    # 3. Resources
    resources: Dict[str, Resource] = {}
    for r in data.get("resources", []):
        rid = str(r["resource_id"])
        resources[rid] = Resource(
            resource_id=rid,
            stage=str(r["stage"]),
            capacity=int(r.get("capacity", 1)),
            state=MachineStatus(r.get("state", "IDLE"))
        )

    # 4. Buffers
    buffers: Dict[str, Buffer] = {}
    for b in data.get("buffers", []):
        bid = str(b["buffer_id"])
        cap = int(b["capacity"])
        if cap <= 0:
            raise ValueError(f"Buffer {bid} capacity must be > 0, got {cap}")
        buffers[bid] = Buffer(
            buffer_id=bid,
            edge=str(b["edge"]),
            capacity=cap,
            transit_time_min=int(b.get("transit_time_min", 0)),
            discipline=str(b.get("discipline", "FIFO"))
        )

    # 5. Labor
    l_raw = data.get("labor", {})
    labor = Labor(
        pool_id=str(l_raw.get("pool_id", "MAIN_LABOR")),
        max_workers=int(l_raw.get("max_workers", 2))
    )
    if labor.max_workers <= 0:
        raise ValueError("Labor max_workers must be positive")
    if "skill_pools" in l_raw:
        for sk_name, sk_data in l_raw["skill_pools"].items():
            labor.skill_pools[sk_name] = LaborSkillPool(
                pool_id=str(sk_data.get("pool_id", sk_name)),
                skill_name=sk_name,
                max_workers=int(sk_data.get("max_workers", labor.max_workers)),
                capacity_by_shift={k: int(v) for k, v in sk_data.get("capacity_by_shift", {}).items()},
                time_windows=[(int(a), int(b), int(c)) for a, b, c in sk_data.get("time_windows", [])]
            )
    # Fail-closed validation: reject unknown skills in configurations
    if labor.skill_pools:
        for cfg in configs.values():
            for stg in cfg.stages:
                for phase in ("setup", "processing"):
                    req_sk = cfg.get_skill_req(stg, phase)
                    if req_sk and req_sk not in labor.skill_pools:
                        raise ValueError(
                            f"Configuration '{cfg.config_id}' requires unknown skill '{req_sk}' for stage {stg} {phase}, "
                            f"available skill pools: {list(labor.skill_pools.keys())}"
                        )

    # 6. Inventory & Deliveries
    init_inv = {k: int(v) for k, v in data.get("initial_inventory", {}).items()}
    for mat, qty in init_inv.items():
        if qty < 0:
            raise ValueError(f"Initial inventory for {mat} cannot be negative: {qty}")
    # Verify all BOM materials are declared
    for m in all_materials_used:
        if m not in init_inv:
            init_inv[m] = 0  # Initialize with 0 if not present

    deliveries = []
    for d in data.get("deliveries", []):
        qty = int(d["quantity"])
        if qty <= 0:
            raise ValueError(f"Delivery {d.get('delivery_id')} quantity must be > 0")
        deliveries.append(SupplyDelivery(
            delivery_id=str(d["delivery_id"]),
            material_id=str(d["material_id"]),
            quantity=qty,
            available_at_min=int(d["available_at_min"]),
            source_version=str(d.get("source_version", "v1"))
        ))

    # 7. Orders
    orders = []
    seen_vins: Set[str] = set()
    for o in data.get("orders", []):
        vin = str(o["vin"])
        if vin in seen_vins:
            raise ValueError(f"Duplicate VIN detected in scenario: {vin}")
        seen_vins.add(vin)
        cfg_id = str(o["config_id"])
        if cfg_id not in configs:
            raise ValueError(f"Order {vin} references unknown configuration {cfg_id}")
        # Inherit route from config if not provided
        route = list(o.get("route", configs[cfg_id].stages))
        orders.append(Order(
            order_id=str(o.get("order_id", vin)),
            vin=vin,
            config_id=cfg_id,
            release_at_min=int(o.get("release_at_min", 0)),
            due_at_min=int(o.get("due_at_min", calendar.horizon_end_min)),
            priority=int(o.get("priority", 1)),
            route=route,
            forecast_bucket=str(o.get("forecast_bucket", "")),
            status=VehicleStatus(o.get("status", "UNRELEASED")),
            is_virtual=bool(o.get("is_virtual", False)),
            assigned_day=o.get("assigned_day")
        ))

    # 8. Forecasts
    forecasts = []
    for f in data.get("forecasts", []):
        fc_id = str(f["forecast_id"])
        cfg_id = str(f["config_id"])
        if cfg_id not in configs:
            raise ValueError(f"Forecast {fc_id} references unknown configuration {cfg_id}")
        qty = int(f["quantity"])
        if qty < 0:
            raise ValueError(f"Forecast {fc_id} quantity cannot be negative")
        forecasts.append(Forecast(
            forecast_id=fc_id,
            version=int(f.get("version", 1)),
            month=str(f["month"]),
            plant_id=str(f.get("plant_id", "P1")),
            config_id=cfg_id,
            quantity=qty,
            published_at_min=int(f.get("published_at_min", 0))
        ))

    # 9. Events
    events = []
    for ev in data.get("events", []):
        events.append(Event(
            event_id=str(ev["event_id"]),
            occurred_at_min=int(ev["occurred_at_min"]),
            known_at_min=int(ev["known_at_min"]),
            event_type=str(ev["event_type"]),
            payload=dict(ev.get("payload", {})),
            revision=int(ev.get("revision", 1))
        ))

    return Scenario(
        scenario_id=str(data.get("scenario_id", "SCENARIO_01")),
        calendar=calendar,
        configurations=configs,
        resources=resources,
        buffers=buffers,
        labor=labor,
        initial_inventory=init_inv,
        deliveries=deliveries,
        orders=orders,
        forecasts=forecasts,
        events=events
    )
