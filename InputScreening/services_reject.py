"""Input Screening – Partial Accept / Partial Reject tray allocation engine.

Business rules enforced:
  - Backend is sole source of truth for all allocation calculations.
  - One rejection reason per reject tray (no mixing).
  - Tray qty cannot exceed tray capacity.
  - Existing active trays may be reused (delinked) for reject, up to delink_count.
  - New reject tray IDs are generated sequentially from the highest existing ID.
  - New accept tray IDs must be validated as free/unoccupied in TrayId master.
  - If no free accept tray is available, submission is blocked entirely.
  - All DB writes are wrapped in transaction.atomic().

Architecture:
  This module is the *engine layer* – it performs all calculations and DB writes.
  The views call these helpers; the frontend only renders the returned JSON.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# SUBMISSION FLAGS — update on the correct master table
# ─────────────────────────────────────────────────────────────────────────────

def _mark_lot_submitted_flags(lot_id: str, accepted_qty: int = 0) -> None:
    """Flip the Input-Screening submission flags for ``lot_id``.

    Historically this was attempted on ``ModelMasterCreation`` but those
    columns live on ``TotalStockModel``. Using ``update()`` keeps the write
    cheap and atomic – no-op if the lot has no matching row.
    """
    from modelmasterapp.models import TotalStockModel

    TotalStockModel.objects.filter(lot_id=lot_id).update(
        rejected_ip_stock=False,
        few_cases_accepted_Ip_stock=True,
        accepted_tray_scan_status=True,
        total_IP_accpeted_quantity=accepted_qty,
        last_process_module="Input Screening",
        next_process_module="Brass QC",
        current_stage="Input Screening",
        last_process_date_time=timezone.now(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# TRAY ID GENERATION
# ─────────────────────────────────────────────────────────────────────────────

_TRAY_ID_PATTERN = re.compile(r"^([A-Z]+-[A-Z]+)(\d+)$")


def _parse_tray_number(tray_id: str) -> Optional[Tuple[str, int]]:
    """Return (prefix, number) from a tray ID such as 'NB-A00300', or None."""
    m = _TRAY_ID_PATTERN.match(tray_id.strip().upper())
    if not m:
        return None
    return m.group(1), int(m.group(2))


def _generate_new_tray_ids(prefix: str, count: int, reserved: set) -> List[str]:
    """Generate ``count`` sequential new tray IDs with the given ``prefix``.

    Queries the TrayId master table to find the current maximum number for
    ``prefix``, then emits count IDs starting from max+1.  The ``reserved``
    set prevents collisions with IDs already consumed earlier in the same
    allocation session.

    Args:
        prefix:   e.g. ``"NB-A"``
        count:    how many IDs to generate
        reserved: set of IDs already allocated this session (mutated in place)

    Returns:
        List of new tray ID strings.
    """
    from modelmasterapp.models import TrayId

    existing_ids = TrayId.objects.filter(
        tray_id__startswith=prefix
    ).values_list("tray_id", flat=True)

    max_num = 0
    for tid in existing_ids:
        parsed = _parse_tray_number(tid)
        if parsed and parsed[0] == prefix:
            max_num = max(max_num, parsed[1])

    # Also honour already-reserved IDs from this session
    for tid in reserved:
        parsed = _parse_tray_number(tid)
        if parsed and parsed[0] == prefix:
            max_num = max(max_num, parsed[1])

    # Determine zero-pad width from existing IDs (default 5)
    pad_width = 5
    sample = next(
        (
            t
            for t in existing_ids
            if t.startswith(prefix)
        ),
        None,
    )
    if sample:
        parsed = _parse_tray_number(sample)
        if parsed:
            pad_width = len(sample) - len(parsed[0])

    generated: List[str] = []
    for i in range(1, count + 1):
        new_id = f"{prefix}{(max_num + i):0{pad_width}d}"
        generated.append(new_id)
        reserved.add(new_id)

    return generated


# ─────────────────────────────────────────────────────────────────────────────
# FREE ACCEPT TRAY VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_free_accept_tray_ids(
    tray_type: Optional[str],
    tray_capacity: Optional[int],
    needed: int,
    reserved: set,
) -> List[str]:
    """Fetch ``needed`` free tray IDs from the TrayId master that are:
      - not assigned to any lot (lot_id is null)
      - not linked to any batch (batch_id is null)
      - not delinked, rejected, or scanned
      - match tray_type and tray_capacity when provided

    Raises ValueError if fewer than ``needed`` free trays are available.
    """
    from modelmasterapp.models import TrayId

    qs = TrayId.objects.filter(
        lot_id__isnull=True,
        batch_id__isnull=True,
        delink_tray=False,
        rejected_tray=False,
        scanned=False,
    )
    if tray_type:
        qs = qs.filter(tray_type__iexact=tray_type)
    if tray_capacity:
        qs = qs.filter(tray_capacity=tray_capacity)

    free_ids = list(
        qs.exclude(tray_id__in=reserved)
        .order_by("id")
        .values_list("tray_id", flat=True)[: needed + len(reserved)]
    )

    # Exclude already-reserved from this session
    free_ids = [t for t in free_ids if t not in reserved]

    if len(free_ids) < needed:
        raise ValueError(
            f"Insufficient free trays available for accept allocation. "
            f"Need {needed}, found {len(free_ids)}. "
            f"Please register more free trays in the master before submitting."
        )

    result = free_ids[:needed]
    reserved.update(result)
    return result


def validate_accept_free_tray(tray_id: str) -> Dict[str, Any]:
    """Validate a single tray ID against the TrayId master for accept allocation.

    Returns a dict with 'valid' bool and 'reason' string.
    """
    from modelmasterapp.models import TrayId

    try:
        tray = TrayId.objects.get(tray_id=tray_id)
    except TrayId.DoesNotExist:
        return {"valid": False, "reason": "Tray ID not found in master"}

    if tray.lot_id:
        return {"valid": False, "reason": f"Tray occupied by lot {tray.lot_id}"}
    if tray.batch_id_id:
        return {"valid": False, "reason": "Tray linked to an active batch"}
    if tray.rejected_tray:
        return {"valid": False, "reason": "Tray is permanently rejected"}
    if tray.scanned:
        return {"valid": False, "reason": "Tray is currently scanned/in-use"}
    if tray.delink_tray:
        return {"valid": False, "reason": "Tray is in delinked state"}

    return {"valid": True, "reason": "Free and available"}


# ─────────────────────────────────────────────────────────────────────────────
# REJECT TRAY ALLOCATION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def _allocate_reject_trays(
    reasons: List[Dict[str, Any]],
    active_trays: List[Dict[str, Any]],
    delink_count: int,
    capacity: int,
    reserved_ids: set,
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """Allocate reject qty across trays, one reason per tray.

    Strategy:
      1. Build a delink pool: existing active trays sorted by qty ASC
         (prefer partial/smaller trays first to minimise waste).
      2. For each reason, pack into tray slots of ``capacity`` each.
         - While delink_pool has entries and delink budget allows → use
           existing tray (label 'reused').
         - Otherwise → generate new tray ID (label 'new').
      3. The engine does NOT mix reasons into the same tray.

    Returns:
        (allocations, delinked_tray_ids, new_tray_ids)
        - allocations: list of per-slot dicts with tray_id, reason, qty, source
        - delinked_tray_ids: IDs of existing active trays consumed by reject
        - new_tray_ids: IDs of brand-new tray IDs created for reject
    """
    # Sort active trays by qty ascending – prefer to delink partial trays
    delink_pool: List[Dict] = sorted(
        active_trays, key=lambda t: t.get("qty") or 0
    )[:delink_count]

    delink_available: List[Dict] = list(delink_pool)
    delinked_ids: List[str] = []
    new_tray_ids: List[str] = []
    allocations: List[Dict[str, Any]] = []

    # Pre-count how many new trays we might need (upper bound for generation).
    # NOTE: One reason per tray (no mixing), so each reason consumes
    # ceil(qty / capacity) tray slots independently. Using the combined
    # total_reject would under-provision the pool whenever reasons
    # straddle a capacity boundary (e.g. 11+8+8 in a 16-cap tray needs
    # 3 trays, not ceil(27/16)=2).
    max_new_needed = sum(
        math.ceil((r.get("qty", 0) or 0) / capacity)
        for r in reasons
        if (r.get("qty", 0) or 0) > 0
    )

    # Generate a pool of new IDs upfront (avoids repeated DB scans)
    tray_prefix = _infer_prefix(active_trays)
    new_pool = _generate_new_tray_ids(tray_prefix, max_new_needed, reserved_ids)
    new_pool_idx = 0

    for reason in reasons:
        remaining = reason.get("qty", 0)
        if remaining <= 0:
            continue

        while remaining > 0:
            fill_qty = min(remaining, capacity)

            if delink_available:
                # Use an existing active tray
                tray = delink_available.pop(0)
                tid = tray["tray_id"]
                delinked_ids.append(tid)
                allocations.append(
                    {
                        "reason_id": reason["reason_id"],
                        "reason_text": reason["reason_text"],
                        "tray_id": tid,
                        "qty": fill_qty,
                        "source": "reused",
                    }
                )
            else:
                # Use a new tray from the generated pool
                tid = new_pool[new_pool_idx]
                new_pool_idx += 1
                new_tray_ids.append(tid)
                allocations.append(
                    {
                        "reason_id": reason["reason_id"],
                        "reason_text": reason["reason_text"],
                        "tray_id": tid,
                        "qty": fill_qty,
                        "source": "new",
                    }
                )

            remaining -= fill_qty

    return allocations, delinked_ids, new_tray_ids


def _infer_prefix(active_trays: List[Dict[str, Any]]) -> str:
    """Infer the tray ID prefix from the first active tray. Falls back to 'NB-A'."""
    for tray in active_trays:
        parsed = _parse_tray_number(tray.get("tray_id", ""))
        if parsed:
            return parsed[0]
    return "NB-A"


# ─────────────────────────────────────────────────────────────────────────────
# ACCEPT TRAY ALLOCATION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def _allocate_accept_trays(
    accept_qty: int,
    active_trays: List[Dict[str, Any]],
    delinked_ids: List[str],
    capacity: int,
    tray_type: Optional[str],
    reserved_ids: set,
) -> List[Dict[str, Any]]:
    """Distribute accept_qty items across accept tray containers.

    ONLY uses remaining active trays (those NOT delinked for reject).
    Accept trays must always come from the lot's existing active trays —
    no free master trays are permitted for accept allocation.

    Raises:
        ValueError: if accept_qty cannot fit in remaining active trays.
    """
    delinked_set = set(delinked_ids)
    remaining_active = [t for t in active_trays if t["tray_id"] not in delinked_set]

    allocations: List[Dict[str, Any]] = []
    remaining = accept_qty

    for tray in remaining_active:
        if remaining <= 0:
            break
        fill_qty = min(remaining, capacity)
        allocations.append(
            {
                "tray_id": tray["tray_id"],
                "qty": fill_qty,
                "source": "existing",
            }
        )
        remaining -= fill_qty

    # Accept must ONLY use existing active trays. If accept qty exceeds
    # capacity of remaining active trays, block the submission.
    if remaining > 0:
        raise ValueError(
            f"Accept allocation incomplete — {remaining} items could not fit in "
            f"remaining active trays. Accept trays must come from existing active "
            f"trays only. Reduce reject qty or adjust delink count."
        )

    return allocations


# ─────────────────────────────────────────────────────────────────────────────
# VALIDATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def validate_reason_single_tray_rule(allocations: List[Dict[str, Any]]) -> bool:
    """Confirm no tray holds more than one reason.  Returns True if valid."""
    tray_reasons: Dict[str, set] = {}
    for slot in allocations:
        tid = slot["tray_id"]
        rid = slot["reason_id"]
        tray_reasons.setdefault(tid, set()).add(rid)
    return all(len(v) == 1 for v in tray_reasons.values())


def _validate_reject_qty(total_reject_qty: int, lot_qty: int) -> None:
    if total_reject_qty <= 0:
        raise ValueError("Total reject quantity must be greater than 0.")
    if total_reject_qty >= lot_qty:
        raise ValueError(
            f"Reject qty ({total_reject_qty}) must be less than lot qty ({lot_qty}). "
            "Use Full Reject for total rejection."
        )


# ─────────────────────────────────────────────────────────────────────────────
# SHORTAGE HELPERS
# Shortage = missing items that never arrived; reduces effective lot qty.
# They do NOT create reject trays and do NOT trigger reject scan flow.
# ─────────────────────────────────────────────────────────────────────────────

def _is_shortage_entry(entry: Dict[str, Any]) -> bool:
    """Return True if this rejection entry represents a SHORTAGE (missing items).

    Shortage entries (reason_text contains 'SHORTAGE', case-insensitive) reduce
    the effective lot qty but do NOT create reject trays or require reject scans.
    """
    return "shortage" in (entry.get("reason_text") or "").lower()


def _split_shortage_entries(
    rejection_entries: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split rejection_entries into (shortage_entries, reject_entries).

    shortage_entries → reduce effective lot qty, no reject tray created.
    reject_entries   → create reject trays as normal.
    """
    shortage: List[Dict[str, Any]] = []
    reject: List[Dict[str, Any]] = []
    for e in rejection_entries:
        if _is_shortage_entry(e):
            shortage.append(e)
        else:
            reject.append(e)
    return shortage, reject


# ─────────────────────────────────────────────────────────────────────────────
# DRAIN ENGINE – real physical tray emptying from reject qty
# ─────────────────────────────────────────────────────────────────────────────

def calculate_emptied_trays(
    active_trays: List[Dict[str, Any]],
    reject_qty: int,
) -> Dict[str, Any]:
    """Simulate draining ``reject_qty`` units from the active trays and
    return which trays become fully empty vs partially drained.

    Drain policy:
        Smallest-qty trays first (prefer TOP partial so full capacity
        trays are preserved for accept).  Ties broken by original order.

    Returns:
        {
          "emptied_tray_ids":  [tray_id, ...],    # fully drained
          "partial_tray":      {tray_id, leftover_qty} | None,
          "emptied_count":     int,               # = len(emptied_tray_ids)
          "reject_qty":        int,
          "drain_plan":        [{tray_id, qty_used, qty_before, qty_after}, ...],
        }

    This is the single source of truth for how many trays can be
    reused (= emptied_count) and which tray IDs qualify.
    """
    trays_sorted = sorted(
        enumerate(active_trays),
        key=lambda p: (p[1].get("qty", 0), p[0]),
    )

    emptied_ids: List[str] = []
    drain_plan: List[Dict[str, Any]] = []
    partial: Optional[Dict[str, Any]] = None
    remaining = max(0, int(reject_qty or 0))

    for _, tray in trays_sorted:
        if remaining <= 0:
            break
        tid = tray.get("tray_id")
        qty = int(tray.get("qty") or 0)
        if qty <= 0:
            continue
        take = min(qty, remaining)
        drain_plan.append({
            "tray_id": tid,
            "qty_before": qty,
            "qty_used": take,
            "qty_after": qty - take,
        })
        if take == qty:
            emptied_ids.append(tid)
        else:
            partial = {"tray_id": tid, "leftover_qty": qty - take}
        remaining -= take

    return {
        "emptied_tray_ids": emptied_ids,
        "partial_tray": partial,
        "emptied_count": len(emptied_ids),
        "reject_qty": int(reject_qty or 0),
        "drain_plan": drain_plan,
    }


# ─────────────────────────────────────────────────────────────────────────────
# LIVE PREVIEW BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_live_preview(
    lot_id: str,
    rejection_entries: List[Dict[str, Any]],
    delink_count: int,
) -> Dict[str, Any]:
    """Compute and return the tray allocation preview WITHOUT writing to the DB.

    Called by the live preview API each time the user updates reject quantities.
    Returns a serialisable dict ready to be sent as JSON.

    Args:
        lot_id:           Lot under inspection.
        rejection_entries: [{reason_id, reason_text, qty}, ...]
        delink_count:     Max existing trays to reuse for reject.

    Returns:
        {
          success: bool,
          lot_qty, tray_capacity, tray_type,
          total_reject_qty, total_accept_qty,
          reject_allocations: [...],
          accept_allocations: [...],
          delinked_tray_ids: [...],
          new_reject_tray_ids: [...],
          validation_errors: [...],
        }
    """
    from .selectors import get_lot_tray_context

    errors: List[str] = []

    ctx = get_lot_tray_context(lot_id)
    if not ctx["found"]:
        return {"success": False, "error": f"Lot {lot_id} not found."}

    lot_qty: int = ctx["lot_qty"]
    capacity: int = ctx["tray_capacity"]
    tray_type: Optional[str] = ctx["tray_type"]
    active_trays: List[Dict] = ctx["active_trays"]

    # ── Shortage split ────────────────────────────────────────────────────────
    # Shortage = missing items (never arrived). They reduce the effective lot qty
    # but do NOT create reject trays and do NOT trigger the reject scan flow.
    shortage_entries, reject_entries = _split_shortage_entries(rejection_entries)
    shortage_qty: int = sum(e.get("qty", 0) for e in shortage_entries)
    total_non_shortage_reject: int = sum(e.get("qty", 0) for e in reject_entries)

    effective_lot_qty: int = lot_qty - shortage_qty      # adjusted lot qty
    total_reject: int = total_non_shortage_reject        # drives reject tray allocation
    total_accept: int = effective_lot_qty - total_reject # = lot_qty - shortage - reject

    # Basic validations
    if shortage_qty == 0 and total_reject <= 0:
        errors.append("Total reject quantity must be > 0.")
    if shortage_qty > 0 and effective_lot_qty <= 0:
        errors.append(
            f"Shortage qty ({shortage_qty}) equals or exceeds lot qty ({lot_qty})."
        )
    if total_reject > 0 and total_reject >= effective_lot_qty:
        errors.append(
            f"Reject qty ({total_reject}) must be less than effective lot qty ({effective_lot_qty})."
        )
    if total_accept < 0:
        errors.append("Accept qty cannot be negative.")

    if errors:
        return {
            "success": False,
            "validation_errors": errors,
            "total_reject_qty": total_reject,
            "total_accept_qty": max(0, total_accept),
            "total_shortage_qty": shortage_qty,
            "effective_lot_qty": effective_lot_qty,
        }

    reserved: set = set()

    reject_alloc: List[Dict[str, Any]] = []
    accept_alloc: List[Dict[str, Any]] = []
    delinked_ids: List[str] = []
    new_reject_ids: List[str] = []

    try:
        # Only non-shortage entries create reject trays.
        if reject_entries:
            reject_alloc, delinked_ids, new_reject_ids = _allocate_reject_trays(
                reasons=reject_entries,
                active_trays=active_trays,
                delink_count=delink_count,
                capacity=capacity,
                reserved_ids=reserved,
            )

            if not validate_reason_single_tray_rule(reject_alloc):
                errors.append("Single-reason-per-tray rule violated in reject allocation.")

        accept_alloc = _allocate_accept_trays(
            accept_qty=max(0, total_accept),
            active_trays=active_trays,
            delinked_ids=delinked_ids,
            capacity=capacity,
            tray_type=tray_type,
            reserved_ids=reserved,
        )

    except ValueError as exc:
        # Engine is informational only in the manual-scan flow – record
        # the warning but still return the slot plan so the modal renders.
        errors.append(str(exc))

    # Drain engine uses shortage + reject combined (both physically reduce
    # the active tray quantities, determining which trays become fully empty).
    total_drain: int = shortage_qty + total_non_shortage_reject

    return {
        "success": True,
        "lot_id": lot_id,
        "lot_qty": lot_qty,
        "tray_type": tray_type,
        "tray_capacity": capacity,
        "active_tray_count": len(active_trays),
        "active_trays": active_trays,
        "total_reject_qty": total_reject,
        "total_accept_qty": total_accept,
        "total_shortage_qty": shortage_qty,
        "effective_lot_qty": effective_lot_qty,
        "reject_allocations": reject_alloc,
        "accept_allocations": accept_alloc,
        "delinked_tray_ids": delinked_ids,
        "new_reject_tray_ids": new_reject_ids,
        "validation_errors": errors,
        # ── Manual scan flow additions ────────────────────────────────
        # Reuse cap from the DRAIN ENGINE: only trays emptied by combined
        # shortage + reject drain may be reused / delinked.
        **_reuse_counters(
            reject_slots=_build_reject_slots(reject_entries, capacity),
            accept_slots=_build_accept_slots(max(0, total_accept), capacity),
            active_trays=active_trays,
            total_reject=total_drain,
        ),
    }


def _reuse_counters(
    reject_slots: List[Dict[str, Any]],
    accept_slots: List[Dict[str, Any]],
    active_trays: List[Dict[str, Any]],
    total_reject: int,
) -> Dict[str, Any]:
    """Return the single source of truth for reuse / delink / new-tray
    counters, plus the raw drain plan for the UI.

    Emptied count comes from ``calculate_emptied_trays`` and is the hard
    cap on how many existing trays may be reused for reject.
    """
    drain = calculate_emptied_trays(active_trays, max(0, int(total_reject or 0)))
    emptied_count = drain["emptied_count"]
    reject_slot_count = len(reject_slots)
    reusable_count = min(reject_slot_count, emptied_count)
    new_required = max(0, reject_slot_count - reusable_count)
    return {
        "reject_slots": reject_slots,
        "accept_slots": accept_slots,
        "reusable_count": reusable_count,
        "new_required": new_required,
        "delink_available": emptied_count,
        "emptied_tray_ids": drain["emptied_tray_ids"],
        "emptied_count": emptied_count,
        "partial_tray": drain["partial_tray"],
        "drain_plan": drain["drain_plan"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# REJECT MODAL CONTEXT
# ─────────────────────────────────────────────────────────────────────────────

def get_reject_modal_context(lot_id: str) -> Dict[str, Any]:
    """Return all data needed to populate the reject modal popup.

    Fetches lot metadata, active trays, and the live rejection-reason list
    from the DB.  All data is read-only – no writes performed here.
    """
    from .selectors import get_lot_tray_context
    from .models import IP_Rejection_Table, InputScreening_Submitted

    ctx = get_lot_tray_context(lot_id)
    if not ctx["found"]:
        return {"success": False, "error": f"Lot {lot_id} not found."}

    reasons = list(
        IP_Rejection_Table.objects.order_by("rejection_reason_id").values(
            "id",
            "rejection_reason_id",
            "rejection_reason",
        )
    )

    # ── Draft restore data ───────────────────────────────────────────────────
    from .models import IP_Rejection_Draft

    draft_data = None
    saved_draft = IP_Rejection_Draft.objects.filter(lot_id=lot_id).first()
    if saved_draft:
        stored_data = saved_draft.draft_data or {}
        draft_data = {
            "rejection_reasons_json": stored_data.get("rejection_reasons_json", {}),
            "remarks": saved_draft.lot_rejection_remarks or "",
            "reject_assignments": stored_data.get("reject_assignments", []),
            "accept_assignments": stored_data.get("accept_assignments", []),
            "delinked_tray_ids": stored_data.get("delinked_tray_ids", []),
            "full_lot_reject": stored_data.get("full_lot_reject", False),
        }

    return {
        "success": True,
        "lot_id": lot_id,
        "lot_qty": ctx["lot_qty"],
        "tray_type": ctx["tray_type"],
        "tray_capacity": ctx["tray_capacity"],
        "active_tray_count": len(ctx["active_trays"]),
        "active_trays": ctx["active_trays"],
        "rejection_reasons": reasons,
        "batch_id": ctx.get("batch_id"),
        "model_no": ctx.get("model_no"),
        "plating_stk_no": ctx.get("plating_stk_no"),
        "draft_data": draft_data,
    }


# ─────────────────────────────────────────────────────────────────────────────
# FINALIZE SUBMISSION
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def finalize_submission(
    lot_id: str,
    rejection_entries: List[Dict[str, Any]],
    delink_count: int,
    remarks: str,
    user,
) -> Dict[str, Any]:
    """Persist the partial accept / partial reject submission.

    Steps:
      1. Re-run allocation engine (prevents stale preview attack).
      2. Validate single-reason-per-tray rule.
      3. Validate accept trays are genuinely free in master.
      4. Save InputScreening_Submitted snapshot.
      5. Flag ModelMasterCreation as submitted.

    All steps run inside a single atomic transaction – partial saves are
    impossible.

    Returns:
        {success, lot_id, submission_id, total_reject_qty, total_accept_qty}
    """
    from .selectors import get_lot_tray_context
    from .models import InputScreening_Submitted

    ctx = get_lot_tray_context(lot_id, lock=True)
    if not ctx["found"]:
        raise ValueError(f"Lot {lot_id} not found or already submitted.")

    lot_qty: int = ctx["lot_qty"]
    capacity: int = ctx["tray_capacity"]
    tray_type: Optional[str] = ctx["tray_type"]
    active_trays: List[Dict] = ctx["active_trays"]
    batch_id_val: str = ctx.get("batch_id", "")

    total_reject = sum(e.get("qty", 0) for e in rejection_entries)
    total_accept = lot_qty - total_reject

    _validate_reject_qty(total_reject, lot_qty)

    reserved: set = set()

    reject_alloc, delinked_ids, new_reject_ids = _allocate_reject_trays(
        reasons=rejection_entries,
        active_trays=active_trays,
        delink_count=delink_count,
        capacity=capacity,
        reserved_ids=reserved,
    )

    if not validate_reason_single_tray_rule(reject_alloc):
        raise ValueError(
            "Internal: single-reason-per-tray rule was violated during allocation."
        )

    accept_alloc = _allocate_accept_trays(
        accept_qty=total_accept,
        active_trays=active_trays,
        delinked_ids=delinked_ids,
        capacity=capacity,
        tray_type=tray_type,
        reserved_ids=reserved,
    )

    # Build JSON snapshots
    rejection_reasons_json = {
        e["reason_id"]: {
            "reason": e["reason_text"],
            "qty": e["qty"],
        }
        for e in rejection_entries
    }
    allocation_preview_json = {
        "total_reject_qty": total_reject,
        "total_accept_qty": total_accept,
        "delinked_tray_ids": delinked_ids,
        "new_reject_tray_ids": new_reject_ids,
        "reject_allocations": reject_alloc,
        "accept_allocations": accept_alloc,
    }

    reject_trays_json = [
        {"tray_id": s["tray_id"], "qty": s["qty"], "reason_id": s["reason_id"]}
        for s in reject_alloc
    ]
    accept_trays_json = [
        {"tray_id": s["tray_id"], "qty": s["qty"]}
        for s in accept_alloc
    ]
    all_trays_json = reject_trays_json + accept_trays_json

    submission = InputScreening_Submitted.objects.create(
        lot_id=lot_id,
        batch_id=batch_id_val,
        module_name="Input Screening",
        plating_stock_no=ctx.get("plating_stk_no"),
        model_no=ctx.get("model_no"),
        tray_type=tray_type,
        tray_capacity=capacity,
        original_lot_qty=lot_qty,
        submitted_lot_qty=lot_qty,
        accepted_qty=total_accept,
        rejected_qty=total_reject,
        active_trays_count=len(active_trays),
        reject_trays_count=len(reject_alloc),
        accept_trays_count=len(accept_alloc),
        remarks=remarks,
        is_partial_accept=True,
        is_partial_reject=True,
        is_full_accept=False,
        is_full_reject=False,
        is_active=True,
        is_submitted=True,
        submitted_at=timezone.now(),
        Draft_Saved=False,
        all_trays_json=all_trays_json,
        accepted_trays_json=accept_trays_json,
        rejected_trays_json=reject_trays_json,
        rejection_reasons_json=rejection_reasons_json,
        allocation_preview_json=allocation_preview_json,
        delink_trays_json=[{"tray_id": tid} for tid in delinked_ids],
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    # Sync DPTrayId_History so downstream modules (Brass QC) see only the
    # accepted trays. Trays that were reused for reject are delinked; accept
    # tray quantities are updated to the allocated qty.
    from DayPlanning.models import DPTrayId_History as _DPH
    _delink_ids = set(delinked_ids)  # In v1 this equals reused-for-reject trays
    if _delink_ids:
        _DPH.objects.filter(lot_id=lot_id, tray_id__in=_delink_ids).update(delink_tray=True)
    for _a in accept_alloc:
        _DPH.objects.filter(lot_id=lot_id, tray_id=_a["tray_id"]).update(
            tray_quantity=_a["qty"]
        )

    # ✅ CRITICAL: Mark ALL rejected tray IDs as permanently rejected in both
    # IPTrayId and TrayId master so no downstream module (Brass QC, IQF, etc.)
    # can ever reuse them. New reject trays (not previously in this lot) also get
    # flagged to prevent cross-lot reuse.
    from .models import IPTrayId as _IPTrayId
    from modelmasterapp.models import TrayId as _TrayId
    _reject_tray_ids = {r["tray_id"] for r in reject_alloc}
    if _reject_tray_ids:
        _IPTrayId.objects.filter(tray_id__in=_reject_tray_ids).update(rejected_tray=True)
        _TrayId.objects.filter(tray_id__in=_reject_tray_ids).update(rejected_tray=True)
    logger.info(
        "[IS][PARTIAL_SUBMIT] marked %d reject tray(s) as permanently rejected: %s",
        len(_reject_tray_ids), sorted(_reject_tray_ids),
    )

    # Mark the lot as submitted in the master. The submission flags
    # (rejected_ip_stock / few_cases_accepted_Ip_stock) live on
    # TotalStockModel; ModelMasterCreation has no such columns.
    _mark_lot_submitted_flags(lot_id, accepted_qty=total_accept)

    logger.info(
        "[IS][PARTIAL_SUBMIT] lot=%s submission_id=%s reject=%d accept=%d user=%s",
        lot_id,
        submission.id,
        total_reject,
        total_accept,
        getattr(user, "username", "anonymous"),
    )

    return {
        "success": True,
        "lot_id": lot_id,
        "submission_id": submission.id,
        "total_reject_qty": total_reject,
        "total_accept_qty": total_accept,
        "reject_trays": len(reject_alloc),
        "accept_trays": len(accept_alloc),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SLOT PLAN COMPUTATION (manual scan flow)
# ─────────────────────────────────────────────────────────────────────────────

def _build_reject_slots(
    rejection_entries: List[Dict[str, Any]],
    capacity: int,
) -> List[Dict[str, Any]]:
    """Pack each rejection reason into capacity-bound slots (one reason per tray)."""
    slots: List[Dict[str, Any]] = []
    idx = 0
    for entry in rejection_entries:
        qty = int(entry.get("qty") or 0)
        if qty <= 0:
            continue
        rid = entry.get("reason_id")
        rtext = entry.get("reason_text", "")
        remaining = qty
        while remaining > 0:
            fill = min(remaining, capacity)
            slots.append({
                "slot_idx": idx,
                "reason_id": rid,
                "reason_text": rtext,
                "qty": fill,
            })
            idx += 1
            remaining -= fill
    return slots

# Accept Tray Slots
def _build_accept_slots(accept_qty: int, capacity: int) -> List[Dict[str, Any]]:
    """Distribute accept_qty into capacity-bound slots, partial (top-tray) slot first.

    Slots are sorted ascending by qty so the smallest slot (the existing
    top-tray that only has remaining space) is always slot 0.  The user
    is expected to scan that slot first; the remaining full slots are
    auto-filled by the frontend once slot 0 is confirmed.
    """
    if accept_qty <= 0 or capacity <= 0:
        return []
    raw_qtys: List[int] = []
    remaining = accept_qty
    while remaining > 0:
        fill = min(remaining, capacity)
        raw_qtys.append(fill)
        remaining -= fill
    # Ascending order: smallest qty (partial/top tray) appears as slot 0.
    raw_qtys.sort()
    return [{"slot_idx": idx, "qty": qty} for idx, qty in enumerate(raw_qtys)]

# Compute Tray Slots
def compute_slot_plan(
    lot_id: str,
    rejection_entries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return a pure-planning view (no tray IDs assigned).

    The frontend renders empty SCAN inputs based on this plan; the user
    fills them by scanning.  ``validate_scanned_tray`` enforces eligibility
    per scan.
    """
    from .selectors import get_lot_tray_context

    ctx = get_lot_tray_context(lot_id)
    if not ctx["found"]:
        return {"success": False, "error": f"Lot {lot_id} not found."}

    lot_qty = ctx["lot_qty"]
    capacity = ctx["tray_capacity"] or 1
    active_trays = ctx["active_trays"]
    active_count = len(active_trays)

    # ── Shortage split ──────────────────────────────────────────────────────
    shortage_entries, reject_entries = _split_shortage_entries(rejection_entries)
    shortage_qty = sum(int(e.get("qty") or 0) for e in shortage_entries)
    total_non_shortage_reject = sum(int(e.get("qty") or 0) for e in reject_entries)

    effective_lot_qty = lot_qty - shortage_qty   # adjusted lot qty after shortage
    total_reject = total_non_shortage_reject      # drives reject slots
    total_accept = effective_lot_qty - total_reject

    errors: List[str] = []
    if total_reject < 0:
        errors.append("Reject qty cannot be negative.")
    if shortage_qty > 0 and effective_lot_qty <= 0:
        errors.append(
            f"Shortage qty ({shortage_qty}) equals or exceeds lot qty ({lot_qty})."
        )
    if total_reject > effective_lot_qty:
        errors.append(
            f"Reject qty ({total_reject}) cannot exceed effective lot qty ({effective_lot_qty})."
        )

    # Shortage excludes from reject slots; only reject_entries generate tray slots.
    reject_slots = _build_reject_slots(reject_entries, capacity)
    accept_slots = _build_accept_slots(max(0, total_accept), capacity)

    # Drain engine: shortage + reject both physically drain from active trays.
    total_drain = shortage_qty + total_non_shortage_reject
    counters = _reuse_counters(
        reject_slots=reject_slots,
        accept_slots=accept_slots,
        active_trays=active_trays,
        total_reject=total_drain,
    )

    return {
        "success": not errors,
        "lot_id": lot_id,
        "lot_qty": lot_qty,
        "tray_type": ctx["tray_type"],
        "tray_capacity": capacity,
        "active_trays": active_trays,
        "active_tray_count": active_count,
        "total_reject_qty": total_reject,
        "total_accept_qty": max(0, total_accept),
        "total_shortage_qty": shortage_qty,
        "effective_lot_qty": effective_lot_qty,
        "reject_slots": reject_slots,
        "accept_slots": accept_slots,
        "reusable_count": counters["reusable_count"],
        "new_required": counters["new_required"],
        "delink_available": counters["delink_available"],
        "emptied_tray_ids": counters["emptied_tray_ids"],
        "emptied_count": counters["emptied_count"],
        "partial_tray": counters["partial_tray"],
        "drain_plan": counters["drain_plan"],
        "validation_errors": errors,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PER-SCAN TRAY VALIDATION (manual scan flow)
# ─────────────────────────────────────────────────────────────────────────────

def _norm(tid: str) -> str:
    return (tid or "").strip().upper()


def _reject_snapshot_contains_tray(snapshot, tray_id: str) -> bool:
    """Return True when a reject-only snapshot contains ``tray_id`` with qty > 0."""
    tid = _norm(tray_id)
    if not tid or not snapshot:
        return False

    if isinstance(snapshot, dict):
        rows = (
            snapshot.get("trays")
            or snapshot.get("reject_trays")
            or snapshot.get("tray_data")
            or []
        )
    elif isinstance(snapshot, list):
        rows = snapshot
    else:
        return False

    for row in rows:
        if not isinstance(row, dict):
            continue
        row_tid = _norm(row.get("tray_id") or row.get("rejected_tray_id"))
        if row_tid != tid:
            continue
        qty = row.get("qty", row.get("tray_quantity", row.get("rejected_tray_quantity", 0)))
        try:
            qty = int(qty or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty > 0:
            return True
    return False


def _is_rejected_in_other_module(tray_id: str) -> bool:
    """
    Return True when ``tray_id`` is still represented as a rejected tray in a
    downstream module.  This is used only for NEW reject-tray scans in Input
    Screening; current-lot trays are handled before this helper is called.

    Explicit delink/release state remains reusable.  A merely free-looking
    master row is not treated as release evidence because historical data can
    leave the master cleared while a module reject snapshot is still active.
    """
    tid = _norm(tray_id)
    if not tid:
        return False

    from modelmasterapp.models import TrayId
    from Brass_QC.models import (
        BrassTrayId,
        Brass_QC_Rejected_TrayScan,
        Brass_QC_Submission,
    )
    from BrassAudit.models import (
        BrassAuditTrayId,
        Brass_Audit_Rejected_TrayScan,
        Brass_Audit_Submission,
    )
    from IQF.models import IQFTrayId, IQF_Rejected_TrayScan, IQF_Submitted
    from Nickel_Inspection.models import NickelQcTrayId, NickelQC_Submission
    from Nickel_Inspection.services import validate_nickel_wiping_rejection_tray_available
    from Nickel_Audit.models import (
        Nickel_AuditTrayId,
        Nickel_Audit_Rejected_TrayScan,
        NickelAudit_Submission,
    )

    master = TrayId.objects.filter(tray_id__iexact=tid).first()
    explicitly_released = bool(master and master.delink_tray and not master.scanned)

    # Live/current module rows are authoritative blockers even when the master
    # row is stale or incomplete.
    live_reject_checks = (
        BrassTrayId.objects.filter(tray_id__iexact=tid, rejected_tray=True, delink_tray=False),
        BrassAuditTrayId.objects.filter(tray_id__iexact=tid, rejected_tray=True, delink_tray=False),
        IQFTrayId.objects.filter(tray_id__iexact=tid, rejected_tray=True, delink_tray=False),
        NickelQcTrayId.objects.filter(tray_id__iexact=tid, rejected_tray=True, delink_tray=False),
        Nickel_AuditTrayId.objects.filter(tray_id__iexact=tid, rejected_tray=True, delink_tray=False),
    )
    if any(qs.exists() for qs in live_reject_checks):
        return True

    # Nickel Wiping Z1/Z2 share the same lifecycle-aware backend helper.
    nw_available, _ = validate_nickel_wiping_rejection_tray_available(
        tid, current_lot_id=None
    )
    if not nw_available:
        return True

    # Once explicitly delinked/released, historical reject snapshots must not
    # create a false occupied state.
    if explicitly_released:
        return False

    # Reject scan tables cover explicit reject-tray scans that may outlive the
    # master ownership flags.
    if Brass_QC_Rejected_TrayScan.objects.filter(rejected_tray_id__iexact=tid).exists():
        return True
    if Brass_Audit_Rejected_TrayScan.objects.filter(rejected_tray_id__iexact=tid).exists():
        return True
    if IQF_Rejected_TrayScan.objects.filter(tray_id__iexact=tid).exists():
        return True
    if Nickel_Audit_Rejected_TrayScan.objects.filter(rejected_tray_id__iexact=tid).exists():
        return True

    # Full/partial reject snapshots cover flows where the tray is recorded only
    # in the submitted JSON snapshot instead of a live tray row.
    for submission in Brass_QC_Submission.objects.filter(
        submission_type__in=["FULL_REJECT", "PARTIAL"]
    ).only("full_reject_data", "partial_reject_data"):
        if (
            _reject_snapshot_contains_tray(submission.full_reject_data, tid)
            or _reject_snapshot_contains_tray(submission.partial_reject_data, tid)
        ):
            return True

    for submission in Brass_Audit_Submission.objects.filter(
        submission_type__in=["FULL_REJECT", "PARTIAL"]
    ).only("full_reject_data", "partial_reject_data"):
        if (
            _reject_snapshot_contains_tray(submission.full_reject_data, tid)
            or _reject_snapshot_contains_tray(submission.partial_reject_data, tid)
        ):
            return True

    for submission in IQF_Submitted.objects.filter(
        submission_type__in=["FULL_REJECT", "PARTIAL", "LOT_REJECTION"]
    ).only("full_reject_data", "partial_reject_data"):
        if (
            _reject_snapshot_contains_tray(submission.full_reject_data, tid)
            or _reject_snapshot_contains_tray(submission.partial_reject_data, tid)
        ):
            return True

    for submission in NickelQC_Submission.objects.filter(
        submission_type__in=["FULL_REJECT", "PARTIAL"]
    ).only("reject_trays_data"):
        if _reject_snapshot_contains_tray(submission.reject_trays_data, tid):
            return True

    for submission in NickelAudit_Submission.objects.filter(
        submission_type__in=["FULL_REJECT", "PARTIAL"]
    ).only("reject_trays_data"):
        if _reject_snapshot_contains_tray(submission.reject_trays_data, tid):
            return True

    return False


def validate_scanned_tray(
    lot_id: str,
    slot_type: str,
    tray_id: str,
    used_tray_ids: List[str],
    reject_qty: int = 0,
    shortage_qty: int = 0,
) -> Dict[str, Any]:
    """Validate a single user-scanned tray ID for the given slot type.

    slot_type:
        ``reject``  – tray may be (a) an EMPTIED active tray of this lot
                      (physically drained by shortage + reject, will be reused
                      without further delink) or (b) a free master tray
                      (will be created new).  Active trays that are NOT
                      in the emptied set are blocked – they still hold
                      stock and cannot be reassigned.
        ``delink``  – tray must be an EMPTIED active tray of this lot.
                      Non-emptied active trays cannot be delinked in
                      this flow because they still hold production qty.
        ``accept``  – tray may be (a) active for this lot (existing) or
                      (b) a free master tray.

    used_tray_ids: tray IDs already consumed elsewhere in this scan session
                   (so the same tray cannot be assigned twice).
    reject_qty:    non-shortage reject qty the user has entered.
    shortage_qty:  shortage (missing items) qty. Used with reject_qty to
                   compute total drain and which active trays are emptied.
    """
    from .selectors import get_lot_tray_context
    from modelmasterapp.models import TrayId

    tid = _norm(tray_id)
    if not tid:
        return {"valid": False, "reason": "Tray ID is required."}

    used = {_norm(t) for t in (used_tray_ids or [])}
    if tid in used:
        return {"valid": False, "reason": f"Tray {tid} is already used in this session."}

    ctx = get_lot_tray_context(lot_id)
    if not ctx["found"]:
        return {"valid": False, "reason": f"Lot {lot_id} not found."}

    capacity = ctx["tray_capacity"]
    tray_type = ctx["tray_type"]
    active_trays = ctx["active_trays"]
    active_by_id = {_norm(t["tray_id"]): t for t in active_trays}
    active_match = active_by_id.get(tid)

    # Drain engine caps how many active trays may be reused for reject /
    # delinked.  The CAP is on COUNT, not on tray identity – the user is
    # free to scan any original active tray as long as the total reused
    # count does not exceed ``emptied_count``.
    # CRITICAL: Use COMBINED drain (shortage + reject) to determine which
    # trays become physically empty. Both shortage and reject items physically
    # reduce tray quantities.
    total_drain: int = max(0, int(shortage_qty or 0)) + max(0, int(reject_qty or 0))
    drain = calculate_emptied_trays(active_trays, total_drain)
    emptied_count = drain["emptied_count"]
    # How many active trays has the user already consumed in this session?
    already_reused_count = sum(
        1 for t in used if t in active_by_id and t != tid
    )

    slot_type = (slot_type or "").lower().strip()

    if slot_type == "delink":
        if not active_match:
            return {
                "valid": False,
                "reason": f"Tray {tid} is not an active tray of this lot – cannot delink.",
            }
        if already_reused_count >= emptied_count:
            return {
                "valid": False,
                "reason": (
                    f"Reuse / delink quota reached "
                    f"({already_reused_count}/{emptied_count}). "
                    "No tray is fully emptied yet at this reject quantity."
                    if emptied_count == 0 else
                    f"Reuse / delink quota reached "
                    f"({already_reused_count}/{emptied_count}). "
                    "Remove an existing scan before picking another active tray."
                ),
            }
        return {
            "valid": True,
            "source": "delinked",
            "tray_qty": active_match.get("qty", 0),
            "top_tray": active_match.get("top_tray", False),
            "tray_id": tid,
            "reason": "Eligible for delink.",
        }

    if slot_type in ("reject", "accept"):
        if active_match:
            # Count-based cap: reject can reuse at most ``emptied_count``
            # active trays.  Any specific tray is allowed, the constraint
            # is on the total count of reused trays.
            if slot_type == "reject" and already_reused_count >= emptied_count:
                return {
                    "valid": False,
                    "reason": (
                        f"Reuse quota reached ({already_reused_count}/{emptied_count}). "
                        "No tray is fully emptied yet at this reject quantity."
                        if emptied_count == 0 else
                        f"Reuse quota reached ({already_reused_count}/{emptied_count}). "
                        "Only as many active trays may be reused as are emptied "
                        "by the current reject qty."
                    ),
                }
            return {
                "valid": True,
                "source": "reused" if slot_type == "reject" else "existing",
                "tray_qty": active_match.get("qty", 0),
                "top_tray": active_match.get("top_tray", False),
                "tray_id": tid,
                "reason": "Existing active tray – will be reused.",
            }

        # ACCEPT slots: ONLY existing active trays are allowed.
        # No free master trays accepted — all accept trays must come from
        # the lot's original active trays.
        if slot_type == "accept":
            return {
                "valid": False,
                "reason": f"Accept trays must be from existing active trays only. {tid} is not an active tray of this lot.",
            }

        # REJECT slots: fallback to free master tray validation.
        try:
            master = TrayId.objects.get(tray_id=tid)
        except TrayId.DoesNotExist:
            return {"valid": False, "reason": f"Tray {tid} not found in master."}

        # A tray rejected in any downstream module must not be reused as a new
        # Input Screening reject tray.  Current-lot trays were already handled
        # above, and explicit delink/release remains reusable.
        if _is_rejected_in_other_module(tid):
            return {"valid": False, "reason": "Tray is occupied."}

        from .models import IS_AllocationTray
        released_for_reuse = bool(master.delink_tray) or IS_AllocationTray.objects.filter(
            tray_id=tid,
            reject_lot__isnull=False,
            is_delinked=True,
            qty__lte=0,
        ).exists()
        if master.lot_id and not released_for_reuse:
            return {"valid": False, "reason": f"Tray {tid} occupied by lot {master.lot_id}."}
        if master.batch_id_id and not released_for_reuse:
            return {"valid": False, "reason": f"Tray {tid} linked to an active batch."}
        if master.rejected_tray and not released_for_reuse:
            return {"valid": False, "reason": f"Tray {tid} is permanently rejected."}
        if master.scanned and not released_for_reuse:
            return {"valid": False, "reason": f"Tray {tid} is currently in-use."}
        if tray_type and master.tray_type and master.tray_type.lower() != tray_type.lower():
            return {
                "valid": False,
                "reason": f"Tray type mismatch: expected {tray_type}, got {master.tray_type}.",
            }
        if capacity and master.tray_capacity and master.tray_capacity != capacity:
            return {
                "valid": False,
                "reason": f"Capacity mismatch: expected {capacity}, got {master.tray_capacity}.",
            }
        return {
            "valid": True,
            "source": "new",
            "tray_qty": 0,
            "top_tray": False,
            "tray_id": tid,
            "reason": "Free master tray – will be allocated.",
        }

    return {"valid": False, "reason": f"Unknown slot type: {slot_type}"}


# ─────────────────────────────────────────────────────────────────────────────
# FINAL SUBMIT V2 – uses user-scanned tray assignments
# ─────────────────────────────────────────────────────────────────────────────

def _validate_assignments_against_plan(
    plan_slots: List[Dict[str, Any]],
    assignments: List[Dict[str, Any]],
    label: str,
) -> None:
    if len(assignments) != len(plan_slots):
        raise ValueError(
            f"{label} tray count mismatch: plan needs {len(plan_slots)}, "
            f"received {len(assignments)}."
        )
    seen: set = set()
    for a in assignments:
        tid = _norm(a.get("tray_id"))
        if not tid:
            raise ValueError(f"{label} contains an empty tray ID.")
        if tid in seen:
            raise ValueError(f"{label} contains duplicate tray ID {tid}.")
        seen.add(tid)


@transaction.atomic
def finalize_submission_v2(
    lot_id: str,
    rejection_entries: List[Dict[str, Any]],
    reject_assignments: List[Dict[str, Any]],
    delink_tray_ids: List[str],
    accept_assignments: List[Dict[str, Any]],
    remarks: str,
    user,
) -> Dict[str, Any]:
    """Persist a partial reject submission using USER-SCANNED tray IDs.

    All allocations are derived from the user's manual scans.  Server
    re-validates every scan to prevent payload tampering.
    """
    from .selectors import get_lot_tray_context
    from .models import InputScreening_Submitted

    ctx = get_lot_tray_context(lot_id, lock=True)
    if not ctx["found"]:
        raise ValueError(f"Lot {lot_id} not found or already submitted.")

    lot_qty: int = ctx["lot_qty"]
    capacity: int = ctx["tray_capacity"] or 1
    tray_type: Optional[str] = ctx["tray_type"]
    active_trays: List[Dict] = ctx["active_trays"]
    batch_id_val: str = ctx.get("batch_id", "")

    # ── Shortage split ──────────────────────────────────────────────────────
    # Shortage reduces effective lot qty; it does NOT create reject trays.
    shortage_entries, reject_entries = _split_shortage_entries(rejection_entries)
    shortage_qty: int = sum(int(e.get("qty") or 0) for e in shortage_entries)
    total_non_shortage_reject: int = sum(int(e.get("qty") or 0) for e in reject_entries)

    effective_lot_qty: int = lot_qty - shortage_qty
    total_reject: int = total_non_shortage_reject        # only drives reject trays
    total_accept: int = effective_lot_qty - total_reject # = lot_qty - shortage - reject

    # Validate quantities
    if shortage_qty > 0 and effective_lot_qty <= 0:
        raise ValueError(
            f"Shortage qty ({shortage_qty}) equals or exceeds lot qty ({lot_qty})."
        )
    if total_non_shortage_reject > 0:
        _validate_reject_qty(total_non_shortage_reject, effective_lot_qty)
    elif shortage_qty <= 0:
        raise ValueError("No rejection or shortage qty provided.")
    if total_accept < 0:
        raise ValueError(
            "Accept qty cannot be negative – reject + shortage exceeds lot qty."
        )

    # Any prior draft for this lot is superseded by the final submit.
    # lot_id is UNIQUE on InputScreening_Submitted, so the draft row must
    # be removed before we create the finalized row.
    InputScreening_Submitted.objects.filter(
        lot_id=lot_id, Draft_Saved=True, is_submitted=False
    ).delete()

    # Re-derive the slot plan: only reject_entries generate reject slots.
    # Shortage has no reject slots — those items are simply absent.
    reject_slots = _build_reject_slots(reject_entries, capacity)
    accept_slots = _build_accept_slots(max(0, total_accept), capacity)

    _validate_assignments_against_plan(reject_slots, reject_assignments, "Reject")
    _validate_assignments_against_plan(accept_slots, accept_assignments, "Accept")

    # Cross-bucket duplicate check
    used_global: set = set()
    for a in reject_assignments + accept_assignments + [
        {"tray_id": t} for t in (delink_tray_ids or [])
    ]:
        tid = _norm(a.get("tray_id"))
        if tid in used_global:
            raise ValueError(f"Tray {tid} is used in more than one bucket.")
        used_global.add(tid)

    # Re-validate each scan server-side.
    # NOTE: reject_qty/shortage_qty must be passed through so the emptied-tray
    # drain calculation matches what the live /validate_scan/ endpoint used —
    # omitting them defaults both to 0, which collapses emptied_count to 0 and
    # rejects every legitimately reused/delinked tray at final submit.
    seen: List[str] = []
    for a in reject_assignments:
        v = validate_scanned_tray(
            lot_id, "reject", a["tray_id"], seen,
            reject_qty=total_non_shortage_reject, shortage_qty=shortage_qty,
        )
        if not v["valid"]:
            raise ValueError(v["reason"])
        a["_source"] = v["source"]
        seen.append(v["tray_id"])
    for tid in delink_tray_ids or []:
        v = validate_scanned_tray(
            lot_id, "delink", tid, seen,
            reject_qty=total_non_shortage_reject, shortage_qty=shortage_qty,
        )
        if not v["valid"]:
            raise ValueError(v["reason"])
        seen.append(v["tray_id"])
    for a in accept_assignments:
        v = validate_scanned_tray(
            lot_id, "accept", a["tray_id"], seen,
            reject_qty=total_non_shortage_reject, shortage_qty=shortage_qty,
        )
        if not v["valid"]:
            raise ValueError(v["reason"])
        a["_source"] = v["source"]
        a["_top_tray"] = bool(v.get("top_tray", False))
        seen.append(v["tray_id"])

    # Build allocation snapshots (slot order = plan order).
    reject_alloc = [
        {
            "tray_id": _norm(a["tray_id"]),
            "qty": slot["qty"],
            "reason_id": slot["reason_id"],
            "reason_text": slot["reason_text"],
            "source": a.get("_source", "reused"),
        }
        for slot, a in zip(reject_slots, reject_assignments)
    ]
    # TOP is position-based: the FIRST accept slot (index 0) is always the top
    # tray. The original top_tray flag on the scanned tray ID is irrelevant
    # after shuffled allocation — position determines TOP.
    accept_alloc = [
        {
            "tray_id": _norm(a["tray_id"]),
            "qty": slot["qty"],
            "source": a.get("_source", "existing"),
            "top_tray": idx == 0,
        }
        for idx, (slot, a) in enumerate(zip(accept_slots, accept_assignments))
    ]

    if not validate_reason_single_tray_rule(reject_alloc):
        raise ValueError("Single-reason-per-tray rule violated.")

    delinked_ids = [_norm(t) for t in (delink_tray_ids or [])]

    # Save the count of ACTUAL reject trays BEFORE appending delinked entries.
    # Delinked trays are appended for audit purposes but must NOT inflate the
    # reject_trays_count stored on the record.
    reject_only_count = len(reject_alloc)

    # Include delinked trays in reject snapshot so the view-icon selector
    # can derive delinked_tray_ids from source=="reused" AND qty==0 entries.
    for _tid in delinked_ids:
        reject_alloc.append({
            "tray_id": _tid,
            "qty": 0,
            "reason_id": "",
            "reason_text": "",
            "source": "reused",
        })

    # Store all entries (both shortage and reject) in the JSON snapshot.
    # Shortage entries are tagged with is_shortage=True for audit clarity.
    rejection_reasons_json = {
        e["reason_id"]: {
            "reason": e.get("reason_text", ""),
            "qty": int(e.get("qty") or 0),
            "is_shortage": _is_shortage_entry(e),
        }
        for e in rejection_entries
        if int(e.get("qty") or 0) > 0
    }
    from .models import IS_PartialAcceptLot, IS_PartialRejectLot, IS_AllocationTray
    from .services_submitted import generate_lot_id

    _user = user if getattr(user, "is_authenticated", False) else None

    # ── Parent submission record (only fields that exist on the model) ──
    submission = InputScreening_Submitted.objects.create(
        lot_id=lot_id,
        batch_id=batch_id_val,
        module_name="Input Screening",
        plating_stock_no=ctx.get("plating_stk_no"),
        model_no=ctx.get("model_no"),
        tray_type=tray_type,
        tray_capacity=capacity,
        original_lot_qty=lot_qty,
        active_trays_count=len(active_trays),
        remarks=remarks,
        is_partial_accept=True,
        is_partial_reject=True,
        is_full_accept=False,
        is_full_reject=False,
        is_active=True,
        is_submitted=True,
        submitted_at=timezone.now(),
        Draft_Saved=False,
        created_by=_user,
    )

    # ── Accept child lot ────────────────────────────────────────────────
    accept_lot = IS_PartialAcceptLot.objects.create(
        new_lot_id=generate_lot_id(),
        parent_lot_id=lot_id,
        parent_batch_id=batch_id_val,
        parent_submission=submission,
        accepted_qty=total_accept,
        accept_trays_count=len(accept_alloc),
        trays_snapshot=accept_alloc,
        created_by=_user,
    )
    for a in accept_alloc:
        IS_AllocationTray.objects.create(
            accept_lot=accept_lot,
            tray_id=a["tray_id"],
            qty=a["qty"],
            top_tray=bool(a.get("top_tray", False)),
            is_delinked=False,
        )

    # ── Reject child lot (only created when there are non-shortage rejections) ──
    reject_lot = IS_PartialRejectLot.objects.create(
        new_lot_id=generate_lot_id(),
        parent_lot_id=lot_id,
        parent_batch_id=batch_id_val,
        parent_submission=submission,
        rejected_qty=total_non_shortage_reject,  # shortage not counted as reject
        reject_trays_count=reject_only_count,    # only actual reject trays, not delinks
        rejection_reasons=rejection_reasons_json,
        trays_snapshot=reject_alloc,
        delink_count=len(delinked_ids),
        remarks=remarks,
        created_by=_user,
    )
    for r in reject_alloc:
        IS_AllocationTray.objects.create(
            reject_lot=reject_lot,
            tray_id=r["tray_id"],
            qty=r["qty"],
            rejection_reason_id=r.get("reason_id"),
            rejection_reason_text=r.get("reason_text"),
            is_delinked=(r.get("source") == "reused"),
        )

    # Sync DPTrayId_History so downstream modules (Brass QC) see only the
    # accepted trays. Two categories of trays must be delinked:
    #   1. Trays reused as reject trays (source='reused' in reject_alloc)
    #   2. Trays explicitly delinked by the operator (delink_tray_ids)
    # Accept tray quantities are updated to the allocated qty.
    from DayPlanning.models import DPTrayId_History as _DPH
    _reused_ids = {r["tray_id"] for r in reject_alloc if r.get("source") == "reused"}
    _all_delink_ids = set(delinked_ids) | _reused_ids
    if _all_delink_ids:
        _DPH.objects.filter(lot_id=lot_id, tray_id__in=_all_delink_ids).update(delink_tray=True)
    for _a in accept_alloc:
        _DPH.objects.filter(lot_id=lot_id, tray_id=_a["tray_id"]).update(
            tray_quantity=_a["qty"]
        )

    # Mark only real reject trays as permanently rejected. Pure delink rows are
    # audit/release entries (qty=0, no reason) and must remain reusable.
    from .models import IPTrayId as _IPTrayId
    from modelmasterapp.models import TrayId as _TrayId
    _reject_tray_ids_v2 = {
        r["tray_id"]
        for r in reject_alloc
        if int(r.get("qty") or 0) > 0 and (r.get("reason_id") or r.get("reason_text"))
    }
    if _reject_tray_ids_v2:
        _IPTrayId.objects.filter(tray_id__in=_reject_tray_ids_v2).update(
            rejected_tray=True,
            delink_tray=False,
        )
        _TrayId.objects.filter(tray_id__in=_reject_tray_ids_v2).update(
            rejected_tray=True,
            delink_tray=False,
        )

    if delinked_ids:
        _IPTrayId.objects.filter(tray_id__in=delinked_ids).update(
            lot_id=None,
            batch_id=None,
            delink_tray=True,
            rejected_tray=False,
            new_tray=True,
        )
        _TrayId.objects.filter(tray_id__in=delinked_ids).update(
            lot_id=None,
            batch_id=None,
            delink_tray=True,
            rejected_tray=False,
            scanned=False,
            new_tray=True,
        )
    logger.info(
        "[IS][PARTIAL_SUBMIT_V2] marked %d reject tray(s) rejected and released %d delink tray(s): reject=%s delink=%s",
        len(_reject_tray_ids_v2), len(delinked_ids), sorted(_reject_tray_ids_v2), sorted(delinked_ids),
    )

    _mark_lot_submitted_flags(lot_id, accepted_qty=total_accept)

    logger.info(
        "[IS][PARTIAL_SUBMIT_V2] lot=%s sub=%s accept_lot=%s reject_lot=%s "
        "shortage=%d reject=%d accept=%d delink=%d user=%s",
        lot_id, submission.id, accept_lot.new_lot_id, reject_lot.new_lot_id,
        shortage_qty, total_non_shortage_reject, total_accept, len(delinked_ids),
        getattr(user, "username", "anonymous"),
    )

    return {
        "success": True,
        "lot_id": lot_id,
        "submission_id": submission.id,
        "accept_lot_id": accept_lot.new_lot_id,
        "reject_lot_id": reject_lot.new_lot_id,
        "total_reject_qty": total_non_shortage_reject,
        "total_shortage_qty": shortage_qty,
        "total_accept_qty": total_accept,
        "effective_lot_qty": effective_lot_qty,
        "reject_trays": reject_only_count,
        "accept_trays": len(accept_alloc),
        "delink_trays": len(delinked_ids),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SAVE DRAFT – persist modal state "as is" without allocation validation
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def save_draft_partial_reject(
    lot_id: str,
    rejection_entries: List[Dict[str, Any]],
    reject_assignments: List[Dict[str, Any]],
    delink_tray_ids: List[str],
    accept_assignments: List[Dict[str, Any]],
    remarks: str,
    user,
    full_lot_reject: bool = False,
) -> Dict[str, Any]:
    """Persist the current Rejection-Window state as a draft.

    Unlike ``finalize_submission_v2`` this helper performs **no** tray-scan
    re-validation and **no** allocation math. It stores the payload exactly
    as the operator left it so the modal can be re-opened later and the
    user can resume where they stopped.

    Uniqueness: ``InputScreening_Submitted.lot_id`` is UNIQUE, so a single
    draft row per lot is kept.  If a draft already exists it is
    overwritten; if a finalized row exists, saving a draft is rejected.
    """
    from .selectors import get_lot_tray_context
    from .models import InputScreening_Submitted

    ctx = get_lot_tray_context(lot_id, lock=True)
    if not ctx["found"]:
        raise ValueError(f"Lot {lot_id} not found.")

    lot_qty: int = ctx["lot_qty"]
    capacity: int = ctx["tray_capacity"] or 1
    tray_type: Optional[str] = ctx["tray_type"]
    active_trays: List[Dict] = ctx["active_trays"]
    batch_id_val: str = ctx.get("batch_id", "")

    # Block draft if this lot is already finalized.
    existing = InputScreening_Submitted.objects.select_for_update().filter(
        lot_id=lot_id
    ).first()
    if existing and existing.is_submitted:
        raise ValueError(f"Lot {lot_id} is already submitted – cannot save draft.")

    # ── Shortage split: accept qty uses effective lot qty (lot_qty - shortage_qty) ──
    shortage_entries_d, reject_entries_d = _split_shortage_entries(rejection_entries)
    shortage_qty_d: int = sum(int(e.get("qty") or 0) for e in shortage_entries_d)
    total_non_shortage_reject_d: int = sum(int(e.get("qty") or 0) for e in reject_entries_d)
    effective_lot_qty_d: int = lot_qty - shortage_qty_d
    total_reject = total_non_shortage_reject_d
    total_accept = max(0, effective_lot_qty_d - total_reject)

    # Build snapshots "as is" – no re-validation, no ID generation.
    reject_trays_json = [
        {
            "tray_id": _norm(a.get("tray_id")),
            "reason_id": a.get("reason_id") or "",
        }
        for a in (reject_assignments or [])
        if _norm(a.get("tray_id"))
    ]
    accept_trays_json = [
        {"tray_id": _norm(a.get("tray_id"))}
        for a in (accept_assignments or [])
        if _norm(a.get("tray_id"))
    ]
    delinked_ids = [_norm(t) for t in (delink_tray_ids or []) if _norm(t)]

    rejection_reasons_json = {
        e["reason_id"]: {
            "reason": e.get("reason_text", ""),
            "qty": int(e.get("qty") or 0),
            "is_shortage": _is_shortage_entry(e),
        }
        for e in rejection_entries
        if e.get("reason_id")
    }

    # Store draft data in the dedicated draft model
    draft_data = {
        "batch_id": batch_id_val,
        "plating_stock_no": ctx.get("plating_stk_no"),
        "model_no": ctx.get("model_no"),
        "tray_type": tray_type,
        "tray_capacity": capacity,
        "lot_qty": lot_qty,
        "total_reject_qty": total_reject,
        "total_accept_qty": total_accept,
        "rejection_reasons_json": rejection_reasons_json,
        "reject_assignments": reject_trays_json,
        "accept_assignments": accept_trays_json,
        "delinked_tray_ids": delinked_ids,
        "active_trays_count": len(active_trays),
        "full_lot_reject": bool(full_lot_reject),
    }

    from .models import IP_Rejection_Draft
    from modelmasterapp.models import TotalStockModel

    draft_obj, created = IP_Rejection_Draft.objects.update_or_create(
        lot_id=lot_id,
        defaults={
            "user": user if getattr(user, "is_authenticated", False) else None,
            "draft_data": draft_data,
            "lot_rejection_remarks": remarks or "",
        },
    )

    # ✅ Update TotalStockModel to reflect Current Stage = "Input Screening"
    TotalStockModel.objects.filter(lot_id=lot_id).update(
        last_process_module="Input Screening",
        current_stage="Input Screening"
    )

    logger.info(
        "[IS][SAVE_DRAFT] lot=%s draft_id=%s created=%s reject=%d accept=%d user=%s",
        lot_id, draft_obj.id, created, total_reject, total_accept,
        getattr(user, "username", "anonymous"),
    )

    return {
        "success": True,
        "lot_id": lot_id,
        "draft_id": draft_obj.id,
        "created": created,
        "total_reject_qty": total_reject,
        "total_accept_qty": total_accept,
        "reject_trays": len(reject_trays_json),
        "accept_trays": len(accept_trays_json),
        "delink_trays": len(delinked_ids),
    }
