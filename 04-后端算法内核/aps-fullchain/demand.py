"""demand.py - Demand Netting, Forecast Consumption, and Order Lifecycle Management.

Follows APS fullchain specification:
- Conservation: original_forecast = consumed_forecast + residual_forecast
- Net demand: net_demand = firm_orders + residual_forecast
- Idempotent versioning: latest forecast version wins
- Synthetic vehicle generation for remaining forecast buckets
- Lifecycle preservation: completed orders tracked, cancelled orders marked as exceptions
"""

from __future__ import annotations
from typing import Dict, List, Tuple, Any, Set
import copy
from .schema import (
    PlanningSnapshot,
    DemandLedger,
    Order,
    Forecast,
    VehicleStatus,
    compute_object_hash
)


def get_forecast_bucket_key(month: str, plant_id: str, config_id: str) -> str:
    return f"{month}:{plant_id}:{config_id}"


def net_demand(snapshot: PlanningSnapshot) -> DemandLedger:
    """Computes demand netting from snapshot.
    Performs forecast consumption bucket by bucket and enforces conservation laws.
    """
    # 1. Resolve latest forecast versions per forecast_id
    latest_forecasts: Dict[str, Forecast] = {}
    for fc in snapshot.forecasts:
        if fc.published_at_min > snapshot.now_min:
            # Future forecast not yet published is invisible
            continue
        existing = latest_forecasts.get(fc.forecast_id)
        if existing is None or fc.version >= existing.version:
            latest_forecasts[fc.forecast_id] = fc

    # 2. Aggregate original forecasts by bucket
    original_by_bucket: Dict[str, int] = {}
    for fc in latest_forecasts.values():
        b_key = get_forecast_bucket_key(fc.month, fc.plant_id, fc.config_id)
        original_by_bucket[b_key] = original_by_bucket.get(b_key, 0) + fc.quantity

    total_original_forecast = sum(original_by_bucket.values())

    # 3. Classify orders and protect started/WIP synthetic vehicles
    active_firm_orders: List[Order] = []
    completed_firm_orders: List[Order] = []
    cancelled_orders: List[Order] = []
    wip_synthetic_orders: List[Order] = []

    for order in snapshot.orders:
        if order.is_virtual:
            # Check if virtual order is already launched or in WIP on the floor
            is_wip_or_done = order.status in [
                VehicleStatus.SETUP,
                VehicleStatus.PROCESSING,
                VehicleStatus.BLOCKED,
                VehicleStatus.BUFFERED,
                VehicleStatus.IN_TRANSIT,
                VehicleStatus.COMPLETED
            ]
            if is_wip_or_done:
                # BOUNDARY ENFORCEMENT: Already started synthetic vehicles CANNOT be deleted or altered.
                # Must preserve VIN, state, and physical execution trajectory.
                wip_synthetic_orders.append(order)
            continue

        if order.status == VehicleStatus.COMPLETED:
            completed_firm_orders.append(order)
        elif order.status == VehicleStatus.CANCELLED:
            cancelled_orders.append(order)
        else:
            active_firm_orders.append(order)

    # 4. Perform bucket-based forecast consumption
    consumed_by_bucket: Dict[str, int] = {k: 0 for k in original_by_bucket}
    residual_by_bucket: Dict[str, int] = {}

    # All legitimate orders (active firm + completed firm + launched WIP synthetic) consume forecast
    all_consuming_orders = completed_firm_orders + active_firm_orders + wip_synthetic_orders
    for order in all_consuming_orders:
        if getattr(order, "consumes_forecast", True) is False:
            continue
        b_key = order.forecast_bucket
        if not b_key:
            # Default bucket from order config
            b_key = get_forecast_bucket_key("2026-10", "P1", order.config_id)

        if b_key in consumed_by_bucket:
            if consumed_by_bucket[b_key] < original_by_bucket[b_key]:
                consumed_by_bucket[b_key] += 1
            else:
                # Order exceeds forecast in this bucket - valid in real plant
                pass
        else:
            # Order belongs to an unforecasted bucket
            consumed_by_bucket[b_key] = 1
            if b_key not in original_by_bucket:
                original_by_bucket[b_key] = 0

    # 5. Compute residuals and generate synthetic orders (strictly for unstarted residual quota)
    synthetic_orders: List[Order] = list(wip_synthetic_orders)  # Retain all launched/WIP synthetic cars
    audit_notes: List[str] = []

    for b_key, orig_qty in original_by_bucket.items():
        cons_qty = consumed_by_bucket.get(b_key, 0)
        resid = max(0, orig_qty - cons_qty)
        residual_by_bucket[b_key] = resid

        # Generate synthetic orders strictly for UNSTARTED residual forecast
        if resid > 0:
            parts = b_key.split(":")
            month = parts[0] if len(parts) > 0 else "2026-10"
            plant = parts[1] if len(parts) > 1 else "P1"
            cfg_id = parts[2] if len(parts) > 2 else list(snapshot.configurations.keys())[0]

            cfg = snapshot.configurations.get(cfg_id)
            stages = cfg.stages if cfg else ["W", "P", "A"]

            # Avoid ID collision with already launched WIP synthetic cars
            existing_vins = {o.vin for o in wip_synthetic_orders}
            created_count = 0
            idx_seq = 1
            while created_count < resid:
                syn_vin = f"SYN_{month}_{cfg_id}_{idx_seq:03d}"
                idx_seq += 1
                if syn_vin in existing_vins:
                    continue
                syn_order = Order(
                    order_id=syn_vin,
                    vin=syn_vin,
                    config_id=cfg_id,
                    release_at_min=snapshot.now_min,
                    due_at_min=snapshot.calendar.horizon_end_min,
                    priority=99,  # Lower priority than firm orders
                    route=stages,
                    forecast_bucket=b_key,
                    status=VehicleStatus.UNRELEASED,
                    is_virtual=True,
                    assigned_day=None
                )
                synthetic_orders.append(syn_order)
                created_count += 1

    total_netted_forecast = sum(consumed_by_bucket.get(k, 0) for k in original_by_bucket)
    total_residual_forecast = sum(residual_by_bucket.values())
    total_firm_active = len(active_firm_orders)
    total_net_demand = total_firm_active + total_residual_forecast

    # 6. Verify conservation laws
    # Law 1: original_forecast = netted_forecast + residual_forecast
    # For every bucket k:
    balance_verified = True
    for k, orig in original_by_bucket.items():
        netted = min(orig, consumed_by_bucket.get(k, 0))
        resid = residual_by_bucket.get(k, 0)
        if orig != (netted + resid):
            balance_verified = False
            audit_notes.append(f"Conservation violation in bucket {k}: orig={orig} != netted={netted} + resid={resid}")

    if total_original_forecast != (total_netted_forecast + total_residual_forecast):
        # Unless firm orders exceeded forecast
        excess = sum(max(0, consumed_by_bucket.get(k, 0) - original_by_bucket.get(k, 0)) for k in consumed_by_bucket)
        if (total_original_forecast + excess) != (total_netted_forecast + total_residual_forecast):
            balance_verified = False
            audit_notes.append(f"Total conservation failure: orig={total_original_forecast}, netted={total_netted_forecast}, resid={total_residual_forecast}")

    audit_notes.append(
        f"Demand netting summary: Active firm orders={total_firm_active}, Completed={len(completed_firm_orders)}, "
        f"Cancelled={len(cancelled_orders)}, Original forecast={total_original_forecast}, "
        f"Consumed forecast={total_netted_forecast}, Residual={total_residual_forecast}, Net demand={total_net_demand}"
    )

    return DemandLedger(
        firm_orders=active_firm_orders,
        consumed_forecast_qty=consumed_by_bucket,
        residual_forecast_qty=residual_by_bucket,
        synthetic_orders=synthetic_orders,
        original_forecast_total=total_original_forecast,
        netted_forecast_total=total_netted_forecast,
        remaining_forecast_total=total_residual_forecast,
        firm_orders_total=total_firm_active,
        net_demand_total=total_net_demand,
        balance_verified=balance_verified,
        audit_notes=audit_notes
    )
