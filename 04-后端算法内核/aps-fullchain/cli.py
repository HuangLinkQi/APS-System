"""cli.py - Command Line Interface for aps-fullchain.

Usage:
  python3 -m aps_fullchain.cli run [--scenario SCENARIO_ID] [--out OUT_PATH]
  python3 -m aps_fullchain.cli test
"""

from __future__ import annotations
import argparse
import json
import sys
import os
import unittest


def main():
    parser = argparse.ArgumentParser(description="APS Fullchain Simulation & Verification Suite")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Command: run
    run_parser = subparsers.add_parser("run", help="Execute full-chain experiment and generate trace bundle")
    run_parser.add_argument("--scenario", default="MICRO_BENCHMARK_12", help="Scenario ID (e.g. SCENARIO_1_NORMAL, SCENARIO_2_SUPPLY_DELAY, ...)")
    run_parser.add_argument("--all", action="store_true", help="Execute all 7 scenarios x 3 strategies closed-loop matrix")
    run_parser.add_argument("--out", default=None, help="Output JSON path for TraceBundle or Matrix")

    # Command: test
    test_parser = subparsers.add_parser("test", help="Run comprehensive G1-G5 test suite")

    args = parser.parse_args()

    if args.command == "test":
        # Run unittests
        loader = unittest.TestLoader()
        suite = loader.discover(start_dir=os.path.join(os.path.dirname(__file__), "tests"))
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)

    elif args.command == "run" or args.command is None:
        if getattr(args, "all", False):
            from .experiments import run_all_scenarios_matrix
            print("=== Running Full Closed-Loop Matrix: 7 Scenarios x 3 Strategies ===")
            matrix_data = run_all_scenarios_matrix()

            print("\n========================================================================================================================")
            print(f"{'Scenario':<34} | {'Strategy':<30} | {'Valid':<5} | {'Active':<6} | {'Comp':<5} | {'Unmet':<5} | {'SvcRate':<7} | {'TWT(m)':<6} | {'Blk(m)':<6} | {'Color':<5}")
            print("------------------------------------------------------------------------------------------------------------------------")
            for row in matrix_data["summary_table"]:
                v_str = "YES" if row["is_valid"] else "NO"
                print(f"{row['scenario']:<34} | {row['strategy']:<30} | {v_str:<5} | {row['active_orders']:<6} | {row['completed']:<5} | {row['unmet']:<5} | {row['service_rate']:<7} | {row['twt_min']:<6} | {row['blocked_min']:<6} | {row['color_switches']:<5}")
            print("========================================================================================================================")

            out_path = getattr(args, "out", None) or os.path.join(os.path.dirname(__file__), "matrix_results.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(matrix_data, f, indent=2, ensure_ascii=False)
            print(f"\nMatrix results saved to: {out_path}")
            return

        from .experiments import run_experiment
        scenario_id = getattr(args, "scenario", "MICRO_BENCHMARK_12")
        print(f"=== Running APS Fullchain Closed-Loop Experiment [{scenario_id}] ===")
        bundle = run_experiment(scenario_id)

        print("\n--- 1. Demand Netting & Conservation ---")
        dn = bundle["demand_netting"]
        print(f"  Balance verified: {dn['balance_verified']}")
        print(f"  Original Forecast: {dn['original_forecast']} | Consumed: {dn['consumed_forecast']} | Residual: {dn['residual_forecast']}")
        print(f"  Firm Orders: {dn['firm_orders']} | Net Demand: {dn['net_demand']}")

        print("\n--- 2. Aggregate & Multi-Strategy Candidate Benchmarking (t=0) ---")
        for cand in bundle["candidate_evaluations"]:
            m = cand["metrics"]
            print(f"  Candidate: {cand['name']:<28} | Valid: {cand['is_valid']} | Makespan: {m['makespan_min']}m | TWT: {m['true_weighted_tardiness_min']}m | Blocked: {m['total_blocked_min']}m | ColorSwitches: {m['color_switches']} | LexicoRank: {m['lexicographical_rank']}")

        print(f"\n--- 3. Selected Strategy & Lifecycle Approval ---")
        print(f"  Selected: {bundle['selected_strategy']}")
        print(f"  Baseline: {bundle['lifecycle']['baseline_id']} | Status: {bundle['lifecycle']['status']} | Receipt: {bundle['lifecycle']['receipt_id']}")

        print("\n--- 4. Physical Execution & Disruption Re-plan ---")
        ex = bundle["execution_summary"]
        print(f"  Prefix events at tau=100 (frozen): {ex['prefix_events_at_tau100']}")
        print(f"  Total executed events: {ex['total_final_events']}")
        print(f"  Positive blocking (F > C) count: {ex['positive_blocking_occurrences']}")
        for b in ex["blocked_details"]:
            print(f"    -> VIN {b['vin']} at stage {b['stage']}: C={b['C']}m, F={b['F']}m (Blocked {b['blocked_duration']}m)")
        print(f"  Pullout car completed: {ex['pullout_completions']}")
        print(f"  Orders breakdown: Original={ex.get('original_order_count', 16)}, Cancelled(Unstarted)={ex.get('cancelled_unstarted_count', 0)}, Active={ex.get('active_demand_count', 15)}")
        svc_str = f"{ex.get('service_rate') * 100:.1f}%" if ex.get('service_rate') is not None else "N/A"
        print(f"  Completed cars: {ex['completed_vins_count']} | Unmet cars: {ex['unmet_vins_count']} | Service Rate: {svc_str} | True TWT: {ex.get('true_weighted_tardiness_min', 0)}m")

        print("\n--- 5. Independent Validator (G2 Gate) ---")
        val = bundle["validation_report"]
        print(f"  Is Valid: {val['is_valid']}")
        print(f"  Checks: {len(val['checks_performed'])} physical invariants verified")
        if val["errors"]:
            print(f"  ERRORS: {val['errors']}")
        else:
            print("  ALL PHYSICAL INVARIANTS SATISFIED (Zero violations).")

        if "closed_loop_matrix" in bundle:
            clm = bundle["closed_loop_matrix"]
            print("\n--- 6. Closed-Loop Multi-Strategy Physical Execution Comparison ---")
            print(f"{'Strategy':<30} | {'Valid':<5} | {'Comp':<5} | {'Unmet':<5} | {'SvcRate':<7} | {'TWT(m)':<6} | {'Blk(m)':<6} | {'Color':<5}")
            print("-" * 85)
            for sname, sres in clm["results"].items():
                v_str = "YES" if sres["is_valid"] else "NO"
                sr_str = f"{sres['service_rate']*100:.1f}%" if sres["service_rate"] is not None else "N/A"
                print(f"{sname:<30} | {v_str:<5} | {sres['completed_count']:<5} | {sres['unmet_count']:<5} | {sr_str:<7} | {sres['true_weighted_tardiness_min']:<6} | {sres['total_blocked_min']:<6} | {sres['color_switches']:<5}")
            print(f"  Best Executed Strategy: {clm['best_strategy']}")

        out_path = getattr(args, "out", None)
        if out_path:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(bundle, f, indent=2, ensure_ascii=False)
            print(f"\nTraceBundle saved to: {out_path}")
        else:
            default_out = os.path.join(os.path.dirname(__file__), "trace_bundle.json")
            with open(default_out, "w", encoding="utf-8") as f:
                json.dump(bundle, f, indent=2, ensure_ascii=False)
            print(f"\nTraceBundle saved to: {default_out}")


if __name__ == "__main__":
    main()
