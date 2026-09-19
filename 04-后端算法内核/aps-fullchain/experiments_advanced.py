"""experiments_advanced.py - Automated Benchmark Execution Pipeline for Advanced Multi-Scale Scenarios.

Executes closed-loop evaluation of dispatching strategies across multi-scale (16, 32, 48 vehicles)
and compound perturbation scenarios:
1. Batch / Concurrent benchmark runner supporting Strategy A (EDD), Strategy B (ColorAware),
   and Strategy C (FixedNeighborhood).
2. End-to-end physical verification with independent validator (validate_trace).
3. Comprehensive KPI extraction:
   - TWT (True Weighted Tardiness of completed vehicles)
   - Blocking duration (Positive blocking F > C minutes)
   - Color switches (Paint setup frequency and setup duration)
   - Makespan (Total production schedule completion time)
   - Service rate (Completed / Active demand)
   - Stability metrics (Sequence inversions, day shift displacements, unstarted order adjustments)
   - Dynamic replan frequency and WIP protection audit
4. Multi-dimensional aggregation (by scale, by scenario category, by strategy).
5. Structured JSON report export and command-line entry point.
"""

from __future__ import annotations
from typing import Dict, List, Any, Optional, Tuple
import json
import time
import copy
import os
import sys
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from .fixtures.scenarios_advanced import (
    get_advanced_scenario,
    list_all_advanced_scenarios,
    list_scenarios_by_scale,
    ADVANCED_SCENARIOS_MAP
)
from .experiments import execute_strategy_closed_loop
from .schema import compute_object_hash


STRATEGIES = [
    "Strategy_A_EDD",
    "Strategy_B_ColorAware",
    "Strategy_C_FixedNeighborhood"
]


def extract_scenario_metadata(scenario_id: str) -> Dict[str, Any]:
    """Extracts scale and perturbation category from scenario_id."""
    parts = scenario_id.split("_")
    scale = 16
    for p in parts:
        if p.isdigit():
            scale = int(p)
            break
    cat = "NORMAL"
    if "DUAL_DELAY" in scenario_id:
        cat = "DUAL_DELAY_LABOR_CUT"
    elif "VIP_RUSH" in scenario_id:
        cat = "VIP_RUSH_CANCEL_SHOCK"
    elif "BOTTLENECK" in scenario_id:
        cat = "BOTTLENECK_COLOR_ALUM"
    return {"scale": scale, "category": cat}


def run_advanced_scenario_matrix(
    scenario_id: str,
    strategies: Optional[List[str]] = None,
    seed: int = 42,
    include_trace_details: bool = False
) -> Dict[str, Any]:
    """Runs all candidate strategies on the exact same scenario and returns a comparison matrix."""
    if strategies is None:
        strategies = STRATEGIES

    meta = extract_scenario_metadata(scenario_id)
    scenario = get_advanced_scenario(scenario_id)

    results: Dict[str, Dict[str, Any]] = {}
    best_strategy = ""
    best_rank = (999, 999999, 999999, 999999, 999999, 999999)

    for strat in strategies:
        # Run closed-loop execution
        res = execute_strategy_closed_loop(scenario, strat, seed=seed)

        # Build clean output dict
        res_clean = {
            "strategy_name": res["strategy_name"],
            "scenario_id": res["scenario_id"],
            "scale": meta["scale"],
            "category": meta["category"],
            "is_valid": res["is_valid"],
            "validation_errors": res["validation_errors"],
            "service_rate": res["service_rate"],
            "true_weighted_tardiness_min": res["true_weighted_tardiness_min"],
            "raw_completed_tardiness_min": res["raw_completed_tardiness_min"],
            "total_blocked_min": res["total_blocked_min"],
            "color_switches": res["color_switches"],
            "makespan_min": res["makespan_min"],
            "stability_penalty": res["stability_penalty"],
            "inversion_count": res["inversion_count"],
            "day_shift_count": res["day_shift_count"],
            "unmet_count": res["unmet_count"],
            "cancelled_unstarted_count": res["cancelled_unstarted_count"],
            "active_demand_count": res["active_demand_count"],
            "completed_count": res["completed_count"],
            "lexicographical_rank": res["lexicographical_rank"],
            "replan_count": res["replan_count"],
            "replan_records": res["replan_records"]
        }

        if include_trace_details:
            res_clean["material_trajectory"] = res.get("material_trajectory", [])
            res_clean["labor_trajectory"] = res.get("labor_trajectory", [])
            res_clean["buffer_trajectory"] = res.get("buffer_trajectory", [])

        results[strat] = res_clean

        if res_clean["is_valid"] and res_clean["lexicographical_rank"] < best_rank:
            best_rank = res_clean["lexicographical_rank"]
            best_strategy = strat

    return {
        "scenario_id": scenario_id,
        "scale": meta["scale"],
        "category": meta["category"],
        "results": results,
        "best_strategy": best_strategy,
        "best_rank": best_rank
    }


def compute_cross_scenario_aggregates(summary_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Computes multidimensional aggregates across strategies, scales, and perturbation types."""
    # 1. By Strategy
    by_strategy: Dict[str, Dict[str, Any]] = {}
    for row in summary_rows:
        strat = row["strategy"]
        if strat not in by_strategy:
            by_strategy[strat] = {
                "runs_count": 0,
                "valid_runs": 0,
                "total_completed": 0,
                "total_active": 0,
                "total_twt_min": 0,
                "total_blocked_min": 0,
                "total_color_switches": 0,
                "total_makespan_min": 0,
                "total_replans": 0,
                "avg_color_switches": 0.0,
                "avg_twt_min": 0.0,
                "avg_makespan_min": 0.0,
                "overall_service_rate": 0.0
            }
        s = by_strategy[strat]
        s["runs_count"] += 1
        if row["is_valid"]:
            s["valid_runs"] += 1
        s["total_completed"] += row["completed"]
        s["total_active"] += row["active_orders"]
        s["total_twt_min"] += row["twt_min"]
        s["total_blocked_min"] += row["blocked_min"]
        s["total_color_switches"] += row["color_switches"]
        s["total_makespan_min"] += row["makespan_min"]
        s["total_replans"] += row["replan_count"]

    for strat, s in by_strategy.items():
        n = max(1, s["runs_count"])
        s["avg_color_switches"] = round(s["total_color_switches"] / n, 2)
        s["avg_twt_min"] = round(s["total_twt_min"] / n, 2)
        s["avg_makespan_min"] = round(s["total_makespan_min"] / n, 2)
        s["overall_service_rate"] = round(s["total_completed"] / max(1, s["total_active"]), 4)

    # 2. By Scale & Strategy
    by_scale_strategy: Dict[str, Dict[str, Any]] = {}
    for row in summary_rows:
        key = f"Scale_{row['scale']}_{row['strategy']}"
        if key not in by_scale_strategy:
            by_scale_strategy[key] = {
                "scale": row["scale"],
                "strategy": row["strategy"],
                "runs_count": 0,
                "avg_color_switches": 0.0,
                "avg_twt_min": 0.0,
                "avg_makespan_min": 0.0,
                "service_rate": 0.0,
                "_total_switches": 0,
                "_total_twt": 0,
                "_total_ms": 0,
                "_comp": 0,
                "_active": 0
            }
        bss = by_scale_strategy[key]
        bss["runs_count"] += 1
        bss["_total_switches"] += row["color_switches"]
        bss["_total_twt"] += row["twt_min"]
        bss["_total_ms"] += row["makespan_min"]
        bss["_comp"] += row["completed"]
        bss["_active"] += row["active_orders"]

    for key, bss in by_scale_strategy.items():
        n = max(1, bss["runs_count"])
        bss["avg_color_switches"] = round(bss["_total_switches"] / n, 2)
        bss["avg_twt_min"] = round(bss["_total_twt"] / n, 2)
        bss["avg_makespan_min"] = round(bss["_total_ms"] / n, 2)
        bss["service_rate"] = round(bss["_comp"] / max(1, bss["_active"]), 4)
        del bss["_total_switches"]
        del bss["_total_twt"]
        del bss["_total_ms"]
        del bss["_comp"]
        del bss["_active"]

    # 3. By Category & Strategy
    by_category_strategy: Dict[str, Dict[str, Any]] = {}
    for row in summary_rows:
        key = f"{row['category']}_{row['strategy']}"
        if key not in by_category_strategy:
            by_category_strategy[key] = {
                "category": row["category"],
                "strategy": row["strategy"],
                "runs_count": 0,
                "avg_color_switches": 0.0,
                "avg_twt_min": 0.0,
                "avg_makespan_min": 0.0,
                "_total_switches": 0,
                "_total_twt": 0,
                "_total_ms": 0,
            }
        bcs = by_category_strategy[key]
        bcs["runs_count"] += 1
        bcs["_total_switches"] += row["color_switches"]
        bcs["_total_twt"] += row["twt_min"]
        bcs["_total_ms"] += row["makespan_min"]

    for key, bcs in by_category_strategy.items():
        n = max(1, bcs["runs_count"])
        bcs["avg_color_switches"] = round(bcs["_total_switches"] / n, 2)
        bcs["avg_twt_min"] = round(bcs["_total_twt"] / n, 2)
        bcs["avg_makespan_min"] = round(bcs["_total_ms"] / n, 2)
        del bcs["_total_switches"]
        del bcs["_total_twt"]
        del bcs["_total_ms"]

    return {
        "by_strategy": by_strategy,
        "by_scale_strategy": by_scale_strategy,
        "by_category_strategy": by_category_strategy
    }


def run_advanced_benchmark_suite(
    scenario_ids: Optional[List[str]] = None,
    scales: Optional[List[int]] = None,
    seed: int = 42,
    max_workers: int = 1,
    include_trace_details: bool = False
) -> Dict[str, Any]:
    """Executes full benchmark suite across selected or all advanced scenarios."""
    start_time = time.time()

    if scenario_ids is None:
        if scales is not None:
            scenario_ids = []
            for s in scales:
                scenario_ids.extend(list_scenarios_by_scale(s))
        else:
            scenario_ids = list_all_advanced_scenarios()

    matrix_results: Dict[str, Any] = {}
    summary_table: List[Dict[str, Any]] = []

    if max_workers > 1:
        # Concurrent execution using thread pool
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_sc = {
                executor.submit(run_advanced_scenario_matrix, sc_id, STRATEGIES, seed, include_trace_details): sc_id
                for sc_id in scenario_ids
            }
            for future in as_completed(future_to_sc):
                sc_id = future_to_sc[future]
                res_matrix = future.result()
                matrix_results[sc_id] = res_matrix
    else:
        # Deterministic sequential batch execution
        for sc_id in scenario_ids:
            res_matrix = run_advanced_scenario_matrix(
                sc_id, STRATEGIES, seed=seed, include_trace_details=include_trace_details
            )
            matrix_results[sc_id] = res_matrix

    # Populate summary table in deterministic order
    for sc_id in scenario_ids:
        sc_matrix = matrix_results[sc_id]
        meta = extract_scenario_metadata(sc_id)
        for strat_name in STRATEGIES:
            r = sc_matrix["results"][strat_name]
            summary_table.append({
                "scenario": sc_id,
                "scale": meta["scale"],
                "category": meta["category"],
                "strategy": strat_name,
                "is_valid": r["is_valid"],
                "active_orders": r["active_demand_count"],
                "completed": r["completed_count"],
                "unmet": r["unmet_count"],
                "service_rate": f"{r['service_rate']*100:.1f}%" if r["service_rate"] is not None else "N/A",
                "twt_min": r["true_weighted_tardiness_min"],
                "blocked_min": r["total_blocked_min"],
                "color_switches": r["color_switches"],
                "makespan_min": r["makespan_min"],
                "stability_penalty": r["stability_penalty"],
                "replan_count": r["replan_count"],
                "rank": r["lexicographical_rank"]
            })

    elapsed = round(time.time() - start_time, 2)
    aggregates = compute_cross_scenario_aggregates(summary_table)

    return {
        "metadata": {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_seconds": elapsed,
            "scenarios_count": len(scenario_ids),
            "total_runs": len(scenario_ids) * len(STRATEGIES),
            "scales_evaluated": sorted(list(set(extract_scenario_metadata(sid)["scale"] for sid in scenario_ids))),
            "strategies": STRATEGIES
        },
        "matrix": matrix_results,
        "summary_table": summary_table,
        "aggregates": aggregates
    }


def export_benchmark_report(report_data: Dict[str, Any], file_path: str) -> None:
    """Exports structured benchmark report to a JSON file."""
    abs_path = os.path.abspath(file_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2, ensure_ascii=False)


def print_formatted_summary(report: Dict[str, Any]) -> None:
    """Prints a clean ASCII summary table to stdout."""
    meta = report["metadata"]
    print("=" * 110)
    print(f"APS FULLCHAIN ADVANCED BENCHMARK REPORT | Elapsed: {meta['elapsed_seconds']}s | Runs: {meta['total_runs']}")
    print("=" * 110)
    header = f"{'Scenario':<34} {'Strategy':<28} {'Valid':<6} {'Comp/Act':<9} {'TWT(m)':<8} {'Switches':<9} {'Blocked':<8} {'Makespan':<9}"
    print(header)
    print("-" * 110)
    for row in report["summary_table"]:
        comp_act = f"{row['completed']}/{row['active_orders']}"
        valid_str = "PASS" if row["is_valid"] else "FAIL"
        print(f"{row['scenario']:<34} {row['strategy']:<28} {valid_str:<6} {comp_act:<9} {row['twt_min']:<8} {row['color_switches']:<9} {row['blocked_min']:<8} {row['makespan_min']:<9}")
    print("=" * 110)

    print("\n--- Strategy Performance Aggregates across All Advanced Runs ---")
    for strat, data in report["aggregates"]["by_strategy"].items():
        print(f"  * {strat:<28}: Valid={data['valid_runs']}/{data['runs_count']}, Avg Color Switches={data['avg_color_switches']:>5.1f}, Avg TWT={data['avg_twt_min']:>7.1f}m, Service Rate={data['overall_service_rate']*100:.1f}%")
    print("=" * 110)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="APS Fullchain Advanced Benchmark Runner")
    parser.add_argument("--scales", type=int, nargs="+", choices=[16, 32, 48], help="Target vehicle scales to run")
    parser.add_argument("--scenarios", type=str, nargs="+", help="Specific scenario IDs to evaluate")
    parser.add_argument("--output", type=str, default="/Users/huangrq25/Desktop/其他/Anio/aps-fullchain/advanced_benchmark_results.json", help="Output JSON path")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent workers count")
    parser.add_argument("--include-traces", action="store_true", help="Include fine-grained trace logs in JSON")

    args = parser.parse_args()

    report = run_advanced_benchmark_suite(
        scenario_ids=args.scenarios,
        scales=args.scales,
        max_workers=args.workers,
        include_trace_details=args.include_traces
    )

    export_benchmark_report(report, args.output)
    print_formatted_summary(report)
    print(f"\nReport exported successfully to: {os.path.abspath(args.output)}")
