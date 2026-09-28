"""
IQF Selectors — DB read-only layer.

All queryset building and data-fetching logic lives here.
Views call these functions — no queryset logic directly in views.

Rule: NO writes here. Only reads. NO parent lot fallback ever.
"""

import logging

from django.db.models import OuterRef, Subquery, Exists, F, Q
from modelmasterapp.models import TotalStockModel

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Current Lot Trays — CRITICAL FIX
# ─────────────────────────────────────────────────────────────────────────────

def get_current_trays(lot_id):
    """
    Returns current lot tray data — SINGLE SOURCE OF TRUTH.

    Returns: (tray_data_list, source_name, total_qty)

    Priority order (CURRENT LOT ONLY — no parent fallback):
      1. IQFTrayId rows for same lot_id (active, non-delinked)
      2. IQF_Submitted snapshot for same lot_id
      3. Brass_QC_Rejected_TrayScan for same lot_id
      4. Brass_Audit_Rejected_TrayScan for same lot_id
      5. Brass_Audit_Tray_Tracking for same lot_id

    NEVER EVER use:
      ❌ Brass_QC_Submission (parent lot data)
      ❌ Parent lot's tray records
      ❌ Fallback reconstruction

    Partial lot remains partial forever.
    """
    from ..models import (
        IQFTrayId,
        IQF_Submitted,
    )
    from Brass_QC.models import Brass_QC_Rejected_TrayScan
    from BrassAudit.models import Brass_Audit_Rejected_TrayScan

    tray_data = []
    source = "unknown"

    # Step 1: IQFTrayId — active tray records for current lot ONLY
    if not tray_data:
        iqf_trays = IQFTrayId.objects.filter(
            lot_id=lot_id,
            rejected_tray=False,
            delink_tray=False,
            tray_quantity__gt=0
        ).order_by('-top_tray', 'tray_id')

        if iqf_trays.exists():
            source = "IQFTrayId"
            tray_data = [
                {
                    "tray_id": t.tray_id,
                    "qty": t.tray_quantity or 0,
                    "is_delinked": False,
                    "is_rejected": False,
                    "is_top": t.top_tray or False,
                    "status": "ACCEPT_TOP" if t.top_tray else "ACCEPT",
                }
                for t in iqf_trays
            ]
            logger.info(
                f"[get_current_trays] {lot_id}: "
                f"Found {len(tray_data)} trays in IQFTrayId"
            )

    # Step 2: IQF_Submitted snapshot for current lot ONLY
    if not tray_data:
        iqf_sub = IQF_Submitted.objects.filter(
            lot_id=lot_id,
            is_completed=True
        ).order_by('-created_at').first()

        if iqf_sub and iqf_sub.original_data:
            source = "IQF_Submitted (snapshot)"
            original_trays = iqf_sub.original_data.get('trays', [])
            tray_data = [
                {
                    "tray_id": t.get('tray_id', ''),
                    "qty": int(t.get('qty', 0)),
                    "is_delinked": False,
                    "is_rejected": False,
                    "is_top": bool(t.get('is_top', t.get('top_tray', False))),
                    "status": "ACCEPT_TOP" if t.get('is_top') else "ACCEPT",
                }
                for t in original_trays
                if int(t.get('qty', 0)) > 0
            ]
            logger.info(
                f"[get_current_trays] {lot_id}: "
                f"Found {len(tray_data)} trays in IQF_Submitted snapshot"
            )

    # Step 3: Brass_QC_Rejected_TrayScan for same lot_id
    if not tray_data:
        qc_reject_rows = Brass_QC_Rejected_TrayScan.objects.filter(lot_id=lot_id)
        if qc_reject_rows.exists():
            source = "Brass_QC_Rejected_TrayScan"
            tray_qty_map = {}
            for row in qc_reject_rows:
                tray_id = getattr(row, 'rejected_tray_id', None) or getattr(row, 'tray_id', None) or ''
                if not tray_id:
                    continue
                try:
                    qty = int(row.rejected_tray_quantity or 0)
                except (ValueError, TypeError):
                    qty = 0
                if qty > 0:
                    tray_qty_map[tray_id] = tray_qty_map.get(tray_id, 0) + qty

            tray_data = [
                {
                    "tray_id": tid,
                    "qty": qty,
                    "is_delinked": False,
                    "is_rejected": False,
                    "is_top": False,
                    "status": "ACCEPT",
                }
                for tid, qty in sorted(tray_qty_map.items())
            ]
            logger.info(
                f"[get_current_trays] {lot_id}: "
                f"Found {len(tray_data)} trays in Brass_QC_Rejected_TrayScan"
            )

    # Step 4: Brass_Audit_Rejected_TrayScan for same lot_id
    if not tray_data:
        ba_reject_rows = Brass_Audit_Rejected_TrayScan.objects.filter(lot_id=lot_id)
        if ba_reject_rows.exists():
            source = "Brass_Audit_Rejected_TrayScan"
            tray_qty_map = {}
            for row in ba_reject_rows:
                tray_id = getattr(row, 'rejected_tray_id', None) or getattr(row, 'tray_id', None) or ''
                if not tray_id:
                    continue
                try:
                    qty = int(row.rejected_tray_quantity or 0)
                except (ValueError, TypeError):
                    qty = 0
                if qty > 0:
                    tray_qty_map[tray_id] = tray_qty_map.get(tray_id, 0) + qty

            tray_data = [
                {
                    "tray_id": tid,
                    "qty": qty,
                    "is_delinked": False,
                    "is_rejected": False,
                    "is_top": False,
                    "status": "ACCEPT",
                }
                for tid, qty in sorted(tray_qty_map.items())
            ]
            logger.info(
                f"[get_current_trays] {lot_id}: "
                f"Found {len(tray_data)} trays in Brass_Audit_Rejected_TrayScan"
            )

    # Compute total qty
    total_qty = sum(
        t['qty'] for t in tray_data
        if not t.get('is_delinked') and not t.get('is_rejected')
    )

    logger.info(
        f"[get_current_trays] {lot_id}: source={source}, "
        f"trays={len(tray_data)}, qty={total_qty}"
    )

    return tray_data, source, total_qty


# ─────────────────────────────────────────────────────────────────────────────
# Pick Table
# ─────────────────────────────────────────────────────────────────────────────

def get_iqf_picktable_base_queryset():
    """
    Returns queryset for IQF pick table.
    Filter by next_process_module='IQF' and other stage conditions.
    """
    queryset = TotalStockModel.objects.select_related(
        'batch_id',
        'batch_id__model_stock_no',
        'batch_id__version',
    ).filter(
        Q(next_process_module='IQF') |
        Q(send_brass_qc=True) |
        Q(send_brass_audit_to_iqf=True)
    ).exclude(
        remove_lot=True
    ).distinct()

    return queryset


# ─────────────────────────────────────────────────────────────────────────────
# Single lot lookup
# ─────────────────────────────────────────────────────────────────────────────

def is_current_iqf_scan_tray(tray_id, lot_id):
    """Validate exact current physical-tray ownership for F2, without history fallback."""
    from ..models import IQFTrayId

    tray_id = str(tray_id or '').strip()
    if not tray_id:
        return False

    # Resolve the latest assignment BEFORE checking flags: a released newer
    # row must not resurrect an older active-looking row for the same tray.
    tray = IQFTrayId.objects.filter(tray_id__iexact=tray_id).order_by(
        '-date', '-pk',
    ).values('lot_id', 'delink_tray', 'rejected_tray', 'tray_quantity', 'remaining_qty').first()
    if (not tray or tray['lot_id'] != lot_id or tray['delink_tray']
            or tray['rejected_tray']
            or (tray['remaining_qty'] or tray['tray_quantity'] or 0) <= 0):
        return False

    # Match IQFPickTableView's movement/completion scope without changing it.
    return TotalStockModel.objects.filter(
        lot_id=lot_id,
        batch_id__total_batch_quantity__gt=0,
    ).filter(
        Q(send_brass_audit_to_iqf=True)
        | Q(brass_qc_rejection=True, last_process_module='Brass QC')
    ).exclude(
        Q(brass_audit_accptance=True, send_brass_audit_to_iqf=False)
        | Q(iqf_acceptance=True) | Q(iqf_rejection=True)
        | Q(send_brass_audit_to_iqf=True, brass_audit_onhold_picking=True)
        | Q(iqf_few_cases_acceptance=True, iqf_onhold_picking=False)
        | Q(is_split=True) | Q(remove_lot=True)
    ).filter(brass_qc_transition_reject_lot_id__isnull=True).exists()


def get_lot(lot_id):
    """
    Returns TotalStockModel for the given lot_id.
    Uses select_related for batch_id to avoid N+1.
    Returns None if not found.
    """
    return TotalStockModel.objects.select_related('batch_id').filter(
        lot_id=lot_id
    ).first()


def get_lot_strict(lot_id):
    """
    Returns TotalStockModel for the given lot_id.
    Raises TotalStockModel.DoesNotExist if not found.
    """
    return TotalStockModel.objects.select_related('batch_id').get(lot_id=lot_id)


# ─────────────────────────────────────────────────────────────────────────────
# IQF Submission lookup
# ─────────────────────────────────────────────────────────────────────────────

def get_iqf_submission(lot_id):
    """
    Returns the latest completed IQF submission for a lot.
    Returns None if not found.
    """
    from ..models import IQF_Submitted
    return IQF_Submitted.objects.filter(
        lot_id=lot_id,
        is_completed=True
    ).order_by('-created_at').first()


def get_iqf_active_tray_count(lot_id):
    """
    Returns count of active IQF trays for a lot (non-rejected, non-delinked).
    Uses get_current_trays to ensure correct count.
    """
    tray_data, _source, _total_qty = get_current_trays(lot_id)
    return len([t for t in tray_data if not t.get('is_rejected')])


# ─────────────────────────────────────────────────────────────────────────────
# Rejection reasons
# ─────────────────────────────────────────────────────────────────────────────

def get_rejection_reasons_qs():
    """
    Returns queryset of all rejection reason codes.
    """
    from ..models import IQF_Rejection_Table
    return IQF_Rejection_Table.objects.all()
