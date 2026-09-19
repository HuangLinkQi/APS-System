"""server_integrated.py - Integrated APS Backend with Database Tables, XML Loaders & Fullchain Algorithms.

Features:
1. Physical Relational DDL Tables (aps_plan_header, aps_plan_detail, aps_plan_candidate,
   aps_order_master, aps_material_ledger, aps_dispatch_receipt, aps_event_replan_log).
2. Standard XML Interface Instances (preset scenarios from test-suites-xml).
3. XML Load & Parse into normalized Scenario Domain Model.
4. Advanced Fullchain Algorithm Execution (EDD, ColorAware, ALNS, Workload Smoothing).
5. Database Records Inspection API (/api/plans/{id}/db-records) for user-side verification.
"""
from __future__ import annotations
import argparse
import copy
import json
import math
import os
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse, unquote

# Ensure local packages are importable
ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xml_adapter import parse_xml_to_scenario, scenario_to_display_summary
from business import extract_master, validate_master, readiness, apply_master, display_input
from aps_fullchain.schema import (
    Scenario,
    OrderPlan,
    SimulationTrace,
    Event,
    compute_object_hash
)
from aps_fullchain.demand import net_demand
from aps_fullchain.aggregate import plan_aggregate
from aps_fullchain.allocation import allocate_orders
from aps_fullchain.policies import get_strategy_a, get_strategy_b, get_strategy_c
from aps_fullchain.policies_advanced import get_strategy_alns, get_strategy_smoothing, run_alns
from aps_fullchain.simulator import ExecutionWorld, simulate
from aps_fullchain.validate import validate_trace
from aps_fullchain.evaluate import evaluate_schedule
from aps_fullchain.lifecycle import LifecycleManager

DB_FILE = Path(os.environ.get("APS_DB", ROOT / "data" / "aps.sqlite3"))
LOCK = threading.RLock()

# =====================================================================
# 生产量级周计划仿真（独立子系统，不覆盖旧验收数据）
# PRODUCTION_SIM_CONTRACT: 依赖 aps-v2/production_sim.py（kernel agent 实现）。
# 本服务对其做防御式惰性导入；模块缺失或签名不符时接口返回明确错误，不破坏旧功能。
#   build_production_scenario(n_vehicles=4200, seed=0)->ProductionScenario dataclass(to_dict())
#   sample_reality(scenario, seed, arrival_sigma, process_cv, failure_expected_per_line,
#                  repair_mean_min)->Reality
#   optimize_nominal_plan(scenario, seed=0)->list[str]     固定 W 名义序列
#   simulate(scenario, reality, strategy, w_fixed_seq=None)->StratResult(to_dict())
#   run_samples(scenario, MCConfig, progress=fn(done,total), on_sample=fn(idx,n_total,rec,tl),
#               should_cancel=fn)->dict
#       策略键: EDD / COLOR_GROUP / LOAD_BALANCE / OPTIMIZED；返回 cancelled/n_completed/running/
#               strategy_stats/sample_results/sample0_timeline
# 自然日=1440min；工作时间每班460min×2班，具体班次窗口以 kernel's calendar.to_dict 提供为准。
# =====================================================================
PROD_DATA_DIR = ROOT / "data" / "production-sim"
PROD_SAMPLES = 1000
PROD_SCENARIO_TITLE = "生产量级周计划 · 4200辆 / 1000次仿真"
PROD_STRATEGIES = ["EDD", "COLOR_GROUP", "LOAD_BALANCE", "OPTIMIZED"]
# 每样本进度事件落盘文件名（单事实来源）：<PROD_DATA_DIR>/<task_id>/progress.jsonl
PROD_PROGRESS_FILE = "progress.jsonl"
PROD_PROGRESS_PAGE_DEFAULT = 250   # production-progress 单次返回事件数上限（分页 bounded）
PROD_PROGRESS_PAGE_MAX = 1000
PROD_STRATEGY_LABELS = {
    "EDD": "交期优先",
    "COLOR_GROUP": "颜色集中",
    "LOAD_BALANCE": "负荷均衡",
    "OPTIMIZED": "综合优化",
}
PID_CFG_PATH = "prod_sample_batch"  # mini progress report key

USERS = {
    "planner-a": {"role": "planner", "factories": ["F1", "FACTORY_1"], "menus": ["plans", "rules"]},
    "planner-b": {"role": "planner", "factories": ["F2", "FACTORY_2"], "menus": ["plans", "rules"]},
    "manager": {"role": "manager", "factories": ["F1", "F2", "FACTORY_1", "FACTORY_2"], "menus": ["plans", "rules", "inbox"]},
    "engineer": {"role": "engineer", "factories": ["F1", "FACTORY_1"], "menus": ["plans", "rules"]},
    "admin": {"role": "admin", "factories": [], "menus": ["system"]},
}

XML_DIR = PROJECT_ROOT / "test-suites-xml"

PRESET_SCENARIOS = [
    {"id": "case_00_micro_benchmark_12", "name": "正常生产 · 12辆基准", "xml_file": "case_00_micro_benchmark_12.xml", "description": "既有12辆小规模演示输入；计算前不预置结果。"},
    {
        "id": "case_01_normal_baseline",
        "name": "场景1：正常基准混流排产 (Normal Baseline)",
        "badge": "基准稳态",
        "tag_color": "green",
        "description": "双班制作业下的标准连续混流排程，包含 SUV 与 SEDAN 车型，验证需求净额守恒与基准流水节拍平稳流动。",
        "xml_file": "case_01_normal_baseline.xml",
        "focus": "需求冲销守恒 / 无缺料 / 标称换色",
        "recommended_strategy": "Strategy_E_ALNS"
    },
    {
        "id": "case_02_supply_delay_stress",
        "name": "场景2：关键物料突发延期断料 (Supply Delay Stress)",
        "badge": "重压扰动",
        "tag_color": "orange",
        "description": "关键电池库存吃紧且到货推迟至 t=250 分钟，总装面临断料停工。验证离散事件引擎在 Node B 拦截待料与在制保护。",
        "xml_file": "case_02_supply_delay_stress.xml",
        "focus": "物料到货延期 / 总装饥饿 / 真实正拖期",
        "recommended_strategy": "Strategy_E_ALNS"
    },
    {
        "id": "case_03_material_shortage",
        "name": "场景3：上游严重缺料断料拦截 (Material Shortage)",
        "badge": "产能受限",
        "tag_color": "red",
        "description": "关键电池全周期仅供给 8 台，面临 7 辆缺料缺口。验证系统如实报告未满足清单，绝不伪造全量完工。",
        "xml_file": "case_03_material_shortage.xml",
        "focus": "物料硬门禁 / 7辆未排拦截 / 优雅退出",
        "recommended_strategy": "Strategy_B_ColorAware"
    },
    {
        "id": "case_04_rush_tight_due",
        "name": "场景4：紧急 VIP 加急插单冲击 (VIP Rush Tight Due)",
        "badge": "加急插单",
        "tag_color": "purple",
        "description": "突发插入 Priority 1 紧交期 VIP 订单，总需求扩增至 17 辆。验证非抢占混流队列中的排队优先置换与交期传递。",
        "xml_file": "case_04_rush_tight_due.xml",
        "focus": "未冲销突发插单 / 排队优先 / 紧交期传导",
        "recommended_strategy": "Strategy_B_ColorAware"
    },
    {
        "id": "case_05_order_cancellation",
        "name": "场景5：在制保护与订单撤单响应 (Order Cancellation)",
        "badge": "撤单扰动",
        "tag_color": "blue",
        "description": "未开工车辆撤销排产授权，在制品打标 CANCELLED_IN_WIP 保护继续流转出线。验证在制资产完备保护。",
        "xml_file": "case_05_order_cancellation.xml",
        "focus": "未开工授权撤除 / 在制保护 / 填补空位",
        "recommended_strategy": "Strategy_A_EDD"
    },
    {
        "id": "adv_scenario_16_bottleneck_color_alum",
        "name": "场景6：小颜色交织与铝车身工艺双瓶颈 (Color & Alum Bottleneck)",
        "badge": "工艺瓶颈",
        "tag_color": "cyan",
        "description": "铝车身超 50%（焊装耗时翻倍）+ 涂装 5 色高频跳变。验证 ALNS 算法与涂装颜色感知派工在减少换色上的显著优势。",
        "xml_file": "adv_scenario_16_bottleneck_color_alum.xml",
        "focus": "涂装换色大幅降低 / 正阻塞压减 / ALNS优化",
        "recommended_strategy": "Strategy_E_ALNS"
    }
]

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

@contextmanager
def connect():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()

def init_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        # 1. Compatibility tables
        c.executescript('''
        CREATE TABLE IF NOT EXISTS catalogs(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, time TEXT, actor TEXT, action TEXT, object_id TEXT);
        ''')

        # 2. Advanced Relational DDL Tables (from Section 4 of Tech Spec)
        c.executescript('''
        CREATE TABLE IF NOT EXISTS aps_plan_header (
            plan_id TEXT PRIMARY KEY,
            plan_name TEXT NOT NULL,
            plan_type TEXT NOT NULL,
            plant_code TEXT NOT NULL,
            status TEXT NOT NULL,
            version INT NOT NULL DEFAULT 1,
            parent_plan_id TEXT,
            input_hash TEXT NOT NULL,
            selected_candidate_id TEXT,
            created_by TEXT NOT NULL,
            approved_by TEXT,
            approved_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS aps_plan_detail (
            detail_id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            vin TEXT NOT NULL,
            order_id TEXT NOT NULL,
            config_id TEXT NOT NULL,
            assigned_day INT NOT NULL,
            sequence_position INT NOT NULL,
            stage TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            node_b_min INT NOT NULL,
            node_s_min INT NOT NULL,
            node_c_min INT NOT NULL,
            node_f_min INT NOT NULL,
            setup_duration_min INT NOT NULL,
            process_duration_min INT NOT NULL,
            blocked_duration_min INT NOT NULL,
            execution_status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS aps_plan_candidate (
            candidate_id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            strategy_name TEXT NOT NULL,
            is_valid INT NOT NULL,
            unmet_count INT NOT NULL,
            true_weighted_tardiness_min INT NOT NULL,
            total_blocked_min INT NOT NULL,
            color_switches INT NOT NULL,
            makespan_min INT NOT NULL,
            stability_penalty INT NOT NULL,
            composite_score REAL NOT NULL,
            is_selected INT NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS aps_order_master (
            vin TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            plant_code TEXT NOT NULL,
            config_id TEXT NOT NULL,
            model_type TEXT NOT NULL,
            color TEXT NOT NULL,
            material_type TEXT NOT NULL,
            is_pullout INT NOT NULL,
            priority INT NOT NULL,
            due_at_min INT NOT NULL,
            forecast_bucket TEXT,
            lifecycle_status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS aps_material_ledger (
            ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
            material_id TEXT NOT NULL,
            plant_code TEXT NOT NULL,
            batch_no TEXT NOT NULL,
            on_hand_qty INT NOT NULL,
            available_at_min INT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS aps_dispatch_receipt (
            receipt_id TEXT PRIMARY KEY,
            baseline_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            plan_version INT NOT NULL,
            target_shop TEXT NOT NULL,
            receipt_status TEXT NOT NULL,
            signature_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS aps_event_replan_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            epoch_min INT NOT NULL,
            trigger_event_types TEXT NOT NULL,
            parent_plan_id TEXT NOT NULL,
            child_plan_id TEXT NOT NULL,
            receipt_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        ''')

        # ---- 生产量级周计划仿真（独立于旧验收表，不覆盖旧数据）----
        c.executescript('''
        CREATE TABLE IF NOT EXISTS prod_simulation (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            factory TEXT NOT NULL,
            plan_type TEXT NOT NULL DEFAULT 'PRODUCTION_WEEKLY',
            status TEXT NOT NULL DEFAULT 'draft',
            scenario TEXT,          -- 名义场景摘要 + 事实/产能来源/扰动假设（summary only）
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS prod_task (
            id TEXT PRIMARY KEY,
            sim_id TEXT NOT NULL,
            status TEXT NOT NULL,
            progress TEXT,          -- 小型进度JSON {completed,total,started_at,elapsed,cancel_requested}
            req_conf TEXT,          -- 请求配置摘要 {seed,n,strategies}
            result_ref TEXT,        -- 完成后的结果文件引用(相对PROD_DATA_DIR) 或 取消时置空
            error TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT
        );
        ''')

def load_payload(c, table: str, identifier: str) -> dict:
    r = c.execute(f"SELECT payload FROM {table} WHERE id=?", (identifier,)).fetchone()
    if not r:
        raise HTTPError(404, "记录不存在或不在授权范围")
    return json.loads(r["payload"])

def save_payload(c, table: str, obj: dict):
    c.execute(f"UPDATE {table} SET payload=? WHERE id=?", (json.dumps(obj, ensure_ascii=False, allow_nan=False), obj["id"]))

def audit_log(c, actor: str, action: str, obj_id: str):
    c.execute("INSERT INTO audit(time,actor,action,object_id) VALUES(?,?,?,?)", (now_iso(), actor, action, obj_id))

class HTTPError(Exception):
    def __init__(self, code: int, message: str):
        self.code, self.message = code, message


def run_fullchain_solve(scenario: Scenario, progress_fn=None) -> Dict[str, Any]:
    """Runs the fullchain multi-algorithm suite: EDD, ColorAware, ALNS, and Workload Smoothing."""
    if progress_fn: progress_fn("需求净额与冲销")
    from aps_fullchain.demand import net_demand
    from aps_fullchain.aggregate import plan_aggregate
    from aps_fullchain.allocation import allocate_orders
    from aps_fullchain.policies import get_strategy_a, get_strategy_b, get_strategy_c
    from aps_fullchain.policies_advanced import get_strategy_alns, get_strategy_smoothing, run_alns

    # 1. Snapshot at t=0
    day_len = scenario.calendar.day_length_min or 1440
    init_world = ExecutionWorld(
        calendar=scenario.calendar,
        configurations=scenario.configurations,
        resources=scenario.resources,
        buffers=scenario.buffers,
        labor=scenario.labor,
        initial_inventory=scenario.initial_inventory,
        deliveries=scenario.deliveries,
        orders={o.vin: o.clone() for o in scenario.orders},
        stage_policies=get_strategy_a(),
        horizon_end_min=scenario.calendar.horizon_end_min
    )
    from aps_fullchain.rolling import make_snapshot
    snapshot_t0 = make_snapshot(init_world, 0, scenario.events, scenario.deliveries, scenario.forecasts)

    # 2. Demand Netting
    if progress_fn: progress_fn("聚合供需配额计算")
    ledger = net_demand(snapshot_t0)
    all_netted_orders = [o.clone() for o in ledger.firm_orders + ledger.synthetic_orders]
    snapshot_t0.orders = [o.clone() for o in all_netted_orders]
    scenario.orders = [o.clone() for o in all_netted_orders]

    # 3. Aggregate Plan
    aggr_plan = plan_aggregate(snapshot_t0, ledger)

    # 4. Multi-Strategy Generation
    if progress_fn: progress_fn("周/日逐车排程与多策略派工")
    plan_a, _ = allocate_orders(snapshot_t0, aggr_plan, policy_name="STRATEGY_A_EDD")
    plan_b, _ = allocate_orders(snapshot_t0, aggr_plan, policy_name="STRATEGY_B_COLOR_AWARE")
    plan_d, _ = allocate_orders(snapshot_t0, aggr_plan, policy_name="STRATEGY_D_SMOOTHING")

    # ALNS candidate
    if progress_fn: progress_fn("自适应大邻域搜索(ALNS)迭代优化")
    alns_res = run_alns(
        snapshot_t0, scenario, base_plan=plan_b, max_iterations=25, time_budget_sec=2.0, seed=42
    )
    alns_plan = alns_res.best_plan
    alns_bundle = alns_res.best_bundle

    strategies = [
        ("Strategy_A_EDD", plan_a, get_strategy_a()),
        ("Strategy_B_ColorAware", plan_b, get_strategy_b()),
        ("Strategy_D_Smoothing", plan_d, get_strategy_smoothing()),
        ("Strategy_E_ALNS", alns_plan, alns_bundle)
    ]

    evaluated_candidates = {}
    candidate_records = []
    best_candidate_name = "Strategy_A_EDD"
    best_rank = (999, 999999, 999999, 999999, 999999, 999999)

    for idx, (s_name, s_plan, s_bundle) in enumerate(strategies):
        if progress_fn: progress_fn(f"离散事件物理仿真与正阻塞核验: {s_name}")
        sim_trace = simulate(snapshot_t0, s_plan, s_bundle, stop_at_min=scenario.calendar.horizon_end_min)
        val_rep = validate_trace(scenario, sim_trace)
        metrics = evaluate_schedule(scenario, sim_trace, s_plan)

        # Build UI compatible schedule & events
        schedule_sequence = []
        for pos, rec in enumerate(sim_trace.records):
            if rec.stage == "W" and rec.node == "B":
                vin = rec.vin
                o = next((ord for ord in scenario.orders if ord.vin == vin), None)
                cfg = scenario.configurations.get(o.config_id if o else "")
                # 按期口径与 evaluate_schedule 一致：末道工序 F 时刻对比 due_at_min；
                # 天粒度按引擎 day_length 归一（不再固定1440），未完工车辆记 None 而非 0
                lateness_days = None
                if o:
                    last_stage = o.route[-1] if getattr(o, "route", None) else None
                    stage_times = sim_trace.stage_node_times.get(vin) if last_stage else None
                    nodes = stage_times.get(last_stage) if stage_times else None
                    if nodes and "F" in nodes:
                        lateness_days = max(0, nodes["F"] // day_len - o.due_at_min // day_len)
                schedule_sequence.append({
                    "position": len(schedule_sequence) + 1,
                    "day": o.assigned_day if o else 0,
                    "car_id": vin,
                    "order_id": o.order_id if o else vin,
                    "model": cfg.model_type if cfg else "SUV",
                    "color": cfg.color if cfg else "WHITE",
                    "material": cfg.material_type if cfg else "STEEL",
                    "release_day": o.release_at_min // day_len if o else 0,
                    "due_day": o.due_at_min // day_len if o else 1,
                    "priority": o.priority if o else 2,
                    "lateness_days": lateness_days
                })

        events_out = []
        for rec in sim_trace.records:
            events_out.append({
                "car_id": rec.vin,
                "order_id": rec.order_id,
                "stage": rec.stage,
                "node": rec.node,
                "timestamp_min": rec.timestamp_min,
                "resource_id": rec.resource_id,
                "details": rec.details
            })

        # === 未完成口径（以 evaluate 的有效需求/取消语义为准）===
        # 有效需求 = 净额后的订单集合（firm + 未启动残差合成，已剔除"取消且未开工"订单）
        # 完成集合 = 到达末道工序 F 的车辆；未完工 = 有效需求 − 真正完成集合。
        # 任意未完工（含已分配/已开工但未完工）都计入并阻断提交，不以未排列表判空为准。
        # 说明：sim_trace.unmet_vins 由 simulate 在结束时填为"未完工+未取消"的全部车辆，
        # 并非分配阶段被拒集合，不能作为"未排入"依据；授权集合取自 s_plan.daily_allocations，
        # 开工状态取自实际节点记录（stage_node_times）。
        allocated_vins = {v for day in s_plan.daily_allocations.values() for v in day}
        active_vins = {o.vin for o in scenario.orders}
        completed_vins = set()
        for v, stages in sim_trace.stage_node_times.items():
            o = next((x for x in scenario.orders if x.vin == v), None)
            if o and getattr(o, "route", None) and o.route[-1] in stages and "F" in stages[o.route[-1]]:
                completed_vins.add(v)
        unfinished_vins = sorted(active_vins - completed_vins)
        unfinished_count = len(unfinished_vins)
        unfinished_details = []
        for v in unfinished_vins:
            ord_obj = next((x for x in scenario.orders if x.vin == v), None)
            started = v in sim_trace.stage_node_times and bool(sim_trace.stage_node_times[v])
            if v not in allocated_vins:
                kind = "未排入"
                reason = "分配阶段未排入（未满足排程配额或材料/产能约束）；未编造具体缺料归因"
            elif not started:
                kind = "已排未开工"
                reason = "已分配指挥序列但未能在计算时域内开工"
            else:
                kind = "已排未完成(已开工未完工)"
                reason = "已投入序列并开工但未能在计算时域内到达末道工序完工（在制未出线）"
            unfinished_details.append({
                "vin": v,
                "order_id": ord_obj.order_id if ord_obj else v,
                "config": ord_obj.config_id if ord_obj else '',
                "kind": kind,
                "reason": reason
            })
        unallocated_count = sum(1 for d in unfinished_details if d["kind"] == "未排入")
        allocated_unfinished_count = unfinished_count - unallocated_count
        unscheduled_details = [d for d in unfinished_details if d["kind"] == "未排入"]
        submit_blockers = []
        if not val_rep.is_valid:
            submit_blockers.append("独立模型校验未通过（" + "；".join(val_rep.errors[:3]) + "）")
        if unfinished_count:
            shown = "、".join(d["vin"] for d in unfinished_details[:8]) + ("…" if unfinished_count > 8 else "")
            submit_blockers.append(f"存在 {unfinished_count} 台未完工订单（未排 {unallocated_count} 台、已排未完工 {allocated_unfinished_count} 台）：" + shown)

        cand_data = {
            "name": s_name,
            "status": "feasible" if (val_rep.is_valid and unfinished_count == 0) else "infeasible",
            "strategy": {"assign": "fullchain_aggregate", "sequence": s_name},
            "schedule": {
                "days": [{"day": d, "assigned_cars": len(vins)} for d, vins in s_plan.daily_allocations.items()],
                "sequence": schedule_sequence
            },
            "events": events_out,
            "blocked_durations": sim_trace.blocked_durations,
            "metrics": metrics,
            "submittable": bool(val_rep.is_valid and unfinished_count == 0),
            "submit_blockers": submit_blockers,
            "validation": {
                "valid": val_rep.is_valid,
                "passed": val_rep.is_valid and unfinished_count == 0,
                "violations": [{"rule": e, "severity": "error"} for e in val_rep.errors],
                "checks": [{"rule": c, "passed": True} for c in val_rep.checks_performed]
            },
            "unscheduled": [d["vin"] for d in unscheduled_details],
            "unscheduled_details": unscheduled_details,
            "unfinished": unfinished_vins,
            "unfinished_details": unfinished_details,
            "unfinished_count": unfinished_count,
            "raw_trace": sim_trace
        }
        evaluated_candidates[s_name] = cand_data

        # Rank evaluation
        r_rank = metrics.get("lexicographical_rank", (0, 0, 0, 0, 0, 0))
        if r_rank < best_rank and val_rep.is_valid:
            best_rank = r_rank
            best_candidate_name = s_name

    if progress_fn: progress_fn("方案优选与多指标综合评价")

    # Baseline is Strategy A, Optimized is the winning candidate
    baseline_cand = evaluated_candidates["Strategy_A_EDD"]
    optimized_cand = evaluated_candidates[best_candidate_name]

    return {
        "engine": "aps_fullchain_v1_integrated",
        "scenario_id": scenario.scenario_id,
        "calendar_day_length_min": scenario.calendar.day_length_min,
        "baseline": baseline_cand,
        "optimized": optimized_cand,
        "candidates": evaluated_candidates,
        "selected_strategy": best_candidate_name,
        "validation": optimized_cand["validation"],
        "metrics": optimized_cand["metrics"],
        "schedule": optimized_cand["schedule"],
        "events": optimized_cand["events"],
        "unscheduled": optimized_cand["unscheduled"],
        "blocked_durations": optimized_cand["blocked_durations"]
    }


def worker_fullchain(task_id: str):
    """Background worker executing fullchain simulation and inserting into DB relational tables."""
    def progress_callback(stage: str):
        with LOCK, connect() as c:
            t = load_payload(c, "tasks", task_id)
            t.update(status="running", stage=stage)
            t["progress_log"].append({"time": now_iso(), "stage": stage})
            save_payload(c, "tasks", t)

    try:
        with LOCK, connect() as c:
            t = load_payload(c, "tasks", task_id)
            plan = load_payload(c, "plans", t["plan_id"])
            input_data = t["input"]

        # Check if input is XML or contains raw_xml
        scenario = None
        if isinstance(input_data, dict) and "raw_xml" in input_data:
            scenario = parse_xml_to_scenario(input_data["raw_xml"])
        elif isinstance(input_data, dict) and "scenario_id" in input_data:
            raise ValueError("场景缺少完整输入，请重新选择场景")
        else:
            # Standard legacy engine solve fallback if not a fullchain scenario
            from engine import solve
            progress_callback("输入检查与基线求解")
            result = solve(input_data, progress=progress_callback)
            scenario = None

        if scenario is not None:
            result = run_fullchain_solve(scenario, progress_fn=progress_callback)

            # Insert into Physical DDL Relational Tables
            with LOCK, connect() as c:
                plan_id = plan["id"]
                now_str = now_iso()
                
                # 1. aps_plan_header
                c.execute("""
                    INSERT OR REPLACE INTO aps_plan_header 
                    (plan_id, plan_name, plan_type, plant_code, status, version, input_hash, selected_candidate_id, created_by, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    plan_id, plan["name"], plan["type"], plan["factory"], "READY", 1,
                    plan.get("input_hash", "HASH_001"), result["selected_strategy"],
                    plan.get("created_by", "planner-a"), plan["created_at"], now_str
                ))

                # 2. aps_order_master
                for o in scenario.orders:
                    cfg = scenario.configurations.get(o.config_id)
                    c.execute("""
                        INSERT OR REPLACE INTO aps_order_master
                        (vin, order_id, plant_code, config_id, model_type, color, material_type, is_pullout, priority, due_at_min, forecast_bucket, lifecycle_status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        o.vin, o.order_id, plan["factory"], o.config_id,
                        cfg.model_type if cfg else "SUV",
                        cfg.color if cfg else "WHITE",
                        cfg.material_type if cfg else "STEEL",
                        1 if (cfg and cfg.is_pullout) else 0,
                        o.priority, o.due_at_min, o.forecast_bucket,
                        "PLANNED", now_str
                    ))

                # 3. aps_material_ledger
                for mat_id, qty in scenario.initial_inventory.items():
                    c.execute("""
                        INSERT INTO aps_material_ledger
                        (material_id, plant_code, batch_no, on_hand_qty, available_at_min, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (mat_id, plan["factory"], "INIT_BATCH", qty, 0, now_str))

                # 4. aps_plan_candidate
                for c_name, cand in result["candidates"].items():
                    m = cand["metrics"]
                    c.execute("""
                        INSERT OR REPLACE INTO aps_plan_candidate
                        (candidate_id, plan_id, strategy_name, is_valid, unmet_count, true_weighted_tardiness_min,
                         total_blocked_min, color_switches, makespan_min, stability_penalty, composite_score, is_selected, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        f"CAND_{plan_id}_{c_name}", plan_id, c_name,
                        1 if cand["validation"]["valid"] else 0,
                        m.get("unmet_count", 0),
                        m.get("true_weighted_tardiness_min", 0),
                        m.get("total_blocked_min", 0),
                        m.get("color_switches", 0),
                        m.get("makespan_min", 0),
                        m.get("stability_penalty", 0),
                        m.get("composite_score", 0.0),
                        1 if c_name == result["selected_strategy"] else 0,
                        now_str
                    ))

                # 5. aps_plan_detail (for winning candidate)
                opt = result["optimized"]
                opt_raw = opt.get("raw_trace")
                if opt_raw and hasattr(opt_raw, "stage_node_times"):
                    for vin, stages in opt_raw.stage_node_times.items():
                        ord_obj = next((x for x in scenario.orders if x.vin == vin), None)
                        for stg, nodes in stages.items():
                            b_m = nodes.get("B", 0)
                            s_m = nodes.get("S", b_m)
                            c_m = nodes.get("C", s_m)
                            f_m = nodes.get("F", c_m)
                            c.execute("""
                                INSERT INTO aps_plan_detail
                                (plan_id, candidate_id, vin, order_id, config_id, assigned_day, sequence_position,
                                 stage, resource_id, node_b_min, node_s_min, node_c_min, node_f_min,
                                 setup_duration_min, process_duration_min, blocked_duration_min, execution_status, created_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                plan_id, f"CAND_{plan_id}_{result['selected_strategy']}",
                                vin, ord_obj.order_id if ord_obj else vin,
                                ord_obj.config_id if ord_obj else "CFG",
                                ord_obj.assigned_day if (ord_obj and ord_obj.assigned_day is not None) else 0,
                                1, stg, f"{stg}_LINE",
                                b_m, s_m, c_m, f_m,
                                s_m - b_m, c_m - s_m, f_m - c_m,
                                "SCHEDULED", now_str
                            ))

        # Update JSON tasks and plan payloads for legacy compatibility
        with LOCK, connect() as c:
            t = load_payload(c, "tasks", task_id)
            # Strip raw_trace before serialization
            clean_res = copy.deepcopy(result)
            if "candidates" in clean_res:
                for cand in clean_res["candidates"].values():
                    cand.pop("raw_trace", None)
            if "optimized" in clean_res:
                clean_res["optimized"].pop("raw_trace", None)
            if "baseline" in clean_res:
                clean_res["baseline"].pop("raw_trace", None)

            t.update(status="completed", stage="计算完成", result=clean_res, finished_at=now_iso())
            save_payload(c, "tasks", t)

            p = load_payload(c, "plans", t["plan_id"])
            p.update(status="ready", result=clean_res, updated_at=now_iso())
            save_payload(c, "plans", p)

    except Exception as e:
        import traceback
        traceback.print_exc()
        with LOCK, connect() as c:
            t = load_payload(c, "tasks", task_id)
            t.update(status="failed", stage="计算失败", error=str(e), finished_at=now_iso())
            save_payload(c, "tasks", t)
            p = load_payload(c, "plans", t["plan_id"])
            p.update(status="failed", updated_at=now_iso())
            save_payload(c, "plans", p)


# =====================================================================
# 生产量级周计划仿真 · 后台引擎与持久化
# 说明：所有统计结果仅为研究仿真，不具备生产指令语义；不允许沿旧 /api/plans .../submit 提交。
# 样本与结果以 JSONL/JSON 落盘到 PROD_DATA_DIR/<task_id>/，DB 仅存 summary 引用，避免列表巨量结果。
# =====================================================================

# =====================================================================
# 生产量级周计划仿真 · 后台引擎与持久化
# 说明：所有统计结果仅为研究仿真，不具备生产指令语义；不允许沿旧 /api/plans .../submit 提交。
# 样本与结果以 JSONL/JSON 落盘到 PROD_DATA_DIR/<task_id>/，DB 仅存 summary 引用，避免列表巨量结果。
# =====================================================================

def prod_task_dir(task_id: str) -> Path:
    d = PROD_DATA_DIR / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pstats(values):
    """Return {mean,std,count,p05,p95,ci95_low,ci95_high} for a list of floats (None filtered)."""
    import statistics
    vals = [float(v) for v in values if v is not None]
    n = len(vals)
    if n == 0:
        return {"count": 0, "mean": None, "std": None, "p05": None, "p95": None, "ci95_low": None, "ci95_high": None}
    mean = statistics.fmean(vals)
    if n == 1:
        return {"count": n, "mean": mean, "std": 0.0, "p05": mean, "p95": mean,
                "ci95_low": mean, "ci95_high": mean}
    sd = statistics.stdev(vals)
    sorted_vals = sorted(vals)
    def pct(p):
        k = (n - 1) * p
        lo, hi = int(k), min(n - 1, int(k) + 1)
        return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)
    z = 1.96
    half = z * sd / (n ** 0.5)
    return {"count": n, "mean": mean, "std": sd, "p05": pct(0.05), "p95": pct(0.95),
            "ci95_low": mean - half, "ci95_high": mean + half}


def _import_production_sim():
    try:
        import production_sim  # type: ignore
        return production_sim
    except Exception as e:
        raise HTTPError(500, "production_sim 尚未就绪（实现方接入中）：" + str(e))


def prod_create_sim(c, user: dict, body: dict) -> dict:
    if user["role"] != "planner":
        raise HTTPError(403, "仅计划员可创建生产量级仿真")
    factory = body.get("factory")
    if factory not in user["factories"]:
        raise HTTPError(403, "工厂未授权")
    seed = int(body.get("seed") or 0)
    n_vehicles = int(body.get("n_vehicles") or 4200)
    name = (body.get("name") or PROD_SCENARIO_TITLE).strip()[:120] or PROD_SCENARIO_TITLE
    ps = _import_production_sim()
    scenario = ps.build_production_scenario(n_vehicles=n_vehicles, seed=seed)
    s_id = uuid.uuid4().hex[:12]
    params = {
        "seed": seed, "n_vehicles": n_vehicles,
        "arrival_sigma": float(body.get("arrival_sigma") or 720.0),
        "process_cv": float(body.get("process_cv") or 0.08),
        "failure_expected_per_line": float(body.get("failure_expected_per_line") or 1.1),
        "repair_mean_min": float(body.get("repair_mean_min") or 40.0),
    }
    summary = worker_scenario_summary(scenario, params=params)
    now = now_iso()
    c.execute(
        "INSERT INTO prod_simulation (id,name,factory,plan_type,status,scenario,created_by,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (s_id, name, factory, "PRODUCTION_WEEKLY", "draft",
         json.dumps(summary, ensure_ascii=False), user["user"], now, now))
    return {"id": s_id, "name": name, "factory": factory, "plan_type": "PRODUCTION_WEEKLY",
            "status": "draft", "scenario": summary, "created_by": user["user"], "created_at": now, "updated_at": now}


def worker_scenario_summary(scenario, params: Optional[dict] = None) -> dict:
    """Converts a ProductionScenario dataclass to a small persisted summary (keeps DB small)."""
    d = scenario.to_dict() if hasattr(scenario, "to_dict") else {}
    params = params or {}
    configs = d.get("configs", {})
    # 产能来源：各配置在各工序的等效加工时长（瓶颈口径节拍）
    capacity_sources = [
        {"config": cid, "model": cfg.get("model"), "color": cfg.get("color"),
         "takt_sec": {st: cfg.get("process_sec", {}).get(st) for st in ("W", "P", "A")}}
        for cid, cfg in configs.items()
    ]
    orders = getattr(scenario, "orders", None)
    orders_sample = []
    if orders:
        for o in orders[:10]:
            orders_sample.append({"vin": o.vin, "config": o.config, "model": o.model,
                                  "color": o.color, "priority": o.priority,
                                  "release_at_sec": round(o.release_at, 1), "due_at_sec": round(o.due_at, 1)})
    disturbance = [
        {"hypothesis": "到货延迟", "param": "arrival_sigma", "value_sec": round(params.get("arrival_sigma", 720.0), 1)},
        {"hypothesis": "加工耗时波动", "param": "process_cv", "value": round(params.get("process_cv", 0.08), 3)},
        {"hypothesis": "故障(每线期望台次)", "param": "failure_expected_per_line", "value": round(params.get("failure_expected_per_line", 1.1), 3)},
        {"hypothesis": "平均修复时长", "param": "repair_mean_min", "value_min": round(params.get("repair_mean_min", 40.0), 1)},
    ]
    return {
        "title": PROD_SCENARIO_TITLE,
        "scenario_id": d.get("scenario_id"),
        "n_vehicles": d.get("n_orders"),
        "seed": params.get("seed", 0),
        "calendar": d.get("calendar"),
        "configs": configs,
        "deliveries_sample": d.get("deliveries", [])[:10],
        "deliveries_count": len(d.get("deliveries", [])),
        "initial_stock": d.get("initial_stock", {}),
        "buffer_cap": d.get("buffer_cap", {}),
        "n_pullout": d.get("n_pullout", 0),
        "orders_count": len(orders) if orders else d.get("n_orders", 0),
        "orders_sample": orders_sample,
        "capacity_sources": capacity_sources,
        "disturbance": disturbance,
        "workshops": ["W", "P", "A"],
        "calibration": "study-sim",
        "caveats": [
            "秒级等效瓶颈流水 DES（3车间串行+有限缓冲+正阻塞不丢车），非工位级数字孪生，非工厂实测精度。",
            "自然日=1440min，工作时间为每班460min×2班；具体班次窗口以 kernel calendar.to_dict 为准。",
        ],
    }


def _load_samples_rows(task_id: str) -> list:
    """Read all persisted sample rows from <task>/samples.jsonl (small; stats on demand)."""
    p = prod_task_dir(task_id) / "samples.jsonl"
    if not p.exists():
        return []
    rows = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _stream_progress(task_id: str, done: int, current_means: dict):
    with LOCK, connect() as c:
        t = _load_prod_task_row(c, task_id)
        prog = dict(t.get("progress") or {})
        prog["completed"] = done
        prog["updated_at"] = now_iso()
        prog["current_means"] = current_means
        c.execute("UPDATE prod_task SET progress=? WHERE id=?",
                  (json.dumps(prog, ensure_ascii=False), task_id))


def _persist_full_samples(task_id: str, raw: dict, strategies: list):
    """Fallback path when kernel has no streaming callbacks: write full sample_rows to JSONL once."""
    rows = raw.get("sample_results") or []
    write_rows = []
    for r in rows:
        write_rows.append({
            "sample_idx": r.get("sample_idx"),
            "seed": r.get("seed"),
            "reality": r.get("reality"),
            "strategies": r.get("strategies"),
        })
    _write_jsonl(task_id, "samples.jsonl", write_rows)


def _finish_failed(task_id: str, state: dict, error: str, t0: float):
    """任务失败时落一条终态进度事件（保留已完成计数；不重放、不模拟）。"""
    try:
        append_prod_progress_event(task_id, {
            "seq": int(state.get("done") or 0), "done": int(state.get("done") or 0),
            "total": int(state.get("total") or PROD_SAMPLES),
            "elapsed_sec": int(time.time() - t0), "failed": int(state.get("failed") or 0),
            "means": state.get("means") or {}, "phase": "failed", "ts": now_iso(),
        })
    except Exception:
        pass  # 进度日志写失败不得掩盖原始错误


def worker_production(task_id: str):
    """Plan-bound production worker: n-sample x 4-strategy via run_samples streaming.

    Task lives in the existing 'tasks' table (plan_id-linked). MC config is derived from the
    plan's production input (scenario summary + disturbance params). on_sample streams each
    sample to JSONL and updates lightweight progress. Any real TypeError/exception fails the
    task (kept as failed); there is no ValueType fallback that swallows real errors.
    """
    def set_task(status, progress=None, result_ref=None, error=None):
        with LOCK, connect() as c:
            row = c.execute("SELECT plan_id,payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return
            t = json.loads(row[1]); t["status"] = status
            if progress is not None:
                t["progress"] = progress
            if result_ref is not None:
                t["result_ref"] = result_ref
            if error is not None:
                t["error"] = error
            t["finished_at"] = now_iso()
            c.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(t, ensure_ascii=False), task_id))

    def _cancel_req():
        with LOCK, connect() as c:
            row = c.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return False
            t = json.loads(row[0])
            return bool((t.get("progress") or {}).get("cancel_requested"))

    def should_cancel():
        return _cancel_req()

    # 进度事件所需的最小状态（异常路径也要能落盘终态事件，故在 try 之前初始化）
    _t0 = time.time()
    state = {"done": 0, "failed": 0, "means": {}, "total": PROD_SAMPLES}

    try:
        with LOCK, connect() as c:
            row = c.execute("SELECT plan_id,payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise HTTPError(404, "任务不存在")
            plan_id = row[0]; task = json.loads(row[1])
            conf = task.get("conf") or {}
            n = int(conf.get("n") or PROD_SAMPLES)
            strategies = conf.get("strategies") or PROD_STRATEGIES
            plan = load_payload(c, "plans", plan_id)
            prod = ((plan.get("input") or {}).get("scenario")) or {}
            seed = int(prod.get("seed") or 0)
            n_vehicles = int(prod.get("n_vehicles") or 4200)
            dist = (plan.get("input") or {}).get("disturbance") or []

            def find_param(nm):
                for it in dist:
                    if it.get("param") == nm:
                        if it.get("value") is not None:
                            return it.get("value")
                        if it.get("value_sec") is not None:
                            return it.get("value_sec")
                        return it.get("value_min")
                return None
            arrival_sigma = float(find_param("arrival_sigma") or 720.0)
            process_cv = float(find_param("process_cv") or 0.08)
            failure_expected_per_line = float(find_param("failure_expected_per_line") or 1.1)
            repair_mean_min = float(find_param("repair_mean_min") or 40.0)

        ps = _import_production_sim()
        scenario = ps.build_production_scenario(n_vehicles=n_vehicles, seed=seed)
        _t0 = time.time()
        state["total"] = n
        set_task("running", {"completed": 0, "total": n, "cancel_requested": False, "current_means": {},
                             "failed": 0, "elapsed_sec": 0, "phase": "nominal", "started_at": now_iso()})
        # 进度事件单事实来源：seq=0 表示进入运行，尚未产生任何样本（名义方案选优阶段）
        append_prod_progress_event(task_id, {
            "seq": 0, "done": 0, "total": n, "elapsed_sec": 0, "failed": 0,
            "means": {st: None for st in strategies}, "phase": "nominal", "ts": now_iso(),
        })

        samples_path = prod_task_dir(task_id) / "samples.jsonl"
        fh = open(samples_path, "w", encoding="utf-8")
        run_acc = {st: {"sum": 0.0, "n": 0} for st in strategies}
        fail_acc = {"n": 0}

        def on_sample(idx, n_total, sample_record, timelines_or_none):
            fh.write(json.dumps(sample_record, ensure_ascii=False) + "\n")
            fh.flush()
            if timelines_or_none is not None:
                _write_json({"timelines": timelines_or_none}, task_id, "sample0_timeline.json")
            st_map = (sample_record or {}).get("strategies", {}) or {}
            if any((st_map.get(st) or {}).get("failed") is True for st in strategies):
                fail_acc["n"] += 1
            for st in strategies:
                r = st_map.get(st) or {}
                if r.get("failed") is True:
                    continue
                cr = r.get("completion_rate")
                if cr is not None:
                    run_acc[st]["sum"] += cr
                    run_acc[st]["n"] += 1
            done = idx + 1
            means = {st: _finite_or_none(run_acc[st]["sum"] / run_acc[st]["n"]) if run_acc[st]["n"] else None
                     for st in strategies}
            elapsed = int(time.time() - _t0)
            # 1) 真实样本完成事件先落盘（进度单事实来源；文件IO在锁外，不阻塞/不长持锁）
            append_prod_progress_event(task_id, {
                "seq": done, "done": done, "total": n, "elapsed_sec": elapsed, "failed": fail_acc["n"],
                "means": means, "phase": "sampling", "ts": now_iso(),
            })
            state.update({"done": done, "failed": fail_acc["n"], "means": means})
            # 2) 轻量刷新任务快照（短持锁一次；供旧接口与「无日志旧任务」回退）
            with LOCK, connect() as c:
                r = c.execute("SELECT plan_id,payload FROM tasks WHERE id=?", (task_id,)).fetchone()
                if r:
                    t = json.loads(r[1])
                    t["progress"] = {"completed": done, "total": n,
                                     "cancel_requested": bool((t.get("progress") or {}).get("cancel_requested")),
                                     "current_means": means, "failed": fail_acc["n"],
                                     "elapsed_sec": elapsed, "phase": "sampling", "updated_at": now_iso()}
                    c.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(t, ensure_ascii=False), task_id))

        mc_cfg = ps.MCConfig(n_samples=n, seed_base=seed, strategies=strategies,
                             arrival_sigma=arrival_sigma, process_cv=process_cv,
                             failure_expected_per_line=failure_expected_per_line,
                             repair_mean_min=repair_mean_min)
        raw = ps.run_samples(scenario, mc_cfg, progress=None, on_sample=on_sample,
                             should_cancel=should_cancel, keep_sample0_timeline=True)
        fh.close()

        rows = _load_samples_rows(task_id)
        n_done = len(rows)
        means = {st: (run_acc[st]["sum"] / run_acc[st]["n"]) if run_acc[st]["n"] else None for st in strategies}
        cancelled = bool(raw.get("cancelled")) or n_done < n or bool(raw.get("status_term") == "not_complete")
        if cancelled:
            append_prod_progress_event(task_id, {
                "seq": n_done, "done": n_done, "total": n, "elapsed_sec": int(time.time() - _t0),
                "failed": fail_acc["n"], "means": means, "phase": "cancelled", "ts": now_iso(),
            })
            set_task("cancelled", {"completed": n_done, "total": n, "finished_at": now_iso(), "cancel_requested": False,
                                   "current_means": means, "failed": fail_acc["n"], "phase": "cancelled",
                                   "elapsed_sec": int(time.time() - _t0)})
            with LOCK, connect() as c:
                p = load_payload(c, "plans", plan_id); p.update(status="failed", updated_at=now_iso()); save_payload(c, "plans", p)
            return

        stats = compute_prod_stats(rows, strategies)
        result = {
            "config": {"seed": seed, "n": n, "strategies": strategies, "plan_id": plan_id,
                       "complete": True, "n_vehicles": n_vehicles,
                       "arrival_sigma": arrival_sigma, "process_cv": process_cv,
                       "failure_expected_per_line": failure_expected_per_line,
                       "repair_mean_min": repair_mean_min},
            "per_strategy": stats,
            "completed_samples": n_done,
            "failed_samples": sum(1 for r in rows if any(q is None or q.get("failed") is True
                                                         for q in (r.get("strategies") or {}).values())),
            "caveats": ["研究仿真统计结果，非生产方案；不得沿旧 /api/plans submit 误提交。"],
        }
        _write_json(result, task_id, "result.json")
        append_prod_progress_event(task_id, {
            "seq": n_done, "done": n_done, "total": n, "elapsed_sec": int(time.time() - _t0),
            "failed": fail_acc["n"], "means": means, "phase": "completed", "ts": now_iso(),
        })
        set_task("completed", {"completed": n_done, "total": n, "finished_at": now_iso(), "current_means": means,
                               "failed": fail_acc["n"], "phase": "completed",
                               "elapsed_sec": int(time.time() - _t0)}, result_ref="result.json")
        with LOCK, connect() as c:
            p = load_payload(c, "plans", plan_id)
            p["result"] = {"type": "production", "summary": stats, "caveats": result["caveats"]}
            p["status"] = "ready"
            p["updated_at"] = now_iso()
            save_payload(c, "plans", p)
    except HTTPError as e:
        _finish_failed(task_id, state, str(e.message), _t0)
        set_task("failed", {"completed": state["done"], "total": state["total"], "failed": state["failed"],
                            "current_means": state["means"], "phase": "failed",
                            "elapsed_sec": int(time.time() - _t0), "cancel_requested": False,
                            "finished_at": now_iso()}, error=str(e.message))
    except Exception as e:
        import traceback
        traceback.print_exc()
        _finish_failed(task_id, state, str(e), _t0)
        set_task("failed", {"completed": state["done"], "total": state["total"], "failed": state["failed"],
                            "current_means": state["means"], "phase": "failed",
                            "elapsed_sec": int(time.time() - _t0), "cancel_requested": False,
                            "finished_at": now_iso()}, error=str(e))


def _load_prod_sim(c, sim_id: str) -> dict:
    row = c.execute("SELECT * FROM prod_simulation WHERE id=?", (sim_id,)).fetchone()
    if not row:
        raise HTTPError(404, "仿真计划不存在")
    d = dict(row)
    d["scenario"] = json.loads(d["scenario"] or "{}")
    return d


def _load_prod_task_row(c, task_id: str) -> dict:
    row = c.execute("SELECT * FROM prod_task WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise HTTPError(404, "仿真任务不存在")
    d = dict(row)
    d["progress"] = json.loads(d["progress"] or "{}")
    d["req_conf"] = json.loads(d["req_conf"] or "{}")
    return d


def _update_prod_status(task_id: str, status: str, error: Optional[str] = None,
                        progress: Optional[dict] = None, result: Optional[str] = None):
    with LOCK, connect() as c:
        c.execute(
            "UPDATE prod_task SET status=?, error=?, finished_at=? WHERE id=?",
            (status, error, now_iso(), task_id),
        )
        if progress is not None:
            c.execute("UPDATE prod_task SET progress=? WHERE id=?",
                      (json.dumps(progress, ensure_ascii=False), task_id))
        if result is not None:
            c.execute("UPDATE prod_task SET result_ref=? WHERE id=?",
                      (result, task_id))


def _update_prod_progress(task_id: str, completed: int):
    with LOCK, connect() as c:
        t = _load_prod_task_row(c, task_id)
        prog = dict(t.get("progress") or {})
        prog["completed"] = completed
        prog["total"] = prog.get("total", PROD_SAMPLES)
        prog["updated_at"] = now_iso()
        c.execute("UPDATE prod_task SET progress=? WHERE id=?",
                  (json.dumps(prog, ensure_ascii=False), task_id))


def _set_prod_sim_status(sim_id: str, status: str):
    with LOCK, connect() as c:
        c.execute("UPDATE prod_simulation SET status=?, updated_at=? WHERE id=?",
                  (status, now_iso(), sim_id))


def _write_jsonl(task_id: str, name: str, rows):
    p = prod_task_dir(task_id) / name
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def _write_json(result: dict, task_id: str, name: str):
    p = prod_task_dir(task_id) / name
    with open(p, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return p


def compute_prod_stats(samples: list, strategies: list) -> dict:
    """Per-strategy stats from kernel StratResult.to_dict() rows.

    Each sample row: {"sample_idx","seed","reality":{...},"strategies":{st: StratResult.to_dict() or None}}
    StratResult fields: failed, completion_rate, overall_on_time_rate, conditional_on_time_rate,
    blocked_sec, makespan, mean_tardy_sec (nullable), completed, on_time.
    均值仅作指标展示，不参与策略顺序排名；失败样本保留，分母=该策略合格样本数。
    """
    METRIC_KEYS = ["completion_rate", "overall_on_time_rate", "conditional_on_time_rate",
                   "blocked_sec", "makespan", "mean_tardy_over_completed_sec",
                   "mean_tardy_over_late_sec"]
    out = {}
    for strat in strategies:
        ok_stats = {k: [] for k in METRIC_KEYS}
        completed_total = 0
        on_time_total = 0
        ok_n = 0
        fail_n = 0
        for s in samples:
            q = (s.get("strategies") or {}).get(strat)
            if not q:
                fail_n += 1
                continue
            if q.get("failed"):
                fail_n += 1
                continue
            ok_n += 1
            completed_total += int(q.get("completed") or 0)
            on_time_total += int(q.get("on_time") or 0)
            for k in METRIC_KEYS:
                v = q.get(k)
                ok_stats[k].append(v)  # _pstats filters None
        stat = {k: _pstats(ok_stats[k]) for k in METRIC_KEYS}
        out[strat] = {
            "label": PROD_STRATEGY_LABELS.get(strat, strat),
            "ok": ok_n,
            "failed": fail_n,
            "denominator": ok_n,
            "completed_total": completed_total,
            "on_time_total": on_time_total,
            "stat": stat,
        }
    return out


def prod_jsonl_rows(task_id: str):
    p = prod_task_dir(task_id) / "samples.jsonl"
    if not p.exists():
        return []
    rows = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def params_flat(summary: dict) -> list:
    """Flatten scenario summary disturbance entries into the input payload."""
    return summary.get("disturbance", [])


# ---------------------------------------------------------------------
# 每样本进度事件持久化（append JSONL）：真实样本完成事件的单事实来源。
# 事件仅含进度字段（seq/done/total/elapsed_sec/failed/means/phase/ts），不含任何业务结果。
# 文件IO一律在全局 LOCK 之外进行，避免长持有锁；同进程读取靠 flush 即刻可见。
# ---------------------------------------------------------------------

PROD_PROGRESS_FIELDS = ("seq", "done", "total", "elapsed_sec", "failed", "means", "phase", "ts")


def prod_progress_path(task_id: str) -> Path:
    return prod_task_dir(task_id) / PROD_PROGRESS_FILE


def _finite_or_none(v):
    """进度均值等只允许有限数值写入 JSONL（非有限/不可转换 -> None）。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _clean_means(means) -> dict:
    if not isinstance(means, dict):
        return {}
    return {str(k): _finite_or_none(v) for k, v in means.items()}


def append_prod_progress_event(task_id: str, event: dict) -> dict:
    """追加一条真实进度事件到 <task>/progress.jsonl。

    只保留进度字段，业务结果（样本明细/时间表）不进入该文件。调用方不得持有全局 LOCK。
    """
    ev = {k: event.get(k) for k in PROD_PROGRESS_FIELDS}
    ev["seq"] = int(ev.get("seq") or 0)
    ev["done"] = int(ev.get("done") or 0)
    ev["total"] = int(ev.get("total") or PROD_SAMPLES)
    ev["elapsed_sec"] = int(ev.get("elapsed_sec") or 0)
    ev["failed"] = int(ev.get("failed") or 0)
    ev["means"] = _clean_means(ev.get("means"))
    ev["ts"] = ev.get("ts") or now_iso()
    p = prod_progress_path(task_id)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False, allow_nan=False) + "\n")
        f.flush()
    return ev


def read_prod_progress_events(task_id: str, after: Optional[int] = None,
                              limit: int = PROD_PROGRESS_PAGE_DEFAULT) -> dict:
    """读取真实进度事件。

    after=None  -> 只返回最新快照（events 为空），用于刷新后直接恢复“已完成 N”，不重放历史。
    after=N     -> 返回 seq > N 的事件（按写入顺序，最多 limit 条）。
    返回 {source, events, latest, first_seq, last_seq, next_after, has_more}；不含业务结果。
    """
    p = prod_progress_path(task_id)
    out = {"source": "none", "events": [], "latest": None,
           "first_seq": None, "last_seq": None, "next_after": after, "has_more": False}
    if not p.exists():
        return out
    out["source"] = "log"
    collected = []
    first_seq = None
    last_seq = None
    latest = None
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue  # 容忍并发写入造成的残缺尾行
                if not isinstance(ev, dict) or not isinstance(ev.get("seq"), int):
                    continue
                first_seq = ev["seq"] if first_seq is None else first_seq
                last_seq = ev["seq"]
                latest = ev
                if after is not None and ev["seq"] > after:
                    collected.append(ev)
    except Exception:
        return out
    if latest is None:
        return out
    has_more = len(collected) > limit
    page = collected[:limit]
    out.update({
        "events": page,
        "latest": latest,
        "first_seq": first_seq,
        "last_seq": last_seq,
        "next_after": page[-1]["seq"] if page else (after if after is not None else last_seq),
        "has_more": has_more,
    })
    return out


def prod_progress_snapshot_from_task(task: dict) -> dict:
    """旧任务（无 progress.jsonl）回退：用已持久化的真实 progress 快照，不做任何计时模拟。"""
    prog = task.get("progress") or {}
    if not prog:
        return {}
    status = task.get("status")
    done = int(prog.get("completed") or 0)
    phase = prog.get("phase")
    if not phase:
        phase = "done" if status == "completed" else ("nominal" if done == 0 else "sampling")
    return {
        "seq": done, "done": done, "total": int(prog.get("total") or PROD_SAMPLES),
        "elapsed_sec": int(prog.get("elapsed_sec") or 0), "failed": int(prog.get("failed") or 0),
        "means": _clean_means(prog.get("current_means")), "phase": phase,
        "ts": prog.get("updated_at") or task.get("created_at"),
    }


def _load_task_or_404(c, task_id: str, plan_id: str) -> dict:
    row = c.execute("SELECT plan_id,payload FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row or row[0] != plan_id:
        raise HTTPError(404, "任务不存在或不在授权范围")
    return json.loads(row[1])


def prod_result_status(plan: dict, task: dict, page: int = 1, per_page: int = 100) -> dict:
    """Return production result summary for a completed task (light; samples paged via file)."""
    if task.get("status") != "completed":
        raise HTTPError(409, "任务未完成，无法输出最终统计结果（需 n>=1000 且未取消）")
    ref = task.get("result_ref")
    path = (PROD_DATA_DIR / task["id"] / ref) if ref else None
    if ref and path and path.exists():
        result = json.loads(path.read_text(encoding="utf-8"))
    else:
        rows = prod_jsonl_rows(task["id"])
        strategies = (task.get("conf") or {}).get("strategies") or PROD_STRATEGIES
        result = {"config": {"seed": 0, "n": (task.get("conf") or {}).get("n", PROD_SAMPLES),
                             "strategies": strategies, "plan_id": plan["id"], "complete": len(rows) >= PROD_SAMPLES},
                  "per_strategy": compute_prod_stats(rows, strategies),
                  "completed_samples": len(rows),
                  "failed_samples": sum(1 for r in rows if any(q is None or q.get("failed") is True
                                                                for q in (r.get("strategies") or {}).values()))}
    all_rows = prod_jsonl_rows(task["id"])
    total = len(all_rows)
    start = (page - 1) * per_page
    paged = all_rows[start:start + per_page]
    result["samples_page"] = {
        "page": page, "per_page": per_page, "total": total,
        "n_pages": max(1, -(-total // per_page)) if per_page else 1,
        "items": [{k: r.get(k) for k in ("sample_idx", "seed", "reality", "strategies")} for r in paged],
    }
    return {"type": "production", "task_id": task["id"], "plan_id": plan["id"], "result": result,
            "caveats": ["研究仿真统计结果，非生产方案；不得提交生产审批。"]}


class IntegratedHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "web"), **kwargs)

    def respond(self, code: int, data: Any):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def respond_csv(self, text: str, filename: str):
        payload = text.encode("utf-8-sig")  # BOM so Excel reads UTF-8 Chinese correctly
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def prod_csv(self, task: dict):
        rows = prod_jsonl_rows(task["id"])
        strategies = (task.get("conf") or {}).get("strategies") or PROD_STRATEGIES
        lines = [",".join(["sample_idx", "seed", "strategy", "ok", "error",
                           "completion_rate", "overall_on_time_rate", "conditional_on_time_rate",
                           "blocked_sec", "makespan", "mean_tardy_over_completed_sec",
                           "mean_tardy_over_late_sec"])]
        if not rows:
            lines.append("(无样本数据)")
        for r in rows:
            for st in strategies:
                q = (r.get("strategies") or {}).get(st) or {}
                ok_short = "0" if q.get("failed") else "1"
                err = (q.get("error") or "").replace(",", " ").replace("\n", " ")
                lines.append(",".join([str(r.get("sample_idx")), str(r.get("seed")), st, ok_short, err,
                                       str(q.get("completion_rate") or ""), str(q.get("overall_on_time_rate") or ""),
                                       str(q.get("conditional_on_time_rate") or ""), str(q.get("blocked_sec") or ""),
                                       str(q.get("makespan") or ""), str(q.get("mean_tardy_over_completed_sec") or ""),
                                       str(q.get("mean_tardy_over_late_sec") or "")]))
        return self.respond_csv("\n".join(lines) + "\n", f"production_samples_{task['id']}.csv")

    def _prod_scenario_params(self, scenario_sum: dict, dist: list):
        def find_param(nm):
            for it in (dist or []):
                if it.get("param") == nm:
                    if it.get("value") is not None:
                        return it.get("value")
                    if it.get("value_sec") is not None:
                        return it.get("value_sec")
                    return it.get("value_min")
            return None
        return {
            "seed": int(scenario_sum.get("seed") or 0),
            "n_vehicles": int(scenario_sum.get("n_vehicles") or 4200),
            "arrival_sigma": float(find_param("arrival_sigma") or 720.0),
            "process_cv": float(find_param("process_cv") or 0.08),
            "failure_expected_per_line": float(find_param("failure_expected_per_line") or 1.1),
            "repair_mean_min": float(find_param("repair_mean_min") or 40.0),
        }

    def prod_orders(self, plan: dict, query: dict):
        """计划输入订单分页（可到 4200 末页），来自同一名义场景（真实内核场景）。"""
        inp = plan.get("input") or {}
        sc_sum = inp.get("scenario") or {}
        prm = self._prod_scenario_params(sc_sum, inp.get("disturbance") or [])
        ps_mod = _import_production_sim()
        scenario = ps_mod.build_production_scenario(n_vehicles=prm["n_vehicles"], seed=prm["seed"])
        orders = getattr(scenario, "orders", [])
        q = (query.get("q", [""])[0] or "").strip().lower()
        if q:
            orders = [o for o in orders if q in str(o.vin).lower() or q in str(getattr(o, "config", "")).lower() or q in str(getattr(o, "model", "")).lower()]
        page = max(1, int(query.get("page", ["1"])[0]))
        per_page = max(1, int(query.get("per_page", ["100"])[0]))
        total = len(orders)
        n_pages = max(1, -(-total // per_page))
        start = (page - 1) * per_page
        items = []
        for o in orders[start:start + per_page]:
            items.append({"vin": o.vin, "order_id": o.vin, "model": getattr(o, "model", ""),
                          "color": getattr(o, "color", ""), "config": getattr(o, "config", ""),
                          "priority": getattr(o, "priority", None),
                          "release_at_sec": round(getattr(o, "release_at", 0.0), 1),
                          "due_at_sec": round(getattr(o, "due_at", 0.0), 1)})
        return self.respond(200, {"total": total, "page": page, "per_page": per_page,
                                  "n_pages": n_pages, "q": q, "orders": items,
                                  "caveats": ["名义场景订单，非工厂实测数据。"]})

    def prod_sample(self, task: dict, query: dict):
        """样本时间表：优先落盘的 sample0 时间表(真实持久化)，否则同 seed 复放 simulate；
        支持按工序(stage)与订单(vin)筛选并分页。不是假数据。"""
        idx = max(0, int(query.get("sample", ["0"])[0]))
        strategy = query.get("strategy", [PROD_STRATEGIES[0]])[0]
        stage = query.get("stage", [None])[0] or query.get("line", [None])[0]
        q = (query.get("q", [""])[0] or "").strip().lower()
        page = max(1, int(query.get("page", ["1"])[0]))
        per_page = max(1, int(query.get("per_page", ["100"])[0]))
        inp = task.get("input") or {}
        sc_sum = inp.get("scenario") or {}
        prm = self._prod_scenario_params(sc_sum, inp.get("disturbance") or [])
        sub_seed = prm["seed"] + idx
        source = "replay"
        timeline = None
        metrics = {}
        # 1) 落盘的 sample0 时间表（运行中首样本落盘后即可查看，无需等待整批）
        if idx == 0:
            tl_path = prod_task_dir(task["id"]) / "sample0_timeline.json"
            if tl_path.exists():
                try:
                    blob = json.loads(tl_path.read_text(encoding="utf-8"))
                    tls = blob.get("timelines") or {}
                    if strategy in tls:
                        timeline = tls[strategy]
                        source = "stored"
                except Exception:
                    timeline = None
        # 2) 否则同 seed 复放
        if timeline is None:
            ps_mod = _import_production_sim()
            scenario = ps_mod.build_production_scenario(n_vehicles=prm["n_vehicles"], seed=prm["seed"])
            reality = ps_mod.sample_reality(scenario, sub_seed, arrival_sigma=prm["arrival_sigma"],
                                            process_cv=prm["process_cv"],
                                            failure_expected_per_line=prm["failure_expected_per_line"],
                                            repair_mean_min=prm["repair_mean_min"])
            w_fixed = ps_mod.optimize_nominal_plan(scenario, seed=prm["seed"])
            res = ps_mod.simulate(scenario, reality, strategy, w_fixed_seq=w_fixed)
            metrics = res.to_dict() if hasattr(res, "to_dict") else {}
            timeline = res.timeline if hasattr(res, "timeline") else []
            source = "replay"
        all_rows = list(timeline or [])
        if stage:
            all_rows = [t for t in all_rows if str(t.get("stage")) == str(stage)]
        if q:
            all_rows = [t for t in all_rows if q in str(t.get("vin", "")).lower()]
        total = len(all_rows)
        n_pages = max(1, -(-total // per_page))
        start = (page - 1) * per_page
        rows = all_rows[start:start + per_page]
        return self.respond(200, {
            "sample_id": idx, "strategy": strategy, "stage_filter": stage, "q": q,
            "sub_seed": sub_seed, "page": page, "per_page": per_page, "total": total, "n_pages": n_pages,
            "rows": rows, "timeline": rows, "source": source, "metrics": metrics,
            "caveats": ["样本时间表为等效瓶颈流水仿真（无扰名义 vs 同 seed 扰动复放），非工位数字孪生/非工厂实测。"],
        })

    def prod_progress(self, plan: dict, task: dict, query: dict):
        """计划绑定的真实进度事件流（分页 bounded）。

        GET /api/plans/{id}/production-progress?task=<tid>&after=<seq>&limit=<n>
        - after 省略：只回最新快照 + 状态（刷新后直接恢复完成数，不重放历史）。
        - after=seq：回 seq 之后的事件（按真实完成顺序），可 has_more 继续取。
        返回明确 status/error，便于前端在取消/失败时立即停动画并保留准确计数。
        """
        t_id = task["id"]
        raw_after = (query.get("after", [""])[0] or "").strip()
        after: Optional[int] = None
        if raw_after != "":
            try:
                after = max(0, int(raw_after))
            except ValueError:
                raise HTTPError(400, "after 必须为非负整数")
        try:
            limit = int((query.get("limit", [""])[0] or "").strip() or PROD_PROGRESS_PAGE_DEFAULT)
        except ValueError:
            limit = PROD_PROGRESS_PAGE_DEFAULT
        limit = max(1, min(PROD_PROGRESS_PAGE_MAX, limit))

        conf = task.get("conf") or {}
        total_conf = int(conf.get("n") or PROD_SAMPLES)
        log = read_prod_progress_events(t_id, after=after, limit=limit)
        latest = log.get("latest")
        source = log.get("source")
        if not latest:
            # 旧任务/日志缺失：回退到真实任务快照，不伪造事件、不用计时模拟
            latest = prod_progress_snapshot_from_task(task)
            source = "snapshot" if latest else "none"
        latest = latest or {}
        done = int(latest.get("done") or 0)
        status = task.get("status")
        phase = latest.get("phase")
        if phase == "nominal" and status in ("completed", "cancelled", "failed"):
            phase = status
        return self.respond(200, {
            "task_id": t_id, "plan_id": plan["id"], "status": status, "error": task.get("error"),
            "total": int(latest.get("total") or total_conf), "done": done,
            "failed": int(latest.get("failed") or 0),
            "elapsed_sec": int(latest.get("elapsed_sec") or 0),
            "means": latest.get("means") or {}, "phase": phase, "updated_at": latest.get("ts"),
            "after": after, "events": log.get("events") or [],
            "first_seq": log.get("first_seq"), "last_seq": log.get("last_seq"),
            "next_after": log.get("next_after"), "has_more": bool(log.get("has_more")),
            "source": source, "server_time": now_iso(),
            "config": {"n": total_conf, "strategies": conf.get("strategies") or PROD_STRATEGIES},
            "caveats": ["进度来自每样本真实完成事件(progress.jsonl)；无计时模拟样本，非生产方案，不提交审批。"],
        })

    def body_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 <= length <= 5_000_000:
            raise HTTPError(413, "请求体超过大小限制")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            raise HTTPError(400, "JSON格式错误")

    def identity(self) -> dict:
        name = self.headers.get("X-Demo-User", "planner-a")
        if name not in USERS:
            raise HTTPError(401, "未知演示身份")
        return {"user": name, **USERS[name]}

    def authorize(self, user: dict, plan: dict, required_role: Optional[str] = None):
        if plan["factory"] not in user["factories"]:
            raise HTTPError(404, "记录不存在或不在授权范围")
        if required_role and user["role"] != required_role:
            raise HTTPError(403, "当前角色无权执行该操作")

    def plan_view(self, c, p: dict) -> dict:
        p = dict(p)
        is_production = (p.get("input") or {}).get("schema") == "aps_production_weekly" or p.get("type") == "PRODUCTION_WEEKLY"
        p["production"] = is_production
        p["production_tasks"] = []
        p["tasks"] = []
        for row in c.execute("SELECT payload FROM tasks WHERE plan_id=? ORDER BY rowid DESC", (p["id"],)):
            t = json.loads(row["payload"])
            slim = {k: t.get(k) for k in ("id", "status", "stage", "created_at", "finished_at", "error", "progress", "result_ref", "conf")}
            p["tasks"].append(slim)
            if is_production and t.get("status") in ("running", "queued", "cancelling", "completed", "failed", "cancelled"):
                p["production_tasks"].append(slim)
        return p

    def handle_api(self, method: str):
        try:
            path = urlparse(self.path).path
            user = self.identity()
            body = self.body_json() if method in ("POST", "PATCH") else {}

            with LOCK, connect() as c:
                # 1. User info
                if path == "/api/me" and method == "GET":
                    return self.respond(200, user)

                if path == "/api/catalogs" and method == "GET":
                    rows = [json.loads(r[0]) for r in c.execute("SELECT payload FROM catalogs ORDER BY rowid DESC")]
                    return self.respond(200, {"catalogs": [x for x in rows if x["factory"] in user["factories"]]})
                if path.startswith("/api/catalogs/") and method == "PATCH":
                    catalog = load_payload(c, "catalogs", unquote(path.split("/")[-1]))
                    self.authorize(user, catalog, "engineer")
                    if body.get("revision") != catalog["revision"]:
                        raise HTTPError(409, "基础资料已更新，请刷新后重新编辑")
                    data = body.get("data")
                    try:
                        validate_master(data)
                        old = catalog["data"]
                        for key, fields in [("times", ("config", "model", "color", "stage")), ("buffers", ("id", "edge")), ("setups", ("config", "stage", "pair"))]:
                            if [{k: x[k] for k in fields} for x in data[key]] != [{k: x[k] for k in fields} for x in old[key]]:
                                raise ValueError("不允许改变来源配置及工艺映射")
                        if any(data.get(k) != old[k] for k in ("horizon", "day_length")):
                            raise ValueError("不允许改变演示时间基准")
                    except (ValueError, KeyError, TypeError) as exc:
                        raise HTTPError(422, str(exc))
                    catalog.update(data=data, revision=catalog["revision"] + 1, updated_at=now_iso(), updated_by=user["user"], checks=readiness(data))
                    save_payload(c, "catalogs", catalog)
                    audit_log(c, user["user"], "save_master", catalog["id"])
                    return self.respond(200, {"catalog": catalog})

                # 2. Preset Scenarios API (XML Interface Instances)
                if path == "/api/preset-scenarios" and method == "GET":
                    return self.respond(200, {"scenarios": PRESET_SCENARIOS})

                if path.startswith("/api/preset-scenarios/") and method == "GET":
                    parts = path.strip("/").split("/")
                    if len(parts) == 4 and parts[3] == "xml":
                        sc_id = parts[2]
                        matched = next((s for s in PRESET_SCENARIOS if s["id"] == sc_id), None)
                        if not matched:
                            raise HTTPError(404, f"未找到预设场景: {sc_id}")
                        xml_path = XML_DIR / matched["xml_file"]
                        if not xml_path.exists():
                            raise HTTPError(404, f"XML文件不存在: {matched['xml_file']}")
                        xml_content = xml_path.read_text(encoding="utf-8")
                        sc_obj = parse_xml_to_scenario(xml_content)
                        summary = scenario_to_display_summary(sc_obj)
                        return self.respond(200, {
                            "scenario_id": sc_id,
                            "name": matched["name"],
                            "xml_file": matched["xml_file"],
                            "summary": summary,
                            "xml_content": xml_content
                        })

                # 3. Plans List & Create
                if path == "/api/plans":
                    if method == "GET":
                        plans = [json.loads(r[0]) for r in c.execute("SELECT payload FROM plans ORDER BY rowid DESC")]
                        return self.respond(200, {"plans": [self.plan_view(c, p) for p in plans if p["factory"] in user["factories"]]})
                    if method == "POST":
                        if user["role"] != "planner":
                            raise HTTPError(403, "仅计划员可创建计划")
                        if body.get("factory") not in user["factories"]:
                            raise HTTPError(403, "工厂未授权")
                        name = body.get("name", "").strip()
                        if not (1 <= len(name) <= 100):
                            raise HTTPError(400, "计划名称须为1—100字符")
                        if body.get("type", "W+3") != "W+3":
                            raise HTTPError(422, "本轮仅开放W+3演示排程")
                        p_id = uuid.uuid4().hex[:12]
                        p = {
                            "id": p_id,
                            "name": name,
                            "type": body.get("type", "W+3"),
                            "factory": body["factory"],
                            "status": "draft",
                            "created_at": now_iso(),
                            "updated_at": now_iso(),
                            "created_by": user["user"],
                            "input": None,
                            "result": None,
                            "selected_candidate": None,
                            "revision": 1
                        }
                        c.execute("INSERT INTO plans VALUES(?,?)", (p_id, json.dumps(p, ensure_ascii=False)))
                        audit_log(c, user["user"], "create", p_id)
                        return self.respond(201, {"plan": self.plan_view(c, p)})

                # 4. Plan details & operations
                parts = path.strip("/").split("/")
                if len(parts) >= 3 and parts[:2] == ["api", "plans"]:
                    p_id = parts[2]
                    p = load_payload(c, "plans", p_id)
                    self.authorize(user, p)

                    # 4.1 GET Plan
                    if len(parts) == 3 and method == "GET":
                        return self.respond(200, {"plan": self.plan_view(c, p)})

                    if len(parts) == 4 and parts[3] == "business-input" and method == "GET":
                        if not (p.get("input") or {}).get("raw_xml"):
                            raise HTTPError(422, "尚未载入演示输入")
                        return self.respond(200, display_input(p["input"]))
                    if len(parts) == 4 and parts[3] == "refresh-master" and method == "POST":
                        self.authorize(user, p, "planner")
                        if p["status"] not in ("draft", "ready", "failed"):
                            raise HTTPError(409, "当前状态不允许更换基础资料")
                        inp = p.get("input") or {}
                        catalog = load_payload(c, "catalogs", inp.get("master_id", ""))
                        self.authorize(user, catalog)
                        inp.update(raw_xml=apply_master(inp["source_xml"], catalog["data"]), master=copy.deepcopy(catalog["data"]), master_revision=catalog["revision"], checks=readiness(catalog["data"]), loaded_at=now_iso())
                        p.update(input=inp, input_hash=compute_object_hash(inp), result=None, selected_candidate=None, status="draft", updated_at=now_iso(), revision=p.get("revision", 1) + 1)
                        save_payload(c, "plans", p)
                        audit_log(c, user["user"], "refresh_master", p_id)
                        return self.respond(200, {"plan": self.plan_view(c, p)})

                    # 4.2 PATCH Plan
                    if len(parts) == 3 and method == "PATCH":
                        self.authorize(user, p, "planner")
                        if p["status"] not in ("draft", "ready", "failed"):
                            raise HTTPError(409, "当前状态不允许修改")
                        if "input" in body:
                            p["input"] = body["input"]
                            p["result"] = None
                            p["selected_candidate"] = None
                            p["status"] = "draft"
                        if "name" in body:
                            p["name"] = str(body["name"]).strip()
                        p["updated_at"] = now_iso()
                        p["revision"] = p.get("revision", 1) + 1
                        save_payload(c, "plans", p)
                        audit_log(c, user["user"], "edit", p_id)
                        return self.respond(200, {"plan": self.plan_view(c, p)})

                    # 4.3 Database Records View API
                    if len(parts) == 4 and parts[3] == "db-records" and method == "GET":
                        header_rows = [dict(r) for r in c.execute("SELECT * FROM aps_plan_header WHERE plan_id=?", (p_id,))]
                        candidate_rows = [dict(r) for r in c.execute("SELECT * FROM aps_plan_candidate WHERE plan_id=?", (p_id,))]
                        detail_rows = [dict(r) for r in c.execute("SELECT * FROM aps_plan_detail WHERE plan_id=? ORDER BY assigned_day, stage, sequence_position LIMIT 100", (p_id,))]
                        order_rows = [dict(r) for r in c.execute("SELECT * FROM aps_order_master WHERE plant_code=? LIMIT 50", (p["factory"],))]
                        material_rows = [dict(r) for r in c.execute("SELECT * FROM aps_material_ledger WHERE plant_code=? LIMIT 50", (p["factory"],))]
                        receipt_rows = [dict(r) for r in c.execute("SELECT * FROM aps_dispatch_receipt WHERE plan_id=?", (p_id,))]
                        return self.respond(200, {
                            "plan_id": p_id,
                            "header_table": header_rows,
                            "candidates_table": candidate_rows,
                            "details_table_count": len(detail_rows),
                            "details_table_sample": detail_rows[:30],
                            "orders_table_sample": order_rows,
                            "material_table_sample": material_rows,
                            "receipts_table": receipt_rows
                        })

                    # 4.4 Load XML Scenario into Plan
                    if len(parts) == 4 and parts[3] == "load-xml-scenario" and method == "POST":
                        self.authorize(user, p, "planner")
                        if p["status"] not in ("draft", "ready", "failed"):
                            raise HTTPError(409, "当前状态不允许更换场景")
                        sc_id = body.get("scenario_id")
                        raw_xml = body.get("xml_content")

                        if not raw_xml and sc_id:
                            matched = next((s for s in PRESET_SCENARIOS if s["id"] == sc_id), None)
                            if not matched: raise HTTPError(404, "未知场景ID")
                            raw_xml = (XML_DIR / matched["xml_file"]).read_text(encoding="utf-8")

                        if not raw_xml:
                            raise HTTPError(400, "缺少XML内容")

                        if not sc_id or not any(x["id"] == sc_id for x in PRESET_SCENARIOS):
                            raise HTTPError(422, "请选择已登记演示场景")
                        catalog_id = p["factory"] + ":" + sc_id
                        row = c.execute("SELECT payload FROM catalogs WHERE id=?", (catalog_id,)).fetchone()
                        if row:
                            catalog = json.loads(row[0])
                        else:
                            data = extract_master(raw_xml)
                            catalog = {"id": catalog_id, "factory": p["factory"], "scenario_id": sc_id, "name": next(x["name"] for x in PRESET_SCENARIOS if x["id"] == sc_id), "revision": 1, "data": data, "checks": readiness(data), "updated_at": now_iso(), "updated_by": user["user"]}
                            c.execute("INSERT INTO catalogs VALUES(?,?)", (catalog_id, json.dumps(catalog, ensure_ascii=False)))
                        source_xml = raw_xml
                        raw_xml = apply_master(raw_xml, catalog["data"])
                        sc_obj = parse_xml_to_scenario(raw_xml)
                        summary = scenario_to_display_summary(sc_obj)
                        input_payload = {
                            "schema": "aps_fullchain_v2_business", "scenario_id": sc_id,
                            "source_name": catalog["name"], "source_xml": source_xml,
                            "raw_xml": raw_xml, "summary": summary, "loaded_at": now_iso(),
                            "master_id": catalog_id, "master_revision": catalog["revision"],
                            "master": copy.deepcopy(catalog["data"]), "checks": readiness(catalog["data"])
                        }
                        p["input"] = input_payload
                        p["input_hash"] = compute_object_hash(input_payload)
                        p["revision"] = p.get("revision", 1) + 1
                        p["result"] = None
                        p["selected_candidate"] = None
                        p["status"] = "draft"
                        p["updated_at"] = now_iso()
                        save_payload(c, "plans", p)
                        audit_log(c, user["user"], f"load_xml_{sc_id or 'custom'}", p_id)
                        return self.respond(200, {"plan": self.plan_view(c, p), "summary": summary})

                    # 4.45 加载生产量级周计划场景（绑定当前 plan，不新建第二条）
                    if len(parts) == 4 and parts[3] == "load-production-scenario" and method == "POST":
                        self.authorize(user, p, "planner")
                        if p["status"] not in ("draft", "ready", "failed"):
                            raise HTTPError(409, "锁定状态不允许更换场景")
                        ps = _import_production_sim()
                        seed = int(body.get("seed") or 0)
                        n_vehicles = int(body.get("n_vehicles") or 4200)
                        scenario = ps.build_production_scenario(n_vehicles=n_vehicles, seed=seed)
                        params = {
                            "seed": seed, "n_vehicles": n_vehicles,
                            "arrival_sigma": float(body.get("arrival_sigma") or 720.0),
                            "process_cv": float(body.get("process_cv") or 0.08),
                            "failure_expected_per_line": float(body.get("failure_expected_per_line") or 1.1),
                            "repair_mean_min": float(body.get("repair_mean_min") or 40.0),
                        }
                        summary = worker_scenario_summary(scenario, params=params)
                        p["type"] = "PRODUCTION_WEEKLY"
                        p["input"] = {"schema": "aps_production_weekly", "scenario": summary,
                                      "source_name": PROD_SCENARIO_TITLE,
                                      "disturbance": params_flat(summary), "production": params}
                        p["input_hash"] = compute_object_hash(p["input"])
                        p["result"] = None
                        p["selected_candidate"] = None
                        p["status"] = "draft"
                        p["revision"] = p.get("revision", 1) + 1
                        p["updated_at"] = now_iso()
                        save_payload(c, "plans", p)
                        audit_log(c, user["user"], "load_production_scenario", p_id)
                        return self.respond(200, {"plan": self.plan_view(c, p), "summary": summary, "production": True})

                    # 4.46 生产量级：只读结果 / CSV / 样本 / 订单分页 —— 均挂在 plan 上按 plan 权限
                    if len(parts) == 4 and method == "GET" and parts[3] in ("production-result", "production-csv", "production-sample", "production-orders", "production-timeline", "production-progress"):
                        sub = parts[3]
                        if p.get("input") and (p["input"].get("schema") == "aps_production_weekly"):
                            qs = parse_qs(urlparse(self.path).query)
                            if sub == "production-progress":
                                # 每样本真实进度事件流（分页 bounded）；绑定 plan 权限，禁止跨计划读取
                                t_id = qs.get("task", [""])[0]
                                if not t_id:
                                    raise HTTPError(400, "缺少任务ID")
                                task = _load_task_or_404(c, t_id, plan_id=p_id)
                                return self.prod_progress(p, task, qs)
                            if sub == "production-orders":
                                # 计划输入订单分页（可到 4200 末页），来自同一名义场景
                                return self.prod_orders(p, qs)
                            if sub == "production-timeline":
                                # 运行中样本时间表：优先读取落盘的 sample0 时间表；否则同 seed 复放
                                t_id = qs.get("task", [""])[0]
                                if not t_id:
                                    raise HTTPError(400, "缺少任务ID")
                                task = _load_task_or_404(c, t_id, plan_id=p_id)
                                return self.prod_sample(task, qs)
                            if sub == "production-result":
                                t_id = parse_qs(urlparse(self.path).query).get("task", [""])[0]
                                page = int(parse_qs(urlparse(self.path).query).get("page", ["1"])[0])
                                per_page = int(parse_qs(urlparse(self.path).query).get("per_page", ["100"])[0])
                                if not t_id:
                                    raise HTTPError(400, "缺少任务ID")
                                task = _load_task_or_404(c, t_id, plan_id=p_id)
                                return self.respond(200, prod_result_status(p, task, page=page, per_page=per_page))
                            if sub == "production-csv":
                                t_id = parse_qs(urlparse(self.path).query).get("task", [""])[0]
                                if not t_id:
                                    raise HTTPError(400, "缺少任务ID")
                                task = _load_task_or_404(c, t_id, plan_id=p_id)
                                return self.prod_csv(task)
                            if sub == "production-sample":
                                t_id = parse_qs(urlparse(self.path).query).get("task", [""])[0]
                                if not t_id:
                                    raise HTTPError(400, "缺少任务ID")
                                task = _load_task_or_404(c, t_id, plan_id=p_id)
                                return self.prod_sample(task, parse_qs(urlparse(self.path).query))

                    # 4.47 生产量级：取消任务
                    if len(parts) == 4 and parts[3] == "production-cancel" and method == "POST":
                        self.authorize(user, p, "planner")
                        t_id = body.get("task") or (parse_qs(urlparse(self.path).query).get("task", [""])[0] or "")
                        if not t_id:
                            raise HTTPError(400, "缺少任务ID")
                        task = _load_task_or_404(c, t_id, plan_id=p_id)
                        if task.get("status") in ("completed", "failed", "cancelled"):
                            raise HTTPError(409, "任务已结束，不能取消")
                        prog = dict(task.get("progress") or {})
                        prog["cancel_requested"] = True
                        task["progress"] = prog
                        task["status"] = "cancelling"
                        with LOCK, connect() as cw:
                            cw.execute("UPDATE tasks SET payload=? WHERE id=?",
                                       (json.dumps(task, ensure_ascii=False), t_id))
                        return self.respond(200, {"task_id": t_id, "status": "cancelling",
                                                  "note": "取消标记已设置；未完成不足 1000 样本不输出最终均值或提交"})

                    # 4.5 Run Plan
                    if len(parts) == 4 and parts[3] == "run" and method == "POST":
                        self.authorize(user, p, "planner")
                        if p["status"] not in ("draft", "ready", "failed"):
                            raise HTTPError(409, "当前状态不允许生成方案")
                        if p.get("type") == "PRODUCTION_WEEKLY":
                            # 生产量级：用生产 worker，任务挂到 plan
                            if not p.get("input") or p["input"].get("schema") != "aps_production_weekly":
                                raise HTTPError(400, "请先加载生产量级场景")
                            t_id = uuid.uuid4().hex[:12]
                            strategy_list = PROD_STRATEGIES
                            task = {
                                "id": t_id, "plan_id": p_id, "status": "queued", "stage": "已排队",
                                "progress_log": [], "created_at": now_iso(),
                                "input": p["input"], "conf": {"n": PROD_SAMPLES, "strategies": strategy_list},
                                "result": None, "error": None,
                            }
                            c.execute("INSERT INTO tasks VALUES(?,?,?)", (t_id, p_id, json.dumps(task, ensure_ascii=False)))
                            p.update(status="running", result=None, selected_candidate=None, updated_at=now_iso())
                            save_payload(c, "plans", p)
                            audit_log(c, user["user"], "run_production", p_id)
                            threading.Thread(target=worker_production, args=(t_id,), daemon=True).start()
                            return self.respond(202, {"task_id": t_id})
                        if p.get("type") != "W+3":
                            raise HTTPError(422, "长期计划引擎尚未开放")
                        if not p.get("input"):
                            raise HTTPError(400, "请先选择演示场景")
                        if p["input"].get("master"):
                            issues = readiness(p["input"]["master"])
                            if issues:
                                raise HTTPError(422, "；".join(issues))
                        t_id = uuid.uuid4().hex[:12]
                        task = {
                            "id": t_id,
                            "plan_id": p_id,
                            "status": "queued",
                            "stage": "已排队",
                            "progress_log": [],
                            "created_at": now_iso(),
                            "input": p["input"],
                            "result": None,
                            "error": None
                        }
                        c.execute("INSERT INTO tasks VALUES(?,?,?)", (t_id, p_id, json.dumps(task, ensure_ascii=False)))
                        p.update(status="running", result=None, selected_candidate=None, updated_at=now_iso())
                        save_payload(c, "plans", p)
                        audit_log(c, user["user"], "run_fullchain", p_id)
                        threading.Thread(target=worker_fullchain, args=(t_id,), daemon=True).start()
                        return self.respond(202, {"task_id": t_id})

# 4.6 Submit Candidate
                    if len(parts) == 4 and parts[3] == "submit" and method == "POST":
                        self.authorize(user, p, "planner")
                        if p.get("type") == "PRODUCTION_WEEKLY":
                            return self.respond(422, {"error": "生产量级周计划的均值统计结果非生产方案，不可提交生产审批。"})
                        if p["status"] != "ready":
                            raise HTTPError(409, "仅计算完成的计划可提交")
                        c_name = body.get("candidate", "Strategy_E_ALNS")
                        r = p.get("result") or {}
                        cand = (r.get("candidates") or {}).get(c_name)
                        if cand is None:
                            return self.respond(422, {"error": "候选 " + str(c_name) + " 不存在或该计划尚无计算结果，不能提交"})
                        if not cand.get("submittable"):
                            reasons = "；".join(cand.get("submit_blockers") or ["未通过提交门槛"])
                            return self.respond(422, {"error": "该候选不可提交：" + reasons})
                        p.update(status="pending", selected_candidate=c_name, submitted_by=user["user"], updated_at=now_iso())
                        save_payload(c, "plans", p)
                        audit_log(c, user["user"], f"submit_{c_name}", p_id)
                        return self.respond(200, {"plan": self.plan_view(c, p)})

                    # 4.7 Approve Plan
                    if len(parts) == 4 and parts[3] == "approve" and method == "POST":
                        self.authorize(user, p, "manager")
                        if p["status"] != "pending":
                            raise HTTPError(409, "仅待审批计划可批准")
                        p.update(status="approved", approved_by=user["user"], approved_at=now_iso(), updated_at=now_iso())
                        save_payload(c, "plans", p)
                        audit_log(c, user["user"], "approve", p_id)
                        return self.respond(200, {"plan": self.plan_view(c, p)})

                # 5. Tasks API
                if len(parts) == 3 and parts[:2] == ["api", "tasks"] and method == "GET":
                    t_id = parts[2]
                    t = load_payload(c, "tasks", t_id)
                    self.authorize(user, load_payload(c, "plans", t["plan_id"]))
                    return self.respond(200, {k: v for k, v in t.items() if k != "input"})

                # 6. 【生产量级周计划仿真】独立子系统（与旧 /api/plans 完全隔离，不走旧 submit）
                if len(parts) >= 2 and parts[:2] == ["api", "production"]:
                    return self.handle_production(method, parts, c, user, body)

                raise HTTPError(404, "接口不存在")
        except HTTPError as e:
            self.respond(e.code, {"error": e.message})
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.respond(500, {"error": str(e)})

    def handle_production(self, method: str, parts: list, c, user: dict, body: dict):
        """生产量级周计划仿真 API。权限/工厂隔离后端强制；结果仅研究统计，不沿旧 submit 提交。"""
        # ---- 列表 & 创建 ----
        if len(parts) == 2 and method == "GET":  # /api/production
            rows = [dict(r) for r in c.execute("SELECT * FROM prod_simulation ORDER BY created_at DESC")]
            sims = []
            for r in rows:
                if r["factory"] not in user["factories"]:
                    continue
                r["scenario"] = json.loads(r["scenario"] or "{}")
                # 只给 summary；不给 result/sample
                sims.append({k: r.get(k) for k in
                             ("id", "name", "factory", "plan_type", "status", "scenario",
                              "created_by", "created_at", "updated_at")})
            return self.respond(200, {"simulations": sims})

        if len(parts) == 2 and method == "POST":  # /api/production —— 已禁用独立创建，生产量级走计划内场景
            return self.respond(422, {"error": "生产量级周计划已并入计划场景：请在计划内加载生产量级场景，不再独立创建第二条记录。"})

        # ---- 单个计划 ----
        if len(parts) == 3:
            s_id = parts[2]
            row = c.execute("SELECT * FROM prod_simulation WHERE id=?", (s_id,)).fetchone()
            if not row:
                raise HTTPError(404, "仿真计划不存在")
            r = dict(row)
            if r["factory"] not in user["factories"]:
                raise HTTPError(404, "记录不存在或不在授权范围")
            r["scenario"] = json.loads(r["scenario"] or "{}")
            # 附最近任务列表（sync 摘要）
            tasks = []
            for t in c.execute("SELECT * FROM prod_task WHERE sim_id=? ORDER BY created_at DESC LIMIT 20", (s_id,)):
                td = dict(t)
                td["progress"] = json.loads(td["progress"] or "{}")
                td["req_conf"] = json.loads(td["req_conf"] or "{}")
                tasks.append({k: td.get(k) for k in ("id", "status", "progress", "req_conf",
                                                     "created_at", "finished_at", "error")})
            r["tasks"] = tasks
            return self.respond(200, {"simulation": r})

        if len(parts) == 4 and parts[3] == "input" and method == "GET":
            s_id = parts[2]
            row = c.execute("SELECT * FROM prod_simulation WHERE id=?", (s_id,)).fetchone()
            if not row:
                raise HTTPError(404, "仿真不存在")
            r = dict(row)
            if r["factory"] not in user["factories"]:
                raise HTTPError(404, "记录不存在或不在授权范围")
            return self.respond(200, {"scenario": json.loads(r["scenario"] or "{}")})

        if len(parts) == 4 and parts[3] == "orders" and method == "GET":
            # 分页查看订单（可重建场景；避免一次返回巨量）
            s_id = parts[2]
            row = c.execute("SELECT * FROM prod_simulation WHERE id=?", (s_id,)).fetchone()
            if not row:
                raise HTTPError(404, "仿真不存在")
            r = dict(row)
            if r["factory"] not in user["factories"]:
                raise HTTPError(404, "记录不存在或不在授权范围")
            sc = json.loads(r["scenario"] or "{}")
            seed = int(sc.get("seed") or 0)
            n_vehicles = int(sc.get("n_vehicles") or 4200)
            ps = _import_production_sim()
            scenario = ps.build_production_scenario(n_vehicles=n_vehicles, seed=seed)
            orders = getattr(scenario, "orders", [])
            q = parse_qs(urlparse(self.path).query)
            page = max(1, int(q.get("page", ["1"])[0]))
            per_page = int(q.get("per_page", ["100"])[0])
            total = len(orders)
            start = (page - 1) * per_page
            items = []
            for o in orders[start:start + per_page]:
                cfg = scenario.configs.get(o.config)
                items.append({"vin": o.vin, "order_id": o.vin, "model": o.model, "color": o.color,
                              "config": o.config, "priority": o.priority,
                              "release_at_sec": round(o.release_at, 1), "due_at_sec": round(o.due_at, 1)})
            return self.respond(200, {"total": total, "page": page, "per_page": per_page,
                                      "n_pages": max(1, -(-total // per_page)), "orders": items})

        # ---- 运行（启动1000次仿真）任务 ----
        if len(parts) == 4 and parts[3] == "run" and method == "POST":
            if user["role"] != "planner":
                raise HTTPError(403, "仅计划员可启动仿真")
            s_id = parts[2]
            row = c.execute("SELECT * FROM prod_simulation WHERE id=?", (s_id,)).fetchone()
            if not row:
                raise HTTPError(404, "仿真不存在")
            r = dict(row)
            if r["factory"] not in user["factories"]:
                raise HTTPError(404, "记录不存在或不在授权范围")
            # 去重：同一仿真已有排队/运行任务则复用
            active = c.execute(
                "SELECT * FROM prod_task WHERE sim_id=? AND status IN ('queued','running') LIMIT 1",
                (s_id,)).fetchone()
            if active:
                ad = dict(active)
                return self.respond(202, {"task_id": ad["id"], "reused": True,
                                          "message": "已有进行中的仿真任务，未重复启动"})
            seed = int(body.get("seed") or 0)
            n = int(body.get("n") or PROD_SAMPLES)
            n_vehicles = int(body.get("n_vehicles") or 4200)
            conf = {"seed": seed, "n": n, "strategies": PROD_STRATEGIES,
                    "n_vehicles": n_vehicles,
                    "arrival_sigma": float(body.get("arrival_sigma") or 720.0),
                    "process_cv": float(body.get("process_cv") or 0.08),
                    "failure_expected_per_line": float(body.get("failure_expected_per_line") or 1.1),
                    "repair_mean_min": float(body.get("repair_mean_min") or 40.0)}
            t_id = uuid.uuid4().hex[:12]
            now = now_iso()
            c.execute(
                "INSERT INTO prod_task (id,sim_id,status,progress,req_conf,result_ref,error,created_at,finished_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (t_id, s_id, "queued", json.dumps({"completed": 0, "total": n, "cancel_requested": False}),
                 json.dumps(conf, ensure_ascii=False), None, None, now, None))
            c.execute("UPDATE prod_simulation SET status='running', updated_at=? WHERE id=?", (now, s_id))
            threading.Thread(target=worker_production, args=(t_id,), daemon=True).start()
            return self.respond(202, {"task_id": t_id, "reused": False})

        # ---- 任务进度/取消/结果/样本/CSV ----
        if len(parts) >= 4 and parts[:3] == ["api", "production", "tasks"]:
            return self.handle_prod_task(method, parts, c, user)

        raise HTTPError(404, "接口不存在")

    def handle_prod_task(self, method: str, parts: list, c: dict, user: dict):
        t_id = parts[3]
        t = _load_prod_task_row(c, t_id)
        sim = _load_prod_sim(c, t["sim_id"])
        if sim["factory"] not in user["factories"]:
            raise HTTPError(404, "记录不存在或不在授权范围")

        if len(parts) == 4:
            if method == "GET":  # progress
                return self.respond(200, {
                    "task": {k: t.get(k) for k in ("id", "sim_id", "status", "progress", "req_conf", "error", "created_at", "finished_at")},
                })
            if method == "POST" and t.get("status") == "running":
                raise HTTPError(409, "任务处理中")

        span = parts[4] if len(parts) >= 5 else None

        # 取消
        if span == "cancel" and method == "POST":
            if t["status"] in ("completed", "failed", "cancelled"):
                raise HTTPError(409, "任务已结束，不能取消")
            prog = dict(t.get("progress") or {})
            prog["cancel_requested"] = True
            c.execute("UPDATE prod_task SET progress=?, status='cancelling' WHERE id=?",
                      (json.dumps(prog, ensure_ascii=False), t_id))
            return self.respond(200, {"task_id": t_id, "status": "cancelling",
                                      "note": "取消标记已设置；未完成任务将不作为最终均值或提交"})

        # 取消后/未完成不得给最终均值
        if span == "result" and method == "GET":
            if t["status"] != "completed":
                raise HTTPError(409, "任务未完成，无法输出最终统计结果（需 n≥1000 且未取消）")
            ref = t.get("result_ref")
            path = (PROD_DATA_DIR / t_id / ref) if ref else None
            if ref and path and path.exists():
                result = json.loads(path.read_text(encoding="utf-8"))
            else:
                # fallback: recompute from stored jsonl
                rows = prod_jsonl_rows(t_id)
                strategies = (t.get("req_conf") or {}).get("strategies") or PROD_STRATEGIES
                stats = compute_prod_stats(rows, strategies)
                result = {"config": {"seed": (t.get("req_conf") or {}).get("seed", 0),
                                     "n": (t.get("req_conf") or {}).get("n", PROD_SAMPLES),
                                     "strategies": strategies,
                                     "complete": len(rows) >= (t.get("req_conf") or {}).get("n", PROD_SAMPLES)},
                          "per_strategy": stats,
                          "completed_samples": len(rows),
                          "failed_samples": sum(1 for r in rows
                                                if any(q is None or q.get("failed") is True
                                                       for q in (r.get("strategies") or {}).values()))}
            # 分页样本明细（轻量）
            rows_full = prod_jsonl_rows(t_id)
            page = max(1, int(parse_qs(urlparse(self.path).query).get("page", ["1"])[0]))
            per_page = int(parse_qs(urlparse(self.path).query).get("per_page", ["50"])[0])
            total = len(rows_full)
            start = (page - 1) * per_page
            page_rows = rows_full[start:start + per_page]
            result["samples_page"] = {
                "page": page, "per_page": per_page, "total": total,
                "items": [{k: s.get(k) for k in ("sample_idx", "seed", "reality", "strategies")} for s in page_rows],
            }
            return self.respond(200, result)

        if span == "csv" and method == "GET":
            rows = prod_jsonl_rows(t_id)
            strategies = t.get("req_conf", {}).get("strategies") or PROD_STRATEGIES
            lines = [",".join(["sample_idx", "seed", "strategy", "ok", "error",
                               "completion_rate", "overall_on_time_rate", "conditional_on_time_rate",
                               "blocked_sec", "makespan", "mean_tardy_sec"])]
            if not rows:
                lines.append("(无样本数据)")
            for r in rows:
                for st in strategies:
                    q = (r.get("strategies") or {}).get(st) or {}
                    ok_short = "0" if q.get("failed") else "1"
                    err = (q.get("error") or "").replace(",", " ").replace("\n", " ")
                    csv_row = [str(r.get("sample_idx")), str(r.get("seed")), st, ok_short, err,
                               str(q.get("completion_rate") or ""),
                               str(q.get("overall_on_time_rate") or ""),
                               str(q.get("conditional_on_time_rate") or ""),
                               str(q.get("blocked_sec") or ""),
                               str(q.get("makespan") or ""),
                               str(q.get("mean_tardy_sec") or "")]
                    lines.append(",".join(csv_row))
            return self.respond_csv("\n".join(lines) + "\n",
                                    f"production_samples_{t_id}.csv")

        if span == "sample" and method == "GET":
            # 按需生成单个样本真实方案时间表（无扰动样本1可等待/按产线筛选）
            q = parse_qs(urlparse(self.path).query)
            idx = max(0, int(q.get("sample", ["0"])[0]))
            strategy = q.get("strategy", [PROD_STRATEGIES[0]])[0]
            line = q.get("line", [None])[0]
            ps = _import_production_sim()
            conf = t.get("req_conf") or {}
            seed = int(conf.get("seed") or 0)
            n_vehicles = int(conf.get("n_vehicles") or 4200)
            arrival_sigma = float(conf.get("arrival_sigma") or 720.0)
            process_cv = float(conf.get("process_cv") or 0.08)
            failure_expected_per_line = float(conf.get("failure_expected_per_line") or 1.1)
            repair_mean_min = float(conf.get("repair_mean_min") or 40.0)
            sub_seed = seed + idx  # kernel: seed = seed_base + idx
            scenario = ps.build_production_scenario(n_vehicles=n_vehicles, seed=seed)
            reality = ps.sample_reality(scenario, sub_seed, arrival_sigma=arrival_sigma,
                                        process_cv=process_cv,
                                        failure_expected_per_line=failure_expected_per_line,
                                        repair_mean_min=repair_mean_min)
            w_fixed = ps.optimize_nominal_plan(scenario, seed=seed)
            res = ps.simulate(scenario, reality, strategy, w_fixed_seq=w_fixed)
            rd = res.to_dict() if hasattr(res, "to_dict") else {}
            timeline = res.timeline if hasattr(res, "timeline") else []
            if line is not None:
                timeline = [t for t in timeline if str(t.get("stage")) == str(line)]
            return self.respond(200, {
                "sample_id": idx, "strategy": strategy, "line_filter": line,
                "sub_seed": sub_seed, "timeline": timeline,
                "metrics": rd, "caveats": ["样本时间表为等效瓶颈流水仿真，非工位数字孪生/非工厂实测。"],
            })

        raise HTTPError(404, "接口不存在")

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self.handle_api("GET")
        return super().do_GET()

    def do_POST(self):
        self.handle_api("POST")

    def do_PATCH(self):
        self.handle_api("PATCH")


def recover_interrupted_tasks():
    """启动恢复：任何残留 queued/running/cancelling 任务视为中断（无后台线程存活），
    标 failed 并保留已落盘样本（0 或已完成均可），关联 plan 置 failed 以便重跑。
    不删库、不伪造 completed。"""
    recovered = 0
    with LOCK, connect() as c:
        rows = c.execute("SELECT id,plan_id,payload FROM tasks").fetchall()
        for r in rows:
            try:
                t = json.loads(r["payload"])
            except Exception:
                continue
            if t.get("status") in ("queued", "running", "cancelling"):
                prog = dict(t.get("progress") or {})
                prog["cancel_requested"] = False
                prog["finished_at"] = now_iso()
                t["status"] = "failed"
                t["error"] = "服务重启中断（任务未完成；样品文件保留，可重新启动仿真）"
                t["progress"] = prog
                t["finished_at"] = now_iso()
                c.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(t, ensure_ascii=False), r["id"]))
                try:
                    p = load_payload(c, "plans", r["plan_id"])
                    if p.get("status") == "running":
                        p["status"] = "failed"
                        p["updated_at"] = now_iso()
                        save_payload(c, "plans", p)
                except HTTPError:
                    pass
                recovered += 1
        # 兼容：旧 prod_task 孤立记录同样标 failed，避免永久卡住（不新建）
        try:
            for r in c.execute("SELECT id,payload FROM prod_task").fetchall():
                t = json.loads(r["payload"])
                if t.get("status") in ("queued", "running", "cancelling"):
                    t["status"] = "failed"; t["error"] = "服务重启中断"
                    c.execute("UPDATE prod_task SET payload=? WHERE id=?", (json.dumps(t, ensure_ascii=False), r["id"]))
        except Exception:
            pass
    if recovered:
        print(f"[recover] marked {recovered} interrupted task(s) as failed (samples preserved)", flush=True)
    return recovered


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18744)
    args = parser.parse_args()
    init_db()
    recover_interrupted_tasks()
    print(f"APS Integrated Workbench running on http://127.0.0.1:{args.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), IntegratedHandler).serve_forever()
