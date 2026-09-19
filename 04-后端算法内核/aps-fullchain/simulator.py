"""simulator.py - Discrete Event Simulation Engine (DES) with B/S/C/F Nodes and Positive Blocking.

Implements Section 5 of APS Fullchain Specification and Coordinator Audit Invariants:
- Four explicit nodes per stage:
  * B: Setup start (calendar shift feasibility check, interval labor reservation & material reservation)
  * S: Processing start (material deducted from inventory, reserved released, S starts strictly on time)
  * C: Processing complete (processing labor released, positive blocking evaluated)
  * F: Resource freed (vehicle departs to downstream buffer or exits, machine released)
- Exact interval-based labor reservation:
  At Node B, pre-checks and reserves [B, B+setup) with l_setup and [B+setup, C) with l_proc across
  all active committed intervals, guaranteeing S == B + setup_time without artificial capacity depression.
- Two-phase simultaneous event processing at timestamp t:
  Phase 1: Batch all releases (processing completions, setup completions, supply arrivals, transit arrivals)
  Phase 2: Atomic acquisitions (start processing transitions, unblock F>C, dispatch new B)
- Positive Blocking (F > C) when downstream finite buffer is full; cascading unblock.
- Pull-out vehicle logic: terminates after Paint (P), bypasses P-A buffer, does not enter Assembly.
- Continuation and full state restoration from PlanningSnapshot at time tau.
- Full audit recording of event records, inventory trajectory, labor trajectory, and buffer trajectory.
- Strict deep-copying of all scenario inputs in ExecutionWorld.__init__ to preserve immutable initial state.
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple, Any, Set
import heapq
import copy
from .schema import (
    Calendar,
    Configuration,
    Resource,
    Buffer,
    Labor,
    SupplyDelivery,
    Order,
    EventRecord,
    SimulationTrace,
    OrderPlan,
    VehicleStatus,
    MachineStatus,
    PlanningSnapshot
)
from .policies import PolicyBundle, get_strategy_a


class ExecutionWorld:
    """Stateful execution environment holding physical shop floor state."""

    def __init__(
        self,
        calendar: Calendar,
        configurations: Dict[str, Configuration],
        resources: Dict[str, Resource],
        buffers: Dict[str, Buffer],
        labor: Labor,
        initial_inventory: Dict[str, int],
        deliveries: List[SupplyDelivery],
        orders: Dict[str, Order],
        stage_policies: PolicyBundle,
        horizon_end_min: Optional[int] = None,
        snapshot: Optional[PlanningSnapshot] = None,
        external_events: Optional[List[Any]] = None,
        authorized_vins: Optional[Set[str]] = None,
        order_plan: Optional[OrderPlan] = None
    ):
        # Deep copy all external scenario objects to strictly protect immutable initial state
        self.calendar = copy.deepcopy(calendar)
        self.configurations = copy.deepcopy(configurations)
        self.resources = copy.deepcopy(resources)
        self.buffers = copy.deepcopy(buffers)
        self.labor = copy.deepcopy(labor)
        self.inventory = copy.deepcopy(initial_inventory)
        self.reserved_inventory: Dict[str, int] = {m: 0 for m in initial_inventory}
        self.deliveries = copy.deepcopy(deliveries)
        self.orders = {k: v.clone() for k, v in orders.items()}
        self.stage_policies = stage_policies
        self.horizon_end_min = horizon_end_min or calendar.horizon_end_min
        self.authorized_vins: Optional[Set[str]] = set(authorized_vins) if authorized_vins is not None else None

        if order_plan is not None:
            self.authorized_vins = set()
            for day, vins in order_plan.daily_allocations.items():
                for v in vins:
                    self.authorized_vins.add(v)
                    if v in self.orders:
                        self.orders[v].assigned_day = day

        self.trace = SimulationTrace()
        self.completed_vins: Set[str] = set()
        self.event_queue: List[Tuple[int, int, int, str, Dict[str, Any]]] = []
        self._event_seq: int = 0

        # Fine-grained interval labor ledger: (start_min, end_min, worker_count, skill_name, key)
        self.committed_labor_intervals: List[Tuple[Any, ...]] = []

        if snapshot is not None and snapshot.now_min > 0:
            # Full physical snapshot restoration
            self.current_time_min = snapshot.now_min
            self.received_delivery_ids: Set[str] = {d.delivery_id for d in snapshot.received_deliveries}
            self.active_jobs = copy.deepcopy(snapshot.wip_vin_states)
            self.vehicle_completed_stages = copy.deepcopy(snapshot.vehicle_completed_stages)
            for v in self.orders:
                if v not in self.vehicle_completed_stages:
                    self.vehicle_completed_stages[v] = set()

            for r in snapshot.prefix_records:
                self.trace.record_node(copy.deepcopy(r))

            for v, o in self.orders.items():
                if o.status == VehicleStatus.COMPLETED:
                    self.completed_vins.add(v)

            # Reconstruct labor intervals and re-schedule pending in-flight events
            for res_id, job in self.active_jobs.items():
                vin = job["vin"]
                stage = job["stage"]
                cfg = self.configurations.get(self.orders[vin].config_id) if vin in self.orders else None
                node_type = job.get("node")
                comp_t = job.get("complete_t")

                if cfg and comp_t is not None and comp_t > snapshot.now_min:
                    if node_type == "SETUP":
                        l_setup = cfg.get_labor_req(stage, "setup")
                        l_proc = cfg.get_labor_req(stage, "processing")
                        sk_setup = cfg.get_skill_req(stage, "setup")
                        sk_proc = cfg.get_skill_req(stage, "processing")
                        p_time = cfg.process_times.get(stage, 1)
                        self.committed_labor_intervals.append((
                            job.get("start_t", snapshot.now_min), comp_t, l_setup, sk_setup, f"{res_id}_{vin}_setup"
                        ))
                        self.committed_labor_intervals.append((
                            comp_t, comp_t + p_time, l_proc, sk_proc, f"{res_id}_{vin}_proc"
                        ))
                        self._schedule_event(comp_t, 1, "SETUP_COMPLETE", {
                            "resource_id": res_id, "vin": vin, "stage": stage
                        })
                    elif node_type == "PROCESSING":
                        l_proc = cfg.get_labor_req(stage, "processing")
                        sk_proc = cfg.get_skill_req(stage, "processing")
                        self.committed_labor_intervals.append((
                            job.get("start_t", snapshot.now_min), comp_t, l_proc, sk_proc, f"{res_id}_{vin}_proc"
                        ))
                        self._schedule_event(comp_t, 1, "PROCESS_COMPLETE", {
                            "resource_id": res_id, "vin": vin, "stage": stage
                        })

            # Re-schedule buffer in-transit
            for buf in self.buffers.values():
                for (vin, arr) in buf.in_transit:
                    if arr > snapshot.now_min:
                        self._schedule_event(arr, 2, "TRANSIT_COMPLETE", {
                            "buffer_id": buf.buffer_id, "vin": vin
                        })

            # Re-schedule future unreceived deliveries
            for d in self.deliveries:
                if d.available_at_min > snapshot.now_min and d.delivery_id not in self.received_delivery_ids:
                    self._schedule_event(d.available_at_min, 1, "SUPPLY_ARRIVAL", {"delivery": d})
        else:
            self.current_time_min = 0
            self.received_delivery_ids = set()
            self.active_jobs = {}
            self.vehicle_completed_stages = {v: set() for v in self.orders}
            for d in self.deliveries:
                self._schedule_event(d.available_at_min, 1, "SUPPLY_ARRIVAL", {"delivery": d})

        # Schedule shift starts and order release events so DES advances across multi-day calendar boundaries
        for s in self.calendar.shifts:
            if s.start_min >= self.current_time_min and s.start_min < self.horizon_end_min:
                self._schedule_event(s.start_min, 4, "SHIFT_START", {"shift_id": s.shift_id})
        for o in self.orders.values():
            if o.release_at_min > self.current_time_min and o.release_at_min < self.horizon_end_min:
                self._schedule_event(o.release_at_min, 4, "ORDER_RELEASE", {"vin": o.vin})

        # Schedule external events at their known_at_min
        self.external_events = copy.deepcopy(external_events or [])
        for ev in self.external_events:
            if ev.known_at_min >= self.current_time_min and ev.known_at_min < self.horizon_end_min:
                self._schedule_event(ev.known_at_min, 1, ev.event_type, ev.payload)

        self._update_current_allocated_labor()

    def _schedule_event(self, t: int, priority: int, ev_type: str, payload: Dict[str, Any]) -> None:
        self._event_seq += 1
        heapq.heappush(self.event_queue, (t, priority, self._event_seq, ev_type, payload))

    def _update_current_allocated_labor(self) -> None:
        self.labor.current_allocated = sum(
            iv[2] for iv in self.committed_labor_intervals
            if iv[0] <= self.current_time_min < iv[1]
        )

    def can_reserve_labor(self, new_intervals: List[Tuple[Any, ...]]) -> bool:
        """Checks if new labor interval requirements can be accommodated without exceeding
        total max_workers or named skill pool capacities across all critical capacity transition points.
        """
        active_intervals = [
            iv for iv in self.committed_labor_intervals
            if iv[1] > self.current_time_min
        ] + [iv for iv in new_intervals if iv[1] > iv[0] and iv[2] > 0]

        check_times = set()
        for iv in active_intervals:
            if iv[0] >= self.current_time_min:
                check_times.add(iv[0])
            if iv[1] > self.current_time_min:
                check_times.add(max(self.current_time_min, iv[1] - 1))
        check_times.add(self.current_time_min)

        # Include capacity change transition points from dynamic labor time windows
        for st, ed, _ in self.labor.time_windows:
            if st >= self.current_time_min:
                check_times.add(st)
            if ed > self.current_time_min:
                check_times.add(max(self.current_time_min, ed - 1))

        # Include skill pool dynamic capacity transition points
        for pool in self.labor.skill_pools.values():
            for st, ed, _ in pool.time_windows:
                if st >= self.current_time_min:
                    check_times.add(st)
                if ed > self.current_time_min:
                    check_times.add(max(self.current_time_min, ed - 1))

        # Include shift boundaries
        for s in self.calendar.shifts:
            if s.start_min >= self.current_time_min and s.start_min < self.horizon_end_min:
                check_times.add(s.start_min)

        for t_pt in sorted(check_times):
            # 1. Total labor capacity at time t_pt
            total_allocated = sum(iv[2] for iv in active_intervals if iv[0] <= t_pt < iv[1])
            if total_allocated > self.labor.get_total_capacity(t_pt):
                return False

            # 2. Skill pool capacity if defined, evaluated with exact shift_id
            if self.labor.skill_pools:
                shift = self.calendar.get_current_shift(t_pt)
                shift_id = shift.shift_id if shift else None
                skill_allocated: Dict[str, int] = {}
                for iv in active_intervals:
                    if iv[0] <= t_pt < iv[1]:
                        sk = iv[3] if len(iv) >= 5 else "GENERAL"
                        skill_allocated[sk] = skill_allocated.get(sk, 0) + iv[2]
                for sk, needed in skill_allocated.items():
                    if needed > self.labor.get_skill_capacity(sk, t_pt, shift_id=shift_id):
                        return False
        return True

    def record_node_event(self, vin: str, stage: str, node: str, t: int, res_id: str, details: Dict[str, Any]) -> None:
        rec = EventRecord(
            vin=vin,
            order_id=self.orders[vin].order_id,
            stage=stage,
            node=node,
            timestamp_min=t,
            resource_id=res_id,
            details=details
        )
        self.trace.record_node(rec)

    def run(self, stop_at_min: Optional[int] = None, pause_before_dispatch_at_stop: bool = False) -> SimulationTrace:
        target_stop = min(self.horizon_end_min, stop_at_min) if stop_at_min is not None else self.horizon_end_min

        # Initial unblock and dispatch check at current time upon entry/resume
        self._try_unblock_stages()
        self._try_dispatch_all_stages()

        while self.event_queue:
            next_t = self.event_queue[0][0]
            if next_t > target_stop:
                break

            # Collect all events occurring at timestamp next_t (simultaneous events)
            events_at_t: List[Tuple[int, int, int, str, Dict[str, Any]]] = []
            while self.event_queue and self.event_queue[0][0] == next_t:
                events_at_t.append(heapq.heappop(self.event_queue))

            self.current_time_min = next_t

            # =========================================================================
            # PHASE 1: DETERMINISTIC SIMULTANEOUS EVENT ARBITRATION
            # Priority order:
            # 1. External delays/disruptions (MATERIAL_DELAY, cancellations, rush orders, line/labor)
            # 2. Releases (PROCESS_COMPLETE, SETUP_COMPLETE, TRANSIT_COMPLETE)
            # 3. Deliveries (SUPPLY_ARRIVAL) - suppressed if delayed at this exact timestamp!
            # 4. Routine releases (SHIFT_START, ORDER_RELEASE)
            # =========================================================================
            delayed_deliv_ids_at_t: Set[str] = set()

            def _phase1_prio(ev_item: Tuple[int, int, int, str, Dict[str, Any]]) -> int:
                etype = ev_item[3]
                if etype in ("MATERIAL_DELAY", "ORDER_CANCEL", "RUSH_ORDER", "LINE_STOP", "LINE_RESTORE", "LABOR_REDUCTION"):
                    return 0
                if etype in ("PROCESS_COMPLETE", "SETUP_COMPLETE", "TRANSIT_COMPLETE"):
                    return 1
                if etype == "SUPPLY_ARRIVAL":
                    return 2
                return 3

            events_at_t.sort(key=_phase1_prio)

            for t_val, prio, seq, ev_type, payload in events_at_t:
                if ev_type == "MATERIAL_DELAY":
                    d_id = payload.get("delivery_id")
                    if d_id:
                        delayed_deliv_ids_at_t.add(d_id)
                    self._handle_material_delay(payload)
                elif ev_type == "ORDER_CANCEL":
                    self._handle_order_cancel(payload)
                elif ev_type == "RUSH_ORDER":
                    self._handle_rush_order(payload)
                elif ev_type == "LINE_STOP":
                    self._handle_line_stop(payload)
                elif ev_type == "LINE_RESTORE":
                    self._handle_line_restore(payload)
                elif ev_type == "LABOR_REDUCTION":
                    self._handle_labor_reduction(payload)
                elif ev_type == "PROCESS_COMPLETE":
                    self._handle_process_complete_release(payload)
                elif ev_type == "SETUP_COMPLETE":
                    self._handle_setup_complete_release(payload)
                elif ev_type == "SUPPLY_ARRIVAL":
                    deliv_obj = payload.get("delivery")
                    if deliv_obj and deliv_obj.delivery_id in delayed_deliv_ids_at_t:
                        # Explicit arbitration: simultaneous delay at t intercepted before intake!
                        self.trace.inventory_snapshots.append({
                            "time": self.current_time_min,
                            "material_id": deliv_obj.material_id,
                            "on_hand": self.inventory.get(deliv_obj.material_id, 0),
                            "reserved": self.reserved_inventory.get(deliv_obj.material_id, 0),
                            "reason": f"SUPPRESSED_SIMULTANEOUS_ARRIVAL_{deliv_obj.delivery_id}",
                            "vin": None
                        })
                        continue
                    self._handle_supply_arrival(payload)
                elif ev_type == "TRANSIT_COMPLETE":
                    self._handle_transit_complete(payload)

            # Decision Barrier: if pause requested at this target_stop timestamp,
            # stop AFTER all releases/facts are applied, BEFORE new Node B dispatching starts!
            if pause_before_dispatch_at_stop and next_t == target_stop:
                break

            # =========================================================================
            # PHASE 2: ATOMIC ACQUISITIONS & CASCADING FIXED POINT LOOP
            # =========================================================================
            iterations = 0
            while iterations < 50:
                progress = False
                # 2.1 Unblock any blocked machines if downstream buffer now has space
                if self._try_unblock_stages():
                    progress = True
                # 2.2 Dispatch new jobs to idle resources
                if self._try_dispatch_all_stages():
                    progress = True
                if not progress:
                    break
                iterations += 1

        self.current_time_min = max(self.current_time_min, target_stop)

        # Identify unmet VINs
        for v, o in self.orders.items():
            if o.status != VehicleStatus.COMPLETED and o.status != VehicleStatus.CANCELLED:
                if v not in self.trace.unmet_vins:
                    self.trace.unmet_vins.append(v)

        return self.trace

    # --- Phase 1: Batch Releases & Transitions ---

    def _handle_supply_arrival(self, payload: Dict[str, Any]) -> None:
        d: SupplyDelivery = payload["delivery"]
        mat = d.material_id
        qty = d.quantity
        self.inventory[mat] = self.inventory.get(mat, 0) + qty
        self.received_delivery_ids.add(d.delivery_id)
        self.trace.inventory_snapshots.append({
            "time": self.current_time_min,
            "material_id": mat,
            "on_hand": self.inventory[mat],
            "reserved": self.reserved_inventory.get(mat, 0),
            "reason": f"DELIVERY_{d.delivery_id}",
            "vin": None
        })

    def _handle_material_delay(self, payload: Dict[str, Any]) -> None:
        """Handles MATERIAL_DELAY event:
        - If delivery was already received into physical inventory, reject delay and preserve inventory.
        - Updates unreceived delivery available_at_min.
        - Purges old pending SUPPLY_ARRIVAL events from queue to prevent duplicate or early arrivals.
        - Reschedules arrival at new_available_at_min.
        """
        deliv_id = payload.get("delivery_id")
        new_available = payload.get("new_available_at_min")
        if deliv_id is None or new_available is None:
            return

        # Physical reality constraint: once material is on the shop floor, it cannot be delayed
        if deliv_id in self.received_delivery_ids:
            self.trace.inventory_snapshots.append({
                "time": self.current_time_min,
                "material_id": "UNKNOWN",
                "on_hand": 0,
                "reserved": 0,
                "reason": f"REJECTED_DELAY_ALREADY_RECEIVED_{deliv_id}",
                "vin": None
            })
            return

        target_deliv = next((d for d in self.deliveries if d.delivery_id == deliv_id), None)
        if not target_deliv:
            return

        target_deliv.available_at_min = new_available

        # Purge any old pending SUPPLY_ARRIVAL events for this delivery
        new_queue = []
        for item in self.event_queue:
            t_ev, prio, seq, e_type, p_load = item
            if e_type == "SUPPLY_ARRIVAL":
                d_obj = p_load.get("delivery")
                if d_obj and d_obj.delivery_id == deliv_id:
                    continue
            new_queue.append(item)
        heapq.heapify(new_queue)
        self.event_queue = new_queue

        # Reschedule arrival strictly once at new_available
        if new_available < self.horizon_end_min:
            self._schedule_event(new_available, 1, "SUPPLY_ARRIVAL", {"delivery": target_deliv})

        self.trace.inventory_snapshots.append({
            "time": self.current_time_min,
            "material_id": target_deliv.material_id,
            "on_hand": self.inventory.get(target_deliv.material_id, 0),
            "reserved": self.reserved_inventory.get(target_deliv.material_id, 0),
            "reason": f"MATERIAL_DELAY_{deliv_id}_TO_{new_available}",
            "vin": None
        })

    def _handle_transit_complete(self, payload: Dict[str, Any]) -> None:
        buf_id = payload["buffer_id"]
        vin = payload["vin"]
        buf = self.buffers[buf_id]
        buf.in_transit = [(v, arr) for (v, arr) in buf.in_transit if v != vin]
        buf.items.append(vin)
        self.orders[vin].status = VehicleStatus.BUFFERED
        self.trace.buffer_snapshots.append({
            "time": self.current_time_min,
            "buffer_id": buf.buffer_id,
            "occupancy": buf.current_occupancy,
            "capacity": buf.capacity,
            "action": "TRANSIT_ARRIVE",
            "vin": vin
        })

    def _handle_line_stop(self, payload: Dict[str, Any]) -> None:
        res_id = payload.get("resource_id")
        dur = payload.get("duration_min", 60)
        if res_id and res_id in self.resources:
            res = self.resources[res_id]
            res.down_until_min = self.current_time_min + dur
            if res.state == MachineStatus.IDLE:
                res.state = MachineStatus.DOWN
            self._schedule_event(self.current_time_min + dur, 1, "LINE_RESTORE", {"resource_id": res_id})

    def _handle_line_restore(self, payload: Dict[str, Any]) -> None:
        res_id = payload.get("resource_id")
        if res_id and res_id in self.resources:
            res = self.resources[res_id]
            res.down_until_min = 0
            if res.state == MachineStatus.DOWN:
                res.state = MachineStatus.IDLE

    def _handle_labor_reduction(self, payload: Dict[str, Any]) -> None:
        new_cap = payload.get("new_max_workers", 1)
        dur = payload.get("duration_min", 60)
        sk = payload.get("skill_name")
        if sk and sk in self.labor.skill_pools:
            self.labor.skill_pools[sk].time_windows.append(
                (self.current_time_min, self.current_time_min + dur, new_cap)
            )
        else:
            self.labor.time_windows.append(
                (self.current_time_min, self.current_time_min + dur, new_cap)
            )
        self._update_current_allocated_labor()

    def _handle_order_cancel(self, payload: Dict[str, Any]) -> None:
        vin = payload.get("vin")
        if vin and vin in self.orders:
            o = self.orders[vin]
            if o.status in [
                VehicleStatus.SETUP,
                VehicleStatus.PROCESSING,
                VehicleStatus.BLOCKED,
                VehicleStatus.BUFFERED,
                VehicleStatus.IN_TRANSIT
            ]:
                o.exception_tag = "CANCELLED_IN_WIP"
            else:
                o.status = VehicleStatus.CANCELLED

    def _handle_rush_order(self, payload: Dict[str, Any]) -> None:
        order = payload.get("order")
        if order:
            b_key = payload.get("consumes_synthetic_bucket")
            if b_key:
                # Net against unstarted synthetic vehicle in same bucket if available
                unstarted_syn = next((
                    v for v, o in self.orders.items()
                    if o.is_virtual and o.forecast_bucket == b_key
                    and o.status in [VehicleStatus.UNRELEASED, VehicleStatus.READY]
                    and v not in self.trace.stage_node_times
                ), None)
                if unstarted_syn:
                    del self.orders[unstarted_syn]
                    if unstarted_syn in self.vehicle_completed_stages:
                        del self.vehicle_completed_stages[unstarted_syn]
            else:
                # Unforecasted extra emergency demand: does not net against forecast bucket
                order.consumes_forecast = False

            self.orders[order.vin] = order.clone()
            if order.vin not in self.vehicle_completed_stages:
                self.vehicle_completed_stages[order.vin] = set()
            self._schedule_event(self.current_time_min, 4, "ORDER_RELEASE", {"vin": order.vin})

    def apply_disruption_event(self, ev: Event) -> None:
        """Explicitly applies an external event to the world at the current simulation time."""
        if ev.event_type == "MATERIAL_DELAY":
            self._handle_material_delay(ev.payload)
        elif ev.event_type == "ORDER_CANCEL":
            self._handle_order_cancel(ev.payload)
        elif ev.event_type == "RUSH_ORDER":
            self._handle_rush_order(ev.payload)
        elif ev.event_type == "LINE_STOP":
            self._handle_line_stop(ev.payload)
        elif ev.event_type == "LINE_RESTORE":
            self._handle_line_restore(ev.payload)
        elif ev.event_type == "LABOR_REDUCTION":
            self._handle_labor_reduction(ev.payload)

    def set_active_plan(self, plan: OrderPlan) -> None:
        """Applies an approved plan version: updates authorized_vins and unstarted orders."""
        new_alloc = {}
        self.authorized_vins = set()
        for day, vins in plan.daily_allocations.items():
            for v in vins:
                new_alloc[v] = day
                self.authorized_vins.add(v)
        for v, o in self.orders.items():
            if o.status in [VehicleStatus.UNRELEASED, VehicleStatus.READY]:
                if v in new_alloc:
                    o.assigned_day = new_alloc[v]
                else:
                    o.assigned_day = None

    def _handle_setup_complete_release(self, payload: Dict[str, Any]) -> None:
        """Setup completed at S = B + setup_time. Transitions immediately to Node S without labor waiting."""
        res_id = payload["resource_id"]
        vin = payload["vin"]
        stage = payload["stage"]
        res = self.resources[res_id]
        cfg = self.configurations[self.orders[vin].config_id]

        # Update labor ledger for setup -> processing transition
        self._update_current_allocated_labor()
        l_setup = cfg.get_labor_req(stage, "setup")
        l_proc = cfg.get_labor_req(stage, "processing")
        self.trace.labor_snapshots.append({
            "time": self.current_time_min,
            "allocated": self.labor.current_allocated,
            "max_workers": self.labor.max_workers,
            "stage": stage,
            "phase": "PROC_START_TRANSITION",
            "vin": vin,
            "delta": l_proc - l_setup
        })

        # S starts strictly contiguous with setup completion
        res.state = MachineStatus.PROCESSING
        self.orders[vin].status = VehicleStatus.PROCESSING

        # Deduct materials at Node S
        bom = cfg.process_boms.get(stage, {})
        consumed_mats = {}
        for m, q in bom.items():
            self.inventory[m] -= q
            self.reserved_inventory[m] -= q
            consumed_mats[m] = q
            self.trace.inventory_snapshots.append({
                "time": self.current_time_min,
                "material_id": m,
                "on_hand": self.inventory[m],
                "reserved": self.reserved_inventory.get(m, 0),
                "reason": "CONSUMPTION_NODE_S",
                "vin": vin
            })

        # NODE S: Processing Start
        self.record_node_event(
            vin=vin,
            stage=stage,
            node="S",
            t=self.current_time_min,
            res_id=res_id,
            details={"materials_consumed": consumed_mats, "labor_proc": l_proc}
        )

        p_time = cfg.process_times.get(stage, 1)
        finish_t = self.current_time_min + p_time
        self.active_jobs[res_id] = {
            "vin": vin,
            "stage": stage,
            "node": "PROCESSING",
            "start_t": self.current_time_min,
            "complete_t": finish_t
        }
        self._schedule_event(finish_t, 1, "PROCESS_COMPLETE", {
            "resource_id": res_id,
            "vin": vin,
            "stage": stage
        })

    def _handle_process_complete_release(self, payload: Dict[str, Any]) -> None:
        res_id = payload["resource_id"]
        vin = payload["vin"]
        stage = payload["stage"]
        res = self.resources[res_id]
        cfg = self.configurations[self.orders[vin].config_id]

        # NODE C: Processing Complete
        # Remove ended processing interval from labor ledger
        self.committed_labor_intervals = [
            iv for iv in self.committed_labor_intervals if iv[1] > self.current_time_min
        ]
        self._update_current_allocated_labor()

        l_proc = cfg.get_labor_req(stage, "processing")
        self.trace.labor_snapshots.append({
            "time": self.current_time_min,
            "allocated": self.labor.current_allocated,
            "max_workers": self.labor.max_workers,
            "stage": stage,
            "phase": "PROC_RELEASE",
            "vin": vin,
            "delta": -l_proc
        })
        self.vehicle_completed_stages[vin].add(stage)

        # Update machine state attributes
        res.last_color = cfg.color
        res.last_model_type = cfg.model_type

        self.record_node_event(
            vin=vin,
            stage=stage,
            node="C",
            t=self.current_time_min,
            res_id=res_id,
            details={"completed_stage": stage}
        )

        # Evaluate transfer or enter BLOCKED
        self._attempt_free_resource_and_transfer(res, vin, stage)

    # --- Phase 2: Atomic Acquisitions ---

    def _attempt_free_resource_and_transfer(self, res: Resource, vin: str, stage: str) -> bool:
        cfg = self.configurations[self.orders[vin].config_id]

        # Case 1: Pullout vehicle at Paint (P)
        if cfg.is_pullout and stage == "P":
            self._finalize_resource_free(res, vin, stage)
            self.orders[vin].status = VehicleStatus.COMPLETED
            self.orders[vin].completed_at_min = self.current_time_min
            self.completed_vins.add(vin)
            self.trace.pullout_completions.append(vin)
            return True

        # Case 2: Final stage of the vehicle's route
        route = self.orders[vin].route
        if stage == route[-1]:
            self._finalize_resource_free(res, vin, stage)
            self.orders[vin].status = VehicleStatus.COMPLETED
            self.orders[vin].completed_at_min = self.current_time_min
            self.completed_vins.add(vin)
            return True

        # Case 3: Intermediate stage -> downstream buffer
        next_stage_idx = route.index(stage) + 1
        next_stage = route[next_stage_idx]
        edge_key = f"{stage}->{next_stage}"
        buf = next((b for b in self.buffers.values() if b.edge == edge_key), None)

        if buf is None:
            down_res = next((r for r in self.resources.values() if r.stage == next_stage), None)
            if down_res and down_res.state == MachineStatus.IDLE:
                self._finalize_resource_free(res, vin, stage)
                return True
            else:
                self._set_machine_blocked(res, vin, stage)
                return False

        if buf.has_space():
            self._finalize_resource_free(res, vin, stage)
            if buf.transit_time_min > 0:
                buf.in_transit.append((vin, self.current_time_min + buf.transit_time_min))
                self.orders[vin].status = VehicleStatus.IN_TRANSIT
                self._schedule_event(
                    self.current_time_min + buf.transit_time_min,
                    2,
                    "TRANSIT_COMPLETE",
                    {"buffer_id": buf.buffer_id, "vin": vin}
                )
            else:
                buf.items.append(vin)
                self.orders[vin].status = VehicleStatus.BUFFERED

            self.trace.buffer_snapshots.append({
                "time": self.current_time_min,
                "buffer_id": buf.buffer_id,
                "occupancy": buf.current_occupancy,
                "capacity": buf.capacity,
                "action": "ENTER",
                "vin": vin
            })
            return True
        else:
            # POSITIVE BLOCKING!
            self._set_machine_blocked(res, vin, stage)
            return False

    def _set_machine_blocked(self, res: Resource, vin: str, stage: str) -> None:
        res.state = MachineStatus.BLOCKED
        res.blocked_until_freed = True
        self.orders[vin].status = VehicleStatus.BLOCKED
        self.active_jobs[res.resource_id] = {
            "vin": vin,
            "stage": stage,
            "node": "BLOCKED",
            "blocked_at": self.current_time_min
        }

    def _finalize_resource_free(self, res: Resource, vin: str, stage: str) -> None:
        c_time = self.trace.get_node_time(vin, stage, "C")
        f_time = self.current_time_min

        # NODE F: Resource Freed
        self.record_node_event(
            vin=vin,
            stage=stage,
            node="F",
            t=f_time,
            res_id=res.resource_id,
            details={"blocked_duration": f_time - (c_time or f_time)}
        )
        if c_time is not None and f_time > c_time:
            self.trace.blocked_durations.append({
                "vin": vin,
                "stage": stage,
                "C": c_time,
                "F": f_time,
                "blocked_duration": f_time - c_time
            })

        res.state = MachineStatus.IDLE
        res.current_vin = None
        res.blocked_until_freed = False
        if res.resource_id in self.active_jobs:
            del self.active_jobs[res.resource_id]

    def _try_unblock_stages(self) -> bool:
        progress = False
        for res_id, job in list(self.active_jobs.items()):
            if job.get("node") == "BLOCKED":
                res = self.resources[res_id]
                vin = job["vin"]
                stage = job["stage"]
                if self._attempt_free_resource_and_transfer(res, vin, stage):
                    progress = True
        return progress

    def _try_dispatch_all_stages(self) -> bool:
        progress = False
        for res in sorted(self.resources.values(), key=lambda r: r.resource_id):
            if res.state != MachineStatus.IDLE or getattr(res, "down_until_min", 0) > self.current_time_min:
                continue
            if self._try_dispatch_stage(res):
                progress = True
        return progress

    def _try_dispatch_stage(self, res: Resource) -> bool:
        stage = res.stage
        cfg_map = self.configurations
        orders_map = self.orders

        eligible_vins: List[str] = []
        # Check if an authorized plan or day assignment is active on this world
        has_active_plan = (getattr(self, "authorized_vins", None) is not None) or any(
            ord_obj.assigned_day is not None for ord_obj in orders_map.values()
        )

        if stage == "W":
            for v, o in orders_map.items():
                if o.status in [VehicleStatus.UNRELEASED, VehicleStatus.READY]:
                    if o.release_at_min <= self.current_time_min:
                        # Explicit plan authorization gate:
                        # When a plan is active, vehicles excluded from the plan (assigned_day is None
                        # or not in authorized_vins) MUST NOT be dispatched to Stage W (Node B).
                        if has_active_plan:
                            if getattr(self, "authorized_vins", None) is not None:
                                if v not in self.authorized_vins:
                                    continue
                            if o.assigned_day is None:
                                continue
                            day_start = self.calendar.day_to_start_min(o.assigned_day)
                            if self.current_time_min < day_start:
                                continue
                        eligible_vins.append(v)
        else:
            prev_stage = "W" if stage == "P" else "P"
            edge_key = f"{prev_stage}->{stage}"
            buf = next((b for b in self.buffers.values() if b.edge == edge_key), None)
            if buf:
                for v in buf.items:
                    if stage in orders_map[v].route and stage not in self.vehicle_completed_stages[v]:
                        eligible_vins.append(v)

        if not eligible_vins:
            return False

        policy = self.stage_policies.get_stage_policy(stage)
        selected_vin = policy.select_next(
            stage=stage,
            eligible_vins=eligible_vins,
            orders_map=orders_map,
            configs_map=cfg_map,
            resource=res,
            current_time_min=self.current_time_min
        )

        if not selected_vin:
            return False

        cfg = cfg_map[orders_map[selected_vin].config_id]
        setup_time = cfg.get_setup_time(
            stage,
            res.last_color if stage == "P" else res.last_model_type,
            cfg.color if stage == "P" else cfg.model_type
        )
        proc_time = cfg.process_times.get(stage, 1)
        total_service_time = setup_time + proc_time
        s_time = self.current_time_min + setup_time
        c_time = s_time + proc_time

        # 1. Calendar shift check: entire service block cannot cross shift boundary
        if not self.calendar.can_fit_job(self.current_time_min, total_service_time):
            earliest_fit = self.calendar.find_earliest_fit_minute(self.current_time_min, total_service_time)
            if earliest_fit > self.current_time_min and earliest_fit < self.horizon_end_min:
                self._schedule_event(earliest_fit, 3, "TRY_DISPATCH", {})
            return False

        # 2. Fine-grained interval labor check: reserve [B, S) for setup and [S, C) for processing
        l_setup = cfg.get_labor_req(stage, "setup")
        l_proc = cfg.get_labor_req(stage, "processing")
        sk_setup = cfg.get_skill_req(stage, "setup")
        sk_proc = cfg.get_skill_req(stage, "processing")
        new_intervals = []
        if setup_time > 0:
            new_intervals.append((self.current_time_min, s_time, l_setup, sk_setup))
        new_intervals.append((s_time, c_time, l_proc, sk_proc))

        if not self.can_reserve_labor(new_intervals):
            return False

        # 3. Check materials availability (including existing reservations)
        bom = cfg.process_boms.get(stage, {})
        for mat, qty in bom.items():
            avail = self.inventory.get(mat, 0) - self.reserved_inventory.get(mat, 0)
            if avail < qty:
                return False

        # If pulling from upstream buffer, atomically remove from buffer items
        if stage != "W":
            prev_stage = "W" if stage == "P" else "P"
            edge_key = f"{prev_stage}->{stage}"
            buf = next((b for b in self.buffers.values() if b.edge == edge_key), None)
            if buf and selected_vin in buf.items:
                buf.items.remove(selected_vin)
                self.trace.buffer_snapshots.append({
                    "time": self.current_time_min,
                    "buffer_id": buf.buffer_id,
                    "occupancy": buf.current_occupancy,
                    "capacity": buf.capacity,
                    "action": "DEPART",
                    "vin": selected_vin
                })

        # Commit labor interval reservations atomically
        if setup_time > 0:
            self.committed_labor_intervals.append((
                self.current_time_min, s_time, l_setup, sk_setup, f"{res.resource_id}_{selected_vin}_setup"
            ))
        self.committed_labor_intervals.append((
            s_time, c_time, l_proc, sk_proc, f"{res.resource_id}_{selected_vin}_proc"
        ))
        self._update_current_allocated_labor()

        # Reserve materials
        for mat, qty in bom.items():
            self.reserved_inventory[mat] = self.reserved_inventory.get(mat, 0) + qty

        # NODE B: Setup Start
        self.record_node_event(
            vin=selected_vin,
            stage=stage,
            node="B",
            t=self.current_time_min,
            res_id=res.resource_id,
            details={"setup_time": setup_time, "labor_setup": l_setup}
        )

        res.current_vin = selected_vin

        if setup_time == 0:
            # Zero setup: S starts immediately and atomically at the same minute t
            res.state = MachineStatus.PROCESSING
            self.orders[selected_vin].status = VehicleStatus.PROCESSING

            consumed_mats = {}
            for m, q in bom.items():
                self.inventory[m] -= q
                self.reserved_inventory[m] -= q
                consumed_mats[m] = q
                self.trace.inventory_snapshots.append({
                    "time": self.current_time_min,
                    "material_id": m,
                    "on_hand": self.inventory[m],
                    "reserved": self.reserved_inventory.get(m, 0),
                    "reason": "CONSUMPTION_NODE_S",
                    "vin": selected_vin
                })

            # NODE S: Processing Start
            self.record_node_event(
                vin=selected_vin,
                stage=stage,
                node="S",
                t=self.current_time_min,
                res_id=res.resource_id,
                details={"materials_consumed": consumed_mats, "labor_proc": l_proc}
            )

            finish_t = self.current_time_min + proc_time
            self.active_jobs[res.resource_id] = {
                "vin": selected_vin,
                "stage": stage,
                "node": "PROCESSING",
                "start_t": self.current_time_min,
                "complete_t": finish_t
            }
            self._schedule_event(finish_t, 1, "PROCESS_COMPLETE", {
                "resource_id": res.resource_id,
                "vin": selected_vin,
                "stage": stage
            })
        else:
            res.state = MachineStatus.SETUP
            self.orders[selected_vin].status = VehicleStatus.SETUP

            self.trace.labor_snapshots.append({
                "time": self.current_time_min,
                "allocated": self.labor.current_allocated,
                "max_workers": self.labor.max_workers,
                "stage": stage,
                "phase": "SETUP_START",
                "vin": selected_vin,
                "delta": l_setup
            })

            setup_finish_t = self.current_time_min + setup_time
            self.active_jobs[res.resource_id] = {
                "vin": selected_vin,
                "stage": stage,
                "node": "SETUP",
                "start_t": self.current_time_min,
                "complete_t": setup_finish_t
            }
            self._schedule_event(setup_finish_t, 1, "SETUP_COMPLETE", {
                "resource_id": res.resource_id,
                "vin": selected_vin,
                "stage": stage
            })

        return True


def simulate(
    snapshot: PlanningSnapshot,
    order_plan: OrderPlan,
    stage_policies: PolicyBundle,
    stop_at_min: Optional[int] = None
) -> SimulationTrace:
    """Executes discrete event simulation starting from snapshot state with full restoration."""
    resources = {k: copy.deepcopy(v) for k, v in snapshot.resources.items()}
    buffers = {k: copy.deepcopy(v) for k, v in snapshot.buffers.items()}
    labor = copy.deepcopy(snapshot.labor)
    orders = {o.vin: o.clone() for o in snapshot.orders}

    for day, vins in order_plan.daily_allocations.items():
        for v in vins:
            if v in orders:
                orders[v].assigned_day = day

    world = ExecutionWorld(
        calendar=snapshot.calendar,
        configurations=snapshot.configurations,
        resources=resources,
        buffers=buffers,
        labor=labor,
        initial_inventory=snapshot.current_inventory,
        deliveries=snapshot.known_deliveries,
        orders=orders,
        stage_policies=stage_policies,
        horizon_end_min=snapshot.calendar.horizon_end_min,
        snapshot=snapshot
    )

    return world.run(stop_at_min=stop_at_min)
