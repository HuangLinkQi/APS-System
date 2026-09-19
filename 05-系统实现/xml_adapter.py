"""xml_adapter.py - Adapter for standard XML Interface Payloads and Fullchain Engine.

Parses ApsInterfacePayload XML (B2MML/OAGIS style) from test-suites-xml
into Scenario and internal engine domain objects, and generates relational
database row records for database demonstration.
"""
from __future__ import annotations
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Any, Optional
import copy

from aps_fullchain.schema import (
    Scenario,
    Calendar,
    CalendarShift,
    Resource,
    Buffer,
    Labor,
    LaborSkillPool,
    SupplyDelivery,
    Configuration,
    Order,
    Event,
    VehicleStatus,
    compute_object_hash
)

def parse_xml_to_scenario(xml_text: str) -> Scenario:
    """Parses standard ApsInterfacePayload XML string into an aps_fullchain Scenario object."""
    root = ET.fromstring(xml_text)
    if root.tag != "ApsInterfacePayload":
        raise ValueError(f"Invalid root tag: {root.tag}, expected ApsInterfacePayload")

    # 1. Header
    header = root.find("MessageHeader")
    scenario_id = header.findtext("ScenarioId", "SCENARIO_IMPORTED")
    content_hash = header.findtext("ContentHash", "")

    # 2. Calendar
    cal_elem = root.find("FactoryCalendar")
    cal_name = cal_elem.findtext("CalendarName", "Standard_Calendar")
    horizon_end = int(cal_elem.findtext("HorizonEndMin", "2880"))
    day_len = int(cal_elem.findtext("DayLengthMin", "1440"))
    shifts = []
    for s_elem in cal_elem.findall("Shifts/Shift"):
        shifts.append(CalendarShift(
            shift_id=s_elem.findtext("ShiftId"),
            day_idx=int(s_elem.findtext("DayIndex", "0")),
            start_min=int(s_elem.findtext("StartMin")),
            end_min=int(s_elem.findtext("EndMin"))
        ))
    calendar = Calendar(name=cal_name, shifts=shifts, horizon_end_min=horizon_end, day_length_min=day_len)

    # 3. Resources
    resources = {}
    for r_elem in root.findall("ShopResources/Resource"):
        rid = r_elem.findtext("ResourceId")
        init_color = r_elem.findtext("InitialColor")
        if init_color == "NONE": init_color = None
        init_model = r_elem.findtext("InitialModelType")
        if init_model == "NONE": init_model = None
        resources[rid] = Resource(
            resource_id=rid,
            stage=r_elem.findtext("Stage"),
            capacity=int(r_elem.findtext("Capacity", "1")),
            last_color=init_color,
            last_model_type=init_model
        )

    # 4. Buffers
    buffers = {}
    for b_elem in root.findall("InterStageBuffers/Buffer"):
        bid = b_elem.findtext("BufferId")
        buffers[bid] = Buffer(
            buffer_id=bid,
            edge=b_elem.findtext("Edge"),
            capacity=int(b_elem.findtext("Capacity", "2")),
            transit_time_min=int(b_elem.findtext("TransitTimeMin", "0")),
            discipline=b_elem.findtext("Discipline", "FIFO")
        )

    # 5. Labor
    labor_elem = root.find("LaborSkills")
    pool_id = labor_elem.findtext("PoolId", "GENERAL_LABOR_POOL")
    max_workers = int(labor_elem.findtext("MaxWorkers", "4"))
    skill_pools = {}
    for sp_elem in labor_elem.findall("SkillPools/SkillPool"):
        sp_id = sp_elem.findtext("PoolId")
        sp_name = sp_elem.findtext("SkillName")
        sp_max = int(sp_elem.findtext("MaxWorkers", "2"))
        tw = []
        tw_elem = sp_elem.find("TimeWindows")
        if tw_elem is not None:
            for w in tw_elem.findall("Window"):
                tw.append((int(w.get("startMin")), int(w.get("endMin")), int(w.get("capacity"))))
        skill_pools[sp_name] = LaborSkillPool(pool_id=sp_id, skill_name=sp_name, max_workers=sp_max, time_windows=tw)
    labor = Labor(pool_id=pool_id, max_workers=max_workers, skill_pools=skill_pools)

    # 6. Initial Inventory
    initial_inventory = {}
    for item in root.findall("InitialInventory/StockItem"):
        mat_id = item.findtext("MaterialId")
        qty = int(item.findtext("OnHandQuantity", "0"))
        initial_inventory[mat_id] = qty

    # 7. Deliveries
    deliveries = []
    for d_elem in root.findall("SupplyDeliveries/Delivery"):
        deliveries.append(SupplyDelivery(
            delivery_id=d_elem.findtext("DeliveryId"),
            material_id=d_elem.findtext("MaterialId"),
            quantity=int(d_elem.findtext("Quantity")),
            available_at_min=int(d_elem.findtext("AvailableAtMin")),
            known_at_min=int(d_elem.findtext("KnownAtMin", "0")),
            source_version=d_elem.findtext("SourceVersion", "v1")
        ))

    # 8. Configurations
    configurations = {}
    for c_elem in root.findall("VehicleConfigurations/Configuration"):
        cid = c_elem.findtext("ConfigId")
        model = c_elem.findtext("ModelType")
        color = c_elem.findtext("Color")
        mat_type = c_elem.findtext("MaterialType")
        is_pullout = c_elem.findtext("IsPullout", "false").lower() == "true"
        stages = [s.text for s in c_elem.findall("Stages/Stage")]
        ptimes = {st.get("stage"): int(st.get("durationMin")) for st in c_elem.findall("ProcessTimes/StageTime")}
        boms = {}
        for it in c_elem.findall("BOM/Item"):
            stg = it.get("stage")
            m_id = it.get("materialId")
            q = int(it.get("qty"))
            boms.setdefault(stg, {})[m_id] = q

        setup_matrix = {
            "W": {"SUV->SEDAN": 5, "SEDAN->SUV": 5},
            "P": {f"{c1}->{c2}": 10 for c1 in ["WHITE", "BLACK", "RED", "BLUE", "SILVER"] for c2 in ["WHITE", "BLACK", "RED", "BLUE", "SILVER"] if c1 != c2},
            "A": {}
        }
        if c_elem.find("SetupMatrix") is not None:
            setup_matrix = {"W": {}, "P": {}, "A": {}}
            for setup in c_elem.findall("SetupMatrix/Setup"):
                setup_matrix[setup.get("stage")][setup.get("pair")] = int(setup.get("durationMin"))
        labor_req = {"W": {"setup": 1, "processing": 1}, "P": {"setup": 1, "processing": 1}, "A": {"setup": 0, "processing": 2}}
        skill_req = {"W": {"setup": "WELD_SKILL", "processing": "WELD_SKILL"}, "P": {"setup": "PAINT_SKILL", "processing": "PAINT_SKILL"}, "A": {"setup": "GENERAL", "processing": "ASSY_SKILL"}}

        configurations[cid] = Configuration(
            config_id=cid,
            model_type=model,
            color=color,
            material_type=mat_type,
            is_pullout=is_pullout,
            stages=stages,
            process_times=ptimes,
            process_boms=boms,
            setup_matrix=setup_matrix,
            labor_requirements=labor_req,
            skill_requirements=skill_req
        )

    # 9. Orders
    orders = []
    for o_elem in root.findall("ProductionOrders/Order"):
        cid = o_elem.findtext("ConfigId")
        cfg = configurations.get(cid)
        stgs = list(cfg.stages) if cfg else ["W", "P", "A"]
        orders.append(Order(
            order_id=o_elem.findtext("OrderId"),
            vin=o_elem.findtext("VIN"),
            config_id=cid,
            route=stgs,
            priority=int(o_elem.findtext("Priority", "2")),
            release_at_min=int(o_elem.findtext("ReleaseAtMin", "0")),
            due_at_min=int(o_elem.findtext("DueAtMin", "2880")),
            forecast_bucket=o_elem.findtext("ForecastBucket", "2026-10:F1:COMMON"),
            status=VehicleStatus(o_elem.findtext("Status", "UNRELEASED"))
        ))

    # 10. Forecasts（按订单自带的 ForecastBucket 字符串聚合）
    # 桶键必须与订单一致（month:plant:config），否则净额时订单消费不到对应预测，
    # 会把全部固定需求翻倍成残差虚拟订单，虚增未排车辆。
    forecasts = []
    from aps_fullchain.schema import Forecast
    buckets = {}
    for o in orders:
        b_key = o.forecast_bucket or f"2026-10:FACTORY_1:{o.config_id}"
        buckets[b_key] = buckets.get(b_key, 0) + 1
    for idx, (b_key, qty) in enumerate(buckets.items(), 1):
        parts = b_key.split(":")
        month = parts[0] if len(parts) > 0 else "2026-10"
        plant = parts[1] if len(parts) > 1 else "FACTORY_1"
        cfg_id = parts[2] if len(parts) > 2 else b_key
        forecasts.append(Forecast(
            forecast_id=f"FC_XML_{idx:02d}_{b_key.replace(':', '_')}",
            version=1,
            month=month,
            plant_id=plant,
            config_id=cfg_id,
            quantity=qty,
            published_at_min=0
        ))

    # 11. Events
    events = []
    for ev_elem in root.findall("ExternalEvents/Event"):
        payload = {p.get("key"): p.get("value") for p in ev_elem.findall("Payload/Property")}
        # convert numeric payloads
        for k in ["duration_min", "new_available_at_min", "max_workers"]:
            if k in payload:
                try: payload[k] = int(payload[k])
                except: pass
        ev_type = ev_elem.findtext("EventType")
        # RUSH_ORDER 接口契约：订单以结构化属性表达，还原为 Order 对象供引擎使用；
        # 不接受把 Python 对象 repr 塞进 value 的历史写法（下游无法解析）。
        if ev_type == "RUSH_ORDER":
            vin = str(payload.get("vin", "") or "")
            if not vin:
                raise ValueError(
                    "RUSH_ORDER 事件 " + str(ev_elem.findtext("EventId")) +
                    " 缺少结构化订单属性（vin/config_id/release_at_min/due_at_min/priority/forecast_bucket）"
                )
            payload["order"] = Order(
                order_id=vin,
                vin=vin,
                config_id=str(payload.get("config_id", "") or ""),
                release_at_min=int(payload.get("release_at_min", 0)),
                due_at_min=int(payload.get("due_at_min", 0)),
                priority=int(payload.get("priority", 1)),
                forecast_bucket=str(payload.get("forecast_bucket", "") or ""),
            )
        events.append(Event(
            event_id=ev_elem.findtext("EventId"),
            event_type=ev_elem.findtext("EventType"),
            occurred_at_min=int(ev_elem.findtext("OccurredAtMin")),
            known_at_min=int(ev_elem.findtext("KnownAtMin")),
            payload=payload
        ))

    return Scenario(
        scenario_id=scenario_id,
        calendar=calendar,
        configurations=configurations,
        resources=resources,
        buffers=buffers,
        labor=labor,
        initial_inventory=initial_inventory,
        deliveries=deliveries,
        orders=orders,
        forecasts=forecasts,
        events=events
    )

def scenario_to_display_summary(sc: Scenario) -> Dict[str, Any]:
    """Extracts high-level business metrics from Scenario for UI presentation."""
    order_count = len(sc.orders)
    pullout_count = sum(1 for o in sc.orders if sc.configurations.get(o.config_id, None) and sc.configurations[o.config_id].is_pullout)
    mat_summary = {m: sc.initial_inventory.get(m, 0) for m in sc.initial_inventory}
    for d in sc.deliveries:
        mat_summary[d.material_id] = mat_summary.get(d.material_id, 0) + d.quantity

    models = {}
    colors = {}
    for o in sc.orders:
        c = sc.configurations.get(o.config_id)
        if c:
            models[c.model_type] = models.get(c.model_type, 0) + 1
            colors[c.color] = colors.get(c.color, 0) + 1

    return {
        "scenario_id": sc.scenario_id,
        "orders_total": order_count,
        "pullout_orders": pullout_count,
        "horizon_hours": sc.calendar.horizon_end_min / 60,
        "shifts_count": len(sc.calendar.shifts),
        "models_breakdown": models,
        "colors_breakdown": colors,
        "material_supply_total": mat_summary,
        "events_count": len(sc.events),
        "event_types": [e.event_type for e in sc.events]
    }
