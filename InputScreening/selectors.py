"""Input Screening – read-side selectors.

All heavy ORM read queries used by Input Screening views live here so the
views stay thin. Behaviour is intentionally identical to the previous
inline implementations – the same fields are annotated and the same
filters applied. The only differences are:

* Subqueries are built once and reused.
* ``select_related`` is added for FK joins to avoid N+1 hits during
  template rendering / row enrichment.
* The list of ``.values(...)`` columns lives in a single constant.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from django.db.models import Exists, F, OuterRef, Q, QuerySet, Subquery

logger = logging.getLogger(__name__)

# Imported lazily inside functions to avoid heavy import-time fan-out.

PICK_TABLE_COLUMNS = (
    "batch_id",
    "date_time",
    "model_stock_no__model_no",
    "plating_color",
    "polish_finish",
    "version__version_name",
    "vendor_internal",
    "location__location_name",
    "no_of_trays",
    "tray_type",
    "total_batch_quantity",
    "tray_capacity",
    "Moved_to_D_Picker",
    "last_process_module",
    "next_process_module",
    "Draft_Saved",
    "wiping_required",
    "stock_lot_id",
    "ip_person_qty_verified",
    "accepted_Ip_stock",
    "rejected_ip_stock",
    "few_cases_accepted_Ip_stock",
    "accepted_tray_scan_status",
    "IP_pick_remarks",
    "dp_pick_remarks",
    "ip_onhold_picking",
    "created_at",
    "plating_stk_no",
    "polishing_stk_no",
    "category",
    "version__version_internal",
    "total_ip_accepted_quantity",
    "ip_hold_lot",
    "ip_holding_reason",
    "ip_release_lot",
    "ip_release_reason",
    "tray_verify",
    "lot_rejected_comment",
    "draft_tray_verify",
    "has_draft",  # ✅ Added for draft indicator
)

def _latest(field: str):
    """Return a Subquery that pulls ``field`` from the most recent
    ``TotalStockModel`` row for the outer ``ModelMasterCreation``.

    Excludes synthetic ``EX-*`` excess-lot rows: Jig Loading creates its own
    ``TotalStockModel`` row (sharing the same ``batch_id``) when a lot has
    leftover excess quantity, and that row keeps advancing through later
    stages (Jig Unloading, Nickel Inspection, ...) independently of the
    original lot. Once it becomes the newest row for the batch it would
    otherwise hijack every annotated field here — including ``stock_lot_id``
    — making the Input Screening pick table show the excess lot (with no
    Input Screening data of its own) instead of the real production lot.
    """
    from modelmasterapp.models import TotalStockModel

    return Subquery(
        TotalStockModel.objects.filter(batch_id=OuterRef("pk"))
        .exclude(lot_id__startswith="EX-")
        .order_by("-id")
        .values(field)[:1]
    )

def pick_table_queryset() -> QuerySet:
    """Build the queryset that powers the Input Screening Pick Table.

    Mirrors the exact filter / annotate / exclude / order chain of the
    legacy view so the page contents are unchanged.
    
    **ERR3 FIX**: Excludes submitted lots (those in InputScreening_Submitted).
    Once a lot is submitted, it moves to the appropriate Completed/Reject table.
    
    **DRAFT SUPPORT**: Lots with active drafts (Draft_Saved=True, is_submitted=False)
    remain in Pick Table so users can continue work. Only final submit removes them.
    """
    from modelmasterapp.models import ModelMasterCreation, TotalStockModel
    from .models import IP_Rejection_ReasonStore, InputScreening_Submitted

    tray_scan_exists = Exists(TotalStockModel.objects.filter(batch_id=OuterRef("pk")))
    
    # Check if this lot_id has been FINALIZED (is_submitted=True)
    # Draft lots (Draft_Saved=True, is_submitted=False) should NOT be excluded
    submitted_lots = Exists(
        InputScreening_Submitted.objects.filter(
            lot_id=OuterRef("stock_lot_id"),
            is_active=True,
            is_submitted=True  # ✅ Only exclude finalized submissions
        )
    )
    
    submitted_batches = Exists(
        InputScreening_Submitted.objects.filter(
            batch_id=OuterRef("batch_id"),
            is_active=True,
            is_submitted=True,
        )
    )

    # Check if this lot has an active reject-modal draft (saved via Save Draft button)
    from .models import IP_Rejection_Draft
    has_draft = Exists(
        IP_Rejection_Draft.objects.filter(
            lot_id=OuterRef("stock_lot_id"),
        )
    )

    qs = (
        ModelMasterCreation.objects.select_related(
            "model_stock_no",
            "version",
            "location",
        )
        .filter(total_batch_quantity__gt=0)
        .annotate(
            last_process_module=_latest("last_process_module"),
            next_process_module=_latest("next_process_module"),
            wiping_required=F("model_stock_no__wiping_required"),
            stock_lot_id=_latest("lot_id"),
            ip_person_qty_verified=_latest("ip_person_qty_verified"),
            lot_rejected_comment=Subquery(
                IP_Rejection_ReasonStore.objects.filter(
                    lot_id=OuterRef("stock_lot_id")
                ).values("lot_rejected_comment")[:1]
            ),
            accepted_Ip_stock=_latest("accepted_Ip_stock"),
            accepted_tray_scan_status=_latest("accepted_tray_scan_status"),
            rejected_ip_stock=_latest("rejected_ip_stock"),
            few_cases_accepted_Ip_stock=_latest("few_cases_accepted_Ip_stock"),
            ip_onhold_picking=_latest("ip_onhold_picking"),
            tray_verify=_latest("tray_verify"),
            draft_tray_verify=_latest("draft_tray_verify"),
            tray_scan_exists=tray_scan_exists,
            IP_pick_remarks=_latest("IP_pick_remarks"),
            created_at=_latest("created_at"),
            total_ip_accepted_quantity=_latest("total_IP_accpeted_quantity"),
            ip_hold_lot=_latest("ip_hold_lot"),
            ip_holding_reason=_latest("ip_holding_reason"),
            ip_release_lot=_latest("ip_release_lot"),
            ip_release_reason=_latest("ip_release_reason"),
            remove_lot=_latest("remove_lot"),
            submitted=submitted_lots,
            submitted_by_batch=submitted_batches,
            has_draft=has_draft,  # ✅ Indicate if lot has active draft
        )
        .filter(tray_scan_exists=True, Moved_to_D_Picker=True)
        .exclude(
            Q(accepted_Ip_stock=True)
            | Q(accepted_tray_scan_status=True)
            | Q(rejected_ip_stock=True)
            | Q(few_cases_accepted_Ip_stock=True)
            | Q(remove_lot=True)
            | Q(submitted=True)  # ERR3: Exclude submitted lots
            | Q(submitted_by_batch=True)
            | Q(last_process_module='Jig Loading (Excess)')  # Exclude Jig Loading (Excess) lots
        )
        .order_by("-created_at")
    )
    return qs


# ─────────────────────────────────────────────────────────────────────────────
# REJECT MODAL — LOT + TRAY CONTEXT QUERY
# ─────────────────────────────────────────────────────────────────────────────

def get_lot_tray_context(lot_id: str, lock: bool = False) -> Dict[str, Any]:
    """Fetch all tray and lot metadata required for the reject modal and
    allocation engine.

    Args:
        lot_id: Lot ID string (stock_lot_id on ModelMasterCreation).
        lock:   When True, applies ``select_for_update()`` on DPTrayId_History
                rows to prevent concurrent allocation races during final submit.

    Returns:
        {
            found: bool,
            lot_qty: int,
            tray_type: str|None,
            tray_capacity: int,
            active_trays: [{tray_id, qty}],
            batch_id: str|None,
            model_no: str|None,
            plating_stk_no: str|None,
        }
    """
    from DayPlanning.models import DPTrayId_History
    from modelmasterapp.models import ModelMasterCreation, TotalStockModel
    from .models import IS_PartialAcceptLot, IS_PartialRejectLot

    # ``lot_id`` arriving here is the value stored on TotalStockModel.lot_id
    # (the same value rendered as ``data-stock-lot-id`` in the pick table).
    # Resolve to the parent ModelMasterCreation row via the FK on TotalStockModel.
    ts_row = (
        TotalStockModel.objects.filter(lot_id=lot_id)
        .only("batch_id")
        .first()
    )
    if not ts_row or not ts_row.batch_id_id:
        # Check if this is a child lot from IS partial rejection
        partial_accept = IS_PartialAcceptLot.objects.filter(new_lot_id=lot_id).first()
        if partial_accept:
            # Use accepted lot data: parent batch_id and accepted qty
            ts_row = TotalStockModel.objects.filter(lot_id=partial_accept.parent_lot_id).only("batch_id").first()
            if ts_row and ts_row.batch_id_id:
                # Build active_trays from trays_snapshot
                active_trays: List[Dict[str, Any]] = [
                    {
                        "tray_id": t["tray_id"],
                        "qty": t.get("qty", 0),
                        "top_tray": bool(t.get("is_top", t.get("top_tray", False))),
                    }
                    for t in (partial_accept.trays_snapshot or [])
                ]
                
                mmc = ModelMasterCreation.objects.filter(pk=ts_row.batch_id_id).select_related("model_stock_no").only(
                    "batch_id", "total_batch_quantity", "tray_capacity", "tray_type", "plating_stk_no", "model_stock_no__model_no"
                ).first()
                
                if mmc:
                    capacity = mmc.tray_capacity or next((t["qty"] for t in active_trays if t["qty"] > 0), 16)
                    tray_type_val = active_trays[0].get("tray_type") if active_trays else None
                    
                    return {
                        "found": True,
                        "lot_qty": partial_accept.accepted_qty,
                        "tray_type": tray_type_val,
                        "tray_capacity": capacity,
                        "active_trays": active_trays,
                        "batch_id": str(mmc.batch_id) if mmc.batch_id else None,
                        "model_no": mmc.model_stock_no.model_no if mmc.model_stock_no_id else None,
                        "plating_stk_no": mmc.plating_stk_no,
                    }
        
        # Check if this is a reject lot from IS partial rejection (shouldn't reach Brass QC, but handle it)
        partial_reject = IS_PartialRejectLot.objects.filter(new_lot_id=lot_id).first()
        if partial_reject:
            # Use rejected lot data: parent batch_id and rejected qty
            ts_row = TotalStockModel.objects.filter(lot_id=partial_reject.parent_lot_id).only("batch_id").first()
            if ts_row and ts_row.batch_id_id:
                # Build active_trays from trays_snapshot
                active_trays: List[Dict[str, Any]] = [
                    {
                        "tray_id": t["tray_id"],
                        "qty": t.get("qty", 0),
                        "top_tray": bool(t.get("is_top", t.get("top_tray", False))),
                    }
                    for t in (partial_reject.trays_snapshot or [])
                ]
                
                mmc = ModelMasterCreation.objects.filter(pk=ts_row.batch_id_id).select_related("model_stock_no").only(
                    "batch_id", "total_batch_quantity", "tray_capacity", "tray_type", "plating_stk_no", "model_stock_no__model_no"
                ).first()
                
                if mmc:
                    capacity = mmc.tray_capacity or next((t["qty"] for t in active_trays if t["qty"] > 0), 16)
                    tray_type_val = active_trays[0].get("tray_type") if active_trays else None
                    
                    return {
                        "found": True,
                        "lot_qty": partial_reject.rejected_qty,
                        "tray_type": tray_type_val,
                        "tray_capacity": capacity,
                        "active_trays": active_trays,
                        "batch_id": str(mmc.batch_id) if mmc.batch_id else None,
                        "model_no": mmc.model_stock_no.model_no if mmc.model_stock_no_id else None,
                        "plating_stk_no": mmc.plating_stk_no,
                    }
        
        return {"found": False}

    mmc = (
        ModelMasterCreation.objects.filter(pk=ts_row.batch_id_id)
        .select_related("model_stock_no")
        .only(
            "batch_id",
            "total_batch_quantity",
            "tray_capacity",
            "tray_type",
            "plating_stk_no",
            "model_stock_no__model_no",
        )
        .first()
    )

    if not mmc:
        return {"found": False}

    tray_qs = DPTrayId_History.objects.filter(lot_id=lot_id, delink_tray=False)
    if lock:
        tray_qs = tray_qs.select_for_update()

    active_trays: List[Dict[str, Any]] = [
        {
            "tray_id": t["tray_id"],
            "qty": t["tray_quantity"] or 0,
            "top_tray": bool(t.get("top_tray")),
        }
        for t in tray_qs.order_by("id").values(
            "tray_id", "tray_quantity", "top_tray"
        )
    ]

    capacity = (
        mmc.tray_capacity
        or next((t["qty"] for t in active_trays if t["qty"] > 0), 16)
    )
    tray_type_val: Optional[str] = None
    if active_trays:
        first_tray = (
            DPTrayId_History.objects.filter(lot_id=lot_id, delink_tray=False)
            .only("tray_type")
            .first()
        )
        tray_type_val = first_tray.tray_type if first_tray else None

    return {
        "found": True,
        "lot_qty": mmc.total_batch_quantity or 0,
        "tray_type": tray_type_val,
        "tray_capacity": capacity,
        "active_trays": active_trays,
        "batch_id": str(mmc.batch_id) if mmc.batch_id else None,
        "model_no": (
            mmc.model_stock_no.model_no if mmc.model_stock_no_id else None
        ),
        "plating_stk_no": mmc.plating_stk_no,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SUBMITTED TABLE SELECTORS — IS Completed / Accept / Reject tables
# ─────────────────────────────────────────────────────────────────────────────

def _get_model_images(batch) -> List[str]:
    """Return a list of image URLs for the given ModelMasterCreation batch."""
    if not batch or not batch.model_stock_no_id:
        return []
    try:
        from modelmasterapp.image_utils import sort_images_front_first
        result = []
        for img in sort_images_front_first(batch.model_stock_no.images.all()):
            if img.master_image:
                result.append(img.master_image.url)
        return result
    except Exception:
        return []


def _enrich_from_batch(batch) -> Dict[str, Any]:
    """Return template-compatible fields from a ModelMasterCreation row."""
    from modelmasterapp.type_of_input import get_type_of_input_for_batch

    if not batch:
        return {
            "polishing_stk_no": "",
            "plating_color": "",
            "category": "",
            "polish_finish": "",
            "location__location_name": "",
            "no_of_trays": 0,
            "model_images": [],
            "Moved_to_D_Picker": False,
            "type_of_input": get_type_of_input_for_batch(None),
        }
    return {
        "polishing_stk_no": batch.polishing_stk_no or "",
        "plating_color": batch.plating_color or "",
        "category": batch.category or "",
        "polish_finish": batch.polish_finish or "",
        "location__location_name": (
            batch.location.location_name if batch.location_id else ""
        ),
        "no_of_trays": batch.no_of_trays or 0,
        "model_images": _get_model_images(batch),
        "Moved_to_D_Picker": bool(getattr(batch, "Moved_to_D_Picker", False)),
        "type_of_input": get_type_of_input_for_batch(batch),
    }


def _current_stage_display(stock) -> str:
    """Live current-stage label for a TotalStockModel row (SSOT-backed)."""
    from modelmasterapp.stage_service import get_stock_current_stage

    return get_stock_current_stage(stock)


def _enrich_from_stock(stock) -> Dict[str, Any]:
    """Return template-compatible flags from a TotalStockModel row."""
    if not stock:
        return {
            "ip_person_qty_verified": False,
            "accepted_Ip_stock": False,
            "rejected_ip_stock": False,
            "few_cases_accepted_Ip_stock": False,
            "brass_qc_accepted_qty_verified": False,
            "last_process_module": "",
            "last_process_date_time": None,
            "IP_pick_remarks": "",
            "lot_rejected_comment": "",
            "audio_remark": None,
            "ip_onhold_picking": False,
        }
    return {
        "ip_person_qty_verified": bool(stock.ip_person_qty_verified),
        "accepted_Ip_stock": bool(stock.accepted_Ip_stock),
        "rejected_ip_stock": bool(stock.rejected_ip_stock),
        "few_cases_accepted_Ip_stock": bool(stock.few_cases_accepted_Ip_stock),
        "brass_qc_accepted_qty_verified": bool(stock.brass_qc_accepted_qty_verified),
        "last_process_module": _current_stage_display(stock),
        "last_process_date_time": stock.last_process_date_time,
        "IP_pick_remarks": stock.IP_pick_remarks or "",
        "lot_rejected_comment": "",
        "audio_remark": None,
        "ip_onhold_picking": bool(stock.ip_onhold_picking),
    }


def get_completed_table_rows(from_date=None, to_date=None) -> List[Dict[str, Any]]:
    """Build rows for the IS Completed Table from InputScreening_Submitted."""
    from .models import InputScreening_Submitted
    from modelmasterapp.models import ModelMasterCreation, TotalStockModel

    subs_qs = (
        InputScreening_Submitted.objects
        .filter(is_submitted=True, is_active=True)
        .prefetch_related("partial_accept_lots", "partial_reject_lots")
        .order_by("-submitted_at")
    )
    if from_date:
        subs_qs = subs_qs.filter(submitted_at__date__gte=from_date)
    if to_date:
        subs_qs = subs_qs.filter(submitted_at__date__lte=to_date)

    subs = list(subs_qs)

    batch_ids = [s.batch_id for s in subs if s.batch_id]
    lot_ids = [s.lot_id for s in subs if s.lot_id]

    batches = {
        b.batch_id: b
        for b in ModelMasterCreation.objects.filter(batch_id__in=batch_ids)
        .select_related("model_stock_no", "location", "version")
    }
    stocks = {
        ts.lot_id: ts
        for ts in TotalStockModel.objects.filter(lot_id__in=lot_ids)
    }

    rows: List[Dict[str, Any]] = []
    for sub in subs:
        batch = batches.get(sub.batch_id)
        stock = stocks.get(sub.lot_id)

        accept_lot = sub.partial_accept_lots.first()
        reject_lot = sub.partial_reject_lots.first()
        accepted_qty = accept_lot.accepted_qty if accept_lot else 0
        rejected_qty = reject_lot.rejected_qty if reject_lot else 0

        row: Dict[str, Any] = {
            "batch_id": sub.batch_id,
            "stock_lot_id": sub.lot_id,
            "plating_stk_no": sub.plating_stock_no or "",
            "tray_type": sub.tray_type or (batch.tray_type if batch else ""),
            "tray_capacity": sub.tray_capacity or (batch.tray_capacity if batch else 0),
            "total_stock": sub.original_lot_qty,
            "display_accepted_qty": accepted_qty,
            "ip_rejection_total_qty": rejected_qty,
            "tray_qty_list": (
                accept_lot.trays_snapshot if accept_lot else []
            ),
            "ip_hold_lot": False,
        }
        row.update(_enrich_from_batch(batch))
        row.update(_enrich_from_stock(stock))

        # Override no_of_trays with actual tray count from the submission record
        # (batch.no_of_trays is the original planned count, not the post-split actual count)
        # For partial accept/reject: count BOTH accept AND reject trays (including delinked)
        accept_count = 0
        reject_count = 0
        
        if accept_lot:
            accept_count = accept_lot.accept_trays_count
            if not accept_count and accept_lot.trays_snapshot:
                accept_count = len(accept_lot.trays_snapshot)
        
        if reject_lot:
            # reject_trays_count includes both reject trays and delinked trays
            # because trays_snapshot contains all trays (reject + delinked)
            reject_count = reject_lot.reject_trays_count
            if not reject_count and reject_lot.trays_snapshot:
                reject_count = len(reject_lot.trays_snapshot)
        
        total_tray_count = accept_count + reject_count
        if total_tray_count > 0:
            row["no_of_trays"] = total_tray_count

        # Fallback timestamp: use submitted_at when stock record is absent
        if not row.get("last_process_date_time"):
            row["last_process_date_time"] = sub.submitted_at

        rows.append(row)
    return rows


def get_accept_table_rows(from_date=None, to_date=None) -> List[Dict[str, Any]]:
    """Build rows for the IS Accept Table from IS_PartialAcceptLot."""
    from .models import IS_PartialAcceptLot, InputScreening_Submitted
    from modelmasterapp.models import ModelMasterCreation, TotalStockModel

    pal_qs = (
        IS_PartialAcceptLot.objects
        .select_related("parent_submission")
        .prefetch_related("allocation_trays")
        .order_by("-created_at")
    )
    if from_date:
        pal_qs = pal_qs.filter(created_at__date__gte=from_date)
    if to_date:
        pal_qs = pal_qs.filter(created_at__date__lte=to_date)

    accept_lots = list(pal_qs)

    batch_ids = [a.parent_batch_id for a in accept_lots if a.parent_batch_id]
    lot_ids = [a.parent_lot_id for a in accept_lots if a.parent_lot_id]

    batches = {
        b.batch_id: b
        for b in ModelMasterCreation.objects.filter(batch_id__in=batch_ids)
        .select_related("model_stock_no", "location", "version")
    }
    stocks = {
        ts.lot_id: ts
        for ts in TotalStockModel.objects.filter(lot_id__in=lot_ids)
    }

    rows: List[Dict[str, Any]] = []
    for al in accept_lots:
        sub = al.parent_submission
        batch = batches.get(al.parent_batch_id)
        stock = stocks.get(al.parent_lot_id)

        # Resolve tray list — fall back to IS_AllocationTray when trays_snapshot absent
        # (full accept records created before this fix, or created via create_full_accept_child_lot)
        trays_list = al.trays_snapshot or []
        if not trays_list:
            trays_list = [
                {
                    "tray_id": t.tray_id,
                    "qty": t.qty,
                    "top_tray": t.top_tray,
                    "source": "existing",
                }
                for t in al.allocation_trays.all()
            ]

        # Identify top tray from resolved list
        top_tray_entry = next(
            (t for t in trays_list if t.get("top_tray")),
            None,
        )

        row: Dict[str, Any] = {
            "batch_id": al.parent_batch_id,
            "stock_lot_id": al.parent_lot_id,
            "accept_lot_id": al.new_lot_id,
            "plating_stk_no": sub.plating_stock_no if sub else "",
            "tray_type": (sub.tray_type if sub else "") or (batch.tray_type if batch else ""),
            "tray_capacity": (sub.tray_capacity if sub else 0) or (batch.tray_capacity if batch else 0),
            "total_stock": sub.original_lot_qty if sub else 0,
            "display_accepted_qty": al.accepted_qty,
            "ip_rejection_total_qty": 0,
            "no_of_trays": al.accept_trays_count,
            "top_tray_id": top_tray_entry.get("tray_id") if top_tray_entry else "",
            "top_tray_qty": top_tray_entry.get("qty") if top_tray_entry else 0,
            "tray_qty_list": trays_list,
            "submission_type": "FULL ACCEPT" if (sub and sub.is_full_accept) else "PARTIAL ACCEPT",
            "ip_hold_lot": False,
        }
        row.update(_enrich_from_batch(batch))
        row.update(_enrich_from_stock(stock))
        # Override no_of_trays with accept lot count
        row["no_of_trays"] = al.accept_trays_count

        if not row.get("last_process_date_time"):
            row["last_process_date_time"] = al.created_at

        rows.append(row)
    return rows


def get_reject_table_rows(from_date=None, to_date=None) -> List[Dict[str, Any]]:
    """Build rows for the IS Reject Table from IS_PartialRejectLot."""
    from .models import IS_PartialRejectLot, InputScreening_Submitted
    from modelmasterapp.models import ModelMasterCreation, TotalStockModel

    prl_qs = (
        IS_PartialRejectLot.objects
        .select_related("parent_submission")
        .prefetch_related("allocation_trays")
        .order_by("-created_at")
    )
    if from_date:
        prl_qs = prl_qs.filter(created_at__date__gte=from_date)
    if to_date:
        prl_qs = prl_qs.filter(created_at__date__lte=to_date)

    reject_lots = list(prl_qs)

    batch_ids = [r.parent_batch_id for r in reject_lots if r.parent_batch_id]
    lot_ids = [r.parent_lot_id for r in reject_lots if r.parent_lot_id]

    batches = {
        b.batch_id: b
        for b in ModelMasterCreation.objects.filter(batch_id__in=batch_ids)
        .select_related("model_stock_no", "location", "version")
    }
    stocks = {
        ts.lot_id: ts
        for ts in TotalStockModel.objects.filter(lot_id__in=lot_ids)
    }

    rows: List[Dict[str, Any]] = []
    for rl in reject_lots:
        sub = rl.parent_submission
        batch = batches.get(rl.parent_batch_id)
        stock = stocks.get(rl.parent_lot_id)

        # Build rejection_reason_letters from first char of each reason text
        reasons_json: Dict = rl.rejection_reasons or {}
        rejection_reason_letters = [
            v.get("reason", "?")[0].upper()
            for v in reasons_json.values()
            if v.get("reason")
        ]

        # Resolve tray list — fall back to IS_AllocationTray when trays_snapshot absent
        # (full reject records created before this fix, or created via create_full_reject_child_lot)
        rl_trays = rl.trays_snapshot or []
        if not rl_trays:
            rl_trays = [
                {
                    "tray_id": t.tray_id,
                    "qty": t.qty,
                    "top_tray": t.top_tray,
                    "reason_text": t.rejection_reason_text or "",
                    "source": "existing",
                }
                for t in rl.allocation_trays.all()
            ]

        # Delinked trays from resolved list
        delinked_tray_ids = [
            t["tray_id"]
            for t in rl_trays
            if t.get("source") == "reused"
        ]

        row: Dict[str, Any] = {
            "batch_id": rl.parent_batch_id,
            "stock_lot_id": rl.parent_lot_id,
            "reject_lot_id": rl.new_lot_id,
            "plating_stk_no": sub.plating_stock_no if sub else "",
            "tray_type": (sub.tray_type if sub else "") or (batch.tray_type if batch else ""),
            "tray_capacity": (sub.tray_capacity if sub else 0) or (batch.tray_capacity if batch else 0),
            "total_stock": sub.original_lot_qty if sub else 0,
            "ip_rejection_total_qty": rl.rejected_qty,
            "display_accepted_qty": 0,
            "no_of_trays": rl.reject_trays_count,
            "rejection_reason_letters": rejection_reason_letters,
            "rejection_reasons_json": reasons_json,
            "delinked_tray_ids": delinked_tray_ids,
            "tray_qty_list": rl_trays,
            "submission_type": "FULL REJECT" if (sub and sub.is_full_reject) else "PARTIAL REJECT",
            "lot_rejected_comment": rl.remarks or "",
            "tray_id_in_trayid": True,
            "ip_hold_lot": False,
            "Moved_to_D_Picker": False,
        }
        row.update(_enrich_from_batch(batch))
        row.update(_enrich_from_stock(stock))
        # Override lot_rejected_comment with reject remarks
        if rl.remarks:
            row["lot_rejected_comment"] = rl.remarks
        # Override no_of_trays
        row["no_of_trays"] = rl.reject_trays_count

        if not row.get("last_process_date_time"):
            row["last_process_date_time"] = rl.created_at

        rows.append(row)
    return rows


def get_submitted_detail(lot_id: str) -> Dict[str, Any]:
    """Return accept, reject and delinked tray details for a submitted lot.

    Used by IS_SubmittedDetailAPI to populate the view-icon modal.
    """
    from django.utils import timezone
    from .models import InputScreening_Submitted

    sub = (
        InputScreening_Submitted.objects
        .filter(lot_id=lot_id, is_submitted=True, is_active=True)
        .prefetch_related(
            "partial_accept_lots",
            "partial_accept_lots__allocation_trays",
            "partial_reject_lots",
            "partial_reject_lots__allocation_trays",
        )
        .first()
    )
    if not sub:
        return {"success": False, "error": f"No submitted record found for lot {lot_id}"}

    def _fmt_dt(dt):
        if not dt:
            return ""
        local_dt = timezone.localtime(dt)
        return local_dt.strftime("%B %d, %Y, %I:%M %p").lstrip("0")

    accept_lots_data = []
    for al in sub.partial_accept_lots.all().order_by("created_at"):
        # Fall back to IS_AllocationTray records when trays_snapshot absent (full accept records)
        al_trays = al.trays_snapshot or []
        if not al_trays:
            al_trays = [
                {
                    "tray_id": t.tray_id,
                    "qty": t.qty,
                    "top_tray": t.top_tray,
                    "source": "existing",
                }
                for t in al.allocation_trays.all()
            ]
        accept_lots_data.append({
            "new_lot_id": al.new_lot_id,
            "accepted_qty": al.accepted_qty,
            "accept_trays_count": al.accept_trays_count,
            "trays": al_trays,
            "created_at": _fmt_dt(al.created_at),
        })

    reject_lots_data = []
    for rl in sub.partial_reject_lots.all().order_by("created_at"):
        # Fall back to IS_AllocationTray records when trays_snapshot absent (full reject records)
        rl_trays = rl.trays_snapshot or []
        if not rl_trays:
            rl_trays = [
                {
                    "tray_id": t.tray_id,
                    "qty": t.qty,
                    "top_tray": t.top_tray,
                    "reason_text": t.rejection_reason_text or "",
                    "source": "existing",
                }
                for t in rl.allocation_trays.all()
            ]
        # ✅ FIX: Delinked trays have source="reused" AND qty=0
        # Reused reject trays have source="reused" AND qty>0
        delinked = [
            t["tray_id"]
            for t in rl_trays
            if t.get("source") == "reused" and t.get("qty", 0) == 0
        ]
        reject_lots_data.append({
            "new_lot_id": rl.new_lot_id,
            "rejected_qty": rl.rejected_qty,
            "reject_trays_count": rl.reject_trays_count,
            "rejection_reasons": rl.rejection_reasons or {},
            "trays": rl_trays,
            "delinked_tray_ids": delinked,
            "delink_count": rl.delink_count,
            "remarks": rl.remarks or "",
            "created_at": _fmt_dt(rl.created_at),
        })

    return {
        "success": True,
        "lot_id": lot_id,
        "original_lot_qty": sub.original_lot_qty,
        "plating_stock_no": sub.plating_stock_no,
        "model_no": sub.model_no,
        "is_full_accept": sub.is_full_accept,
        "is_full_reject": sub.is_full_reject,
        "is_partial_accept": sub.is_partial_accept,
        "is_partial_reject": sub.is_partial_reject,
        "parent_created_at": _fmt_dt(sub.created_at) if hasattr(sub, "created_at") else "",
        "accept_lots": accept_lots_data,
        "reject_lots": reject_lots_data,
    }
