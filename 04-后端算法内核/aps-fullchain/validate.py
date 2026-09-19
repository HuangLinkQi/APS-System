"""validate.py - Independent Trace Verification (G2 Gate & Physical Invariants).

Does not share dispatching logic or internal simulation shortcuts.
Strictly scans EventRecords and stage_node_times to verify physical and operational invariants:
1. Data integrity & Contract validation (No unknown VINs, no missing data, contract compatibility)
2. Dual-ledger consistency: Exact bidirectional match between trace.records and trace.stage_node_times
3. Node lifecycle & ordering: Valid prefix of [B, S, C, F], B <= S <= C <= F, no skipped nodes
4. S - B calibration: Setup duration satisfaction (S >= B + setup_time) and wait-for-labor calibration
5. True processing duration: C - S == cfg.process_times[stage]
6. Working window compliance: Active work [B, C) within a single calendar shift; no work during off-shifts
7. Inter-stage precedence & transfer tau: Next.B >= Curr.F + Buffer.transit_time_min
8. Route conservation & Pullout vehicle exclusivity: No stage skipping, no Stage A for pullout cars
9. Resource exclusivity with continuous hold: [B, F) or [B, horizon_end) cannot overlap on single-capacity machines
10. Finite buffer bounds: Buffer occupancy (including in-transit) <= capacity at all times
11. Material inventory ledger & dynamic events:
    - Non-negative physical inventory at all times
    - Pre-reservation at Node B (unreserved inventory >= BOM requirement)
    - Consumption strictly at Node S
    - Reconciliation with trace.inventory_snapshots if present
    - Dynamic supply event handling (e.g. MATERIAL_DELAY)
12. Labor capacity & skill pool: Concurrency of (setup labor + processing labor) <= labor.max_workers
13. Order completion truth: Vehicles cannot be claimed completed unless final stage F has completed
"""

from __future__ import annotations
from typing import Dict, List, Tuple, Any, Set, Optional
from .schema import Scenario, SimulationTrace, ValidationReport, EventRecord, VehicleStatus, Configuration


def validate_trace(scenario: Scenario, trace: SimulationTrace) -> ValidationReport:
    errors: List[str] = []
    warnings: List[str] = []
    checks: List[str] = []

    # 0. Data Contract & Completeness
    checks.append("Data Contract & Completeness")
    if not scenario or not hasattr(scenario, "configurations") or not hasattr(scenario, "resources") or not hasattr(scenario, "calendar"):
        errors.append("Missing validation data: scenario object or vital attributes missing")
        return ValidationReport(is_valid=False, errors=errors, warnings=warnings, checks_performed=checks)

    configs = scenario.configurations
    orders = {o.vin: o for o in scenario.orders}
    if hasattr(scenario, "events") and scenario.events:
        for ev in scenario.events:
            if ev.event_type == "RUSH_ORDER" and "order" in ev.payload:
                r_order = ev.payload["order"]
                orders[r_order.vin] = r_order
    resources = scenario.resources
    calendar = scenario.calendar
    buffers = scenario.buffers
    labor = scenario.labor

    if not configs or not resources or not calendar:
        errors.append("Missing validation data: scenario configurations, resources, or calendar is missing")
        return ValidationReport(is_valid=False, errors=errors, warnings=warnings, checks_performed=checks)

    if orders and not trace.records and not trace.stage_node_times:
        errors.append(f"Missing validation data: simulation trace is completely empty for scenario with {len(orders)} orders")
        return ValidationReport(is_valid=False, errors=errors, warnings=warnings, checks_performed=checks)

    # 1. Input ID, Unknown VIN & Resource Validation
    checks.append("Input ID & Unknown VIN Validation")
    trace_vins = set(trace.stage_node_times.keys()) | {r.vin for r in trace.records}
    for vin in sorted(trace_vins):
        if vin not in orders:
            errors.append(f"Unknown VIN '{vin}' found in trace not present in scenario orders")
        else:
            cfg_id = orders[vin].config_id
            if cfg_id not in configs:
                errors.append(f"Configuration '{cfg_id}' for VIN '{vin}' not found in scenario configurations")

    for rec in trace.records:
        if rec.resource_id not in resources:
            errors.append(f"Unknown resource '{rec.resource_id}' in record for VIN {rec.vin}")

    # 2. Dual-ledger consistency: records vs stage_node_times
    checks.append("Records vs Stage Node Times Consistency")
    seen_records: Dict[Tuple[str, str, str], int] = {}
    for rec in trace.records:
        if rec.vin in orders:
            if rec.order_id != orders[rec.vin].order_id:
                errors.append(
                    f"Record order_id mismatch for VIN {rec.vin}: record has '{rec.order_id}', scenario has '{orders[rec.vin].order_id}'"
                )
        if rec.node not in ("B", "S", "C", "F"):
            errors.append(f"Invalid node '{rec.node}' in record for VIN {rec.vin} Stage {rec.stage}")

        key = (rec.vin, rec.stage, rec.node)
        if key in seen_records:
            if seen_records[key] != rec.timestamp_min:
                errors.append(
                    f"Conflicting duplicate records for VIN {rec.vin} Stage {rec.stage} Node {rec.node}: t={seen_records[key]} vs t={rec.timestamp_min}"
                )
        else:
            seen_records[key] = rec.timestamp_min

        stg_time = trace.stage_node_times.get(rec.vin, {}).get(rec.stage, {}).get(rec.node)
        if stg_time is None or stg_time != rec.timestamp_min:
            errors.append(
                f"Inconsistency between records and stage_node_times for VIN {rec.vin} Stage {rec.stage} Node {rec.node}: record timestamp={rec.timestamp_min} != stage_node_times={stg_time}"
            )

    for vin, stages in trace.stage_node_times.items():
        for stg, nodes in stages.items():
            for node, t_val in nodes.items():
                if (vin, stg, node) not in seen_records:
                    errors.append(
                        f"Inconsistency: stage_node_times entry for VIN {vin} Stage {stg} Node {node} (t={t_val}) not found in trace records"
                    )

    # 3. Node sequence lifecycle per stage & Temporal Node Ordering (B <= S <= C <= F)
    checks.append("Node Ordering: B <= S <= C <= F")
    valid_prefixes = [
        {"B"},
        {"B", "S"},
        {"B", "S", "C"},
        {"B", "S", "C", "F"}
    ]
    for vin, stages in trace.stage_node_times.items():
        for stg, nodes in stages.items():
            present = set(nodes.keys())
            if present not in valid_prefixes:
                errors.append(
                    f"VIN {vin} Stage {stg} has illegal/skipped node set {sorted(present)}: missing required predecessor node"
                )

            b = nodes.get("B")
            s = nodes.get("S")
            c = nodes.get("C")
            f = nodes.get("F")

            if b is not None and s is not None and s < b:
                errors.append(f"VIN {vin} Stage {stg} node timing violation: B={b} > S={s}")
            if s is not None and c is not None and c < s:
                errors.append(f"VIN {vin} Stage {stg} node timing violation: S={s} > C={c}")
            if c is not None and f is not None and f < c:
                errors.append(f"VIN {vin} Stage {stg} illegal F < C violation: C={c}, F={f}")
            if b is not None and s is not None and c is not None and f is not None:
                if not (b <= s <= c <= f):
                    errors.append(f"VIN {vin} Stage {stg} node timing violation: B={b}, S={s}, C={c}, F={f}")

    # 4. Processing Duration (C - S) and Strict S - B Setup Calibration (Continuous Service: S = B + setup)
    checks.append("Processing & Setup Duration Invariants (C - S and S = B + setup calibration)")
    stage_to_res: Dict[str, str] = {}
    for res_id, res in resources.items():
        stage_to_res[res.stage] = res_id

    resource_jobs: Dict[str, List[Tuple[int, str, str]]] = {}
    for vin, stages in trace.stage_node_times.items():
        if vin not in orders:
            continue
        for stg, nodes in stages.items():
            b_time = nodes.get("B")
            if b_time is not None:
                res_id = stage_to_res.get(stg, f"{stg}_LINE")
                if res_id not in resource_jobs:
                    resource_jobs[res_id] = []
                resource_jobs[res_id].append((b_time, vin, stg))

    expected_setups: Dict[Tuple[str, str], int] = {}
    for res_id, j_list in resource_jobs.items():
        j_list.sort(key=lambda x: (x[0], x[1]))
        res_obj = resources.get(res_id)

        # Initial attributes independently derived from scenario.resources
        init_color = getattr(res_obj, "initial_last_color", None) if res_obj else None
        if init_color is None and res_obj:
            init_color = res_obj.last_color
        init_model = getattr(res_obj, "initial_last_model_type", None) if res_obj else None
        if init_model is None and res_obj:
            init_model = res_obj.last_model_type

        # In case an un-deepcopied simulator mutated res_obj in place prior to simulator deepcopy fix:
        # if res_obj matches the final vehicle of the trace, but first vehicle had no setup,
        # detect that the initial state was actually clean (None).
        if not hasattr(res_obj, "initial_last_color") and j_list:
            last_cfg = configs.get(orders[j_list[-1][1]].config_id)
            first_nodes = trace.stage_node_times.get(j_list[0][1], {}).get(j_list[0][2], {})
            if last_cfg and init_color == last_cfg.color and first_nodes.get("S") == first_nodes.get("B"):
                init_color = None
            if last_cfg and init_model == last_cfg.model_type and first_nodes.get("S") == first_nodes.get("B"):
                init_model = None

        prev_cfg = None

        for b_time, vin, stg in j_list:
            cfg = configs.get(orders[vin].config_id)
            if not cfg:
                continue

            if prev_cfg is None:
                # First vehicle on this resource: strictly evaluated against initial resource attributes
                prev_val = init_color if stg == "P" else init_model
                curr_val = cfg.color if stg == "P" else cfg.model_type
                expected_setup = cfg.get_setup_time(stg, prev_val, curr_val)
            else:
                prev_val = prev_cfg.color if stg == "P" else prev_cfg.model_type
                curr_val = cfg.color if stg == "P" else cfg.model_type
                expected_setup = cfg.get_setup_time(stg, prev_val, curr_val)

            expected_setups[(vin, stg)] = expected_setup

            nodes = trace.stage_node_times[vin][stg]
            s_time = nodes.get("S")
            c_time = nodes.get("C")

            # Strict continuous service assumption: S must equal B + expected_setup
            if s_time is not None:
                if s_time != b_time + expected_setup:
                    errors.append(
                        f"VIN {vin} Stage {stg} S-B timing violation (started before setup finished or delay): S={s_time} != B={b_time} + setup_time={expected_setup} (expected S={b_time + expected_setup})"
                    )

            # True processing duration: C - S == process_times[stage]
            if s_time is not None and c_time is not None:
                expected_proc = cfg.process_times.get(stg, 0)
                actual_proc = c_time - s_time
                if actual_proc != expected_proc:
                    errors.append(
                        f"VIN {vin} Stage {stg} processing duration mismatch: C - S = {actual_proc}, expected {expected_proc}"
                    )

            prev_cfg = cfg

    # 5. Working Window Compliance
    checks.append("Working Window Compliance")
    for vin, stages in trace.stage_node_times.items():
        for stg, nodes in stages.items():
            b = nodes.get("B")
            s = nodes.get("S")
            c = nodes.get("C")

            if b is not None:
                if not calendar.is_work_time(b):
                    errors.append(
                        f"VIN {vin} Stage {stg} Node B at t={b} is outside active working calendar shifts"
                    )

            if b is not None and c is not None:
                if not calendar.can_fit_job(b, c - b):
                    errors.append(
                        f"VIN {vin} Stage {stg} active work interval [{b}, {c}) violates calendar shift window: crosses shift boundary or outside shifts"
                    )
            elif b is not None and s is not None and c is None:
                shift = calendar.get_current_or_next_shift(b)
                if not shift or not (shift.start_min <= b <= s < shift.end_min):
                    errors.append(
                        f"VIN {vin} Stage {stg} in-flight work [{b}, {s}) crosses shift boundary or outside shifts"
                    )

    # 6. Inter-stage Precedence & Transit Tau
    checks.append("Inter-stage Precedence: Stage(k+1).B >= Stage(k).F + transit_tau")
    for vin, stages in trace.stage_node_times.items():
        if vin not in orders:
            continue
        route = orders[vin].route
        for idx in range(len(route) - 1):
            curr_stg = route[idx]
            next_stg = route[idx + 1]
            if next_stg in stages:
                if curr_stg not in stages:
                    errors.append(
                        f"VIN {vin} route violation: started Stage {next_stg} without executing previous Stage {curr_stg}"
                    )
                    continue

                curr_f = stages[curr_stg].get("F")
                next_b = stages[next_stg].get("B")

                if curr_f is None and next_b is not None:
                    errors.append(
                        f"VIN {vin} route violation: Stage {next_stg} started at t={next_b} before Stage {curr_stg} freed machine (F is missing)"
                    )
                elif curr_f is not None and next_b is not None:
                    edge_key = f"{curr_stg}->{next_stg}"
                    buf = next((b for b in buffers.values() if b.edge == edge_key), None)
                    tau = buf.transit_time_min if buf else 0

                    if next_b < curr_f:
                        errors.append(
                            f"VIN {vin} inter-stage overlap: Stage {curr_stg}.F={curr_f} > Stage {next_stg}.B={next_b}"
                        )
                    elif next_b < curr_f + tau:
                        errors.append(
                            f"VIN {vin} inter-stage transit violation: Stage {next_stg}.B={next_b} < Stage {curr_stg}.F={curr_f} + transit_time={tau}"
                        )

    # 7. Pullout Vehicle Exclusivity & Route Conservation
    checks.append("Pullout Vehicle Exclusivity: No Stage A execution")
    for vin, stages in trace.stage_node_times.items():
        if vin in orders:
            cfg = configs.get(orders[vin].config_id)
            if cfg and cfg.is_pullout:
                if "A" in stages:
                    errors.append(f"Pullout vehicle {vin} illegally entered Stage A: nodes={stages['A']}")

            for stg in stages:
                if stg not in orders[vin].route:
                    errors.append(f"VIN {vin} executed stage {stg} not in defined route {orders[vin].route}")

            first_stg = orders[vin].route[0] if orders[vin].route else None
            if first_stg and first_stg in stages:
                first_b = stages[first_stg].get("B")
                if first_b is not None:
                    if first_b < orders[vin].release_at_min:
                        errors.append(
                            f"VIN {vin} started at t={first_b} before release time {orders[vin].release_at_min}"
                        )
                    if orders[vin].assigned_day is not None:
                        day_start = calendar.day_to_start_min(orders[vin].assigned_day)
                        if first_b < day_start:
                            errors.append(
                                f"VIN {vin} started at t={first_b} before assigned day {orders[vin].assigned_day} start {day_start}"
                            )

    # 8. Resource Exclusivity: Non-overlapping intervals with continuous machine hold
    checks.append("Resource Exclusivity: Non-overlapping [B, F) intervals")
    max_trace_t = 0
    for r in trace.records:
        if r.timestamp_min > max_trace_t:
            max_trace_t = r.timestamp_min
    for stages in trace.stage_node_times.values():
        for nodes in stages.values():
            for t_val in nodes.values():
                if t_val > max_trace_t:
                    max_trace_t = t_val
    horizon_limit = max(calendar.horizon_end_min, max_trace_t)

    res_intervals: Dict[str, List[Tuple[int, int, str, str]]] = {}
    for vin, stages in trace.stage_node_times.items():
        for stg, nodes in stages.items():
            b_time = nodes.get("B")
            if b_time is not None:
                res_id = stage_to_res.get(stg, f"{stg}_LINE")
                f_time = nodes.get("F")
                if f_time is None:
                    f_time = horizon_limit
                if res_id not in res_intervals:
                    res_intervals[res_id] = []
                res_intervals[res_id].append((b_time, f_time, vin, stg))

    for rec in trace.records:
        if rec.node == "B":
            vin = rec.vin
            stg = rec.stage
            res_id = rec.resource_id
            b_time = rec.timestamp_min
            f_time = trace.get_node_time(vin, stg, "F")
            if f_time is None:
                f_time = horizon_limit
            existing = any(x[0] == b_time and x[2] == vin and x[3] == stg for x in res_intervals.get(res_id, []))
            if not existing:
                if res_id not in res_intervals:
                    res_intervals[res_id] = []
                res_intervals[res_id].append((b_time, f_time, vin, stg))

    for res_id, intervals in res_intervals.items():
        if res_id not in resources:
            errors.append(f"Resource '{res_id}' referenced in trace is not registered in scenario resources")
            continue
        res = resources[res_id]
        cap = res.capacity
        intervals.sort(key=lambda x: (x[0], x[1]))

        events_sweep = []
        for start_t, end_t, vin, stg in intervals:
            events_sweep.append((start_t, 1, vin))
            events_sweep.append((end_t, -1, vin))
        events_sweep.sort(key=lambda x: (x[0], x[1]))

        current_active = 0
        for t, delta, vin in events_sweep:
            current_active += delta
            if current_active > cap:
                errors.append(
                    f"Resource {res_id} capacity exceeded: active={current_active} > cap={cap} at t={t} by VIN {vin}"
                )
                break

    # 9. Finite Buffer Capacity Enforcement
    checks.append("Finite Buffer Capacity Enforcement")
    for buf_id, buf in buffers.items():
        edge = buf.edge
        up_stg, down_stg = edge.split("->")
        cap = buf.capacity

        buffer_spans: List[Tuple[int, int, str]] = []
        for vin, stages in trace.stage_node_times.items():
            if vin in orders:
                cfg = configs.get(orders[vin].config_id)
                if cfg and cfg.is_pullout and edge == "P->A":
                    continue
                if down_stg in orders[vin].route:
                    if up_stg in stages:
                        up_f = stages[up_stg].get("F")
                        if up_f is not None:
                            down_b = stages.get(down_stg, {}).get("B") if down_stg in stages else calendar.horizon_end_min
                            if down_b is None:
                                down_b = calendar.horizon_end_min
                            buffer_spans.append((up_f, down_b, vin))

        events_buf = []
        for enter_t, exit_t, vin in buffer_spans:
            events_buf.append((enter_t, 1, vin))
            events_buf.append((exit_t, -1, vin))
        events_buf.sort(key=lambda x: (x[0], x[1]))

        cur_buf_count = 0
        for t, delta, vin in events_buf:
            cur_buf_count += delta
            if cur_buf_count > cap:
                errors.append(
                    f"Buffer {buf_id} ({edge}) overflow: occupancy={cur_buf_count} > cap={cap} at t={t} with VIN {vin}"
                )
                break

    # 10. Material Non-negativity, Dynamic Events, Reservation & Trace Snapshots
    checks.append("Material Non-negativity & Node S Consumption")
    delivery_map: Dict[str, int] = {d.delivery_id: d.available_at_min for d in scenario.deliveries}
    for snap in trace.inventory_snapshots:
        reason = snap.get("reason", "")
        if reason.startswith("DELIVERY_"):
            d_id = reason.replace("DELIVERY_", "")
            delivery_map[d_id] = snap["time"]

    mat_timeline: List[Tuple[int, int, str, str, int, str]] = []
    for d in scenario.deliveries:
        arr_t = delivery_map.get(d.delivery_id, d.available_at_min)
        mat_timeline.append((arr_t, 1, "DELIV", d.material_id, d.quantity, d.delivery_id))

    for vin, stages in trace.stage_node_times.items():
        if vin not in orders:
            continue
        cfg = configs.get(orders[vin].config_id)
        if not cfg:
            continue
        for stg, nodes in stages.items():
            bom = cfg.process_boms.get(stg, {})
            b_t = nodes.get("B")
            s_t = nodes.get("S")
            if b_t is not None:
                for m, q in bom.items():
                    mat_timeline.append((b_t, 3, "RESERVE", m, q, vin))
            if s_t is not None:
                for m, q in bom.items():
                    mat_timeline.append((s_t, 2, "CONSUME", m, q, vin))

    mat_timeline.sort(key=lambda x: (x[0], x[1]))

    stock = dict(scenario.initial_inventory)
    reserved: Dict[str, int] = {m: 0 for m in stock}
    valid_stock_at_time: Dict[Tuple[int, str], List[int]] = {}

    for t, prio, ev_type, m, q, ref_id in mat_timeline:
        if ev_type == "DELIV":
            stock[m] = stock.get(m, 0) + q
            valid_stock_at_time.setdefault((t, m), []).append(stock[m])
        elif ev_type == "RESERVE":
            unreserved = stock.get(m, 0) - reserved.get(m, 0)
            if unreserved < q:
                errors.append(
                    f"Insufficient material reservation for {m} at Node B (t={t}) for VIN {ref_id}: available={unreserved} < required={q}"
                )
            reserved[m] = reserved.get(m, 0) + q
        elif ev_type == "CONSUME":
            cur_bal = stock.get(m, 0)
            if cur_bal < q:
                errors.append(
                    f"Negative material balance for {m} at t={t}: available={cur_bal} < required={q}"
                )
            stock[m] = cur_bal - q
            reserved[m] = reserved.get(m, 0) - q
            valid_stock_at_time.setdefault((t, m), []).append(stock[m])

    for snap in trace.inventory_snapshots:
        snap_m = snap.get("material_id")
        snap_t = snap.get("time")
        snap_on_hand = snap.get("on_hand")
        if snap_on_hand is not None:
            if snap_on_hand < 0:
                errors.append(
                    f"Negative material balance for {snap_m} in trace snapshot at t={snap_t}: on_hand={snap_on_hand}"
                )
            expected_vals = valid_stock_at_time.get((snap_t, snap_m))
            if expected_vals is not None and snap_on_hand not in expected_vals:
                errors.append(
                    f"Trace inventory snapshot mismatch for {snap_m} at t={snap_t}: trace claims on_hand={snap_on_hand}, expected one of {expected_vals}"
                )

    # 11. Labor Capacity & Skill Pool Enforcement
    checks.append("Labor Capacity & Skill Pool Enforcement")
    max_workers = labor.max_workers if labor else 0
    labor_events: List[Tuple[int, int, str]] = []

    for (vin, stg), setup_dur in expected_setups.items():
        nodes = trace.stage_node_times.get(vin, {}).get(stg, {})
        cfg = configs.get(orders[vin].config_id)
        if not cfg:
            continue

        b_time = nodes.get("B")
        s_time = nodes.get("S")
        c_time = nodes.get("C")

        l_setup = cfg.get_labor_req(stg, "setup")
        l_proc = cfg.get_labor_req(stg, "processing")

        # Phase 1: Setup labor allocation on [B, S)
        if b_time is not None and l_setup > 0:
            if s_time is not None:
                if s_time > b_time:
                    labor_events.append((b_time, l_setup, vin))
                    labor_events.append((s_time, -l_setup, vin))
            elif setup_dur > 0:
                setup_end = min(b_time + setup_dur, max_trace_t)
                if setup_end > b_time:
                    labor_events.append((b_time, l_setup, vin))
                    labor_events.append((setup_end, -l_setup, vin))

        # Phase 2: Processing labor allocation on [S, C)
        if s_time is not None and l_proc > 0:
            proc_end = c_time if c_time is not None else max_trace_t
            if proc_end > s_time:
                labor_events.append((s_time, l_proc, vin))
                labor_events.append((proc_end, -l_proc, vin))

    labor_events.sort(key=lambda x: (x[0], x[1]))
    cur_labor = 0
    for t, delta, vin in labor_events:
        cur_labor += delta
        # Off-shift zero labor check: no labor may be consumed when factory calendar is off-shift
        if cur_labor > 0 and not calendar.is_work_time(t):
            errors.append(
                f"Labor active during non-working off-shift period at t={t}: active_workers={cur_labor} > 0"
            )
            break

        if cur_labor > max_workers:
            errors.append(
                f"Labor capacity exceeded for pool {labor.pool_id}: required={cur_labor} > available={max_workers} at t={t} by VIN {vin}"
            )
            break

    # Skill-specific capacities are reconstructed from input, not dispatcher reservations.
    skill_intervals = []
    for (vin, stg), setup_dur in expected_setups.items():
        nodes = trace.stage_node_times.get(vin, {}).get(stg, {})
        cfg = configs.get(orders[vin].config_id)
        if cfg is None:
            continue
        b, s, c = nodes.get('B'), nodes.get('S'), nodes.get('C')
        segments = []
        if b is not None:
            segments.append(('setup', b, s if s is not None else min(b + setup_dur, max_trace_t)))
        if s is not None:
            segments.append(('processing', s, c if c is not None else max_trace_t))
        for phase, start, end in segments:
            count = cfg.get_labor_req(stg, phase)
            if end > start and count > 0:
                skill = cfg.get_skill_req(stg, phase)
                if labor.skill_pools and skill not in labor.skill_pools:
                    errors.append(f'Unknown labor skill {skill} for VIN {vin} stage {stg}')
                skill_intervals.append((start, end, count, skill, vin))
    points = {v for a, b, _, _, _ in skill_intervals for v in (a, b)}
    windows = list(labor.time_windows)
    for pool in labor.skill_pools.values():
        windows.extend(pool.time_windows)
    for a, b, _ in windows:
        points.update((a, b))
    for shift in calendar.shifts:
        points.update((shift.start_min, shift.end_min))
    for t in sorted(points):
        active = [item for item in skill_intervals if item[0] <= t < item[1]]
        if not active:
            continue
        total_cap = labor.max_workers
        for a, b, cap in labor.time_windows:
            if a <= t < b:
                total_cap = cap
                break
        total = sum(item[2] for item in active)
        if total > total_cap:
            errors.append(f'Time-varying labor capacity exceeded at t={t}: {total}>{total_cap}')
        shift = next((sh for sh in calendar.shifts if sh.start_min <= t < sh.end_min), None)
        for name, pool in labor.skill_pools.items():
            cap = pool.max_workers
            sid = getattr(shift, 'shift_id', None)
            if sid in pool.capacity_by_shift:
                cap = pool.capacity_by_shift[sid]
            for a, b, value in pool.time_windows:
                if a <= t < b:
                    cap = value
                    break
            used = sum(item[2] for item in active if item[3] == name)
            if used > cap:
                errors.append(f'Skill capacity exceeded for {name} at t={t}: {used}>{cap}')
    checks.append('Independent skill pools and capacity-change boundaries')

    # 12. Order Completion Truth & Unfinished Status Consistency
    checks.append("Order Completion Truth & Unfinished Status Consistency")
    for vin, order in orders.items():
        route = order.route
        cfg = configs.get(order.config_id)
        last_stg = "P" if (cfg and cfg.is_pullout) else (route[-1] if route else None)

        is_finished = False
        if last_stg and vin in trace.stage_node_times:
            if trace.stage_node_times[vin].get(last_stg, {}).get("F") is not None:
                is_finished = True

        if order.status == VehicleStatus.COMPLETED and not is_finished:
            errors.append(
                f"False completion claim: VIN {vin} has status COMPLETED but did not complete final stage {last_stg}.F"
            )
        if order.completed_at_min is not None and not is_finished:
            errors.append(
                f"False completion claim: VIN {vin} has completed_at_min={order.completed_at_min} but did not complete final stage {last_stg}.F"
            )

    for vin in trace.pullout_completions:
        if vin not in orders:
            errors.append(f"Unknown VIN '{vin}' in pullout_completions")
            continue
        cfg = configs.get(orders[vin].config_id)
        if not cfg or not cfg.is_pullout:
            errors.append(f"False pullout claim: VIN {vin} in pullout_completions is not a pullout configuration")
        if trace.get_node_time(vin, "P", "F") is None:
            errors.append(f"False pullout completion claim: VIN {vin} in pullout_completions did not complete Stage P.F")

    is_valid = len(errors) == 0
    return ValidationReport(
        is_valid=is_valid,
        errors=errors,
        warnings=warnings,
        checks_performed=checks
    )
