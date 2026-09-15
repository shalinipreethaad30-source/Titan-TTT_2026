import logging
logger = logging.getLogger(__name__)
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.renderers import TemplateHTMLRenderer
from django.shortcuts import render
from django.shortcuts import redirect
from django.db.models import OuterRef, Subquery, Exists, F, Sum
from django.core.paginator import Paginator
from django.templatetags.static import static
import math
from Brass_QC.models import *
from BrassAudit.models import *
from InputScreening.models import *
from DayPlanning.models import *
from modelmasterapp.models import TrayId
from modelmasterapp.type_of_input import get_type_of_input_for_batch, get_type_of_input_map
from .models import *
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.contrib.auth.decorators import login_required
import traceback
from rest_framework import status
from django.http import JsonResponse
import json
from rest_framework.permissions import IsAuthenticated
from django.views.decorators.http import require_GET
from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from math import ceil
from django.utils import timezone
from datetime import datetime, timedelta
import pytz
import time
from django.db.models import Sum
from django.views.decorators.http import require_http_methods
from django.views import View
from django.db.models import Sum, F, Func, IntegerField
from django.db import transaction
from collections import OrderedDict


# Generate Lot ID (microsecond-based with exists() retry loop)
def generate_new_lot_id():
    from datetime import datetime
    import time

    max_attempts = 10
    attempt = 0
    while True:
        ts = datetime.now().strftime("%d%m%Y%H%M%S%f")  # includes microseconds
        lot_id = f"LID{ts}"
        # Check for collision and retry if necessary
        if not TotalStockModel.objects.filter(lot_id=lot_id).exists():
            return lot_id

        attempt += 1
        if attempt >= max_attempts:
            # small sleep to allow clock to advance and retry
            time.sleep(0.001)
            attempt = 0


def _safe_int(value, default=0):
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _get_brass_qc_rejected_trays_exact(lot_id):
    rows = Brass_QC_Rejected_TrayScan.objects.filter(lot_id=lot_id)
    tray_qty_map = {}
    for row in rows:
        tray_id = (
            getattr(row, 'rejected_tray_id', None)
            or getattr(row, 'tray_id', None)
            or ''
        ).strip()
        qty = _safe_int(getattr(row, 'rejected_tray_quantity', 0))
        if tray_id and qty > 0:
            tray_qty_map[tray_id] = tray_qty_map.get(tray_id, 0) + qty

    tray_data = [
        {
            'tray_id': tray_id,
            'qty': qty,
            'is_delinked': False,
            'is_rejected': False,
            'is_top': False,
            'status': 'ACCEPT',
        }
        for tray_id, qty in sorted(tray_qty_map.items())
    ]
    return tray_data, 'Brass_QC_Rejected_TrayScan', sum(t['qty'] for t in tray_data)


def _get_brass_qc_submission_rejected_trays_exact(lot_id):
    sub = Brass_QC_Submission.objects.filter(
        lot_id=lot_id,
        is_completed=True,
    ).order_by('-created_at').first()
    if not sub:
        return [], 'Brass_QC_Submission', 0

    reject_src = sub.partial_reject_data or sub.full_reject_data or {}
    tray_qty_map = {}
    tray_top_map = {}
    for tray in reject_src.get('trays') or []:
        tray_id = str(tray.get('tray_id') or '').strip()
        qty = _safe_int(tray.get('qty'))
        if tray_id and qty > 0:
            tray_qty_map[tray_id] = tray_qty_map.get(tray_id, 0) + qty
            tray_top_map[tray_id] = bool(
                tray.get('top_tray', tray.get('is_top', tray.get('is_top_tray', False)))
            )

    tray_data = [
        {
            'tray_id': tray_id,
            'qty': qty,
            'is_delinked': False,
            'is_rejected': False,
            'is_top': tray_top_map.get(tray_id, False),
            'status': 'ACCEPT_TOP' if tray_top_map.get(tray_id, False) else 'ACCEPT',
        }
        for tray_id, qty in sorted(tray_qty_map.items())
    ]
    return tray_data, 'Brass_QC_Submission(reject_snapshot)', sum(t['qty'] for t in tray_data)


def _iqf_tray_entries_from_rows(rows):
    tray_entries = []
    for tray in rows:
        qty = _safe_int(getattr(tray, 'remaining_qty', 0)) or _safe_int(
            getattr(tray, 'tray_quantity', 0)
        )
        tray_id = str(getattr(tray, 'tray_id', '') or '').strip()
        if tray_id and qty > 0:
            tray_entries.append({
                'tray_id': tray_id,
                'qty': qty,
                'top_tray': bool(getattr(tray, 'top_tray', False)),
            })
    return tray_entries


def _iqf_tray_entries_match_qty(tray_entries, expected_qty):
    return bool(tray_entries) and sum(t['qty'] for t in tray_entries) == int(expected_qty or 0)


def _is_input_screening_rejected_tray_active(tray_id):
    """Return True when Input Screening still owns an active rejected tray state."""
    normalized_tray_id = str(tray_id or '').strip().upper()
    if not normalized_tray_id:
        return False

    from InputScreening.models import IPTrayId, IP_Rejected_TrayScan, IS_AllocationTray

    released_for_reuse = (
        TrayId.objects.filter(
            tray_id=normalized_tray_id,
            delink_tray=True,
            rejected_tray=False,
        ).exists() or
        IPTrayId.objects.filter(
            tray_id=normalized_tray_id,
            delink_tray=True,
            rejected_tray=False,
        ).exists()
    )

    reason_exists = (
        Q(rejection_reason_id__isnull=False) |
        (Q(rejection_reason_text__isnull=False) & ~Q(rejection_reason_text=''))
    )
    active_partial_reject = (
        IS_AllocationTray.objects
        .filter(
            tray_id=normalized_tray_id,
            reject_lot__isnull=False,
            qty__gt=0,
            is_delinked=False,
        )
        .filter(reason_exists)
        .exists()
    )
    if active_partial_reject and not released_for_reuse:
        return True

    if IPTrayId.objects.filter(
        tray_id=normalized_tray_id,
        rejected_tray=True,
        delink_tray=False,
    ).exists():
        return True

    if TrayId.objects.filter(
        tray_id=normalized_tray_id,
        rejected_tray=True,
        delink_tray=False,
    ).exists():
        return True

    legacy_reject_scan_exists = IP_Rejected_TrayScan.objects.filter(
        rejected_tray_id=normalized_tray_id,
    ).exists()
    return legacy_reject_scan_exists and not released_for_reuse


def _input_screening_rejected_tray_ids_from_payload(*payload_lists):
    rejected_tray_ids = []
    seen = set()
    for payload_list in payload_lists:
        if not isinstance(payload_list, list):
            continue
        for item in payload_list:
            tray_id = ''
            if isinstance(item, dict):
                tray_id = item.get('tray_id', '')
            else:
                tray_id = item
            tray_id = str(tray_id or '').strip().upper()
            if tray_id and tray_id not in seen and _is_input_screening_rejected_tray_active(tray_id):
                seen.add(tray_id)
                rejected_tray_ids.append(tray_id)
    return rejected_tray_ids


def _tray_ids_from_payload(*payload_lists):
    tray_ids = []
    seen = set()
    for payload_list in payload_lists:
        if not isinstance(payload_list, list):
            continue
        for item in payload_list:
            if isinstance(item, dict):
                tray_id = item.get('tray_id', '')
            else:
                tray_id = item
            tray_id = str(tray_id or '').strip().upper()
            if tray_id and tray_id not in seen:
                seen.add(tray_id)
                tray_ids.append(tray_id)
    return tray_ids


def _validate_iqf_assigned_tray_ids(lot_id, *payload_lists):
    from .services.validators import validate_iqf_cross_module_tray_available

    for tray_id in _tray_ids_from_payload(*payload_lists):
        error = validate_iqf_cross_module_tray_available(
            tray_id,
            current_lot_id=lot_id,
        )
        if error:
            return tray_id, error
    return None, None




def _get_series_tray_capacity(batch):
    """
    Return IQF tray capacity based on the product series.

    1805 -> 12
    2649 -> 16
    2617 -> 16

    For other series, preserve the existing configured batch capacity.
    """
    if not batch:
        return 16

    series_source = str(getattr(batch, 'plating_stk_no', '') or '').strip()

    if not series_source:
        model_stock = getattr(batch, 'model_stock_no', None)
        series_source = str(getattr(model_stock, 'model_no', '') or '').strip()

    series = series_source[:4]

    return {
        '1805': 12,
        '2649': 16,
        '2617': 16,
    }.get(series, getattr(batch, 'tray_capacity', None) or 16)

def _resolve_tray_type_capacity_from_trays(tray_ids, lot_ids=None, fallback_type='', fallback_capacity=0):
    """Resolve tray type/capacity from actual tray records, with batch as last fallback."""
    clean_tray_ids = [str(tid or '').strip().upper() for tid in tray_ids if str(tid or '').strip()]
    clean_lot_ids = [str(lid or '').strip() for lid in (lot_ids or []) if str(lid or '').strip()]

    if clean_tray_ids:
        iqf_qs = IQFTrayId.objects.filter(tray_id__in=clean_tray_ids)
        if clean_lot_ids:
            iqf_qs = iqf_qs.filter(lot_id__in=clean_lot_ids)
        iqf_meta = (
            iqf_qs.exclude(tray_type__isnull=True).exclude(tray_type='')
            .exclude(tray_capacity__isnull=True).exclude(tray_capacity=0)
            .values('tray_type', 'tray_capacity')
            .first()
        )
        if iqf_meta:
            return iqf_meta.get('tray_type') or fallback_type, iqf_meta.get('tray_capacity') or fallback_capacity

        master_meta = (
            TrayId.objects.filter(tray_id__in=clean_tray_ids)
            .exclude(tray_type__isnull=True).exclude(tray_type='')
            .exclude(tray_capacity__isnull=True).exclude(tray_capacity=0)
            .values('tray_type', 'tray_capacity')
            .first()
        )
        if master_meta:
            return master_meta.get('tray_type') or fallback_type, master_meta.get('tray_capacity') or fallback_capacity

        brass_meta = (
            BrassTrayId.objects.filter(tray_id__in=clean_tray_ids)
            .exclude(tray_type__isnull=True).exclude(tray_type='')
            .exclude(tray_capacity__isnull=True).exclude(tray_capacity=0)
            .values('tray_type', 'tray_capacity')
            .first()
        )
        if brass_meta:
            return brass_meta.get('tray_type') or fallback_type, brass_meta.get('tray_capacity') or fallback_capacity

    return fallback_type, fallback_capacity


def _release_iqf_delinked_physical_trays(tray_ids):
    """Release explicitly delinked physical trays while preserving IQF history."""
    clean_tray_ids = [str(tid or '').strip().upper() for tid in tray_ids if str(tid or '').strip()]
    if not clean_tray_ids:
        return 0

    freed = TrayId.objects.filter(tray_id__in=clean_tray_ids).update(
        delink_tray=True,
        lot_id=None,
        batch_id=None,
        scanned=False,
        IP_tray_verified=False,
        rejected_tray=False,
        brass_rejected_tray=False,
        top_tray=False,
        new_tray=True,
    )

    BrassTrayId.objects.filter(tray_id__in=clean_tray_ids).update(delink_tray=True)
    BrassAuditTrayId.objects.filter(tray_id__in=clean_tray_ids).update(delink_tray=True)
    IPTrayId.objects.filter(tray_id__in=clean_tray_ids).update(delink_tray=True)

    return freed


def _build_iqf_rejection_details(raw_items, fallback_lot_id=None, fallback_total=None):
    """Build stored IQF rejection detail rows from submitted/draft reason quantities."""
    source_items = raw_items or []
    if not source_items and fallback_lot_id:
        draft = IQF_Draft_Store.objects.filter(
            lot_id=fallback_lot_id,
            draft_type='batch_rejection',
        ).order_by('-updated_at').first()
        if draft and isinstance(draft.draft_data, dict):
            source_items = draft.draft_data.get('items') or []

    details = []
    reason_ids = []
    for item in source_items:
        reason_id = _safe_int(item.get('reason_id'), None)
        qty = _safe_int(item.get('iqf_qty'), 0)
        if not reason_id or qty <= 0:
            continue
        reason_obj = IQF_Rejection_Table.objects.filter(id=reason_id).first()
        details.append({
            'reason_id': reason_id,
            'reason_text': reason_obj.rejection_reason if reason_obj else '',
            'iqf_qty': qty,
        })
        reason_ids.append(reason_id)

    if not details and fallback_lot_id and fallback_total:
        store = IQF_Rejection_ReasonStore.objects.filter(lot_id=fallback_lot_id).order_by('-id').first()
        if store:
            for reason_obj in store.rejection_reason.all():
                details.append({
                    'reason_id': reason_obj.id,
                    'reason_text': str(reason_obj.rejection_reason),
                    'iqf_qty': fallback_total,
                })
                reason_ids.append(reason_obj.id)

    return details, reason_ids


def build_ui_state(data):
    """Compute ALL UI state for a single IQF pick table row.

    Frontend becomes pure render layer — zero business logic in templates.
    Backend is SINGLE SOURCE OF TRUTH for button states, labels, colors, permissions.

    Returns a dict that the template accesses via {{ data.ui.* }}
    """
    hold_lot = bool(data.get('iqf_hold_lot'))
    verified = bool(data.get('iqf_accepted_qty_verified'))
    acceptance = bool(data.get('iqf_acceptance'))
    rejection = bool(data.get('iqf_rejection'))
    few_cases = bool(data.get('iqf_few_cases_acceptance'))
    onhold = bool(data.get('iqf_onhold_picking'))
    draft = bool(data.get('Draft_Saved'))
    has_remarks = bool(data.get('IQF_pick_remarks'))
    holding_reason = data.get('iqf_holding_reason') or ''
    release_reason = data.get('iqf_release_reason') or ''
    release_lot = bool(data.get('iqf_release_lot'))
    last_module = data.get('last_process_module') or ''

    # ── Row CSS class ──
    row_blur = 'row-inactive-blur' if hold_lot else ''

    # ── Action state machine — ONE decision, backend-only ──
    if acceptance:
        action_type = 'ACCEPTED'
    elif onhold:
        action_type = 'VERIFY'
    elif rejection or few_cases:
        action_type = 'REJECTED'
    elif verified:
        action_type = 'AUDIT_ENABLED'
    else:
        action_type = 'AUDIT_DISABLED'

    # ── Lot status pill — pre-computed label + colors ──
    if hold_lot:
        status_pill = {'label': 'On Hold', 'border': '#dc3545', 'bg': '#f8d7da', 'text': '#721c24'}
    elif draft:
        status_pill = {'label': 'Draft', 'border': '#4997ac', 'bg': '#d1f2f3', 'text': '#03425d'}
    elif onhold:
        status_pill = {'label': 'Draft', 'border': '#4997ac', 'bg': '#e3f2fd', 'text': '#0d47a1'}
    elif rejection or few_cases or acceptance:
        status_pill = {'label': 'Yet to Release', 'border': '#0d5d17', 'bg': '#c5f9c2', 'text': '#2f801b'}
    else:
        status_pill = {'label': 'Yet to Start', 'border': '#f9a825', 'bg': '#fff8e1', 'text': '#b26a00'}

    # ── Process status circles ──
    q_color = '#0c8249' if verified else '#bdbdbd'
    if rejection or acceptance or few_cases:
        qc_style = 'background-color: #0c8249'
    elif onhold:
        qc_style = 'background: linear-gradient(to right, #0c8249 50%, #1565c0 50%)'
    elif draft:
        qc_style = 'background: linear-gradient(to right, #0c8249 50%, #bdbdbd 50%)'
    else:
        qc_style = 'background-color: #bdbdbd'

    # ── Hold/release info ──
    show_hold_info = bool(hold_lot or release_lot or holding_reason or release_reason)
    tooltip_parts = []
    if holding_reason:
        tooltip_parts.append(f'Holding Reason: {holding_reason}')
    if release_reason:
        tooltip_parts.append(f'Release Reason: {release_reason}')
    hold_tooltip = '\n'.join(tooltip_parts)

    # ── Permissions ──
    can_delete = verified
    allow_remarks = not (acceptance or rejection or few_cases)

    # ── Current stage colors and label ──
    # When lot qty is NOT checked (verified=False): show source module (where lot came from)
    # When lot qty IS checked (verified=True): show IQF (current processing stage)
    stage_map = {
        'Input screening': {'border': '#0d5d17', 'bg': '#c5f9c2', 'text': '#2f801b'},
        'IQF': {'border': '#f9a825', 'bg': '#fff8e1', 'text': '#b26a00'},
        'DayPlanning': {'border': '#1976d2', 'bg': '#d1eaff', 'text': '#033b5d'},
        'Brass QC': {'border': '#9adeed', 'bg': '#d1edf3', 'text': '#033b5d'},
        'Brass Audit': {'border': '#9adeed', 'bg': '#d1edf3', 'text': '#033b5d'},
    }
    if verified:
        stage = stage_map['IQF']
        stage_label = 'IQF'
    else:
        stage = stage_map.get(last_module, {'border': '#9adeed', 'bg': '#d1edf3', 'text': '#033b5d'})
        stage_label = last_module or 'N/A'

    ui = {
        'row_blur': row_blur,
        'hold_lot': hold_lot,
        'action_type': action_type,
        'status_pill': status_pill,
        'q_color': q_color,
        'qc_style': qc_style,
        'show_hold_info': show_hold_info,
        'hold_tooltip': hold_tooltip,
        'can_delete': can_delete,
        'allow_remarks': allow_remarks,
        'qty_verified': verified,
        'remarks_saved': has_remarks,
        'stage': stage,
        'stage_label': stage_label,
    }
    print(f"[UI_STATE] lot={data.get('stock_lot_id')}, action={action_type}, status={status_pill['label']}")
    return ui

# IQF - Pick Table   
@method_decorator(login_required, name='dispatch')    
class IQFPickTableView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'IQF/Iqf_PickTable.html'

    def get(self, request):
        user = request.user
        is_admin = user.groups.filter(name='Admin').exists() if user.is_authenticated else False

        lot_id = request.GET.get('lot_id')
        iqf_rejection_reasons = IQF_Rejection_Table.objects.all()

        # ── Dynamic Re-entry: Brass Audit Rejection → IQF Reprocessing ──
        # Detect lots rejected from Brass Audit (batch or partial) that were
        # previously processed by IQF. Use IQF_Submitted existence as indicator
        # since IQF flags get reset when Brass QC accepts the lot onward.
        iqf_submitted_lot_ids = set(
            IQF_Submitted.objects.values_list('lot_id', flat=True)
        )

        if iqf_submitted_lot_ids:
            # ── GUARD: build set of lots that completed a PARTIAL IQF split.
            # These parent lots are permanently consumed and must NEVER be re-entered.
            # Criteria (any one sufficient):
            #   • IQF_Submitted has submission_type=PARTIAL + is_completed=True
            #   • TotalStockModel.iqf_few_cases_acceptance=True (set by PARTIAL submit)
            #   • TotalStockModel.is_split=True (parent consumed into children)
            #   • TotalStockModel.remove_lot=True  (parent marked for removal)
            completed_partial_ids = set(
                IQF_Submitted.objects.filter(
                    submission_type=IQF_Submitted.SUB_PARTIAL,
                    is_completed=True,
                ).values_list('lot_id', flat=True)
            )
            split_lot_ids = set(
                TotalStockModel.objects.filter(
                    Q(is_split=True) | Q(remove_lot=True) | Q(iqf_few_cases_acceptance=True)
                ).values_list('lot_id', flat=True)
            )
            consumed_lot_ids = completed_partial_ids | split_lot_ids
            logger.info("[IQF RE-ENTRY GUARD] Consumed/split lot_ids excluded: %s", consumed_lot_ids)

            # Case 1: Batch rejection from Brass Audit
            batch_reentry = TotalStockModel.objects.filter(
                brass_audit_rejection=True,
                send_brass_audit_to_iqf=False,
                lot_id__in=iqf_submitted_lot_ids,
            ).exclude(
                lot_id__in=consumed_lot_ids
            )
            batch_ids = list(batch_reentry.values_list('lot_id', flat=True))
            if batch_ids:
                # IMPORTANT:
                # This is a NEW IQF processing cycle. Do not carry the previous
                # IQF Accept/Reject quantities into the new IQF stage.
                # The Brass Audit rejection quantity is only the INPUT to IQF;
                # it is NOT an IQF rejection until IQF processing is completed.
                # Also do not touch total_stock / batch quantity here.
                batch_reentry.update(
                    send_brass_audit_to_iqf=True,
                    iqf_acceptance=False,
                    iqf_rejection=False,
                    iqf_few_cases_acceptance=False,
                    iqf_accepted_qty_verified=False,
                    iqf_onhold_picking=False,
                    iqf_accepted_qty=0,
                    iqf_rejection_qty=0,
                    iqf_after_rejection_qty=0,
                    brass_audit_rejection=False,
                    send_brass_audit_to_qc=False,
                )
                # Clean stale IQF data so lot gets fresh processing
                for lid in batch_ids:
                    IQF_Submitted.objects.filter(lot_id=lid).delete()
                    IQF_Draft_Store.objects.filter(lot_id=lid).delete()
                    IQF_Accepted_TrayID_Store.objects.filter(lot_id=lid).delete()
                    IQF_Accepted_TrayScan.objects.filter(lot_id=lid).delete()
                    IQF_Rejected_TrayScan.objects.filter(lot_id=lid).delete()
                    IQF_Rejection_ReasonStore.objects.filter(lot_id=lid).delete()
                    IQFTrayId.objects.filter(lot_id=lid).delete()
                    IQF_OptimalDistribution_Draft.objects.filter(lot_id=lid).delete()
                logger.info("[IQF RE-ENTRY BATCH] Reset %s lot(s) for IQF reprocessing: %s", len(batch_ids), batch_ids)

            # Case 2: Partial rejection from Brass Audit (lot accepted with rejections)
            partial_rej_lot_ids = set(
                Brass_Audit_Rejection_ReasonStore.objects.filter(
                    batch_rejection=False
                ).values_list('lot_id', flat=True)
            ) & iqf_submitted_lot_ids

            if partial_rej_lot_ids:
                partial_reentry = TotalStockModel.objects.filter(
                    brass_audit_few_cases_accptance=True,
                    brass_audit_accepted_tray_scan_status=True,
                    send_brass_audit_to_iqf=False,
                    lot_id__in=partial_rej_lot_ids,
                ).exclude(
                    lot_id__in=consumed_lot_ids
                )
                partial_ids = list(partial_reentry.values_list('lot_id', flat=True))
                if partial_ids:
                    # IMPORTANT:
                    # Brass Audit PARTIAL rejection only moves the rejected
                    # quantity INTO IQF. It must not become an IQF Accept or
                    # IQF Reject result automatically.
                    #
                    # Start the new IQF cycle with both result quantities = 0.
                    # Do NOT modify total_stock / batch quantity.
                    partial_reentry.update(
                        send_brass_audit_to_iqf=True,
                        iqf_acceptance=False,
                        iqf_rejection=False,
                        iqf_few_cases_acceptance=False,
                        iqf_accepted_qty_verified=False,
                        iqf_onhold_picking=False,
                        iqf_accepted_qty=0,
                        iqf_rejection_qty=0,
                        iqf_after_rejection_qty=0,
                    )
                    
                    # Clean stale IQF data so lot gets fresh processing
                    for lid in partial_ids:
                        IQF_Submitted.objects.filter(lot_id=lid).delete()
                        IQF_Draft_Store.objects.filter(lot_id=lid).delete()
                        IQF_Accepted_TrayID_Store.objects.filter(lot_id=lid).delete()
                        IQF_Accepted_TrayScan.objects.filter(lot_id=lid).delete()
                        IQF_Rejected_TrayScan.objects.filter(lot_id=lid).delete()
                        IQF_Rejection_ReasonStore.objects.filter(lot_id=lid).delete()
                        IQFTrayId.objects.filter(lot_id=lid).delete()
                        IQF_OptimalDistribution_Draft.objects.filter(lot_id=lid).delete()
                    logger.info("[IQF RE-ENTRY PARTIAL] Reset %s lot(s) for IQF reprocessing: %s", len(partial_ids), partial_ids)

        # ✅ CHANGED: Query TotalStockModel directly instead of ModelMasterCreation
        brass_rejection_qty_subquery = Brass_QC_Rejection_ReasonStore.objects.filter(
            lot_id=OuterRef('lot_id')
        ).values('total_rejection_quantity')[:1]

        brass_audit_rejection_qty_subquery = Brass_Audit_Rejection_ReasonStore.objects.filter(
            lot_id=OuterRef('lot_id')
        ).values('total_rejection_quantity')[:1]

        iqf_rejection_qty_subquery = IQF_Rejection_ReasonStore.objects.filter(
            lot_id=OuterRef('lot_id')
        ).values('total_rejection_quantity')[:1]

        queryset = TotalStockModel.objects.select_related(
            'batch_id',
            'batch_id__model_stock_no',
            'batch_id__version',
            'batch_id__location'
        ).filter(
            batch_id__total_batch_quantity__gt=0
        ).annotate(
            wiping_required=F('batch_id__model_stock_no__wiping_required'),
            brass_rejection_total_qty=brass_rejection_qty_subquery,
            brass_audit_rejection_qty=brass_audit_rejection_qty_subquery,
            iqf_rejection_qty=iqf_rejection_qty_subquery,
        ).filter(
            # ✅ Include: (1) Brass Audit rejections sent to IQF, (2) Brass QC full rejections
            Q(send_brass_audit_to_iqf=True) | Q(brass_qc_rejection=True, last_process_module='Brass QC')
        ).exclude(
            Q(brass_audit_accptance=True, send_brass_audit_to_iqf=False) |
            Q(iqf_acceptance=True) |
            Q(iqf_rejection=True) |
            Q(send_brass_audit_to_iqf=True, brass_audit_onhold_picking=True)|
            Q(iqf_few_cases_acceptance=True, iqf_onhold_picking=False)
        ).exclude(
            # ✅ EXCLUDE parent lots that have been split into child lots
            # Parent lot still marked with brass_qc_rejection=True, but its children now represent the actual work
            brass_qc_transition_reject_lot_id__isnull=False
        ).exclude(
            # ✅ EXCLUDE IQF PARTIAL-split consumed parents: is_split=True or remove_lot=True
            # These lots were permanently consumed when PARTIAL submit created child lots.
            Q(is_split=True) | Q(remove_lot=True)
        ).order_by('-bq_last_process_date_time', '-lot_id')

        logger.info("[IQF PICK] Found %s IQF pick records", queryset.count())
        logger.info("[IQF PICK] All lot_ids in IQF pick queryset: %s", list(queryset.values_list('lot_id', flat=True)))

        # Pagination
        page_number = request.GET.get('page', 1)
        paginator = Paginator(queryset, 10)
        page_obj = paginator.get_page(page_number)

        # ✅ UPDATED: Build master_data from TotalStockModel records
        master_data = []
        for stock_obj in page_obj.object_list:
            batch = stock_obj.batch_id
            
            # ✅ CHECK FOR IQF-SPECIFIC DRAFTS ONLY WHERE is_draft = True
            iqf_has_drafts = (
                IQF_Draft_Store.objects.filter(lot_id=stock_obj.lot_id, draft_data__is_draft=True).exists() or
                IQF_Accepted_TrayID_Store.objects.filter(lot_id=stock_obj.lot_id, is_draft=True).exists()
            )
            
            data = {
                # ✅ Batch fields from foreign key
                'batch_id': batch.batch_id,
                'bq_last_process_date_time': stock_obj.bq_last_process_date_time,
                'model_stock_no__model_no': batch.model_stock_no.model_no,
                'plating_color': batch.plating_color,
                'polish_finish': batch.polish_finish,
                'version__version_name': batch.version.version_name if batch.version else '',
                'vendor_internal': batch.vendor_internal,
                'location__location_name': batch.location.location_name if batch.location else '',
                'tray_type': '',  # ✅ START EMPTY — only populate from actual trays below
                'tray_capacity': batch.tray_capacity,
                'Moved_to_D_Picker': batch.Moved_to_D_Picker,
                'Draft_Saved': iqf_has_drafts,  # ✅ USE IQF-SPECIFIC DRAFTS INSTEAD OF GLOBAL Draft_Saved
                'plating_stk_no': batch.plating_stk_no,
                'polishing_stk_no': batch.polishing_stk_no,
                'category': batch.category,
                'type_of_input': get_type_of_input_for_batch(batch),

                # ✅ Stock-related fields from TotalStockModel
                'lot_id': stock_obj.lot_id,
                'stock_lot_id': stock_obj.lot_id,
                'last_process_module': stock_obj.last_process_module,
                'next_process_module': stock_obj.next_process_module,
                'wiping_required': stock_obj.wiping_required,
                'iqf_missing_qty': stock_obj.iqf_missing_qty,
                'iqf_physical_qty': stock_obj.iqf_physical_qty,
                'iqf_physical_qty_edited': stock_obj.iqf_physical_qty_edited,
                'accepted_tray_scan_status': stock_obj.accepted_tray_scan_status,
                # IMPORTANT: A lot entering IQF from Brass Audit starts a NEW IQF
                # processing cycle. Any old IQF Accept/Reject quantity must NOT
                # appear in the Pick Table until IQF actually verifies/processes it.
                # The Brass Audit rejected quantity is only the incoming quantity
                # for IQF; it is not an IQF Accept or IQF Reject result.
                # Once iqf_accepted_qty_verified becomes True, show the real IQF
                # result quantities.
                'iqf_rejection_qty': (
                    0 if (stock_obj.send_brass_audit_to_iqf and not stock_obj.iqf_accepted_qty_verified)
                    else (stock_obj.iqf_rejection_qty or 0)
                ),
                'iqf_accepted_qty': (
                    0 if (stock_obj.send_brass_audit_to_iqf and not stock_obj.iqf_accepted_qty_verified)
                    else (stock_obj.iqf_accepted_qty or 0)
                ),
                'IQF_pick_remarks': stock_obj.IQF_pick_remarks,
                'Bq_pick_remarks': stock_obj.Bq_pick_remarks,
                'BA_pick_remarks': stock_obj.BA_pick_remarks,
                'IP_pick_remarks': stock_obj.IP_pick_remarks,
                'brass_rejection_total_qty': stock_obj.brass_rejection_total_qty,
                'brass_audit_rejection_qty': stock_obj.brass_audit_rejection_qty,
                'brass_qc_few_cases_accptance': stock_obj.brass_qc_few_cases_accptance,
                'iqf_accepted_qty_verified': stock_obj.iqf_accepted_qty_verified,
                'iqf_acceptance': stock_obj.iqf_acceptance,
                'iqf_rejection': stock_obj.iqf_rejection,
                'brass_audit_few_cases_accptance': stock_obj.brass_audit_few_cases_accptance,
                'iqf_few_cases_acceptance': stock_obj.iqf_few_cases_acceptance,
                'iqf_onhold_picking': stock_obj.iqf_onhold_picking,
                'brass_onhold_picking': stock_obj.brass_onhold_picking,
                'iqf_hold_lot': stock_obj.iqf_hold_lot,
                'iqf_holding_reason': stock_obj.iqf_holding_reason,
                'iqf_release_lot': stock_obj.iqf_release_lot,
                'iqf_release_reason': stock_obj.iqf_release_reason,
                'brass_audit_onhold_picking': stock_obj.brass_audit_onhold_picking,
                'send_brass_audit_to_iqf': stock_obj.send_brass_audit_to_iqf,  # ✅ Direct access
                'total_IP_accpeted_quantity': stock_obj.total_IP_accpeted_quantity,
            }
            # Attach tray details from IQFTrayId as backend single source of truth
            try:
                # Use lot + batch as single-source filter (no cross-app calls)
                trays_qs = IQFTrayId.objects.filter(lot_id=stock_obj.lot_id, batch_id=batch)
                tray_list = []
                for t in trays_qs:
                    tray_list.append({
                        'id': t.tray_id,
                        'qty': t.tray_quantity
                    })
                data['tray_details'] = tray_list
                try:
                    data['tray_details_json'] = json.dumps(tray_list)
                except Exception:
                    data['tray_details_json'] = '[]'
            except Exception:
                data['tray_details'] = []
                data['tray_details_json'] = '[]'

            master_data.append(data)

        print(f"[IQFPickTableView] Total master_data records: {len(master_data)}")
        
        # ✅ Process the data (same logic as before)
        for data in master_data:
            print(data['batch_id'], data['brass_rejection_total_qty'])

        for data in master_data:
            brass_rejection_total_qty = data.get('brass_rejection_total_qty') or 0
            tray_capacity = data.get('tray_capacity') or 0
            brass_audit_rejection_qty = data.get('brass_audit_rejection_qty') or 0

            data['vendor_location'] = f"{data.get('vendor_internal', '')}_{data.get('location__location_name', '')}"

            # ── Dynamically resolve tray_type and tray_capacity from actual lot trays ──
            # BrassTrayId → IQFTrayId — NO batch fallback
            # ✅ RULE: Only show tray_type if actual trays exist in system (from Jig Unloading onwards)
            _lot_id_for_tray = data.get('stock_lot_id')
            _dyn_tray = (
                BrassTrayId.objects.filter(lot_id=_lot_id_for_tray, delink_tray=False)
                .exclude(tray_type__isnull=True).exclude(tray_type='')
                .exclude(tray_capacity__isnull=True).values('tray_type', 'tray_capacity').first()
            )
            if not _dyn_tray:
                _dyn_tray = (
                    IQFTrayId.objects.filter(lot_id=_lot_id_for_tray, delink_tray=False)
                    .exclude(tray_type__isnull=True).exclude(tray_type='')
                    .exclude(tray_capacity__isnull=True).values('tray_type', 'tray_capacity').first()
                )
            if _dyn_tray:
                # Only set tray_type if found in actual tray records — not from batch
                data['tray_type'] = _dyn_tray['tray_type']
                data['tray_capacity'] = _dyn_tray['tray_capacity'] or data.get('tray_capacity', 0)
                tray_capacity = data['tray_capacity'] or tray_capacity
            # else: keep tray_type as empty (not from batch fallback)

            # ── Count/display actual trays for the Pick Table ──
            # First use the strict CURRENT-lot selector.
            # If that returns no trays, resolve ONLY an explicit Brass QC transition
            # source lot (child -> parent/source relation) and use that source lot's
            # current tray evidence for Pick Table display. This is intentionally
            # scoped here and does NOT change selectors.py's no-parent-fallback rule.
            from .services.selectors import (
                get_current_trays,
            )
            _tray_data, _source, _total_qty = get_current_trays(_lot_id_for_tray)

            # Brass QC -> IQF can preserve tray evidence in two places:
            #   1) Brass_QC_Rejected_TrayScan
            #   2) Brass_QC_Submission reject snapshot (what Brass QC Completed shows)
            # The Pick Table must display the same incoming rejected trays.
            # Resolve only the current lot and an explicitly mapped Brass QC source lot.
            if not _tray_data and data.get('last_process_module') == 'Brass QC':
                _mapped_source_lot = None
                try:
                    _parent_stock = TotalStockModel.objects.filter(
                        Q(brass_qc_transition_accept_lot_id=_lot_id_for_tray) |
                        Q(brass_qc_transition_reject_lot_id=_lot_id_for_tray) |
                        Q(brass_qc_transition_lot_id=_lot_id_for_tray)
                    ).first()
                    if _parent_stock and getattr(_parent_stock, 'lot_id', None):
                        _mapped_source_lot = _parent_stock.lot_id
                except Exception:
                    _mapped_source_lot = None

                _candidate_lots = [_lot_id_for_tray]
                if _mapped_source_lot and _mapped_source_lot != _lot_id_for_tray:
                    _candidate_lots.append(_mapped_source_lot)

                for _candidate_lot in _candidate_lots:
                    # Prefer physical rejection scan rows.
                    _src_trays, _src_source, _src_total = _get_brass_qc_rejected_trays_exact(_candidate_lot)

                    # Brass QC Completed Table can still have a valid immutable reject
                    # snapshot even when scan rows were delinked/moved during transition.
                    if not _src_trays:
                        _src_trays, _src_source, _src_total = _get_brass_qc_submission_rejected_trays_exact(_candidate_lot)

                    if _src_trays:
                        _tray_data = _src_trays
                        _source = f"brass_qc_source:{_candidate_lot}:{_src_source}"
                        _total_qty = _src_total
                        logger.info(
                            "[IQF PICK TRAYS] lot=%s resolved %s Brass QC rejected tray(s) from lot=%s source=%s",
                            _lot_id_for_tray, len(_tray_data), _candidate_lot, _src_source
                        )
                        break

            data['no_of_trays'] = len(_tray_data)
            data['tray_source'] = _source
            _current_tray_list = [
                {
                    'id': t.get('tray_id', ''),
                    'qty': int(t.get('qty', 0) or 0),
                }
                for t in _tray_data
                if t.get('tray_id') and int(t.get('qty', 0) or 0) > 0
            ]
            data['tray_details'] = _current_tray_list
            try:
                data['tray_details_json'] = json.dumps(_current_tray_list)
            except Exception:
                data['tray_details_json'] = '[]'


            # Get model images
            batch_obj = ModelMasterCreation.objects.filter(batch_id=data['batch_id']).first()
            images = []
            if batch_obj:
                model_master = batch_obj.model_stock_no
                from modelmasterapp.image_utils import sort_images_front_first
                for img in sort_images_front_first(model_master.images.all()):
                    if img.master_image:
                        images.append(img.master_image.url)
            if not images:
                images = [static('assets/images/imagePlaceholder.jpg')]
            data['model_images'] = images

            # Add available_qty and RW qty for each row
            lot_id = data.get('stock_lot_id')
            total_stock_obj = TotalStockModel.objects.filter(lot_id=lot_id).first()
            if total_stock_obj:
                # Do NOT persist any healed physical qty here. Instead expose the rejected
                # quantity as `rw_qty` and keep available_qty strictly from real physical qty.
                current_physical_qty = total_stock_obj.iqf_physical_qty or 0

                # Determine rejection total from appropriate reason store (do not save)
                use_audit = getattr(total_stock_obj, 'send_brass_audit_to_iqf', False)
                reason_store = None
                try:
                    # Prefer explicit reason stores to derive origin (audit vs qc)
                    # Prefer Brass Audit when present (some lots originate from audit)
                    if Brass_Audit_Rejection_ReasonStore.objects.filter(lot_id=lot_id).exists():
                        reason_store = Brass_Audit_Rejection_ReasonStore.objects.filter(lot_id=lot_id).order_by('-id').first()
                        inferred_origin = 'Audit'
                    elif Brass_QC_Rejection_ReasonStore.objects.filter(lot_id=lot_id).exists():
                        reason_store = Brass_QC_Rejection_ReasonStore.objects.filter(lot_id=lot_id).order_by('-id').first()
                        inferred_origin = 'QC'
                    else:
                        # Fallback to existing flag
                        inferred_origin = 'Audit' if use_audit else 'QC'
                        if use_audit:
                            reason_store = Brass_Audit_Rejection_ReasonStore.objects.filter(lot_id=lot_id).order_by('-id').first()
                        else:
                            reason_store = Brass_QC_Rejection_ReasonStore.objects.filter(lot_id=lot_id).order_by('-id').first()
                except Exception:
                    reason_store = None
                # expose inferred origin for template use (explicit override of send_brass_audit_to_iqf)
                data['brass_origin'] = inferred_origin

                rw_qty = (reason_store.total_rejection_quantity if reason_store and getattr(reason_store, 'total_rejection_quantity', 0) else 0)

                # ── Fallback: FULL_REJECT lots without reason store → use IP accepted qty ──
                if rw_qty <= 0:
                    rw_qty = total_stock_obj.total_IP_accpeted_quantity or 0

                # available_qty should reflect actual physical qty (if any). If none, leave 0
                if current_physical_qty and current_physical_qty > 0:
                    data['available_qty'] = current_physical_qty
                else:
                    data['available_qty'] = 0

                # expose RW qty separately
                data['rw_qty'] = rw_qty
            else:
                data['available_qty'] = 0
                data['rw_qty'] = 0

            # Add display_physical_qty for frontend (STRICT: only from iqf_physical_qty)
            iqf_physical_qty = data.get('iqf_physical_qty', 0)
            data['display_physical_qty'] = iqf_physical_qty if (iqf_physical_qty and iqf_physical_qty > 0) else 0

            # ── Re-flagged lot fix: override rw_qty and no_of_trays from IQF_Submitted ──
            # For lots previously processed by IQF (FULL_ACCEPT / PARTIAL) that return
            # via Brass QC rejection, the reason-store subquery picks stale values.
            # Use the same source of truth as the iqf_tray_details API endpoint.
            try:
                iqf_sub = IQF_Submitted.objects.filter(lot_id=lot_id, is_completed=True).last()
                if iqf_sub and iqf_sub.submission_type in ('FULL_ACCEPT', 'PARTIAL'):
                    if iqf_sub.submission_type == 'FULL_ACCEPT' and iqf_sub.full_accept_data:
                        src_trays = iqf_sub.full_accept_data.get('trays', [])
                    elif iqf_sub.submission_type == 'PARTIAL' and iqf_sub.partial_accept_data:
                        src_trays = iqf_sub.partial_accept_data.get('trays', [])
                    else:
                        src_trays = []
                    live_rw = sum(int(t.get('qty', 0)) for t in src_trays if int(t.get('qty', 0)) > 0)
                    live_trays = len([t for t in src_trays if int(t.get('qty', 0)) > 0])
                    if live_rw > 0:
                        data['rw_qty'] = live_rw
                        data['no_of_trays'] = live_trays
                        # Keep the green-check tray popup synchronized with the count.
                        # Only fill from the completed IQF snapshot when the current
                        # Brass QC resolution above did not already provide tray rows.
                        if not data.get('tray_details'):
                            _reflag_trays = [
                                {
                                    'id': str(t.get('tray_id', '') or '').strip(),
                                    'qty': int(t.get('qty', 0) or 0),
                                }
                                for t in src_trays
                                if str(t.get('tray_id', '') or '').strip() and int(t.get('qty', 0) or 0) > 0
                            ]
                            data['tray_details'] = _reflag_trays
                            data['tray_details_json'] = json.dumps(_reflag_trays)
                        print(f"[IQF PICK] Re-flagged lot {lot_id}: rw_qty={live_rw}, no_of_trays={live_trays} from IQF_Submitted")
            except Exception:
                pass  # Keep existing values on error

        print("Processed lot_ids:", [data['stock_lot_id'] for data in master_data])

        # ── ATTACH UI STATE — Backend drives ALL UI decisions ──
        for data in master_data:
            data['ui'] = build_ui_state(data)

        # Remove duplicate lot rows (keep first occurrence) — preserves existing master_data order
        seen = set()
        unique_master = []
        for d in master_data:
            lid = d.get('stock_lot_id') or d.get('lot_id')
            if lid not in seen:
                seen.add(lid)
                unique_master.append(d)
        master_data = unique_master

        context = {
            'master_data': master_data,
            'page_obj': page_obj,
            'paginator': paginator,
            'user': user,
            'is_admin': is_admin,
            'iqf_rejection_reasons': iqf_rejection_reasons,
        }
        return Response(context, template_name=self.template_name)

# Audit modal single-source API: RW Qty + rejection reason table
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def iqf_rejection_audit_iqf_reject(request):
    lot_id = request.GET.get('lot_id')
    if not lot_id:
        return Response({'success': False, 'error': 'Missing lot_id'}, status=400)

    try:
        print(f"[AUDIT API] Input lot_id: {lot_id}")

        # Map split child -> parent/source lot for Brass QC lookups
        mapped_brass_lot = lot_id
        try:
            parent_stock = TotalStockModel.objects.filter(
                Q(brass_qc_transition_accept_lot_id=lot_id) |
                Q(brass_qc_transition_reject_lot_id=lot_id) |
                Q(brass_qc_transition_lot_id=lot_id)
            ).first()
            if parent_stock and getattr(parent_stock, 'lot_id', None):
                mapped_brass_lot = parent_stock.lot_id
                print(f"[AUDIT API] Mapped child lot {lot_id} -> parent/source lot {mapped_brass_lot} for Brass QC lookups")
        except Exception:
            mapped_brass_lot = lot_id

        # 1. Get RW Qty — SAME LOGIC AS iqf_lot_details ──
        # Priority order: Brass_Audit_Rejection_ReasonStore → Brass_QC_Rejection_ReasonStore → IQF_Submitted override
        audit_store = Brass_Audit_Rejection_ReasonStore.objects.filter(lot_id=mapped_brass_lot).order_by('-id').first()
        qc_store = Brass_QC_Rejection_ReasonStore.objects.filter(lot_id=mapped_brass_lot).order_by('-id').first()
        rw_qty = 0

        if audit_store and getattr(audit_store, 'total_rejection_quantity', None) is not None:
            rw_qty = audit_store.total_rejection_quantity
        elif qc_store and getattr(qc_store, 'total_rejection_quantity', None) is not None:
            rw_qty = qc_store.total_rejection_quantity

        # If still 0, use get_current_trays to get actual incoming tray data for CURRENT lot
        # This handles FULL_REJECT lots that may not have reason stores
        if rw_qty == 0:
            from .services.selectors import get_current_trays
            _tray_data, _source, _total_qty = get_current_trays(lot_id)
            rw_qty = _total_qty
            if rw_qty > 0:
                print(f"[AUDIT API] Fallback: resolved rw_qty={rw_qty} from {len(_tray_data)} trays (source={_source})")

        # ── Re-flagged lot: override rw_qty from IQF_Submitted (SINGLE SOURCE OF TRUTH) ──
        # When a lot travels Brass QC → IQF multiple times, reason stores hold STALE data.
        try:
            _iqf_sub = IQF_Submitted.objects.filter(lot_id=lot_id, is_completed=True).last()
            if _iqf_sub:
                _override_rw = None
                if _iqf_sub.submission_type in ('FULL_ACCEPT', 'PARTIAL'):
                    if _iqf_sub.submission_type == 'FULL_ACCEPT' and _iqf_sub.full_accept_data:
                        _src = _iqf_sub.full_accept_data.get('trays', [])
                    elif _iqf_sub.submission_type == 'PARTIAL' and _iqf_sub.partial_accept_data:
                        _src = _iqf_sub.partial_accept_data.get('trays', [])
                    else:
                        _src = []
                    _live = sum(int(t.get('qty', 0)) for t in _src if int(t.get('qty', 0)) > 0)
                    if _live > 0:
                        _override_rw = _live
                elif _iqf_sub.submission_type in ('FULL_REJECT', 'LOT_REJECTION'):
                    _override_rw = _iqf_sub.iqf_incoming_qty
                if _override_rw is not None and _override_rw > 0:
                    print(f"[AUDIT API] Re-flagged lot {lot_id}: overriding rw_qty from {rw_qty} to {_override_rw} (source: IQF_Submitted {_iqf_sub.submission_type})")
                    rw_qty = _override_rw
        except Exception as _e:
            print(f"[AUDIT API] Re-flagged lot override check failed: {_e}")

        print(f"[AUDIT API] Resolved rw_qty={rw_qty}")

        # Check if this lot already has an active LOT_REJECTION submission
        _is_lot_rejection = IQF_Submitted.objects.filter(
            lot_id=lot_id, submission_type=IQF_Submitted.SUB_LOT_REJECT
        ).exists()

        # 2. UNIFIED source aggregation — merge Brass QC + Brass Audit + IQF rejected tray scans
        response_data = []
        try:
            brass_qc_rows = list(Brass_QC_Rejected_TrayScan.objects.filter(lot_id=mapped_brass_lot).select_related('rejection_reason'))
        except Exception:
            brass_qc_rows = []
        try:
            brass_audit_rows = list(Brass_Audit_Rejected_TrayScan.objects.filter(lot_id=mapped_brass_lot).select_related('rejection_reason'))
        except Exception:
            brass_audit_rows = []
        try:
            iqf_rows = list(IQF_Rejected_TrayScan.objects.filter(lot_id=lot_id).select_related('rejection_reason'))
        except Exception:
            iqf_rows = []

        all_rows = brass_qc_rows + brass_audit_rows + iqf_rows

        print(f"[AUDIT API] Total rows fetched: {len(all_rows)} (brass_qc={len(brass_qc_rows)}, brass_audit={len(brass_audit_rows)}, iqf={len(iqf_rows)})")

        # Aggregate quantities by reason text + id (preserve insertion order)
        reason_map = OrderedDict()
        for row in all_rows:
            try:
                reason_text = (row.rejection_reason.rejection_reason or '').strip()
            except Exception:
                reason_text = str(getattr(row, 'rejection_reason', ''))
            try:
                qty = int(row.rejected_tray_quantity or 0)
            except Exception:
                try:
                    qty = int(float(row.rejected_tray_quantity or 0))
                except Exception:
                    qty = 0
            r_id = getattr(row.rejection_reason, 'id', None) if hasattr(row, 'rejection_reason') else None
            if reason_text in reason_map:
                reason_map[reason_text]['qty'] += qty
            else:
                reason_map[reason_text] = {'qty': qty, 'reason_id': r_id}

        # Build response using master IQF reasons, filling quantities from unified reason_map
        reasons = IQF_Rejection_Table.objects.all().order_by('rejection_reason_id')
        print(f"[AUDIT API] Master reasons count: {reasons.count()}")

        if not reasons.exists():
            # DYNAMIC FALLBACK: no master table entries — derive reasons from actual data
            print("[AUDIT API] No master reasons found → using dynamic reasons from scan data")
            dynamic_reason_map = OrderedDict()
            for row in all_rows:
                try:
                    reason_text = (row.rejection_reason.rejection_reason or '').strip()
                except Exception:
                    reason_text = str(getattr(row, 'rejection_reason', ''))
                try:
                    qty = int(row.rejected_tray_quantity or 0)
                except Exception:
                    try:
                        qty = int(float(row.rejected_tray_quantity or 0))
                    except Exception:
                        qty = 0
                if reason_text in dynamic_reason_map:
                    dynamic_reason_map[reason_text] += qty
                else:
                    dynamic_reason_map[reason_text] = qty

            for idx, (reason_text, qty) in enumerate(dynamic_reason_map.items(), start=1):
                response_data.append({
                    "s_no": idx,
                    "reason_id": None,
                    "reason": reason_text,
                    "brass_qc_qty": qty,
                    "iqf_qty": 0,
                    "is_editable": True,
                })
        else:
            for index, reason in enumerate(reasons, start=1):
                reason_text = (reason.rejection_reason or '').strip()
                info = reason_map.get(reason_text)
                brass_qty = 0
                if info:
                    brass_qty = info.get('qty', 0) or 0
                else:
                    # id-based fallback match
                    for v in reason_map.values():
                        if v.get('reason_id') and reason.id and v.get('reason_id') == reason.id:
                            brass_qty = v.get('qty', 0) or 0
                            break

                print(f"[AUDIT API] lot_id={lot_id}, reason={reason_text}, brass_qty={brass_qty}")
                response_data.append({
                    "s_no": index,
                    "reason_id": reason.id,
                    "reason": reason_text,
                    "brass_qc_qty": brass_qty,
                    "iqf_qty": 0,
                    "is_editable": True,
                })

        print(f"[AUDIT API] Output count: {len(response_data)}")

        # If a draft exists for this lot, overlay its values into response_data and return total
        # GUARD: skip stale drafts for re-flagged lots (lot returned from Brass QC after IQF completion)
        # ✅ Fetch current lot trays for "Use Existing" feature
        current_lot_trays = []
        try:
            from .services.selectors import get_current_trays
            _tray_data, _source, _total = get_current_trays(lot_id)

            if not _tray_data and mapped_brass_lot != lot_id:
                for _candidate_lot in (lot_id, mapped_brass_lot):
                    _src_trays, _src_source, _src_total = _get_brass_qc_rejected_trays_exact(_candidate_lot)
                    if not _src_trays:
                        _src_trays, _src_source, _src_total = _get_brass_qc_submission_rejected_trays_exact(_candidate_lot)
                    if _src_trays:
                        _tray_data = _src_trays
                        _source = f"brass_qc_source:{_candidate_lot}:{_src_source}"
                        _total = _src_total
                        break

            for t in _tray_data:
                tray_id = str(t.get('tray_id') or '').strip()
                qty = _safe_int(t.get('qty'))
                if not tray_id or qty <= 0:
                    continue
                current_lot_trays.append({
                    'tray_id': tray_id,
                    'qty': qty,
                    'is_top_tray': bool(t.get('is_top_tray', t.get('is_top', t.get('top_tray', False)))),
                    'tray_capacity': _safe_int(t.get('tray_capacity')),
                })
            print(f"[AUDIT API] Current lot trays: {len(current_lot_trays)} trays, source={_source}")
        except Exception as _e:
            print(f"[AUDIT API] Failed to fetch current lot trays: {_e}")

        _has_completed_sub = IQF_Submitted.objects.filter(lot_id=lot_id, is_completed=True).exists()
        try:
            draft = IQF_Draft_Store.objects.filter(lot_id=lot_id, draft_type='batch_rejection').order_by('-updated_at').first()
            if draft and draft.draft_data and draft.draft_data.get('is_draft') is True and not _has_completed_sub:
                d_items = draft.draft_data.get('items') or []
                # map reason_id -> qty
                d_map = { (int(it.get('reason_id')) if it.get('reason_id') is not None else None): int(it.get('iqf_qty') or 0) for it in d_items }
                total_from_draft = int(draft.draft_data.get('total_iqf') or 0)
                draft_accepted_trays = draft.draft_data.get('accepted_trays') or []
                draft_rejected_trays = draft.draft_data.get('rejected_trays') or []
                draft_remark = draft.draft_data.get('remark') or ''
                # overlay
                for row in response_data:
                    rid = row.get('reason_id')
                    if rid in d_map:
                        row['iqf_qty'] = d_map[rid]
                # expose draft total as initial IQF total
                return Response({
                    "success": True,
                    "rw_qty": rw_qty,
                    "rejection_data": response_data,
                    "total_iqf_qty": total_from_draft,
                    "is_lot_rejection": _is_lot_rejection,
                    "current_lot_trays": current_lot_trays,
                    "draft_accepted_trays": draft_accepted_trays,
                    "draft_rejected_trays": draft_rejected_trays,
                    "draft_remark": draft_remark,
                    "has_draft": True,
                })
        except Exception:
            pass

        return Response({
            "success": True,
            "rw_qty": rw_qty,
            "rejection_data": response_data,
            "total_iqf_qty": 0,
            "is_lot_rejection": _is_lot_rejection,
            "current_lot_trays": current_lot_trays,
            "draft_accepted_trays": [],
            "draft_rejected_trays": [],
            "draft_remark": "",
            "has_draft": False,
        })

    except Exception as e:
        print("[AUDIT API ERROR]", str(e))
        traceback.print_exc()
        return Response({'success': False, 'error': 'Server error'}, status=500)

# IQF - Proceed btn - Validation
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def iqf_submit_audit(request):
    """Accepts JSON payload to save draft or proceed with IQF rejection quantities.

    CORE RULE: IQF processes ONLY Brass QC rejection qty (rw_qty), NOT the full lot.
    iqf_incoming_qty = rw_qty (e.g. 55), NOT total_batch_quantity (e.g. 100).

    Expected JSON:
        {
            "lot_id": "LID...",
            "action": "draft" | "proceed",
            "items": [ {"reason_id": 1, "iqf_qty": 5}, ... ]
        }
    """
    data = request.data
    lot_id = data.get('lot_id')
    action = data.get('action')
    items = data.get('items') or []
    remark = (data.get('remark') or '').strip()
    # 'compute' = live total/validation recompute triggered on every qty keystroke.
    # It shares the exact validation path as 'draft' but performs NO database write
    # (no IQF_Draft_Store persistence) â only an explicit 'draft' (Save Draft button)
    # or 'proceed' (Proceed button) persists data.
    if not lot_id or not action or action not in ('draft', 'proceed', 'compute'):
        return Response({'success': False, 'error': 'Missing or invalid parameters'}, status=400)

    # Remark is mandatory when proceeding
    if action == 'proceed' and not remark:
        return Response({'success': False, 'error': 'Remark is mandatory to proceed', 'remark_required': True}, status=400)

    if action in ('draft', 'proceed'):
        blocked_tray_id, tray_error = _validate_iqf_assigned_tray_ids(
            lot_id,
            data.get('accepted_trays') or [],
            data.get('rejected_trays') or [],
        )
        if tray_error:
            return Response({
                'success': False,
                'error': tray_error,
                'tray_id': blocked_tray_id,
                'tray_ids': [blocked_tray_id],
                'message': tray_error,
            }, status=400)

    try:
        # ─── 1. SINGLE SOURCE OF TRUTH: rw_qty from Brass QC/Audit rejection ───
        audit_store = Brass_Audit_Rejection_ReasonStore.objects.filter(lot_id=lot_id).order_by('-id').first()
        qc_store = Brass_QC_Rejection_ReasonStore.objects.filter(lot_id=lot_id).order_by('-id').first()
        rw_qty = 0
        if audit_store and getattr(audit_store, 'total_rejection_quantity', None) is not None:
            rw_qty = audit_store.total_rejection_quantity
        elif qc_store and getattr(qc_store, 'total_rejection_quantity', None) is not None:
            rw_qty = qc_store.total_rejection_quantity

        iqf_incoming_qty = rw_qty  # THIS IS WHAT IQF PROCESSES — NEVER total_batch_quantity

        # ── Re-flagged lot: override rw_qty from IQF_Submitted (SINGLE SOURCE OF TRUTH) ──
        # When a lot travels Brass QC → IQF multiple times, reason stores hold STALE data.
        try:
            _iqf_sub = IQF_Submitted.objects.filter(lot_id=lot_id, is_completed=True).last()
            if _iqf_sub:
                _override_rw = None
                # SKIP override for unfinalized PARTIAL — lot is still mid-flow in IQF
                # Override only applies to re-flagged lots (completed IQF cycle, came back)
                if _iqf_sub.submission_type == 'PARTIAL' and not _iqf_sub.partial_reject_data:
                    print(f'[IQF SUBMIT] Unfinalized PARTIAL for {lot_id} — skipping rw_qty override (using original {iqf_incoming_qty})')
                elif _iqf_sub.submission_type in ('FULL_ACCEPT', 'PARTIAL'):
                    if _iqf_sub.submission_type == 'FULL_ACCEPT' and _iqf_sub.full_accept_data:
                        _src = _iqf_sub.full_accept_data.get('trays', [])
                    elif _iqf_sub.submission_type == 'PARTIAL' and _iqf_sub.partial_accept_data:
                        _src = _iqf_sub.partial_accept_data.get('trays', [])
                    else:
                        _src = []
                    _live = sum(int(t.get('qty', 0)) for t in _src if int(t.get('qty', 0)) > 0)
                    if _live > 0:
                        _override_rw = _live
                elif _iqf_sub.submission_type in ('FULL_REJECT', 'LOT_REJECTION'):
                    _override_rw = _iqf_sub.iqf_incoming_qty
                if _override_rw is not None and _override_rw > 0:
                    print(f'[IQF SUBMIT] Re-flagged lot {lot_id}: overriding iqf_incoming_qty from {iqf_incoming_qty} to {_override_rw} (source: IQF_Submitted {_iqf_sub.submission_type})')
                    iqf_incoming_qty = _override_rw
                    rw_qty = _override_rw
        except Exception as _e:
            print(f'[IQF SUBMIT] Re-flagged lot override check failed: {_e}')

        # Get TotalStockModel — MUST exist, hard fail otherwise
        try:
            ts = TotalStockModel.objects.get(lot_id=lot_id)
        except TotalStockModel.DoesNotExist:
            return Response({'success': False, 'error': f'Lot {lot_id} not found in TotalStockModel'}, status=404)

        # ELIGIBILITY GUARD — lot must be pending IQF processing (matches pick table filter)
        # Allow: (1) send_brass_audit_to_iqf=True (normal), (2) brass_qc_rejection=True from Brass QC (full rejects)
        is_iqf_eligible = (
            ts.send_brass_audit_to_iqf or
            (getattr(ts, 'brass_qc_rejection', False) and getattr(ts, 'last_process_module', '') == 'Brass QC')
        )
        if not is_iqf_eligible:
            return Response({'success': False, 'error': f'Lot {lot_id} is not eligible for IQF'}, status=400)

        original_lot_qty = 0
        batch_id_val = ''
        if getattr(ts, 'batch_id', None):
            original_lot_qty = int(getattr(ts.batch_id, 'total_batch_quantity', 0) or 0)
            batch_id_val = ts.batch_id.batch_id

        # ── Detect Brass QC full lot rejection ──
        is_full_lot_reject = False
        if audit_store and getattr(audit_store, 'batch_rejection', False):
            is_full_lot_reject = True
        elif qc_store and getattr(qc_store, 'batch_rejection', False):
            is_full_lot_reject = True
        # Also detect via TotalStockModel flags (accepted_qty=0 + rejection=True)
        if not is_full_lot_reject:
            if getattr(ts, 'brass_qc_rejection', False) and int(getattr(ts, 'brass_qc_accepted_qty', 0) or 0) == 0:
                is_full_lot_reject = True

        # Full lot reject fallback: use lot qty when rw_qty is 0
        if is_full_lot_reject and iqf_incoming_qty <= 0:
            iqf_incoming_qty = original_lot_qty
            print(f'[IQF FULL LOT REJECT] Fallback: iqf_incoming_qty set to lot_qty={original_lot_qty}')

        print(f'[IQF VALIDATION START] lot_id={lot_id}, lot_qty={original_lot_qty}, is_full_lot_reject={is_full_lot_reject}, iqf_incoming_qty={iqf_incoming_qty}')

        if iqf_incoming_qty <= 0:
            return Response({'success': False, 'error': 'No IQF incoming qty — rw_qty is 0. Nothing to process.'}, status=400)

        # ─── 2. PARSE & VALIDATE ITEMS ───
        total_iqf = 0
        parsed_items = []
        for it in items:
            try:
                rid = int(it.get('reason_id'))
            except Exception:
                rid = None
            try:
                qty = int(it.get('iqf_qty') or 0)
            except Exception:
                return Response({'success': False, 'error': 'Invalid IQF quantity provided; must be integer'}, status=400)
            if qty < 0:
                return Response({'success': False, 'error': 'IQF quantities must be non-negative'}, status=400)
            total_iqf += qty
            parsed_items.append({'reason_id': rid, 'iqf_qty': qty})

        print(f'[IQF TOTAL CALC] Inputs: {parsed_items}, Computed Total: {total_iqf}')

        # ─── 3. BUILD BRASS QTY MAP FOR PER-REASON VALIDATION ───
        by_id_map = {}
        try:
            brass_rows_qs = Brass_QC_Rejected_TrayScan.objects.filter(lot_id=lot_id)
            if not brass_rows_qs.exists():
                brass_rows_qs = Brass_Audit_Rejected_TrayScan.objects.filter(lot_id=lot_id)

            reason_map = OrderedDict()
            for row in brass_rows_qs:
                try:
                    reason_text = (row.rejection_reason.rejection_reason or '').strip()
                except Exception:
                    reason_text = str(getattr(row, 'rejection_reason', ''))
                try:
                    qty = int(row.rejected_tray_quantity or 0)
                except Exception:
                    try:
                        qty = int(float(row.rejected_tray_quantity or 0))
                    except Exception:
                        qty = 0
                if reason_text in reason_map:
                    reason_map[reason_text]['qty'] += qty
                else:
                    reason_map[reason_text] = {'qty': qty, 'reason_id': getattr(row, 'rejection_reason', 'None')}

            reasons = IQF_Rejection_Table.objects.all().order_by('rejection_reason_id')
            for reason in reasons:
                rtext = (reason.rejection_reason or '').strip()
                info = reason_map.get(rtext)
                brass_qty = 0
                if info:
                    brass_qty = info.get('qty', 0) or 0
                else:
                    for k, v in reason_map.items():
                        if v.get('reason_id') and reason.id and v.get('reason_id') == reason.id:
                            brass_qty = v.get('qty', 0) or 0
                            break
                by_id_map[reason.id] = int(brass_qty or 0)
            print(f'[IQF BRASS MAP] by_id_map: {by_id_map}')
        except Exception as e:
            print(f'[IQF BRASS MAP ERROR] {e}')

        # ─── 4. PER-ITEM AND TOTAL VALIDATION (STRICT) ───
        with transaction.atomic():
            if is_full_lot_reject:
                # FULL LOT REJECT: skip per-reason validation — only enforce total <= lot qty
                print(f'[IQF FULL LOT REJECT] Skipping per-reason validation. Total IQF={total_iqf}, limit={iqf_incoming_qty}')
                for itm in parsed_items:
                    rid = itm.get('reason_id')
                    qty = itm.get('iqf_qty') or 0
                    if rid is None and qty > 0:
                        return Response({'success': False, 'error': 'Missing reason_id for provided IQF qty; cannot validate'}, status=400)
                    print(f'[VALIDATION FULL_REJECT] reason_id={rid}, entered={qty}')
            else:
                # NORMAL FLOW: per-reason validation against Brass QC breakdown
                for itm in parsed_items:
                    rid = itm.get('reason_id')
                    qty = itm.get('iqf_qty') or 0
                    if rid is None:
                        if qty > 0:
                            return Response({'success': False, 'error': 'Missing reason_id for provided IQF qty; cannot validate'}, status=400)
                        continue
                    allowed = by_id_map.get(rid, 0)
                    print(f'[VALIDATION] reason_id={rid}, allowed={allowed}, entered={qty}')
                    if allowed == 0 and qty > 0:
                        return Response({'success': False, 'error': f'Cannot accept IQF qty for reason_id {rid}: no Brass QC quantity available', 'reason_id': rid, 'allowed': allowed, 'entered': qty}, status=400)
                    if qty > allowed:
                        return Response({'success': False, 'error': 'IQF Qty cannot exceed Brass QC Reject Qty', 'reason_id': rid, 'allowed': allowed, 'entered': qty}, status=400)

            if total_iqf > iqf_incoming_qty:
                err_msg = f'IQF rejection ({total_iqf}) cannot exceed {"lot qty" if is_full_lot_reject else "RW quantity"} ({iqf_incoming_qty})'
                print(f'[IQF VALIDATION ERROR] {err_msg}')
                return Response({'success': False, 'error': err_msg, 'rw_qty': iqf_incoming_qty, 'submitted_total': total_iqf}, status=400)

            print(f'[IQF REJECTION COMPUTED] total_iqf_rejection={total_iqf}, is_full_lot_reject={is_full_lot_reject}')

            # ─── 5. DRAFT SAVE / LIVE COMPUTE ───
            # 'draft'   â persists to IQF_Draft_Store (Save Draft button, explicit user action).
            # 'compute' â same validation/total calc, NO persistence (live keystroke recompute).
            if action in ('draft', 'compute'):
                if action == 'draft':
                    accepted_trays_payload = data.get('accepted_trays') or []
                    rejected_trays_payload = data.get('rejected_trays') or []
                    IQF_Draft_Store.objects.update_or_create(
                        lot_id=lot_id,
                        draft_type='batch_rejection',
                        defaults={
                            'batch_id': batch_id_val,
                            'user': request.user,
                            'draft_data': {'is_draft': True, 'items': parsed_items, 'total_iqf': total_iqf, 'accepted_trays': accepted_trays_payload, 'rejected_trays': rejected_trays_payload, 'remark': remark},
                        }
                    )
                return Response({'success': True, 'draft': action == 'draft', 'rw_qty': iqf_incoming_qty, 'rejection_rows': parsed_items, 'total_iqf_qty': total_iqf})

            # ─── 6. DECISION ENGINE (action == 'proceed') ───
            rejected_qty = int(total_iqf)
            accepted_qty = int(iqf_incoming_qty - rejected_qty)

            if rejected_qty == 0:
                submission_type = IQF_Submitted.SUB_FULL_ACCEPT
            elif rejected_qty == iqf_incoming_qty:
                submission_type = IQF_Submitted.SUB_FULL_REJECT
            else:
                submission_type = IQF_Submitted.SUB_PARTIAL

            print(f'[DECISION] {submission_type} — accepted={accepted_qty}, rejected={rejected_qty}')

            # ─── 7. TRAY DATA FROM DB (REAL DATA ONLY — IGNORE FRONTEND TRAYS) ───

            # 7a. ORIGINAL SNAPSHOT — full lot trays (tray_quantity) for reference
            # SOURCE OF TRUTH: IQFTrayId only — no BrassTrayId fallback
            all_trays_qs = IQFTrayId.objects.filter(lot_id=lot_id).order_by('id')
            original_tray_list = []
            original_tray_total = 0
            for t in all_trays_qs:
                raw_qty = int(getattr(t, 'tray_quantity', 0) or 0)
                if raw_qty <= 0:
                    continue
                original_tray_total += raw_qty
                original_tray_list.append({
                    'tray_id': getattr(t, 'tray_id', '') or '',
                    'qty': raw_qty,
                    'top_tray': bool(getattr(t, 'top_tray', False)),
                })
            original_data_snapshot = {
                'qty': original_lot_qty,
                'tray_total': original_tray_total,
                'total_trays': len(original_tray_list),
                'trays': original_tray_list,
            }
            print(f'[ORIGINAL] qty={original_lot_qty}, tray_total={original_tray_total}, trays={len(original_tray_list)}')

            # 7b. IQF WORKING SNAPSHOT — eligible trays, excluding delinked
            # SOURCE OF TRUTH: IQFTrayId for tray identifiers
            # QTY RESOLUTION: remaining_qty (post-processed) > BrassTrayId capacity (pre-processed) > tray_quantity (last resort)
            iqf_trays_qs = IQFTrayId.objects.filter(lot_id=lot_id, delink_tray=False).order_by('id')
            source_is_iqf = True

            tray_list = []
            for t in iqf_trays_qs:
                remaining = int(getattr(t, 'remaining_qty', 0) or 0)
                raw_qty = int(getattr(t, 'tray_quantity', 0) or 0)
                # ✅ FIX: IQF-only resolution — NO BrassTrayId dependency
                # Priority: remaining_qty (set by IQF submit) > IQFTrayId tray_quantity
                if remaining > 0:
                    tray_qty = remaining
                else:
                    tray_qty = raw_qty
                if tray_qty <= 0:
                    print(f'[TRAY WARNING] Tray {t.tray_id} has qty=0 (remaining={remaining}, raw={raw_qty}), skipping')
                    continue
                tray_list.append({
                    'obj': t,
                    'tray_id': getattr(t, 'tray_id', '') or '',
                    'qty': tray_qty,
                    'top_tray': bool(getattr(t, 'top_tray', False)),
                    'new_tray': bool(getattr(t, 'new_tray', False)),
                    'delink_flag': bool(getattr(t, 'delink_tray', False)),
                })

            iqf_tray_total = sum(tr['qty'] for tr in tray_list)
            iqf_data_snapshot = {
                'qty': iqf_incoming_qty,
                'tray_total': iqf_tray_total,
                'total_trays': len(tray_list),
                'trays': [
                    {'tray_id': tr['tray_id'], 'qty': tr['qty'], 'top_tray': tr['top_tray']}
                    for tr in tray_list
                ],
            }
            print(f'[IQF] qty={iqf_incoming_qty}, tray_total={iqf_tray_total}, trays={len(tray_list)}')
            if iqf_tray_total != iqf_incoming_qty:
                print(f'[WARNING] iqf_tray_total={iqf_tray_total} != iqf_incoming_qty={iqf_incoming_qty} - tray data may be inconsistent')

            # ─── 8. TRAY VALIDATION ───
            # FULL_ACCEPT / FULL_REJECT: no tray validation needed here
            #   (FULL_ACCEPT has its own per-tray strict validation in section 10 below)
            # PARTIAL: iqf_tray_total must equal accepted_qty
            print(f'[TRAY VALIDATION] flow={submission_type}, iqf_tray_total={iqf_tray_total}, accepted_qty={accepted_qty}, iqf_incoming_qty={iqf_incoming_qty}')

            # PARTIAL tray validation removed — accept is user-driven, reject is system-computed.
            # Only lot-level conservation is enforced: accepted_qty + rejected_qty = iqf_incoming_qty

            # ─── 9. BUILD REJECTION DETAILS (when rejected_qty > 0) ───
            rejection_details = None
            if rejected_qty > 0 and parsed_items:
                rejection_details = []
                for itm in parsed_items:
                    if itm.get('iqf_qty', 0) > 0:
                        reason_obj = IQF_Rejection_Table.objects.filter(id=itm['reason_id']).first()
                        rejection_details.append({
                            'reason_id': itm['reason_id'],
                            'reason_text': reason_obj.rejection_reason if reason_obj else '',
                            'iqf_qty': itm['iqf_qty'],
                        })

            # ─── 10. BUILD LABELED FLOW SNAPSHOTS ───
            full_accept_data = None
            partial_accept_data = None
            full_reject_data = None
            partial_reject_data = None

            # ── Helper: resolve tray capacity for an IQFTrayId record ──
            def _resolve_tray_capacity(iqf_tray_obj):
                """Resolve capacity from the product series for this IQF lot."""
                return _get_series_tray_capacity(ts.batch_id)

            if submission_type == IQF_Submitted.SUB_FULL_ACCEPT:
                # ✅ FULL ACCEPT — Use actual IQFTrayId.tray_quantity when reliable,
                # fall back to capacity-based distribution only when sum doesn't match.
                fa_trays_qs = list(IQFTrayId.objects.filter(lot_id=lot_id, delink_tray=False).order_by('id'))

                accepted_trays = []

                if not fa_trays_qs:
                    _mapped_source_lot = None
                    try:
                        _parent_stock = TotalStockModel.objects.filter(
                            Q(brass_qc_transition_accept_lot_id=lot_id) |
                            Q(brass_qc_transition_reject_lot_id=lot_id) |
                            Q(brass_qc_transition_lot_id=lot_id)
                        ).first()
                        if _parent_stock and getattr(_parent_stock, 'lot_id', None):
                            _mapped_source_lot = _parent_stock.lot_id
                    except Exception:
                        _mapped_source_lot = None

                    _candidate_lots = [lot_id]
                    if _mapped_source_lot and _mapped_source_lot != lot_id:
                        _candidate_lots.append(_mapped_source_lot)

                    for _candidate_lot in _candidate_lots:
                        _src_trays, _src_source, _src_total = _get_brass_qc_rejected_trays_exact(_candidate_lot)
                        if not _src_trays:
                            _src_trays, _src_source, _src_total = _get_brass_qc_submission_rejected_trays_exact(_candidate_lot)
                        if not _src_trays:
                            continue
                        accepted_trays = [
                            {
                                'tray_id': str(t.get('tray_id') or '').strip(),
                                'qty': _safe_int(t.get('qty')),
                                'top_tray': bool(t.get('is_top', t.get('is_top_tray', t.get('top_tray', False)))),
                            }
                            for t in _src_trays
                            if str(t.get('tray_id') or '').strip() and _safe_int(t.get('qty')) > 0
                        ]
                        if accepted_trays:
                            print(
                                f'[FULL_ACCEPT SNAPSHOT] lot={lot_id} source_lot={_candidate_lot} '
                                f'source={_src_source}, trays={len(accepted_trays)}, total={sum(t["qty"] for t in accepted_trays)}'
                            )
                            break
                else:
                    # ✅ FIX: Check if stored tray_quantity values sum to iqf_incoming_qty.
                    # If they do, the tray distribution is already correct — preserve it.
                    stored_sum = sum(int(getattr(t, 'tray_quantity', 0) or 0) for t in fa_trays_qs)
                    use_stored_qty = (stored_sum == iqf_incoming_qty and stored_sum > 0)
                    print(f'  [FA DISTRIBUTE] stored_sum={stored_sum}, iqf_incoming_qty={iqf_incoming_qty}, use_stored_qty={use_stored_qty}')

                    if use_stored_qty:
                        # Tray quantities are reliable — preserve the original distribution
                        for t in fa_trays_qs:
                            qty = int(getattr(t, 'tray_quantity', 0) or 0)
                            if qty <= 0:
                                continue
                            cap = _resolve_tray_capacity(t)
                            is_top = (qty < cap)

                            t.remaining_qty = qty
                            t.top_tray = is_top
                            t.save(update_fields=['remaining_qty', 'top_tray'])

                            accepted_trays.append({'tray_id': t.tray_id, 'qty': qty, 'top_tray': is_top})
                            print(f'  [FA DISTRIBUTE] tray={t.tray_id}, stored_qty={qty}, cap={cap}, top={is_top}')
                    else:
                        # Fallback: distribute by capacity (original behaviour)
                        remaining = iqf_incoming_qty
                        for t in fa_trays_qs:
                            if remaining <= 0:
                                break
                            cap = _resolve_tray_capacity(t)
                            qty = min(remaining, cap)
                            remaining -= qty
                            is_last = (remaining == 0)
                            is_top = is_last and qty < cap

                            t.remaining_qty = qty
                            t.top_tray = is_top
                            t.save(update_fields=['remaining_qty', 'top_tray'])

                            accepted_trays.append({'tray_id': t.tray_id, 'qty': qty, 'top_tray': is_top})
                            print(f'  [FA DISTRIBUTE] tray={t.tray_id}, cap={cap}, assigned={qty}, remaining={remaining}, top={is_top}')

                    # If no tray was marked top_tray (all full fills), mark the last one
                    if accepted_trays and not any(tr['top_tray'] for tr in accepted_trays):
                        accepted_trays[-1]['top_tray'] = True
                        # Also persist to DB
                        last_obj = fa_trays_qs[len(accepted_trays) - 1] if len(accepted_trays) <= len(fa_trays_qs) else None
                        if last_obj:
                            last_obj.top_tray = True
                            last_obj.save(update_fields=['top_tray'])

                fa_tray_total = sum(tr['qty'] for tr in accepted_trays)

                if accepted_trays and fa_tray_total != iqf_incoming_qty:
                    return Response({
                        'success': False,
                        'error': (
                            f'Could not distribute all pieces: distributed {fa_tray_total} of {iqf_incoming_qty}. '
                            f'Available trays ({len(fa_trays_qs)}) have insufficient total capacity. '
                            f'Please verify tray records for lot {lot_id}.'
                        ),
                        'tray_total': fa_tray_total,
                        'iqf_incoming_qty': iqf_incoming_qty,
                    }, status=400)

                # Restore the current same-lot IQF tray mirror only when FULL ACCEPT
                # had to recover its accepted trays from the Brass QC fallback snapshot.
                # Existing same-lot rows may already be present but delinked/zeroed; in
                # that case reactivate ONLY the exact trays recovered for this FULL ACCEPT.
                if not fa_trays_qs and accepted_trays:
                    for tray in accepted_trays:
                        _fa_tray_obj = IQFTrayId.objects.filter(
                            lot_id=lot_id,
                            tray_id=tray['tray_id'],
                        ).order_by('-id').first()

                        if _fa_tray_obj:
                            _fa_tray_obj.tray_quantity = tray['qty']
                            _fa_tray_obj.remaining_qty = tray['qty']
                            _fa_tray_obj.top_tray = tray['top_tray']
                            _fa_tray_obj.rejected_tray = False
                            _fa_tray_obj.delink_tray = False
                            _fa_tray_obj.IP_tray_verified = True
                            _fa_tray_obj.new_tray = False
                            _fa_tray_obj.save(update_fields=[
                                'tray_quantity',
                                'remaining_qty',
                                'top_tray',
                                'rejected_tray',
                                'delink_tray',
                                'IP_tray_verified',
                                'new_tray',
                            ])
                        else:
                            IQFTrayId.objects.create(
                                lot_id=lot_id,
                                tray_id=tray['tray_id'],
                                tray_quantity=tray['qty'],
                                batch_id=ts.batch_id,
                                top_tray=tray['top_tray'],
                                remaining_qty=tray['qty'],
                                rejected_tray=False,
                                delink_tray=False,
                                IP_tray_verified=True,
                                new_tray=False,
                                user=request.user,
                            )

                full_accept_data = {
                    'label': 'FULL_ACCEPT',
                    'qty': accepted_qty,
                    'total_trays': len(accepted_trays),
                    'trays': accepted_trays,
                }
                print(f'[FULL_ACCEPT] iqf_incoming={iqf_incoming_qty}, tray_total={fa_tray_total}, trays={len(accepted_trays)}, VALIDATED=OK')

            elif submission_type == IQF_Submitted.SUB_FULL_REJECT:
                # FULL REJECT — distribute rejected_qty across trays BY CAPACITY (same issue as FULL_ACCEPT)
                fr_trays_qs = list(IQFTrayId.objects.filter(lot_id=lot_id, delink_tray=False).order_by('id'))
                if not fr_trays_qs:
                    same_lot_rows = list(
                        IQFTrayId.objects.filter(
                            lot_id=lot_id,
                            tray_quantity__gt=0,
                        ).order_by('id')
                    )
                    same_lot_entries = _iqf_tray_entries_from_rows(same_lot_rows)
                    if _iqf_tray_entries_match_qty(same_lot_entries, rejected_qty):
                        fr_trays_qs = same_lot_rows
                        print(
                            f'[FULL_REJECT RECOVERY] using same-lot tray rows including '
                            f'delinked rows: trays={len(fr_trays_qs)}, qty={rejected_qty}'
                        )
                distributed_trays = []
                remaining_to_distribute = rejected_qty

                for t in fr_trays_qs:
                    if remaining_to_distribute <= 0:
                        t.remaining_qty = 0
                        t.rejected_tray = True
                        t.delink_tray = False
                        t.save(update_fields=['remaining_qty', 'rejected_tray', 'delink_tray'])
                        continue
                    cap = _resolve_tray_capacity(t)
                    take = min(remaining_to_distribute, cap)
                    remaining_to_distribute -= take
                    # Persist remaining_qty to DB
                    t.remaining_qty = take
                    t.rejected_tray = True
                    t.delink_tray = False
                    t.save(update_fields=['remaining_qty', 'rejected_tray', 'delink_tray'])
                    if take > 0:
                        distributed_trays.append({'tray_id': t.tray_id, 'qty': take, 'top_tray': bool(t.top_tray)})

                full_reject_data = {
                    'label': 'FULL_REJECT',
                    'qty': rejected_qty,
                    'total_trays': len(distributed_trays),
                    'trays': distributed_trays,
                    'reasons': rejection_details,
                }
                print(f'[FULL_REJECT DISTRIBUTE] rejected={rejected_qty}, distributed to {len(distributed_trays)} trays')

            else:
                # ── PARTIAL — IMMEDIATE SPLIT (Brass QC architecture) ──
                # Both child lots created NOW in same atomic transaction.
                # Accept side: user-scanned trays from frontend payload.
                # Reject side: computed from remaining tray quantities.
                # Parent lot is consumed (closed) immediately.

                # ── ACCEPT SIDE: user truth from frontend payload ──
                accepted_trays_payload = data.get('accepted_trays') or []
                accepted_trays = []
                for at in accepted_trays_payload:
                    tray_id = str(at.get('tray_id', '') or '').strip()
                    qty = 0
                    try:
                        qty = int(at.get('qty', 0) or 0)
                    except (ValueError, TypeError):
                        qty = 0
                    is_top = bool(at.get('is_top_tray', False))
                    if tray_id and qty > 0:
                        accepted_trays.append({
                            'tray_id': tray_id,
                            'qty': qty,
                            'is_top_tray': is_top,
                        })

                accept_total = sum(t['qty'] for t in accepted_trays)
                print(f'[PARTIAL ACCEPT] User-scanned: {len(accepted_trays)} trays, total={accept_total}, expected={accepted_qty}')

                # ── FALLBACK: no accepted trays from frontend → auto-distribute from parent lot trays ──
                # This handles the race-condition case where submitAudit is called before
                # modalIqfTotal is updated, so the PARTIAL JS branch is skipped.
                if not accepted_trays and accepted_qty > 0 and tray_list:
                    _remaining_acc = accepted_qty
                    for tray in tray_list:
                        if _remaining_acc <= 0:
                            break
                        _take = min(_remaining_acc, tray['qty'])
                        _remaining_acc -= _take
                        if _take > 0:
                            cap = _resolve_tray_capacity(tray['obj'])
                            accepted_trays.append({
                                'tray_id': tray['tray_id'],
                                'qty': _take,
                                'is_top_tray': _take < cap,
                            })
                    accept_total = sum(t['qty'] for t in accepted_trays)
                    print(f'[PARTIAL ACCEPT AUTO] Auto-distributed from parent trays: {len(accepted_trays)} trays, total={accept_total}')

                if accept_total != accepted_qty:
                    return Response({
                        'success': False,
                        'error': f'Scanned accept tray total ({accept_total}) ≠ accepted qty ({accepted_qty}). Verify tray scans.',
                        'accept_total': accept_total,
                        'accepted_qty': accepted_qty,
                    }, status=400)

                # ── REJECT SIDE: compute from remaining tray quantities ──
                # Handle new tray IDs, existing tray reuse, and mixed scenarios.
                # Deduct accepted_qty from parent trays FIFO (by tray_list order).
                # Whatever remains in parent trays becomes reject child lot.
                # Accepted child keeps user-scanned tray IDs (can be new or existing).
                
                remaining_to_allocate = accepted_qty  # How much to consume from parent trays
                rejected_trays = []
                
                for tray in tray_list:
                    t_id = tray['tray_id']
                    total_qty = tray['qty']
                    
                    # How much of this parent tray goes to rejected side?
                    if remaining_to_allocate > 0:
                        # Still consuming from parent for accepted side
                        consumed = min(remaining_to_allocate, total_qty)
                        remaining_to_allocate -= consumed
                        rej_qty = total_qty - consumed
                    else:
                        # Already done consuming, rest of parent goes to reject
                        rej_qty = total_qty
                    
                    if rej_qty > 0:
                        cap = _resolve_tray_capacity(tray['obj'])
                        is_top = rej_qty < cap
                        rejected_trays.append({'tray_id': t_id, 'qty': rej_qty, 'top_tray': is_top})

                reject_total = sum(t['qty'] for t in rejected_trays)
                if reject_total != rejected_qty:
                    return Response({
                        'success': False,
                        'error': f'Computed reject tray total ({reject_total}) ≠ rejected qty ({rejected_qty}). Tray data inconsistency.',
                        'reject_total': reject_total,
                        'rejected_qty': rejected_qty,
                    }, status=400)

                # ── Generate child lot IDs ──
                accepted_lot_id = generate_new_lot_id()
                time.sleep(0.001)  # Ensure different microsecond timestamp for rejected lot
                rejected_lot_id = generate_new_lot_id()
                print(f'[PARTIAL SPLIT] accepted_lot={accepted_lot_id}, rejected_lot={rejected_lot_id}')

                # ── Create ACCEPTED child lot → Brass QC ──
                TotalStockModel.objects.create(
                    lot_id=accepted_lot_id,
                    batch_id=ts.batch_id,
                    model_stock_no=ts.model_stock_no,
                    version=ts.version,
                    polish_finish=ts.polish_finish,
                    plating_color=ts.plating_color,
                    total_stock=accepted_qty,
                    total_IP_accpeted_quantity=accepted_qty,
                    accepted_Ip_stock=True,
                    iqf_acceptance=True,
                    iqf_accepted_qty=accepted_qty,
                    iqf_accepted_qty_verified=True,
                    send_brass_qc=True,
                    send_brass_audit_to_iqf=False,
                    last_process_module='IQF',
                    next_process_module='Brass QC',
                    last_process_date_time=timezone.now(),
                    iqf_last_process_date_time=timezone.now(),
                )
                for tray in accepted_trays:
                    IQFTrayId.objects.create(
                        lot_id=accepted_lot_id,
                        tray_id=tray['tray_id'],
                        tray_quantity=tray['qty'],
                        batch_id=ts.batch_id,
                        top_tray=tray['is_top_tray'],
                        remaining_qty=tray['qty'],
                        IP_tray_verified=True,
                        new_tray=False,
                        user=request.user,
                    )
                IQF_Accepted_TrayScan.objects.create(
                    lot_id=accepted_lot_id,
                    accepted_tray_quantity=str(accepted_qty),
                    user=request.user,
                )
                for tray in accepted_trays:
                    IQF_Accepted_TrayID_Store.objects.update_or_create(
                        tray_id=tray['tray_id'],
                        defaults={
                            'lot_id': accepted_lot_id,
                            'tray_qty': tray['qty'],
                            'user': request.user,
                            'is_save': True,
                            'is_draft': False,
                        }
                    )
                print(f'[PARTIAL SPLIT] Accept child created: lot={accepted_lot_id}, qty={accepted_qty}, trays={len(accepted_trays)}')

                # ── Create REJECTED child lot → IQF Reject tracking ──
                TotalStockModel.objects.create(
                    lot_id=rejected_lot_id,
                    batch_id=ts.batch_id,
                    model_stock_no=ts.model_stock_no,
                    version=ts.version,
                    polish_finish=ts.polish_finish,
                    plating_color=ts.plating_color,
                    total_stock=rejected_qty,
                    total_IP_accpeted_quantity=rejected_qty,
                    accepted_Ip_stock=True,
                    iqf_rejection=True,
                    iqf_after_rejection_qty=rejected_qty,
                    iqf_accepted_qty=0,
                    send_brass_audit_to_iqf=False,
                    send_brass_qc=False,
                    last_process_module='IQF',
                    next_process_module='IQF Reject',
                    last_process_date_time=timezone.now(),
                    iqf_last_process_date_time=timezone.now(),
                )
                for tray in rejected_trays:
                    IQFTrayId.objects.create(
                        lot_id=rejected_lot_id,
                        tray_id=tray['tray_id'],
                        tray_quantity=tray['qty'],
                        batch_id=ts.batch_id,
                        top_tray=tray['top_tray'],
                        remaining_qty=tray['qty'],
                        rejected_tray=True,
                        IP_tray_verified=True,
                        new_tray=False,
                        user=request.user,
                    )
                # Rejection reason store for rejected child lot
                rej_child_reason_ids = [p['reason_id'] for p in parsed_items if p['reason_id'] and p.get('iqf_qty', 0) > 0]
                rej_child_store = IQF_Rejection_ReasonStore.objects.create(
                    lot_id=rejected_lot_id,
                    user=request.user,
                    total_rejection_quantity=rejected_qty,
                    batch_rejection=False,
                )
                if rej_child_reason_ids:
                    rej_child_store.rejection_reason.set(
                        IQF_Rejection_Table.objects.filter(id__in=rej_child_reason_ids)
                    )
                print(f'[PARTIAL SPLIT] Reject child created: lot={rejected_lot_id}, qty={rejected_qty}, trays={len(rejected_trays)}')

                # ── Mark parent trays as delinked — parent is consumed ──
                IQFTrayId.objects.filter(lot_id=lot_id).update(delink_tray=True)

                # ── Build flow snapshots (both filled immediately — no deferral) ──
                partial_accept_data = {
                    'label': 'PARTIAL_ACCEPT',
                    'qty': accepted_qty,
                    'total_trays': len(accepted_trays),
                    'trays': accepted_trays,
                    'accepted_lot_id': accepted_lot_id,
                }
                partial_reject_data = {
                    'label': 'PARTIAL_REJECT',
                    'qty': rejected_qty,
                    'total_trays': len(rejected_trays),
                    'trays': rejected_trays,
                    'reasons': rejection_details,
                    'rejected_lot_id': rejected_lot_id,
                }

            # ─── 11. CREATE REJECTION REASON STORE (only when rejected_qty > 0) ───
            if rejected_qty > 0:
                store = IQF_Rejection_ReasonStore.objects.create(
                    lot_id=lot_id,
                    user=request.user,
                    total_rejection_quantity=rejected_qty,
                    batch_rejection=False,
                )
                reason_ids = [p['reason_id'] for p in parsed_items if p['reason_id'] and p.get('iqf_qty', 0) > 0]
                if reason_ids:
                    reasons_qs = IQF_Rejection_Table.objects.filter(id__in=reason_ids)
                    store.rejection_reason.set(reasons_qs)

            # Save audit trail draft record
            IQF_Draft_Store.objects.update_or_create(
                lot_id=lot_id,
                draft_type='batch_rejection',
                defaults={
                    'batch_id': batch_id_val,
                    'user': request.user,
                    'draft_data': {'is_draft': False, 'items': parsed_items, 'total_iqf': total_iqf,
                                   'submission_type': submission_type},
                }
            )

            # ─── 12. SAVE IQF_Submitted — ONE LOT → ONE ROW → FULL TRACEABILITY ───
            IQF_Submitted.objects.update_or_create(
                lot_id=lot_id,
                defaults={
                    'batch_id': ts.batch_id,
                    'original_lot_qty': original_lot_qty,
                    'iqf_incoming_qty': iqf_incoming_qty,
                    'total_lot_qty': iqf_incoming_qty,  # backward compat — always = iqf_incoming_qty
                    'accepted_qty': accepted_qty,
                    'rejected_qty': rejected_qty,
                    'submission_type': submission_type,
                    'original_data': original_data_snapshot,
                    'iqf_data': iqf_data_snapshot,
                    'full_accept_data': full_accept_data,
                    'partial_accept_data': partial_accept_data,
                    'full_reject_data': full_reject_data,
                    'partial_reject_data': partial_reject_data,
                    'rejection_details': rejection_details,
                    'remarks': remark,
                    'is_completed': True,
                    'is_draft': False,
                    'created_by': request.user,
                }
            )

            print(f'[DB SAVE] ONE ROW: lot={lot_id}, type={submission_type}, '
                  f'original={original_lot_qty}, incoming={iqf_incoming_qty}, '
                  f'accepted={accepted_qty}, rejected={rejected_qty}')

            # ─── 13. MOVEMENT CONTROL — update TotalStockModel flags ───
            # FULL_ACCEPT / PARTIAL: accepted qty flows to Brass QC (send_brass_qc=True)
            # FULL_REJECT: nothing accepted, stays for rework
            if submission_type == IQF_Submitted.SUB_FULL_ACCEPT:
                ts.iqf_acceptance = True
                ts.iqf_rejection = False
                ts.iqf_few_cases_acceptance = False
                ts.iqf_onhold_picking = False
                ts.send_brass_qc = True  # push lot to Brass QC
                ts.next_process_module = 'Brass QC'  # ✅ LOCK: downstream reads from IQF_Submitted
                ts.last_process_date_time = timezone.now()  # ✅ FIX: ensure lot sorts to top of Brass QC pick table
                # ✅ FIX: Reset Brass QC fields for fresh cycle when lot returns from IQF
                ts.brass_qc_accepted_qty_verified = False
                ts.brass_qc_accptance = False
                ts.brass_qc_rejection = False
                ts.brass_qc_few_cases_accptance = False
                ts.brass_draft = False
                ts.brass_onhold_picking = False
                ts.brass_accepted_tray_scan_status = False
                ts.brass_physical_qty = 0
                ts.brass_missing_qty = 0
                ts.brass_audit_rejection = False
            elif submission_type == IQF_Submitted.SUB_FULL_REJECT:
                ts.iqf_rejection = True
                ts.iqf_acceptance = False
                ts.iqf_few_cases_acceptance = False
                ts.iqf_onhold_picking = False
                ts.send_brass_qc = False  # nothing to send
                ts.brass_audit_rejection = False
            else:  # PARTIAL — parent consumed, children created immediately
                ts.iqf_few_cases_acceptance = True
                ts.iqf_onhold_picking = False
                ts.iqf_acceptance = False
                ts.iqf_rejection = False
                ts.send_brass_qc = False
                ts.next_process_module = None  # parent has no downstream — children carry it
                ts.is_split = True
                ts.remove_lot = True
                # ✅ FIX: Clear brass_audit_rejection so parent NEVER satisfies re-entry filter
                ts.brass_audit_rejection = False

            ts.iqf_accepted_qty = accepted_qty
            ts.iqf_after_rejection_qty = rejected_qty
            ts.iqf_accepted_qty_verified = False  # Reset for fresh cycle on re-entry
            ts.last_process_module = 'IQF'
            ts.current_stage = 'IQF'
            ts.send_brass_audit_to_iqf = False  # Always remove parent from IQF pick table after submit
            ts.iqf_last_process_date_time = timezone.now()

            ts.save(update_fields=[
                'iqf_acceptance', 'iqf_rejection', 'iqf_few_cases_acceptance',
                'iqf_onhold_picking',
                'iqf_accepted_qty', 'iqf_after_rejection_qty',
                'iqf_accepted_qty_verified',
                'last_process_module', 'last_process_date_time', 'send_brass_audit_to_iqf',
                'send_brass_qc', 'next_process_module',
                'iqf_last_process_date_time',
                'brass_qc_accepted_qty_verified', 'brass_qc_accptance', 'brass_qc_rejection',
                'brass_qc_few_cases_accptance', 'brass_draft', 'brass_onhold_picking',
                'brass_accepted_tray_scan_status', 'brass_physical_qty', 'brass_missing_qty',
                'is_split', 'remove_lot', 'brass_audit_rejection',
                'current_stage',
            ])

            print(f'[MOVEMENT] iqf_acceptance={ts.iqf_acceptance}, '
                  f'iqf_rejection={ts.iqf_rejection}, '
                  f'iqf_few_cases_acceptance={ts.iqf_few_cases_acceptance}, '
                  f'send_brass_audit_to_iqf={ts.send_brass_audit_to_iqf}, '
                  f'send_brass_qc={ts.send_brass_qc}')

            # ─── 14. BUILD RESPONSE — PARTIAL gets enriched reject/delink flags ───
            resp_data = {
                'success': True,
                'proceeded': True,
                'submission_type': submission_type,
                'original_lot_qty': original_lot_qty,
                'iqf_incoming_qty': iqf_incoming_qty,
                'accepted_qty': accepted_qty,
                'rejected_qty': rejected_qty,
                'rw_qty': iqf_incoming_qty,
                'total_iqf_qty': total_iqf,
            }

            if submission_type == IQF_Submitted.SUB_PARTIAL:
                resp_data['accepted_lot_id'] = accepted_lot_id
                resp_data['rejected_lot_id'] = rejected_lot_id
                resp_data['message'] = 'IQF partial lots created successfully'
                print(f'[PARTIAL RESPONSE] accepted_lot={accepted_lot_id}(qty={accepted_qty}), rejected_lot={rejected_lot_id}(qty={rejected_qty})')

            return Response(resp_data)

    except Exception as e:
        print(f'[IQF SUBMIT ERROR] {e}')
        traceback.print_exc()
        return Response({'success': False, 'error': 'Server error'}, status=500)

# View Icon - Dynamic fetch
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def iqf_tray_details(request):
    """Return tray details for a lot. Single source of truth for tray modal.

    ARCHITECTURE RULE: Tray qty = SUM(rejected_tray_quantity) GROUP BY tray_id
    from Brass_QC_Rejected_TrayScan. Never trust stored tray_quantity or tray_capacity.

    Query params: ?lot_id=...
    """
    lot_id = request.GET.get('lot_id')
    if not lot_id:
        return Response({'success': False, 'error': 'lot_id required'}, status=400)
    try:
        print(f"[IQF TRAY API] Checking IQF_Submitted first for lot: {lot_id}")

        # ✅ FIX: Check IQF_Submitted FIRST — SINGLE SOURCE OF TRUTH after IQF completes
        iqf_record = IQF_Submitted.objects.filter(lot_id=lot_id, is_completed=True).last()

        if iqf_record and iqf_record.submission_type in ('FULL_ACCEPT', 'PARTIAL', 'FULL_REJECT', 'LOT_REJECTION'):
            if iqf_record.submission_type == 'FULL_ACCEPT' and iqf_record.full_accept_data:
                source_trays = iqf_record.full_accept_data.get('trays', [])
                label = 'FULL_ACCEPT'
                is_rejected = False
                tray_status = 'ACCEPTED'
            elif iqf_record.submission_type in ('FULL_REJECT', 'LOT_REJECTION') and iqf_record.full_reject_data:
                source_trays = iqf_record.full_reject_data.get('trays', [])
                label = 'FULL_REJECT'
                is_rejected = True
                tray_status = 'REJECTED'
            elif iqf_record.submission_type == 'PARTIAL' and iqf_record.partial_accept_data:
                source_trays = iqf_record.partial_accept_data.get('trays', [])
                label = 'PARTIAL_ACCEPT'
                is_rejected = False
                tray_status = 'ACCEPTED'
            else:
                source_trays = []
                label = 'EMPTY'
                is_rejected = False
                tray_status = 'ACCEPTED'

            tray_list = []
            total_qty = 0
            for tray in source_trays:
                qty = int(tray.get('qty', 0))
                if qty <= 0:
                    continue
                total_qty += qty
                tray_list.append({
                    'tray_id': tray.get('tray_id', ''),
                    'tray_qty': qty,
                    'top_tray': bool(tray.get('top_tray', False)),
                    'status': tray_status,
                    'is_rejected': is_rejected,
                    'is_reusable': not is_rejected,
                    'is_new': False,
                    'label': f'IQF {label}',
                })

            print(f"[IQF TRAY API] Using IQF_Submitted ({label}) for lot {lot_id}: {len(tray_list)} trays, total_qty={total_qty}")
            return Response({
                'success': True,
                'lot_id': lot_id,
                'total_qty': total_qty,
                'total_trays': len(tray_list),
                'trays': tray_list,
                'source': 'IQF_Submitted',
            })

        # FALLBACK: Dynamic aggregation from rejection scan logs (pre-IQF completion)
        # ✅ FIX: Check Brass Audit FIRST — for re-entry lots from Brass Audit partial
        # rejection, fresh data is in Brass_Audit_Rejected_TrayScan. Old/stale
        # Brass_QC_Rejected_TrayScan records from the first cycle would give wrong qty.
        print(f"[IQF TRAY API] Fallback: Dynamic aggregation for lot: {lot_id}")

        # SOURCE OF TRUTH: Aggregate rejected_tray_quantity per tray from rejection scan logs
        # rejected_tray_quantity is CharField, so aggregate in Python
        brass_reject_rows = Brass_Audit_Rejected_TrayScan.objects.filter(lot_id=lot_id)

        # If no Brass Audit rows, try Brass QC as fallback
        if not brass_reject_rows.exists():
            brass_reject_rows = Brass_QC_Rejected_TrayScan.objects.filter(lot_id=lot_id)

        # Aggregate per tray_id (field is rejected_tray_id in Brass_QC_Rejected_TrayScan)
        tray_qty_map = {}
        for row in brass_reject_rows:
            tray_id = getattr(row, 'rejected_tray_id', None) or getattr(row, 'tray_id', None) or ''
            if not tray_id:
                continue
            try:
                qty = int(row.rejected_tray_quantity or 0)
            except (ValueError, TypeError):
                qty = 0
            tray_qty_map[tray_id] = tray_qty_map.get(tray_id, 0) + qty

        # ✅ FINAL FALLBACK: If no Brass QC/Audit rejection rows, use IQFTrayId directly
        # Covers case: Brass QC full lot rejection → sent to IQF → IQF not yet completed
        if not tray_qty_map:
            iqf_tray_rows = IQFTrayId.objects.filter(lot_id=lot_id, delink_tray=False).order_by('id')
            for row in iqf_tray_rows:
                qty = row.tray_quantity or 0
                if qty > 0:
                    tray_qty_map[row.tray_id] = tray_qty_map.get(row.tray_id, 0) + qty
            if tray_qty_map:
                print(f"[IQF TRAY API] Using IQFTrayId fallback for lot {lot_id}: {len(tray_qty_map)} trays")

        tray_list = []
        total_qty = 0

        for tray_id in sorted(tray_qty_map.keys()):
            qty = tray_qty_map[tray_id]
            total_qty += qty
            is_new = not IQFTrayId.objects.filter(tray_id=tray_id, lot_id=lot_id).exists()
            tray_list.append({
                'tray_id': tray_id,
                'tray_qty': qty,
                'status': 'NEW' if is_new else 'NORMAL',
                'is_rejected': True,
                'is_reusable': False,
                'is_new': is_new,
                'label': 'New Tray Available' if is_new else 'Tray reuse allowed',
            })

        print(f"[IQF TRAY API] returning {len(tray_list)} trays (dynamic aggregation), total_qty={total_qty}")
        return Response({
            'success': True,
            'lot_id': lot_id,
            'total_qty': total_qty,
            'total_trays': len(tray_list),
            'trays': tray_list
        })
    except Exception as e:
        traceback.print_exc()
        print('[IQF TRAY API ERROR]', str(e))
        return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)

# IQF Completed Table API: returns all lots from IQF_Submitted (SINGLE SOURCE OF TRUTH)
@method_decorator(login_required, name='dispatch')
class IQFCompletedTableView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            print('[IQF COMPLETED API] Called — SOURCE: IQF_Submitted')

            # SINGLE SOURCE OF TRUTH: IQF_Submitted table
            submitted_qs = IQF_Submitted.objects.select_related(
                'batch_id', 'batch_id__model_stock_no', 'batch_id__version',
                'batch_id__location', 'created_by'
            ).filter(is_completed=True).order_by('-created_at')

            data = []
            for sub in submitted_qs:
                # Determine status label
                if sub.submission_type == 'FULL_ACCEPT':
                    status_label = 'ACCEPT'
                elif sub.submission_type in ('FULL_REJECT', 'LOT_REJECTION'):
                    status_label = 'REJECT'
                elif sub.submission_type == 'PARTIAL':
                    status_label = 'PARTIAL'
                else:
                    status_label = sub.submission_type

                # Extract tray data from stored snapshots
                accept_data = sub.partial_accept_data or sub.full_accept_data
                reject_data = sub.partial_reject_data or sub.full_reject_data

                accepted_trays = []
                if accept_data and accept_data.get('trays'):
                    accepted_trays = [{'tray_id': t.get('tray_id', ''), 'tray_qty': int(t.get('qty', 0))} for t in accept_data['trays'] if int(t.get('qty', 0)) > 0]

                rejected_trays = []
                if reject_data and reject_data.get('trays'):
                    rejected_trays = [{'tray_id': t.get('tray_id', ''), 'tray_qty': int(t.get('qty', 0))} for t in reject_data['trays'] if int(t.get('qty', 0)) > 0]

                # Compute delinked from original snapshot
                delinked_trays = []
                accept_ids = {t['tray_id'] for t in accepted_trays}
                reject_ids = {t['tray_id'] for t in rejected_trays}
                orig = sub.original_data or {}
                for t in orig.get('trays', []):
                    tid = t.get('tray_id', '')
                    if tid and tid not in accept_ids and tid not in reject_ids:
                        delinked_trays.append({'tray_id': tid, 'tray_qty': int(t.get('qty', 0))})

                data.append({
                    'lot_id': sub.lot_id,
                    'batch_id': sub.batch_id.batch_id if sub.batch_id else '',
                    'model_no': getattr(sub.batch_id.model_stock_no, 'model_no', '') if sub.batch_id and getattr(sub.batch_id, 'model_stock_no', None) else '',
                    'location': getattr(sub.batch_id.location, 'location_name', '') if sub.batch_id and getattr(sub.batch_id, 'location', None) else '',
                    'iqf_incoming_qty': sub.iqf_incoming_qty,
                    'iqf_accepted_qty': sub.accepted_qty,
                    'iqf_rejection_qty': sub.rejected_qty,
                    'delink_qty': sum(t['tray_qty'] for t in delinked_trays),
                    'submission_type': sub.submission_type,
                    'status_label': status_label,
                    'status': status_label,
                    'last_updated': sub.created_at,
                    'remarks': sub.remarks or '',
                    'created_by': sub.created_by.username if sub.created_by else '',
                    'tray_details': accepted_trays + rejected_trays + delinked_trays,
                    'accepted_trays': accepted_trays,
                    'rejected_trays': rejected_trays,
                    'delinked_trays': delinked_trays,
                })

            print(f'[IQF COMPLETED API] Count: {len(data)}, Source: IQF_Submitted')
            return Response({'success': True, 'count': len(data), 'data': data})
        except Exception as e:
            print('[IQF COMPLETED ERROR]', str(e))
            traceback.print_exc()
            return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)


@method_decorator(login_required, name='dispatch')
class IQFCompletedPageView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'IQF/Iqf_Completed.html'

    def get(self, request):
        """IQF Completed Page — renders table data from IQF_Submitted (SINGLE SOURCE OF TRUTH)."""
        try:
            submitted_qs = IQF_Submitted.objects.select_related(
                'batch_id', 'batch_id__model_stock_no', 'batch_id__version',
                'batch_id__location', 'created_by'
            ).filter(is_completed=True).order_by('-created_at')

            # Pre-fetch pick table remarks AND next_process_module from TotalStockModel (SINGLE SOURCE OF TRUTH)
            all_lot_ids = list(submitted_qs.values_list('lot_id', flat=True))
            pick_remarks_map = dict(
                TotalStockModel.objects.filter(lot_id__in=all_lot_ids)
                .values_list('lot_id', 'IQF_pick_remarks')
            )

            # Also fetch current_stage (live SSOT) + next_process_module + brass_qc activity flags for all lot_ids
            _own_stock_qs = TotalStockModel.objects.filter(lot_id__in=all_lot_ids).values(
                'lot_id', 'next_process_module', 'current_stage',
                'brass_draft', 'brass_qc_accptance', 'brass_qc_rejection', 'brass_qc_few_cases_accptance',
                'iqf_acceptance', 'iqf_rejection', 'iqf_few_cases_acceptance',
                'brass_qc_accepted_qty_verified',
            )
            own_stage_map = {}           # lot_id → next_process_module
            own_current_stage_map = {}   # lot_id → current_stage (live SSOT)
            own_bq_active_map = {}       # lot_id → bool (brass qc started?)
            own_bq_released_map = {}     # lot_id → bool (brass qc released — SSOT: brass_qc_accepted_qty_verified)
            for row in _own_stock_qs:
                own_stage_map[row['lot_id']] = row['next_process_module']
                own_current_stage_map[row['lot_id']] = row['current_stage']
                own_bq_active_map[row['lot_id']] = bool(
                    row['brass_draft'] or row['brass_qc_accptance'] or
                    row['brass_qc_rejection'] or row['brass_qc_few_cases_accptance']
                )
                own_bq_released_map[row['lot_id']] = bool(row['brass_qc_accepted_qty_verified'])

            # For PARTIAL lots: look up child accept lot's live stage via IQF_PartialAcceptLot
            partial_lot_ids = list(
                submitted_qs.filter(submission_type='PARTIAL').values_list('lot_id', flat=True)
            )
            parent_to_child_map = {}  # parent_lot_id → child_accept_lot_id
            if partial_lot_ids:
                for pal in IQF_PartialAcceptLot.objects.filter(parent_lot_id__in=partial_lot_ids).values('parent_lot_id', 'new_lot_id'):
                    parent_to_child_map[pal['parent_lot_id']] = pal['new_lot_id']

            child_lot_ids = list(parent_to_child_map.values())
            child_stage_map = {}       # child_lot_id → next_process_module
            child_current_stage_map = {}  # child_lot_id → current_stage (live SSOT)
            child_bq_active_map = {}   # child_lot_id → bool (brass qc started?)
            child_bq_released_map = {} # child_lot_id → bool (brass qc released — SSOT: brass_qc_accepted_qty_verified)
            if child_lot_ids:
                _child_stock_qs = TotalStockModel.objects.filter(lot_id__in=child_lot_ids).values(
                    'lot_id', 'next_process_module', 'current_stage',
                    'brass_draft', 'brass_qc_accptance', 'brass_qc_rejection', 'brass_qc_few_cases_accptance',
                    'brass_qc_accepted_qty_verified',
                )
                for row in _child_stock_qs:
                    child_stage_map[row['lot_id']] = row['next_process_module']
                    child_current_stage_map[row['lot_id']] = row['current_stage']
                    child_bq_active_map[row['lot_id']] = bool(
                        row['brass_draft'] or row['brass_qc_accptance'] or
                        row['brass_qc_rejection'] or row['brass_qc_few_cases_accptance']
                    )
                    child_bq_released_map[row['lot_id']] = bool(row['brass_qc_accepted_qty_verified'])

            _IQF_VALID_STAGES = {
                'Input Screening', 'IQF', 'Brass QC', 'Brass Audit',
                'Jig Loading', 'Jig Unloading', 'Nickel Inspection',
                'Spider Spindle', 'Day Planning', 'Inprocess Inspection',
                'Nickel Audit',
            }

            master_data = []
            for sub in submitted_qs:
                batch = sub.batch_id
                if not batch:
                    continue

                # Status label
                if sub.submission_type == 'FULL_ACCEPT':
                    status_label = 'ACCEPT'
                elif sub.submission_type in ('FULL_REJECT', 'LOT_REJECTION'):
                    status_label = 'REJECT'
                elif sub.submission_type == 'PARTIAL':
                    status_label = 'PARTIAL'
                else:
                    status_label = sub.submission_type

                # Model images
                images = []
                if batch.model_stock_no:
                    from modelmasterapp.image_utils import sort_images_front_first
                    for img in sort_images_front_first(batch.model_stock_no.images.all()):
                        if img.master_image:
                            images.append(img.master_image.url)
                if not images:
                    images = [static('assets/images/imagePlaceholder.jpg')]

                # Tray count from stored snapshot
                accept_data = sub.partial_accept_data or sub.full_accept_data
                reject_data = sub.partial_reject_data or sub.full_reject_data
                accept_trays = (accept_data or {}).get('trays', [])
                reject_trays = (reject_data or {}).get('trays', [])
                total_trays = len([t for t in accept_trays if int(t.get('qty', 0)) > 0]) + len([t for t in reject_trays if int(t.get('qty', 0)) > 0])

                # Determine live Current Stage. Prefer the centralized current_stage
                # SSOT (modelmasterapp/stage_service.py) — the same field every other
                # Completed Table (Day Planning, Input Screening, Brass QC, Brass Audit)
                # reads — so this lot shows the identical stage everywhere. Fall back to
                # the legacy dynamic next_process_module + activity-guard computation
                # only for rows that pre-date the current_stage field.
                _own_stage = own_stage_map.get(sub.lot_id)
                _own_current = own_current_stage_map.get(sub.lot_id)
                if sub.submission_type == 'PARTIAL':
                    _child_lot_id = parent_to_child_map.get(sub.lot_id)
                    _child_stage = child_stage_map.get(_child_lot_id) if _child_lot_id else None
                    _child_current = child_current_stage_map.get(_child_lot_id) if _child_lot_id else None
                    _live_current = _child_current or _own_current
                    _resolved = _child_stage or _own_stage
                    _bq_active = child_bq_active_map.get(_child_lot_id, False) if _child_lot_id else False
                    _bq_released = child_bq_released_map.get(_child_lot_id, False) if _child_lot_id else False
                else:
                    _live_current = _own_current
                    _resolved = _own_stage
                    _bq_active = own_bq_active_map.get(sub.lot_id, False)
                    _bq_released = own_bq_released_map.get(sub.lot_id, False)

                _fallback = 'IQF'
                if _live_current and _live_current in _IQF_VALID_STAGES:
                    _next_stage = _live_current
                    # Keep the Brass QC "released" pill honest: only trust it once the
                    # live stage has actually reached Brass QC.
                    if _live_current == 'Brass QC':
                        _bq_active = True
                elif _resolved and _resolved in _IQF_VALID_STAGES:
                    if _resolved == 'Brass QC' and not _bq_active:
                        # Lot in Brass QC pick table but untouched → keep showing IQF
                        _next_stage = _fallback
                    else:
                        # Any other valid stage (Brass Audit, Jig Loading, …) → already past Brass QC
                        _next_stage = _resolved
                else:
                    _next_stage = _fallback

                # Only trust the verified flag when this lot's resolved destination is
                # actually Brass QC — otherwise (e.g. FULL_REJECT lots, which terminate
                # at IQF and never route back) a stale flag left over from an earlier
                # Brass QC cycle must not be shown as "Released".
                if _next_stage != 'Brass QC':
                    _bq_released = False

                master_data.append({
                    'batch_id': batch.batch_id,
                    'stock_lot_id': sub.lot_id,
                    'iqf_last_process_date_time': sub.created_at,
                    'plating_stk_no': batch.plating_stk_no or '',
                    'polishing_stk_no': batch.polishing_stk_no or '',
                    'plating_color': batch.plating_color or '',
                    'polish_finish': batch.polish_finish or '',
                    'location__location_name': batch.location.location_name if batch.location else '',
                    'tray_type': batch.tray_type or '',
                    'tray_capacity': batch.tray_capacity or 0,
                    'no_of_trays': total_trays,
                    'display_quantity': sub.iqf_incoming_qty,
                    'iqf_accepted_qty': sub.accepted_qty,
                    'iqf_rejection_qty': sub.rejected_qty,
                    'iqf_acceptance': sub.submission_type == 'FULL_ACCEPT',
                    'iqf_rejection': sub.submission_type in ('FULL_REJECT', 'LOT_REJECTION'),
                    'iqf_few_cases_acceptance': sub.submission_type == 'PARTIAL',
                    'iqf_accepted_qty_verified': True,
                    'iqf_missing_qty': 0,
                    'iqf_physical_qty': sub.iqf_incoming_qty,
                    'iqf_hold_lot': False,
                    'brass_rejection_total_qty': sub.iqf_incoming_qty,
                    'brass_onhold_picking': False,
                    # Live Brass QC release status (SSOT: brass_qc_accepted_qty_verified),
                    # resolved via the same own-lot/child-accept-lot logic as next_process_module.
                    'brass_qc_released': _bq_released,
                    'last_process_module': 'IQF',
                    'next_process_module': _next_stage,
                    'iqf_accepted_tray_scan_status': True,
                    'Moved_to_D_Picker': False,
                    'model_images': images,
                    'status_label': status_label,
                    'submission_type': sub.submission_type,
                    'IQF_pick_remarks': pick_remarks_map.get(sub.lot_id) or '',
                    'tray_qty_list': '',
                    'type_of_input': get_type_of_input_for_batch(batch),
                })

            return Response({'master_data': master_data}, template_name=self.template_name)
        except Exception as e:
            print(f'[IQF COMPLETED PAGE ERROR] {e}')
            traceback.print_exc()
            return Response({'master_data': []}, template_name=self.template_name)


# IQF Accept Table
@method_decorator(login_required, name='dispatch')
class IQFAcceptTablePageView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'IQF/Iqf_AcceptTable.html'

    def get(self, request):
        """IQF Accept Table — shows only ACCEPTED lots from IQF_Submitted."""
        try:
            submitted_qs = IQF_Submitted.objects.select_related(
                'batch_id', 'batch_id__model_stock_no', 'batch_id__version',
                'batch_id__location', 'created_by'
            ).filter(
                is_completed=True, accepted_qty__gt=0
            ).exclude(
                submission_type__in=['FULL_REJECT', 'LOT_REJECTION']
            ).order_by('-created_at')

            # Pre-fetch pick table remarks from TotalStockModel
            all_lot_ids = list(submitted_qs.values_list('lot_id', flat=True))
            pick_remarks_map = dict(
                TotalStockModel.objects.filter(lot_id__in=all_lot_ids)
                .values_list('lot_id', 'IQF_pick_remarks')
            )

            # Live current_stage SSOT (modelmasterapp/stage_service.py) — same field every
            # other Completed Table reads, so this lot shows the identical stage everywhere
            # instead of the hardcoded "IQF" literal this view used to show.
            own_current_stage_map = dict(
                TotalStockModel.objects.filter(lot_id__in=all_lot_ids)
                .values_list('lot_id', 'current_stage')
            )
            partial_lot_ids = list(
                submitted_qs.filter(submission_type='PARTIAL').values_list('lot_id', flat=True)
            )
            parent_to_child_map = {}
            if partial_lot_ids:
                for pal in IQF_PartialAcceptLot.objects.filter(parent_lot_id__in=partial_lot_ids).values('parent_lot_id', 'new_lot_id'):
                    parent_to_child_map[pal['parent_lot_id']] = pal['new_lot_id']
            child_lot_ids = list(parent_to_child_map.values())
            child_current_stage_map = {}
            if child_lot_ids:
                child_current_stage_map = dict(
                    TotalStockModel.objects.filter(lot_id__in=child_lot_ids)
                    .values_list('lot_id', 'current_stage')
                )

            master_data = []
            for sub in submitted_qs:
                batch = sub.batch_id
                if not batch:
                    continue

                # Model images
                images = []
                if batch.model_stock_no:
                    from modelmasterapp.image_utils import sort_images_front_first
                    for img in sort_images_front_first(batch.model_stock_no.images.all()):
                        if img.master_image:
                            images.append(img.master_image.url)
                if not images:
                    images = [static('assets/images/imagePlaceholder.jpg')]

                # Tray info from snapshot
                accept_data = sub.partial_accept_data or sub.full_accept_data
                accept_trays = (accept_data or {}).get('trays', [])
                total_trays = len([t for t in accept_trays if int(t.get('qty', 0)) > 0])

                # Accepted comment from IQF_Accepted_TrayID_Store
                comment_obj = IQF_Accepted_TrayID_Store.objects.filter(lot_id=sub.lot_id).first()
                accepted_comment = comment_obj.accepted_comment if comment_obj else ''

                if sub.submission_type == 'PARTIAL':
                    _child_lot_id = parent_to_child_map.get(sub.lot_id)
                    _live_current = (
                        child_current_stage_map.get(_child_lot_id) if _child_lot_id else None
                    ) or own_current_stage_map.get(sub.lot_id)
                else:
                    _live_current = own_current_stage_map.get(sub.lot_id)
                _current_stage_display = _live_current or 'IQF'

                master_data.append({
                    'batch_id': batch.batch_id,
                    'stock_lot_id': sub.lot_id,
                    'iqf_last_process_date_time': sub.created_at,
                    'plating_stk_no': batch.plating_stk_no or '',
                    'polishing_stk_no': batch.polishing_stk_no or '',
                    'plating_color': batch.plating_color or '',
                    'polish_finish': batch.polish_finish or '',
                    'location__location_name': batch.location.location_name if batch.location else '',
                    'tray_type': batch.tray_type or '',
                    'tray_capacity': batch.tray_capacity or 0,
                    'no_of_trays': total_trays,
                    'iqf_accepted_qty': sub.accepted_qty,
                    'iqf_rejection_qty': sub.rejected_qty,
                    'iqf_acceptance': sub.submission_type == 'FULL_ACCEPT',
                    'iqf_rejection': sub.submission_type in ('FULL_REJECT', 'LOT_REJECTION'),
                    'iqf_few_cases_acceptance': sub.submission_type == 'PARTIAL',
                    'iqf_accepted_qty_verified': True,
                    'iqf_missing_qty': 0,
                    'iqf_physical_qty': sub.iqf_incoming_qty,
                    'brass_onhold_picking': False,
                    'last_process_module': _current_stage_display,
                    'iqf_accepted_tray_scan_status': True,
                    'Moved_to_D_Picker': False,
                    'model_images': images,
                    'status_label': 'ACCEPT' if sub.submission_type == 'FULL_ACCEPT' else 'PARTIAL',
                    'submission_type': sub.submission_type,
                    'accepted_comment': accepted_comment or '',
                    'IQF_pick_remarks': pick_remarks_map.get(sub.lot_id) or '',
                    'tray_qty_list': '',
                    'type_of_input': get_type_of_input_for_batch(batch),
                })

            return Response({'master_data': master_data}, template_name=self.template_name)
        except Exception as e:
            print(f'[IQF ACCEPT TABLE ERROR] {e}')
            traceback.print_exc()
            return Response({'master_data': []}, template_name=self.template_name)


# IQF - Reject table
@method_decorator(login_required, name='dispatch')
class IQFRejectionTableView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'IQF/Iqf_RejectTable.html'

    def get(self, request):
        """IQF Rejection Table — shows all lots with IQF rejections (partial or full).
        Backend computes everything, frontend is pure render.
        """
        try:
            submitted_qs = IQF_Submitted.objects.select_related(
                'batch_id', 'batch_id__model_stock_no', 'batch_id__version',
                'batch_id__location', 'created_by'
            ).filter(
                rejected_qty__gt=0, is_completed=True
            ).order_by('-created_at')

            submitted_list = list(submitted_qs)

            # Pre-fetch lot IDs that have IQFTrayId records (for delink checkbox)
            all_lot_ids = [sub.lot_id for sub in submitted_list]
            partial_reject_lot_map = {}
            for sub in submitted_list:
                if sub.submission_type == 'PARTIAL' and sub.partial_reject_data:
                    reject_child_lot_id = sub.partial_reject_data.get('rejected_lot_id')
                    if reject_child_lot_id:
                        partial_reject_lot_map[sub.lot_id] = reject_child_lot_id
            partial_reject_lot_map.update(dict(
                IQF_PartialRejectLot.objects.filter(parent_lot_id__in=all_lot_ids)
                .values_list('parent_lot_id', 'new_lot_id')
            ))
            active_tray_lot_ids = list(set(all_lot_ids + list(partial_reject_lot_map.values())))
            lots_with_trays = set(
                IQFTrayId.objects.filter(
                    lot_id__in=active_tray_lot_ids, delink_tray=False
                ).values_list('lot_id', flat=True).distinct()
            )

            # Pre-fetch pick table remarks from TotalStockModel
            pick_remarks_map = dict(
                TotalStockModel.objects.filter(lot_id__in=all_lot_ids)
                .values_list('lot_id', 'IQF_pick_remarks')
            )

            master_data = []
            for sub in submitted_list:
                batch = sub.batch_id
                if not batch:
                    continue

                # Model images
                images = []
                if batch.model_stock_no:
                    from modelmasterapp.image_utils import sort_images_front_first
                    for img in sort_images_front_first(batch.model_stock_no.images.all()):
                        if img.master_image:
                            images.append(img.master_image.url)
                if not images:
                    images = [static('assets/images/imagePlaceholder.jpg')]

                # Rejection reason letters (first char of each reason)
                rejection_reason_letters = []
                if sub.rejection_details:
                    for rd in sub.rejection_details:
                        reason_text = rd.get('reason_text', '')
                        if reason_text:
                            rejection_reason_letters.append(reason_text[0].upper())

                # Reject tray count from snapshot
                reject_data = sub.partial_reject_data or sub.full_reject_data
                reject_trays = (reject_data or {}).get('trays', [])
                no_of_trays = len([t for t in reject_trays if int(t.get('qty', 0)) > 0])

                # Lot rejection flag + comment
                is_lot_rejection = sub.submission_type in ('LOT_REJECTION',)

                # Status label
                if sub.submission_type in ('FULL_REJECT', 'LOT_REJECTION'):
                    status_label = 'REJECT'
                elif sub.submission_type == 'PARTIAL':
                    status_label = 'PARTIAL'
                else:
                    status_label = sub.submission_type

                delink_lot_id = partial_reject_lot_map.get(sub.lot_id, sub.lot_id)
                reject_tray_ids = [
                    t.get('tray_id', '') for t in reject_trays
                    if int(t.get('qty', 0) or 0) > 0
                ]
                tray_type, tray_capacity = _resolve_tray_type_capacity_from_trays(
                    reject_tray_ids,
                    lot_ids=[sub.lot_id, delink_lot_id],
                    fallback_type=batch.tray_type or '',
                    fallback_capacity=batch.tray_capacity or 0,
                )

                master_data.append({
                    'batch_id': batch.batch_id,
                    'stock_lot_id': sub.lot_id,
                    'delink_lot_id': delink_lot_id,
                    'iqf_last_process_date_time': sub.created_at,
                    'plating_stk_no': batch.plating_stk_no or '',
                    'polishing_stk_no': batch.polishing_stk_no or '',
                    'plating_color': batch.plating_color or '',
                    'polish_finish': batch.polish_finish or '',
                    'location__location_name': batch.location.location_name if batch.location else '',
                    'tray_type': tray_type,
                    'tray_capacity': tray_capacity,
                    'no_of_trays': no_of_trays,
                    'iqf_rejection_total_qty': sub.rejected_qty,
                    # Reject-table Lot Qty is the quantity represented by this rejected
                    # result, not the parent IQF incoming/original lot quantity.
                    'reject_lot_qty': int(sub.rejected_qty or 0),
                    'brass_rejection_total_qty': sub.iqf_incoming_qty,
                    'iqf_missing_qty': 0,
                    'model_images': images,
                    'rejection_reason_letters': rejection_reason_letters,
                    'batch_rejection': is_lot_rejection,
                    'lot_rejected_comment': sub.remarks or '',
                    'dp_missing_qty': 0,
                    'tray_id_in_trayid': delink_lot_id in lots_with_trays,
                    'status_label': status_label,
                    'submission_type': sub.submission_type,
                    # Original lot quantity BEFORE any rejection (preferred for UI display)
                    'original_lot_qty': int(sub.original_lot_qty or (sub.batch_id.total_batch_quantity if sub.batch_id and getattr(sub.batch_id, 'total_batch_quantity', None) else 0)),
                    # Accepted qty from this submission — feeds the "Accept Qty" column and the
                    # View-modal's Lot Qty display (template reads data.iqf_accepted_qty).
                    'iqf_accepted_qty': int(sub.accepted_qty or 0),
                    'IQF_pick_remarks': pick_remarks_map.get(sub.lot_id) or '',
                    'type_of_input': get_type_of_input_for_batch(batch),
                })

            # Pagination
            page_number = request.GET.get('page', 1)
            paginator = Paginator(master_data, 20)
            page_obj = paginator.get_page(page_number)

            return Response({
                'master_data': page_obj.object_list,
                'page_obj': page_obj,
            }, template_name=self.template_name)
        except Exception as e:
            print(f'[IQF REJECTION TABLE ERROR] {e}')
            traceback.print_exc()
            return Response({'master_data': [], 'page_obj': None}, template_name=self.template_name)


# ── IQF Verify Trays Confirm — Step 2 of PARTIAL flow ──
# GET: returns saved IQF_Submitted data for confirmation popup
# POST: finalizes the submission — sets movement flags for Brass QC
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def iqf_verify_trays_confirm(request):
    lot_id = request.query_params.get('lot_id') if request.method == 'GET' else (request.data or {}).get('lot_id')
    if not lot_id:
        return Response({'success': False, 'error': 'Missing lot_id'}, status=400)

    try:
        ts = TotalStockModel.objects.get(lot_id=lot_id)
    except TotalStockModel.DoesNotExist:
        return Response({'success': False, 'error': f'Lot {lot_id} not found'}, status=404)

    # Guard: lot must be in onhold (VERIFY) state
    if not ts.iqf_onhold_picking:
        return Response({'success': False, 'error': f'Lot {lot_id} is not in IQF verification state'}, status=400)

    sub = IQF_Submitted.objects.filter(lot_id=lot_id).first()
    if not sub:
        return Response({'success': False, 'error': f'No IQF submission found for lot {lot_id}'}, status=404)

    if request.method == 'GET':
        # Return summary data for the confirmation popup
        reject_trays = []
        reject_data = sub.partial_reject_data or sub.full_reject_data
        if reject_data and reject_data.get('trays'):
            reject_trays = reject_data['trays']

        accept_trays = []
        accept_data = sub.partial_accept_data or sub.full_accept_data
        if accept_data and accept_data.get('trays'):
            accept_trays = accept_data['trays']

        # ── Compute freed (delinked) trays dynamically from stored snapshots ──
        freed_trays = []
        original_data = sub.original_data or {}
        original_trays = original_data.get('trays', [])
        if original_trays:
            accept_tray_ids = {t.get('tray_id', '') for t in accept_trays}
            reject_tray_ids = {t.get('tray_id', '') for t in reject_trays}
            for t in original_trays:
                tid = t.get('tray_id', '')
                if tid and tid not in accept_tray_ids and tid not in reject_tray_ids:
                    freed_trays.append({'tray_id': tid, 'qty': t.get('qty', 0)})

        return Response({
            'success': True,
            'lot_id': lot_id,
            'submission_type': sub.submission_type,
            'iqf_incoming_qty': sub.iqf_incoming_qty,
            'accepted_qty': sub.accepted_qty,
            'rejected_qty': sub.rejected_qty,
            'reject_trays': reject_trays,
            'accept_trays': accept_trays,
            'freed_trays': freed_trays,
            'rejection_details': sub.rejection_details or [],
        })

    # POST — finalize the submission
    with transaction.atomic():
        ts.iqf_few_cases_acceptance = True
        ts.iqf_onhold_picking = False
        ts.send_brass_qc = True
        ts.send_brass_audit_to_iqf = False  # Now remove from IQF pick table
        ts.iqf_accepted_qty_verified = False  # Reset for fresh cycle on re-entry
        ts.next_process_module = 'Brass QC'
        # ✅ FIX: Reset Brass QC fields for fresh cycle when lot returns from IQF
        ts.brass_qc_accepted_qty_verified = False
        ts.brass_qc_accptance = False
        ts.brass_qc_rejection = False
        ts.brass_qc_few_cases_accptance = False
        ts.brass_draft = False
        ts.brass_onhold_picking = False
        ts.brass_accepted_tray_scan_status = False
        ts.brass_physical_qty = 0
        ts.brass_missing_qty = 0
        ts.brass_audit_rejection = False
        ts.save(update_fields=[
            'iqf_few_cases_acceptance', 'iqf_onhold_picking',
            'send_brass_qc', 'send_brass_audit_to_iqf', 'iqf_accepted_qty_verified', 'next_process_module',
            'brass_qc_accepted_qty_verified', 'brass_qc_accptance', 'brass_qc_rejection',
            'brass_qc_few_cases_accptance', 'brass_draft', 'brass_onhold_picking',
            'brass_accepted_tray_scan_status', 'brass_physical_qty', 'brass_missing_qty',
            'brass_audit_rejection',
        ])
        print(f'[IQF VERIFY CONFIRM] lot={lot_id} finalized: few_cases=True, onhold=False, send_brass_qc=True, send_brass_audit_to_iqf=False')

    return Response({
        'success': True,
        'message': 'IQF verification completed. Lot moved to Brass QC.',
        'lot_id': lot_id,
    })


# Persist UI 'lot verified' checkbox state so it survives page refresh
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def iqf_toggle_verified(request):
    """Toggle or set the `iqf_accepted_qty_verified` flag on TotalStockModel for a lot.

    Expects JSON: { "lot_id": "LID...", "verified": true }
    """
    try:
        data = request.data
        lot_id = data.get('lot_id')
        verified = data.get('verified')
        if not lot_id:
            return Response({'success': False, 'error': 'Missing lot_id'}, status=400)

        ts = TotalStockModel.objects.filter(lot_id=lot_id).first()
        if not ts:
            return Response({'success': False, 'error': 'Lot not found'}, status=404)

        # Only allow setting to True/False; coerce safely
        ts.iqf_accepted_qty_verified = bool(verified)
        ts.save(update_fields=['iqf_accepted_qty_verified'])

        return Response({'success': True, 'lot_id': lot_id, 'iqf_accepted_qty_verified': ts.iqf_accepted_qty_verified})
    except Exception as e:
        print('[IQF TOGGLE VERIFIED ERROR]', str(e))
        traceback.print_exc()
        return Response({'success': False, 'error': 'Server error'}, status=500)


# ── IQF Pick Table Inline Remarks — Save to TotalStockModel ──
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def iqf_save_pick_remark(request):
    """Save inline remarks entered in IQF pick table.

    Expects JSON: { "lot_id": "LID...", "IQF_pick_remarks": "text" }
    Saves to: TotalStockModel.IQF_pick_remarks
    """
    try:
        lot_id = (request.data.get('lot_id') or '').strip()
        remark = request.data.get('IQF_pick_remarks')

        if not lot_id:
            return Response({'success': False, 'error': 'Missing lot_id'}, status=400)

        if remark is None:
            return Response({'success': False, 'error': 'Remark field not found'}, status=400)

        remark = str(remark).strip()[:100]  # Enforce max_length=100

        ts = TotalStockModel.objects.filter(lot_id=lot_id).first()
        if not ts:
            return Response({'success': False, 'error': 'Lot not found'}, status=404)

        ts.IQF_pick_remarks = remark
        ts.save(update_fields=['IQF_pick_remarks'])

        print(f'[IQF SAVE REMARK] lot={lot_id}, remark={remark[:50]}')

        return Response({
            'success': True,
            'message': 'Remark saved successfully',
            'IQF_pick_remarks': remark,
            'remarks_saved': bool(remark),
        })
    except Exception as e:
        print('[IQF SAVE REMARK ERROR]', str(e))
        traceback.print_exc()
        return Response({'success': False, 'error': 'Server error'}, status=500)


# ── IQF Delete Lot — Remove lot from IQF processing queue ──
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def iqf_delete_lot(request):
    """Remove a verified lot from the IQF pick table queue.

    Only lots with iqf_accepted_qty_verified=True (can_delete=True) are eligible.
    Resets all IQF-specific flags and clears IQF data so the lot can be re-evaluated.

    Expects JSON: { "lot_id": "LID..." }
    """
    try:
        lot_id = request.data.get('lot_id', '').strip()
        if not lot_id:
            return Response({'success': False, 'error': 'Missing lot_id'}, status=400)

        ts = TotalStockModel.objects.filter(lot_id=lot_id).first()
        if not ts:
            return Response({'success': False, 'error': 'Lot not found'}, status=404)

        # Enforce can_delete gate: only verified lots may be deleted
        if not ts.iqf_accepted_qty_verified:
            return Response({'success': False, 'error': 'Lot quantity has not been verified. Cannot delete.'}, status=403)

        # Clear all IQF-specific records for this lot
        IQF_Submitted.objects.filter(lot_id=lot_id).delete()
        IQF_Draft_Store.objects.filter(lot_id=lot_id).delete()
        IQF_Accepted_TrayID_Store.objects.filter(lot_id=lot_id).delete()
        IQF_Accepted_TrayScan.objects.filter(lot_id=lot_id).delete()
        IQF_Rejected_TrayScan.objects.filter(lot_id=lot_id).delete()
        IQF_Rejection_ReasonStore.objects.filter(lot_id=lot_id).delete()
        IQFTrayId.objects.filter(lot_id=lot_id).delete()
        IQF_OptimalDistribution_Draft.objects.filter(lot_id=lot_id).delete()

        # Reset IQF flags on TotalStockModel — lot leaves the IQF queue
        ts.iqf_acceptance = False
        ts.iqf_rejection = False
        ts.iqf_few_cases_acceptance = False
        ts.iqf_accepted_qty_verified = False
        ts.iqf_onhold_picking = False
        ts.iqf_missing_qty = 0
        ts.iqf_physical_qty = 0
        ts.iqf_physical_qty_edited = False
        ts.iqf_accepted_qty = 0
        ts.send_brass_audit_to_iqf = False
        ts.save(update_fields=[
            'iqf_acceptance', 'iqf_rejection', 'iqf_few_cases_acceptance',
            'iqf_accepted_qty_verified', 'iqf_onhold_picking',
            'iqf_missing_qty', 'iqf_physical_qty', 'iqf_physical_qty_edited',
            'iqf_accepted_qty', 'send_brass_audit_to_iqf',
        ])

        print(f'[IQF DELETE LOT] Lot {lot_id} removed from IQF queue by {request.user}')
        return Response({'success': True, 'lot_id': lot_id, 'message': 'Lot removed from IQF queue successfully.'})
    except Exception as e:
        print('[IQF DELETE LOT ERROR]', str(e))
        traceback.print_exc()
        return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)


# ── IQF Accepted Tray Slots — Backend computes, frontend renders ──
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def iqf_accepted_tray_slots(request):
    """Compute accepted tray scan slots based on IQF rejection total.

    SINGLE SOURCE OF TRUTH: IQFTrayId for tray info, Brass QC/Audit for rw_qty.
    Frontend is pure render — zero calculations.

    Query params: ?lot_id=X&iqf_rejection_total=Y
    Returns: { success, rw_qty, accepted_qty, rejected_qty, tray_capacity, slots: [{slot_no, qty, is_top_tray}] }
    """
    lot_id = request.GET.get('lot_id')
    iqf_rejection_total = request.GET.get('iqf_rejection_total', '0')

    if not lot_id:
        return Response({'success': False, 'error': 'Missing lot_id'}, status=400)

    try:
        iqf_rejection_total = int(iqf_rejection_total)
    except (ValueError, TypeError):
        return Response({'success': False, 'error': 'iqf_rejection_total must be integer'}, status=400)

    if iqf_rejection_total < 0:
        return Response({'success': False, 'error': 'iqf_rejection_total must be non-negative'}, status=400)

    try:
        # 1. Resolve rw_qty (IQF incoming) — SINGLE SOURCE OF TRUTH
        audit_store = Brass_Audit_Rejection_ReasonStore.objects.filter(lot_id=lot_id).order_by('-id').first()
        qc_store = Brass_QC_Rejection_ReasonStore.objects.filter(lot_id=lot_id).order_by('-id').first()
        rw_qty = 0
        if audit_store and getattr(audit_store, 'total_rejection_quantity', None) is not None:
            rw_qty = audit_store.total_rejection_quantity
        elif qc_store and getattr(qc_store, 'total_rejection_quantity', None) is not None:
            rw_qty = qc_store.total_rejection_quantity

        # ── Fallback: if rw_qty still 0, count from actual tray scan data ──
        # Handles FULL_REJECT lots where reason stores may not exist
        if rw_qty <= 0:
            _tray_qty_map = {}
            for row in Brass_Audit_Rejected_TrayScan.objects.filter(lot_id=lot_id):
                _tid = getattr(row, 'rejected_tray_id', None) or getattr(row, 'tray_id', None) or ''
                if _tid:
                    _tray_qty_map[_tid] = _tray_qty_map.get(_tid, 0) + (int(row.rejected_tray_quantity or 0))
            if not _tray_qty_map:
                for row in Brass_QC_Rejected_TrayScan.objects.filter(lot_id=lot_id):
                    _tid = getattr(row, 'rejected_tray_id', None) or getattr(row, 'tray_id', None) or ''
                    if _tid:
                        _tray_qty_map[_tid] = _tray_qty_map.get(_tid, 0) + (int(row.rejected_tray_quantity or 0))
            if not _tray_qty_map:
                for row in IQFTrayId.objects.filter(lot_id=lot_id, delink_tray=False):
                    _qty = row.tray_quantity or 0
                    if _qty > 0:
                        _tray_qty_map[row.tray_id] = _tray_qty_map.get(row.tray_id, 0) + _qty
            if not _tray_qty_map:
                from .services.selectors import get_current_trays
                _tray_data, _source, _total_qty = get_current_trays(lot_id)
                if _tray_data:
                    rw_qty = _total_qty
                    print(f'[IQF TRAY SLOTS] Fallback: resolved rw_qty={rw_qty} from {len(_tray_data)} trays (source={_source})')


        # ── Re-flagged lot: override rw_qty from IQF_Submitted (SINGLE SOURCE OF TRUTH) ──
        try:
            _iqf_sub = IQF_Submitted.objects.filter(lot_id=lot_id, is_completed=True).last()
            if _iqf_sub:
                _override_rw = None
                # SKIP override for unfinalized PARTIAL — lot is still mid-flow in IQF
                if _iqf_sub.submission_type == 'PARTIAL' and not _iqf_sub.partial_reject_data:
                    print(f'[IQF TRAY SLOTS] Unfinalized PARTIAL for {lot_id} — skipping rw_qty override')
                elif _iqf_sub.submission_type in ('FULL_ACCEPT', 'PARTIAL'):
                    if _iqf_sub.submission_type == 'FULL_ACCEPT' and _iqf_sub.full_accept_data:
                        _src = _iqf_sub.full_accept_data.get('trays', [])
                    elif _iqf_sub.submission_type == 'PARTIAL' and _iqf_sub.partial_accept_data:
                        _src = _iqf_sub.partial_accept_data.get('trays', [])
                    else:
                        _src = []
                    _live = sum(int(t.get('qty', 0)) for t in _src if int(t.get('qty', 0)) > 0)
                    if _live > 0:
                        _override_rw = _live
                elif _iqf_sub.submission_type in ('FULL_REJECT', 'LOT_REJECTION'):
                    _override_rw = _iqf_sub.iqf_incoming_qty
                if _override_rw is not None and _override_rw > 0:
                    print(f'[IQF TRAY SLOTS] Re-flagged lot {lot_id}: overriding rw_qty from {rw_qty} to {_override_rw}')
                    rw_qty = _override_rw
        except Exception as _e:
            print(f'[IQF TRAY SLOTS] Re-flagged lot override check failed: {_e}')

        # 2. Resolve tray capacity from IQFTrayId → BrassTrayId → ModelMaster
        # Resolved unconditionally so every response branch (including empty-slot
        # early returns) reports the real capacity instead of leaving it blank.
        ts = TotalStockModel.objects.filter(lot_id=lot_id).select_related('batch_id', 'batch_id__model_stock_no').first()
        if not ts:
            return Response({'success': False, 'error': 'Lot not found'}, status=404)

        def _resolve_capacity():
            """Resolve IQF tray capacity from the product series."""
            return _get_series_tray_capacity(ts.batch_id)

        tray_capacity = _resolve_capacity()

        if rw_qty <= 0:
            return Response({'success': True, 'rw_qty': 0, 'accepted_qty': 0, 'rejected_qty': 0, 'tray_capacity': tray_capacity, 'slots': []})

        if iqf_rejection_total > rw_qty:
            return Response({'success': False, 'error': 'Rejection total exceeds RW qty', 'rw_qty': rw_qty}, status=400)

        accepted_qty = rw_qty - iqf_rejection_total
        rejected_qty = iqf_rejection_total

        if accepted_qty <= 0:
            return Response({
                'success': True,
                'rw_qty': rw_qty,
                'accepted_qty': 0,
                'rejected_qty': rejected_qty,
                'tray_capacity': tray_capacity,
                'slots': [],
            })

        # 3. Compute accepted tray slots — REUSE surviving trays after rejection
        #    RULE: Consume rejection from smallest trays first. Survivors are reused.
        #    Only add new slots when accepted qty exceeds reusable tray capacity.

        # Fetch original trays using service layer
        # CRITICAL: ONLY current lot trays, NEVER parent lot data
        def _fetch_original_trays():
            from .services.selectors import get_current_trays
            
            _tray_data, _source, _total_qty = get_current_trays(lot_id)
            if _tray_data:
                return [
                    {'tray_id': t.get('tray_id', ''), 'qty': t.get('qty', 0)}
                    for t in _tray_data
                ]
            return []

        original_trays = _fetch_original_trays()
        print(f'[IQF TRAY SLOTS] original_trays resolved: count={len(original_trays)}, trays={original_trays}')

        slots = []
        slot_no = 1
        max_reuse_limit = 0

        if original_trays:
            # ── 1. Calculate trays needed for rejected amount ──
            reject_trays_needed = math.ceil(iqf_rejection_total / tray_capacity) if tray_capacity > 0 else 0
            
            # ── 2. Derive reusable slots available for accepted amount ──
            total_original_trays = len(original_trays)
            max_reuse_limit = max(0, total_original_trays - reject_trays_needed)
            
            # ── 3. Trays needed for accepted quantity ──
            required_accept_slots = math.ceil(accepted_qty / tray_capacity) if tray_capacity > 0 else 0
            
            reusable_slots_to_create = min(max_reuse_limit, required_accept_slots)
            new_trays_needed = max(0, required_accept_slots - reusable_slots_to_create)

            print(f'[IQF TRAY SLOTS] Reject qty: {iqf_rejection_total} requires {reject_trays_needed} trays.')
            print(f'[IQF TRAY SLOTS] Total original trays: {total_original_trays}, Reusable limit: {max_reuse_limit}')
            print(f'[IQF TRAY SLOTS] Accept qty: {accepted_qty}, needs {required_accept_slots} slots ({reusable_slots_to_create} reusable, {new_trays_needed} new).')

            remaining_acc = accepted_qty
            
            # Helper to generate slots
            slots_to_generate = []
            if remaining_acc > 0:
                rem_new = remaining_acc % tray_capacity
                full_new = remaining_acc // tray_capacity
                
                # Top tray (partial) goes first
                if rem_new > 0:
                    slots_to_generate.append({'qty': rem_new, 'is_top': True})
                # Full trays follow
                for _ in range(full_new):
                    slots_to_generate.append({'qty': tray_capacity, 'is_top': False})

            # Assign statuses based on available reusable slots
            for i, slot_data in enumerate(slots_to_generate):
                status = 'scan_required' if i < reusable_slots_to_create else 'new'
                slots.append({
                    'slot_no': slot_no,
                    'qty': slot_data['qty'],
                    'is_top_tray': slot_data['is_top'],
                    'tray_id': '',
                    'status': status,
                })
                slot_no += 1

        else:
            # Fallback: no original trays found — create abstract slots (new trays)
            full_trays = accepted_qty // tray_capacity
            remainder = accepted_qty % tray_capacity

            # Top tray (partial) goes first, then full-capacity trays.
            if remainder > 0:
                slots.append({
                    'slot_no': slot_no,
                    'qty': remainder,
                    'is_top_tray': True,
                    'tray_id': '',
                    'status': 'new',
                })
                slot_no += 1

            for _ in range(full_trays):
                slots.append({
                    'slot_no': slot_no,
                    'qty': tray_capacity,
                    'is_top_tray': False,
                    'tray_id': '',
                    'status': 'new',
                })
                slot_no += 1

            if remainder == 0 and slots:
                slots[0]['is_top_tray'] = True

        print(f'[IQF TRAY SLOTS] lot={lot_id}, rw={rw_qty}, rej={rejected_qty}, '
              f'acc={accepted_qty}, cap={tray_capacity}, slots={len(slots)}, max_reuse_limit={max_reuse_limit}')

        return Response({
            'success': True,
            'rw_qty': rw_qty,
            'accepted_qty': accepted_qty,
            'rejected_qty': rejected_qty,
            'tray_capacity': tray_capacity,
            'slots': slots,
            'max_reuse_limit': max_reuse_limit,
        })

    except Exception as e:
        print('[IQF TRAY SLOTS ERROR]', str(e))
        traceback.print_exc()
        return Response({'success': False, 'error': 'Server error'}, status=500)


# ── IQF Validate Tray Scan — Backend decides, frontend renders ──
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def iqf_validate_tray_scan(request):
    """Validate a scanned tray ID against the current IQF lot.

    SINGLE SOURCE OF TRUTH: Backend checks format, lot membership, delink status.
    Frontend is pure render — zero validation logic.

    Query params: ?lot_id=X&tray_id=Y
    Returns: { success, status: 'valid'|'invalid_format'|'valid_lot'|'delink', message }
    """
    import re

    lot_id = request.GET.get('lot_id', '').strip()
    tray_id = request.GET.get('tray_id', '').strip()

    if not lot_id:
        return Response({'success': False, 'error': 'Missing lot_id'}, status=400)
    if not tray_id:
        return Response({'success': False, 'error': 'Missing tray_id'}, status=400)

    # ═══ TRAY TYPE COMPATIBILITY CHECK ═══
    def get_tray_category(tray_type_code):
        """Extract category (Jumbo/Normal) from tray type code: J*->Jumbo, N*->Normal"""
        if not tray_type_code:
            return None
        code = str(tray_type_code).upper().strip()
        if code.startswith('J'):
            return 'Jumbo'
        elif code.startswith('N'):
            return 'Normal'
        return None

    try:
        stock = TotalStockModel.objects.select_related('batch_id__model_stock_no').get(lot_id=lot_id)
        # Get model's required tray type from ModelMaster
        model_tray_type = stock.batch_id.model_stock_no.tray_type if stock.batch_id and stock.batch_id.model_stock_no else None
        if model_tray_type:
            model_category = get_tray_category(model_tray_type.tray_type)
        else:
            model_category = None
    except TotalStockModel.DoesNotExist:
        return Response({'success': False, 'error': 'Lot not found'}, status=404)
    
    # ── RULE 1: Length check (frontend enforces 9-char trigger, backend double-checks) ──
    if len(tray_id) != 9:
        return Response({
            'success': True,
            'status': 'invalid_format',
            'message': 'Invalid Tray ID — must be 9 characters',
        })

    # ── RULE 2: Format validation [PREFIX]-[ALPHANUMERIC] e.g. NB-A00001 ──
    if not re.match(r'^[A-Z]{2}-[A-Z0-9]{6}$', tray_id, re.IGNORECASE):
        return Response({
            'success': True,
            'status': 'invalid_format',
            'message': 'Invalid Tray ID',
        })

    # ── RULE 2.5: Block trays that are REJECTED in Input Screening ──
    # Check current partial-reject allocations and legacy IPTrayId records before
    # any branch can classify the tray as New Tray.
    if _is_input_screening_rejected_tray_active(tray_id):
        print(f"🚫 [TRAY_VALIDATION] {tray_id}: active IS-rejected tray — cannot be reused or accepted")
        return Response({
            'success': True,
            'status': 'invalid_format',
            'message': 'Tray rejected in Input Screening',
            'tray_id': tray_id,
        })

    # ── RULE 3a: Compute max reuse limit from rejection qty ──
    iqf_rej_str = request.GET.get('iqf_rejection_total', '')
    reuse_count_str = request.GET.get('reuse_count', '0')
    max_reuse_limit = 0   # 0 = no reuse restriction active
    reuse_count = 0
    if iqf_rej_str:
        try:
            iqf_rej = int(iqf_rej_str)
            if iqf_rej > 0:
                # Resolve tray capacity for this lot
                _cap = 16  # safe default
                iqf_cap_tray = IQFTrayId.objects.filter(lot_id=lot_id, delink_tray=False).exclude(tray_capacity__isnull=True).exclude(tray_capacity=0).first()
                if iqf_cap_tray and iqf_cap_tray.tray_capacity and iqf_cap_tray.tray_capacity > 0:
                    _cap = iqf_cap_tray.tray_capacity
                else:
                    brass_cap_tray = BrassTrayId.objects.filter(lot_id=lot_id, delink_tray=False).exclude(tray_capacity__isnull=True).exclude(tray_capacity=0).first()
                    if brass_cap_tray and brass_cap_tray.tray_capacity and brass_cap_tray.tray_capacity > 0:
                        _cap = brass_cap_tray.tray_capacity
                    else:
                        _ts = TotalStockModel.objects.filter(lot_id=lot_id).select_related('batch_id').first()
                        if _ts and _ts.batch_id and _ts.batch_id.tray_capacity and _ts.batch_id.tray_capacity > 0:
                            _cap = _ts.batch_id.tray_capacity
                _total_trays = IQFTrayId.objects.filter(lot_id=lot_id, delink_tray=False).count()
                if _total_trays == 0:
                    _total_trays = BrassTrayId.objects.filter(lot_id=lot_id, delink_tray=False).count()
                _rejection_trays = math.ceil(iqf_rej / _cap) if _cap > 0 else 0
                max_reuse_limit = max(0, _total_trays - _rejection_trays)
                reuse_count = max(0, int(reuse_count_str))
        except (ValueError, TypeError):
            pass

    # ── RULE 3: Lot membership check ──
    # Check IQFTrayId first (primary), then BrassTrayId (fallback)
    iqf_match = IQFTrayId.objects.filter(lot_id=lot_id, tray_id=tray_id, delink_tray=False).first()
    if iqf_match:
        # ═══ TRAY TYPE COMPATIBILITY CHECK ═══
        if model_category:
            tray_category = get_tray_category(iqf_match.tray_type)
            if tray_category and tray_category != model_category:
                return Response({
                    'success': True,
                    'status': 'invalid_format',
                    'message': f'Tray type mismatch: model requires {model_category} tray, but scanned tray is {tray_category}',
                    'tray_id': tray_id,
                })
        
        if max_reuse_limit > 0 and reuse_count >= max_reuse_limit:
            return Response({
                'success': True,
                'status': 'no_reuse',
                'message': 'Reuse limit reached. Please scan new trays.',
                'tray_id': tray_id,
            })
        return Response({
            'success': True,
            'status': 'valid_lot',
            'message': 'Valid Tray',
            'tray_id': tray_id,
            'tray_qty': int(getattr(iqf_match, 'tray_quantity', 0) or 0),
            'top_tray': bool(getattr(iqf_match, 'top_tray', False)),
        })

    # Check BrassTrayId for the same lot — ONLY rejected trays are valid for IQF reuse
    # (Accepted Brass QC trays are committed to the acceptance flow and must not be reused in IQF)
    brass_match = BrassTrayId.objects.filter(lot_id=lot_id, tray_id=tray_id, delink_tray=False).first()
    if brass_match:
        # ✅ FIX: Only allow BrassTrayId trays that are REJECTED (IQF processes rejected Brass QC trays)
        # Accepted/top trays from Brass QC are already committed and must be blocked
        if not brass_match.rejected_tray:
            print(f"🚫 [TRAY_VALIDATION] {tray_id}: Found in BrassTrayId for lot {lot_id} but NOT rejected (accepted/top tray) — blocking")
            return Response({
                'success': True,
                'status': 'invalid_format',
                'message': 'Tray already accepted in Brass QC — cannot reuse',
                'tray_id': tray_id,
            })
        
        # ═══ TRAY TYPE COMPATIBILITY CHECK ═══
        if model_category:
            tray_category = get_tray_category(brass_match.tray_type)
            if tray_category and tray_category != model_category:
                return Response({
                    'success': True,
                    'status': 'invalid_format',
                    'message': f'Tray type mismatch: model requires {model_category} tray, but scanned tray is {tray_category}',
                    'tray_id': tray_id,
                })
        
        if max_reuse_limit > 0 and reuse_count >= max_reuse_limit:
            return Response({
                'success': True,
                'status': 'no_reuse',
                'message': 'Reuse limit reached. Please scan new trays.',
                'tray_id': tray_id,
            })
        return Response({
            'success': True,
            'status': 'valid_lot',
            'message': 'Valid Tray',
            'tray_id': tray_id,
            'tray_qty': int(getattr(brass_match, 'tray_quantity', 0) or 0),
            'top_tray': bool(getattr(brass_match, 'top_tray', False)),
        })
        return Response({
            'success': True,
            'status': 'valid_lot',
            'message': 'Valid Tray',
            'tray_id': tray_id,
            'tray_qty': int(getattr(brass_match, 'tray_quantity', 0) or 0),
            'top_tray': bool(getattr(brass_match, 'top_tray', False)),
        })

    # ── RULE 3b: Check scan/submission data for lot membership ──
    # Handles lots where trays exist in rejection scan data
    # CRITICAL: ONLY check current lot tray data, NEVER parent lot
    from .services.selectors import get_current_trays
    
    _lot_tray_found = False
    _current_tray_data, _source, _total_qty = get_current_trays(lot_id)
    _current_tray_ids = {t.get('tray_id', '').strip() for t in _current_tray_data}
    
    if tray_id in _current_tray_ids:
        _lot_tray_found = True

    if _lot_tray_found:
        print(f"✅ [TRAY_VALIDATION] {tray_id}: Found in current lot {lot_id} (source={_source})")
        if max_reuse_limit > 0 and reuse_count >= max_reuse_limit:
            return Response({
                'success': True,
                'status': 'no_reuse',
                'message': 'Reuse limit reached. Please scan new trays.',
                'tray_id': tray_id,
            })
        return Response({
            'success': True,
            'status': 'valid_lot',
            'message': 'Valid Tray',
            'tray_id': tray_id,
        })

    # ── RULE 4: Not in current lot — check if it's a genuinely NEW tray ──
    # ✅ FIX: A tray is only "new" if it is UNOCCUPIED in the TrayId master table.
    # If it has a lot_id assigned and is not delinked, it is already in use — REJECT.
    from .services.validators import validate_iqf_cross_module_tray_available
    tray_error = validate_iqf_cross_module_tray_available(
        tray_id,
        current_lot_id=lot_id,
    )
    if tray_error:
        print(f"[TRAY_VALIDATION] {tray_id}: {tray_error}")
        return Response({
            'success': True,
            'status': 'invalid_format',
            'message': tray_error,
            'tray_id': tray_id,
        })

    master_tray = TrayId.objects.filter(tray_id=tray_id).first()
    if master_tray:
        # Check if tray is occupied (has a lot_id and is not delinked)
        master_lot = str(master_tray.lot_id or '').strip()
        is_occupied = master_lot not in ('', 'None') and not master_tray.delink_tray
        if is_occupied:
            # Allow if the tray belongs to the SAME lot (own tray reuse)
            if master_lot == lot_id:
                print(f"✅ [TRAY_VALIDATION] {tray_id}: Occupied in TrayId master but SAME lot ({lot_id}) — allowing reuse")
                if max_reuse_limit > 0 and reuse_count >= max_reuse_limit:
                    return Response({
                        'success': True,
                        'status': 'no_reuse',
                        'message': 'Reuse limit reached. Please scan new trays.',
                        'tray_id': tray_id,
                    })
                return Response({
                    'success': True,
                    'status': 'valid_lot',
                    'message': 'Valid Tray',
                    'tray_id': tray_id,
                })
            print(f"🚫 [TRAY_VALIDATION] {tray_id}: Occupied in TrayId master (lot={master_tray.lot_id}, delink={master_tray.delink_tray}) — blocking")
            return Response({
                'success': True,
                'status': 'invalid_format',
                'message': 'Tray already allocated to a lot — cannot reuse',
                'tray_id': tray_id,
            })
        # Tray exists in master but is unoccupied/delinked → valid new tray
        print(f"✅ [TRAY_VALIDATION] {tray_id}: Found in TrayId master, unoccupied — valid new tray")
        return Response({
            'success': True,
            'status': 'delink',
            'message': 'New Tray',
            'tray_id': tray_id,
        })

    # Also check if tray exists in other tray tables (cross-module) but not in master
    # ✅ FIX: Only block ACTIVE (non-delinked) records — delinked trays from BrassQC can be reused in IQF as new trays
    exists_active_elsewhere = (
        IQFTrayId.objects.filter(tray_id=tray_id, delink_tray=False).exists() or
        BrassTrayId.objects.filter(tray_id=tray_id, delink_tray=False).exists()
    )
    if exists_active_elsewhere:
        print(f"🚫 [TRAY_VALIDATION] {tray_id}: Found active in IQFTrayId/BrassTrayId — blocking")
        return Response({
            'success': True,
            'status': 'invalid_format',
            'message': 'Tray already in use in another module — cannot reuse',
            'tray_id': tray_id,
        })

    # If tray exists only in delinked state — it is a free/released tray, allow as new
    exists_delinked = (
        IQFTrayId.objects.filter(tray_id=tray_id, delink_tray=True).exists() or
        BrassTrayId.objects.filter(tray_id=tray_id, delink_tray=True).exists()
    )
    if exists_delinked:
        print(f"✅ [TRAY_VALIDATION] {tray_id}: Found only as delinked in module tables — valid free tray")
        return Response({
            'success': True,
            'status': 'delink',
            'message': 'New Tray',
            'tray_id': tray_id,
        })

    # Tray ID format valid but not found anywhere in the system — not registered
    return Response({
        'success': True,
        'status': 'invalid_format',
        'message': 'Tray not found in system registry',
        'tray_id': tray_id,
    })


# ── IQF Accept Table → Delink & Reject Allocation Modal — Backend SSOT ──
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def iqf_accept_delink_modal(request):
    """Compute tray allocation for the Accept → Delink & Reject Allocation modal.

    Backend is SINGLE SOURCE OF TRUTH — frontend is pure fetch + render.
    Uses the SAME allocation algorithm as iqf_submit_audit PARTIAL flow.

    GET:  ?lot_id=X&iqf_rejection_total=Y&accepted_tray_ids=NB-001,NB-002
    POST: { lot_id, iqf_rejection_total, accepted_tray_ids: [...], delinked_tray_ids: [...] }

    Returns: original_trays with status, max_delink_count, reject_allocation
    """
    # ── Parse parameters from GET or POST ──
    if request.method == 'GET':
        lot_id = request.GET.get('lot_id', '').strip()
        rej_total_str = request.GET.get('iqf_rejection_total', '0')
        acc_ids_raw = request.GET.get('accepted_tray_ids', '')
        del_ids_raw = request.GET.get('delinked_tray_ids', '')
        accepted_tray_ids = [x.strip() for x in acc_ids_raw.split(',') if x.strip()] if acc_ids_raw else []
        delinked_tray_ids = [x.strip() for x in del_ids_raw.split(',') if x.strip()] if del_ids_raw else []
        rej_ids_raw = request.GET.get('rejected_tray_ids', '')
        rejected_tray_ids_in = [x.strip() for x in rej_ids_raw.split(',') if x.strip()] if rej_ids_raw else []
    else:
        payload = request.data or {}
        lot_id = (payload.get('lot_id') or '').strip()
        rej_total_str = str(payload.get('iqf_rejection_total', '0'))
        accepted_tray_ids = payload.get('accepted_tray_ids') or []
        delinked_tray_ids = payload.get('delinked_tray_ids') or []
        rejected_tray_ids_in = payload.get('rejected_tray_ids') or []
        submitted_items = payload.get('items') or []
        remark = (payload.get('remark') or '').strip()

    if not lot_id:
        return Response({'success': False, 'error': 'Missing lot_id'}, status=400)

    from .services.validators import validate_unique_tray_assignments

    normalized_assignments, assignment_error = validate_unique_tray_assignments(
        accepted_tray_ids,
        rejected_tray_ids_in,
        delinked_tray_ids,
    )
    if assignment_error:
        return Response({'success': False, 'error': assignment_error}, status=400)

    accepted_tray_ids = normalized_assignments['accept']
    rejected_tray_ids_in = normalized_assignments['reject']
    delinked_tray_ids = normalized_assignments['delink']

    blocked_tray_id, tray_error = _validate_iqf_assigned_tray_ids(
        lot_id,
        accepted_tray_ids,
        rejected_tray_ids_in,
    )
    if tray_error:
        return Response({
            'success': False,
            'error': tray_error,
            'tray_id': blocked_tray_id,
            'tray_ids': [blocked_tray_id],
            'message': tray_error,
        }, status=400)

    try:
        iqf_rejection_total = int(rej_total_str)
    except (ValueError, TypeError):
        return Response({'success': False, 'error': 'iqf_rejection_total must be integer'}, status=400)

    if iqf_rejection_total < 0:
        return Response({'success': False, 'error': 'iqf_rejection_total must be non-negative'}, status=400)

    try:
        ts = TotalStockModel.objects.filter(lot_id=lot_id).select_related('batch_id').first()
        if not ts:
            return Response({'success': False, 'error': 'Lot not found'}, status=404)

        # ── Resolve rw_qty — MULTI-SOURCE FALLBACK CHAIN ──
        # PRIORITY: IQF_Submitted (current truth) > Brass_Audit > Brass_QC > scan data fallback
        rw_qty = 0
        rw_source = 'unknown'

        # 1. Check IQF_Submitted first (most reliable, set during PARTIAL submit)
        try:
            _iqf_sub = IQF_Submitted.objects.filter(lot_id=lot_id, is_completed=True).last()
            if _iqf_sub and _iqf_sub.iqf_incoming_qty and _iqf_sub.iqf_incoming_qty > 0:
                rw_qty = _iqf_sub.iqf_incoming_qty
                rw_source = 'IQF_Submitted.iqf_incoming_qty'
                print(f'[DELINK RW_QTY] Source: {rw_source}, value: {rw_qty}')
        except Exception as e:
            print(f'[DELINK RW_QTY] IQF_Submitted lookup failed: {e}')

        # 2. Fallback to Brass_Audit_Rejection_ReasonStore
        if rw_qty <= 0:
            audit_store = Brass_Audit_Rejection_ReasonStore.objects.filter(lot_id=lot_id).order_by('-id').first()
            if audit_store and getattr(audit_store, 'total_rejection_quantity', None) is not None:
                rw_qty = audit_store.total_rejection_quantity
                rw_source = 'Brass_Audit_Rejection_ReasonStore'
                print(f'[DELINK RW_QTY] Source: {rw_source}, value: {rw_qty}')

        # 3. Fallback to Brass_QC_Rejection_ReasonStore
        if rw_qty <= 0:
            qc_store = Brass_QC_Rejection_ReasonStore.objects.filter(lot_id=lot_id).order_by('-id').first()
            if qc_store and getattr(qc_store, 'total_rejection_quantity', None) is not None:
                rw_qty = qc_store.total_rejection_quantity
                rw_source = 'Brass_QC_Rejection_ReasonStore'
                print(f'[DELINK RW_QTY] Source: {rw_source}, value: {rw_qty}')

        # 4. Final fallback: calculate from scan data (Brass_QC + Brass_Audit rejected trays)
        if rw_qty <= 0:
            try:
                qc_scan_total = Brass_QC_Rejected_TrayScan.objects.filter(lot_id=lot_id).aggregate(
                    total=Sum(F('rejected_tray_quantity'), output_field=IntegerField())
                )['total'] or 0
                audit_scan_total = Brass_Audit_Rejected_TrayScan.objects.filter(lot_id=lot_id).aggregate(
                    total=Sum(F('rejected_tray_quantity'), output_field=IntegerField())
                )['total'] or 0
                rw_qty = max(qc_scan_total, audit_scan_total)  # Take the one with more data
                if rw_qty > 0:
                    rw_source = f'scan_data (QC:{qc_scan_total}, Audit:{audit_scan_total})'
                    print(f'[DELINK RW_QTY] Source: {rw_source}, value: {rw_qty}')
            except Exception as e:
                print(f'[DELINK RW_QTY] Scan data fallback failed: {e}')

        if rw_qty <= 0:
            return Response({
                'success': False,
                'error': f'Cannot resolve IQF incoming qty — no data from rejection stores or scan data. rw_qty={rw_qty}',
                'rw_source': rw_source,
            }, status=400)

        accepted_qty = rw_qty - iqf_rejection_total
        rejected_qty = iqf_rejection_total

        if accepted_qty < 0:
            return Response({'success': False, 'error': 'Rejection total exceeds RW qty'}, status=400)

        # ── ACCEPT TRAY IDs FLEXIBILITY ──
        # Do NOT validate that accepted_tray_ids must come from original incoming trays.
        # Accepted trays can be: reused (from original list), reused (from other lots), or new.
        # This endpoint only needs accepted_set to mark which ORIGINAL trays are NOT being accepted.
        print(f'[DELINK MODAL] lot={lot_id}, rw_qty={rw_qty}, accepted_tray_ids_count={len(accepted_tray_ids)}, '
              f'iqf_rejection_total={iqf_rejection_total}, accepted_qty={accepted_qty}, rejected_qty={rejected_qty}')

        # ── Resolve global tray capacity ──
        def _resolve_global_cap():
            """Resolve reject/accept allocation capacity from the product series."""
            return _get_series_tray_capacity(ts.batch_id)

        tray_capacity = _resolve_global_cap()

        # ── Resolve per-tray capacity (same as iqf_submit_audit) ──
        def _resolve_tray_cap(iqf_tray_obj):
            """Resolve capacity from the product series for this IQF lot."""
            return _get_series_tray_capacity(ts.batch_id)

        # ── Resolve capacity for a directly-scanned reject tray ID (may be brand new, not in IQFTrayId) ──
        def _resolve_cap_for_tray_id(tid):
            brass = BrassTrayId.objects.filter(tray_id=tid).exclude(
                tray_capacity__isnull=True).exclude(tray_capacity=0).first()
            if brass and brass.tray_capacity and brass.tray_capacity > 0:
                return brass.tray_capacity
            tray_master = TrayId.objects.filter(tray_id=tid).exclude(
                tray_capacity__isnull=True).exclude(tray_capacity=0).first()
            if tray_master and tray_master.tray_capacity and tray_master.tray_capacity > 0:
                return tray_master.tray_capacity
            return tray_capacity

        # ── Get all original lot trays (non-delinked) — MULTI-SOURCE FALLBACK ──
        # PRIMARY: IQFTrayId (DB source of truth)
        all_trays_qs = list(IQFTrayId.objects.filter(lot_id=lot_id, delink_tray=False).order_by('id'))
        original_tray_source = 'IQFTrayId'

        # FALLBACK 1: If no IQFTrayId records, load from IQF_Submitted.iqf_data snapshot
        if not all_trays_qs:
            try:
                iqf_sub = IQF_Submitted.objects.filter(lot_id=lot_id, is_completed=True).last()
                if iqf_sub and iqf_sub.iqf_data and iqf_sub.iqf_data.get('trays'):
                    all_trays_qs = iqf_sub.iqf_data.get('trays', [])
                    original_tray_source = 'IQF_Submitted.iqf_data'
                    print(f'[DELINK ORIGINAL TRAYS] Loaded from IQF_Submitted snapshot: {len(all_trays_qs)} trays')
            except Exception as e:
                print(f'[DELINK ORIGINAL TRAYS] IQF_Submitted fallback failed: {e}')

        # FALLBACK 2: If still empty, load from original_data snapshot
        if not all_trays_qs:
            try:
                iqf_sub = IQF_Submitted.objects.filter(lot_id=lot_id, is_completed=True).last()
                if iqf_sub and iqf_sub.original_data and iqf_sub.original_data.get('trays'):
                    all_trays_qs = iqf_sub.original_data.get('trays', [])
                    original_tray_source = 'IQF_Submitted.original_data'
                    print(f'[DELINK ORIGINAL TRAYS] Loaded from IQF_Submitted.original_data snapshot: {len(all_trays_qs)} trays')
            except Exception as e:
                print(f'[DELINK ORIGINAL TRAYS] original_data fallback failed: {e}')

        # FALLBACK 3: Use service layer to get current lot trays (never parent lot)
        # CRITICAL FIX: No parent lot fallback - only current lot data
        if not all_trays_qs:
            try:
                from .services.selectors import get_current_trays
                _tray_data, _source, _total_qty = get_current_trays(lot_id)
                if _tray_data:
                    all_trays_qs = _tray_data
                    original_tray_source = f'get_current_trays({_source})'
                    print(f'[DELINK ORIGINAL TRAYS] Loaded from service layer ({_source}): {len(all_trays_qs)} trays')
            except Exception as e:
                print(f'[DELINK ORIGINAL TRAYS] service layer fallback failed: {e}')

        print(f'[DELINK ORIGINAL TRAYS] Source={original_tray_source}, Count={len(all_trays_qs)}')

        # ── Extract tray IDs from original trays (handle both objects and dicts) ──
        def _get_tray_id(tray_obj):
            """Extract tray_id from IQFTrayId object or dict snapshot."""
            if isinstance(tray_obj, dict):
                return tray_obj.get('tray_id', '')
            return getattr(tray_obj, 'tray_id', '') or ''

        def _get_tray_qty(tray_obj):
            """Extract original qty from IQFTrayId object or dict snapshot."""
            if isinstance(tray_obj, dict):
                return int(tray_obj.get('qty', 0) or 0)
            qty = getattr(tray_obj, 'tray_quantity', 0) or 0
            return int(qty)

        # ── ACCEPT TRAY IDs FLEXIBILITY: NO STRICT VALIDATION ──
        # accepted_tray_ids from frontend can include:
        # - Reused trays from original list (e.g., NB-A00051 scanned with qty > original)
        # - Reused trays from other lots (e.g., NB-A00151 not in original incoming)
        # - New trays
        # We only care which ORIGINAL trays are NOT in the accepted_set → become reject pool.
        accepted_set = set(accepted_tray_ids)
        delinked_set = set(delinked_tray_ids) - accepted_set  # safety: accepted cannot be delinked

        # Extract tray IDs for logging and analysis
        original_tray_ids = [_get_tray_id(t) for t in all_trays_qs if _get_tray_id(t)]
        print(f'[DELINK MODAL] Accepted tray IDs: {accepted_tray_ids} (count={len(accepted_tray_ids)})')
        print(f'[DELINK MODAL] Delinked tray IDs: {delinked_tray_ids} (count={len(delinked_tray_ids)})')
        print(f'[DELINK MODAL] Original incoming trays: {original_tray_ids} (count={len(original_tray_ids)})')

        # ── REJECT TRAY IDs FLEXIBILITY (same rule as accepted_tray_ids) ──
        # When the caller explicitly scans reject trays (Accept Tray Scan + Reject Tray Scan
        # workflow), those tray IDs are the SOURCE OF TRUTH for the reject allocation — they
        # may be brand-new tray IDs never seen on the original incoming lot (e.g. only 1
        # original tray existed and it was consumed entirely by Accept). The legacy
        # "delink an original tray to free up reject capacity" algorithm below only applies
        # when the caller does NOT supply rejected_tray_ids (older tap-to-delink modal flow).
        rejected_tray_id_list = [str(x).strip().upper() for x in rejected_tray_ids_in if str(x).strip()]
        explicit_reject_mode = bool(rejected_tray_id_list)

        if explicit_reject_mode:
            print(f'[DELINK MODAL] Explicit reject tray IDs supplied: {rejected_tray_id_list} (count={len(rejected_tray_id_list)})')
            max_delink_count = 0
            show_reject = True

            # ── Distribute rejected_qty across the explicitly-scanned reject trays ──
            # Mirrors the accept-side distribution (rem + full slots) since the frontend
            # only sends tray IDs, not per-tray quantities — backend owns the qty split.
            reject_allocation = []
            remaining_to_reject = rejected_qty
            if rejected_qty > 0:
                rem = rejected_qty % tray_capacity
                full = rejected_qty // tray_capacity

                slots = []
                if rem > 0:
                    slots.append({'qty': rem, 'top_tray': True})
                slots.extend(
                    {'qty': tray_capacity, 'top_tray': False}
                    for _ in range(full)
                )
                for i, tid in enumerate(rejected_tray_id_list):
                    if i < len(slots):
                        reject_allocation.append({
                            'tray_id': tid,
                            'qty': slots[i]['qty'],
                            'top_tray': slots[i]['top_tray'],
                        })
                        remaining_to_reject -= slots[i]['qty']

            reject_pool = [t for t in all_trays_qs if _get_tray_id(t) in set(rejected_tray_id_list)]
            reject_pool_capacity = sum(_resolve_cap_for_tray_id(tid) for tid in rejected_tray_id_list)
            # delinked_set already reflects the caller-supplied delinked_tray_ids (original trays
            # not used in accept or reject) — no further adjustment needed here.

            print(f'[DELINK MODAL] Explicit reject allocation={reject_allocation}, remaining={remaining_to_reject}, pool_capacity={reject_pool_capacity}')
        else:
            # ── Compute max delink count — based on original tray capacity ──
            non_accepted = [t for t in all_trays_qs if _get_tray_id(t) not in accepted_set]
            min_reject_trays_needed = ceil(rejected_qty / tray_capacity) if rejected_qty > 0 else 0
            max_delink_count = max(0, len(non_accepted) - min_reject_trays_needed)

            print(f'[DELINK MODAL] non_accepted={len(non_accepted)}, min_reject_trays_needed={min_reject_trays_needed}, max_delink_count={max_delink_count}')

            # Enforce delink limit — only valid original tray IDs, up to max
            all_tray_id_set = set(_get_tray_id(t) for t in all_trays_qs if _get_tray_id(t))
            valid_delinked = [tid for tid in delinked_tray_ids if tid in all_tray_id_set and tid not in accepted_set]
            if len(valid_delinked) > max_delink_count:
                valid_delinked = valid_delinked[:max_delink_count]
            delinked_set = set(valid_delinked)

            # ── Show reject allocation only when delink selection is complete ──
            # Delink complete = user has selected max_delink_count trays (or no delinks possible)
            show_reject = len(valid_delinked) >= max_delink_count

            if show_reject:
                # ── Build reject pool = non-accepted, non-delinked ──
                reject_pool = [t for t in all_trays_qs
                               if _get_tray_id(t) not in accepted_set
                               and _get_tray_id(t) not in delinked_set]

                # ── Allocate rejected_qty in REVERSE order — same algo as iqf_submit_audit ──
                reject_allocation = []
                remaining_to_reject = rejected_qty
                for t in reversed(reject_pool):
                    if remaining_to_reject <= 0:
                        break
                    cap = _resolve_tray_cap(t)
                    tid = _get_tray_id(t)
                    is_top = False
                    if isinstance(t, dict):
                        is_top = bool(t.get('top_tray', False))
                    else:
                        is_top = bool(getattr(t, 'top_tray', False))
                    take = min(cap, remaining_to_reject)
                    reject_allocation.append({
                        'tray_id': tid,
                        'qty': take,
                        'top_tray': is_top,
                    })
                    remaining_to_reject -= take
                reject_allocation.reverse()
            else:
                # Delink not fully selected — don't compute reject yet
                reject_pool = []
                reject_allocation = []
                remaining_to_reject = rejected_qty

            # ── Compute reject pool capacity ──
            reject_pool_capacity = 0
            for t in all_trays_qs:
                tid = _get_tray_id(t)
                if tid not in accepted_set and tid not in delinked_set:
                    reject_pool_capacity += _resolve_tray_cap(t)

        print(f'[DELINK MODAL] reject_pool={len(reject_pool)}, capacity={reject_pool_capacity} / {rejected_qty} needed')

        # ── Build tray list with status for modal rendering ──
        original_trays = []
        reject_tray_id_set = set(r['tray_id'] for r in reject_allocation)
        reject_qty_map = {r['tray_id']: r['qty'] for r in reject_allocation}

        for t in all_trays_qs:
            tid = _get_tray_id(t)
            if not tid:
                continue

            # Get quantity (for dicts from snapshot)
            if isinstance(t, dict):
                qty = t.get('qty', 0)
                cap = t.get('capacity', tray_capacity)
                is_top = bool(t.get('top_tray', False))
            else:
                # IQFTrayId object
                remaining = int(getattr(t, 'remaining_qty', 0) or 0)
                raw_qty = int(getattr(t, 'tray_quantity', 0) or 0)
                qty = remaining if remaining > 0 else raw_qty
                cap = _resolve_tray_cap(t)
                is_top = bool(getattr(t, 'top_tray', False))

            if tid in accepted_set:
                tray_status = 'ACCEPTED'
            elif tid in delinked_set:
                tray_status = 'DELINKED'
            elif show_reject and tid in reject_tray_id_set:
                tray_status = 'REJECT'
            else:
                tray_status = 'PENDING'

            entry = {
                'tray_id': tid,
                'original_qty': qty,
                'capacity': cap,
                'top_tray': is_top,
                'status': tray_status,
            }
            if tray_status == 'REJECT':
                entry['reject_qty'] = reject_qty_map.get(tid, 0)
            original_trays.append(entry)

        print(f'[IQF DELINK MODAL] lot={lot_id}, source={original_tray_source}, rw={rw_qty}, '
              f'acc={accepted_qty}, rej={rejected_qty}, orig_trays={len(all_trays_qs)}, '
              f'in_accept={len(accepted_set)}, delinked={len(delinked_set)}, '
              f'reject_pool={len(reject_pool)}, max_delink={max_delink_count}, '
              f'reject_capacity={reject_pool_capacity}/{rejected_qty}, reject_allocated={remaining_to_reject == 0}')

        # ── CONFIRM MODE: Update IQF_Submitted and finalize ──
        # GUARD: Ensure delink/reject allocation is complete before confirming
        if request.method == 'POST' and payload.get('confirm'):
            # SAFETY CHECK: reject pool must have capacity for all rejected qty
            if reject_pool_capacity < rejected_qty:
                print(f'[DELINK CONFIRM ERROR] Insufficient capacity: pool={reject_pool_capacity}, needed={rejected_qty}')
                return Response({
                    'success': False,
                    'error': f'Selected trays cannot fulfill reject quantity. Pool capacity ({reject_pool_capacity}) < reject needed ({rejected_qty}). Please select fewer delinks.',
                    'reject_pool_capacity': reject_pool_capacity,
                    'rejected_qty': rejected_qty,
                }, status=400)

            if not show_reject or remaining_to_reject != 0:
                print(f'[DELINK CONFIRM ERROR] Allocation incomplete: show_reject={show_reject}, remaining={remaining_to_reject}')
                return Response({'success': False, 'error': 'Cannot confirm: reject allocation incomplete. Select all delink trays first.'}, status=400)

            try:
                with transaction.atomic():
                    sub = IQF_Submitted.objects.filter(lot_id=lot_id).first()
                    if not sub:
                        # ── FIRST-TIME SUBMISSION via delink modal (iqf_submit_audit was not called as 'proceed') ──
                        # Build accept tray snapshot by distributing accepted_qty across accepted_tray_ids
                        # using tray_capacity. Do NOT look up per-tray qty from DB — new trays have no stored qty.
                        print(f'[DELINK CONFIRM NEW] Creating PARTIAL submission for lot {lot_id}')

                        acc_trays_for_sub = []
                        if accepted_tray_ids and accepted_qty > 0:
                            rem = accepted_qty % tray_capacity
                            full = accepted_qty // tray_capacity
                            slots_list = []
                            if rem > 0:
                                slots_list.append({'qty': rem, 'top_tray': True})
                            for _ in range(full):
                                slots_list.append({'qty': tray_capacity, 'top_tray': False})
                            for i, tid in enumerate(accepted_tray_ids):
                                if i < len(slots_list):
                                    acc_trays_for_sub.append({
                                        'tray_id': tid,
                                        'qty': slots_list[i]['qty'],
                                        'top_tray': slots_list[i]['top_tray'],
                                    })

                        # Validate accept total matches expected accepted_qty
                        accept_total = sum(t['qty'] for t in acc_trays_for_sub)
                        if accept_total != accepted_qty:
                            print(f'[DELINK CONFIRM NEW ERROR] accept_total={accept_total} != accepted_qty={accepted_qty}')
                            return Response({
                                'success': False,
                                'error': f'Accept tray total ({accept_total}) does not match accepted qty ({accepted_qty}). Re-verify tray scans.',
                            }, status=400)

                        # Validate reject total matches expected rejected_qty
                        rej_total_check = sum(r['qty'] for r in reject_allocation)
                        if rej_total_check != rejected_qty:
                            print(f'[DELINK CONFIRM NEW ERROR] rej_total={rej_total_check} != rejected_qty={rejected_qty}')
                            return Response({
                                'success': False,
                                'error': f'Reject tray total ({rej_total_check}) does not match rejected qty ({rejected_qty}). Tray allocation error.',
                            }, status=400)

                        _new_rejection_details, _reason_ids_for_store = _build_iqf_rejection_details(
                            submitted_items,
                            fallback_lot_id=lot_id,
                            fallback_total=rejected_qty,
                        )

                        # Generate child lot IDs
                        accepted_lot_id = generate_new_lot_id()
                        time.sleep(0.001)
                        rejected_lot_id = generate_new_lot_id()
                        print(f'[DELINK CONFIRM NEW] accept_lot={accepted_lot_id}, reject_lot={rejected_lot_id}')

                        # Create accepted child lot → Brass QC
                        TotalStockModel.objects.create(
                            lot_id=accepted_lot_id,
                            batch_id=ts.batch_id,
                            model_stock_no=ts.model_stock_no,
                            version=ts.version,
                            polish_finish=ts.polish_finish,
                            plating_color=ts.plating_color,
                            total_stock=accepted_qty,
                            total_IP_accpeted_quantity=accepted_qty,
                            accepted_Ip_stock=True,
                            iqf_acceptance=True,
                            iqf_accepted_qty=accepted_qty,
                            iqf_accepted_qty_verified=True,
                            send_brass_qc=True,
                            send_brass_audit_to_iqf=False,
                            last_process_module='IQF',
                            next_process_module='Brass QC',
                            last_process_date_time=timezone.now(),
                            iqf_last_process_date_time=timezone.now(),
                        )
                        for _tray in acc_trays_for_sub:
                            IQFTrayId.objects.create(
                                lot_id=accepted_lot_id,
                                tray_id=_tray['tray_id'],
                                tray_quantity=_tray['qty'],
                                batch_id=ts.batch_id,
                                top_tray=_tray['top_tray'],
                                remaining_qty=_tray['qty'],
                                IP_tray_verified=True,
                                new_tray=False,
                                user=request.user,
                            )
                        IQF_Accepted_TrayScan.objects.create(
                            lot_id=accepted_lot_id,
                            accepted_tray_quantity=str(accepted_qty),
                            user=request.user,
                        )
                        for _tray in acc_trays_for_sub:
                            IQF_Accepted_TrayID_Store.objects.update_or_create(
                                tray_id=_tray['tray_id'],
                                defaults={
                                    'lot_id': accepted_lot_id,
                                    'tray_qty': _tray['qty'],
                                    'user': request.user,
                                    'is_save': True,
                                    'is_draft': False,
                                }
                            )
                        print(f'[DELINK CONFIRM NEW] Accept child: lot={accepted_lot_id}, qty={accepted_qty}, trays={len(acc_trays_for_sub)}')

                        # Create rejected child lot → IQF Reject
                        TotalStockModel.objects.create(
                            lot_id=rejected_lot_id,
                            batch_id=ts.batch_id,
                            model_stock_no=ts.model_stock_no,
                            version=ts.version,
                            polish_finish=ts.polish_finish,
                            plating_color=ts.plating_color,
                            total_stock=rejected_qty,
                            total_IP_accpeted_quantity=rejected_qty,
                            accepted_Ip_stock=True,
                            iqf_rejection=True,
                            iqf_after_rejection_qty=rejected_qty,
                            iqf_accepted_qty=0,
                            send_brass_audit_to_iqf=False,
                            send_brass_qc=False,
                            last_process_module='IQF',
                            next_process_module='IQF Reject',
                            last_process_date_time=timezone.now(),
                            iqf_last_process_date_time=timezone.now(),
                        )
                        for _tray in reject_allocation:
                            IQFTrayId.objects.create(
                                lot_id=rejected_lot_id,
                                tray_id=_tray['tray_id'],
                                tray_quantity=_tray['qty'],
                                batch_id=ts.batch_id,
                                top_tray=bool(_tray.get('top_tray', False)),
                                remaining_qty=_tray['qty'],
                                rejected_tray=True,
                                IP_tray_verified=True,
                                new_tray=False,
                                user=request.user,
                            )
                        _rej_child_store = IQF_Rejection_ReasonStore.objects.create(
                            lot_id=rejected_lot_id,
                            user=request.user,
                            total_rejection_quantity=rejected_qty,
                            batch_rejection=False,
                        )
                        if _reason_ids_for_store:
                            _rej_child_store.rejection_reason.set(
                                IQF_Rejection_Table.objects.filter(id__in=_reason_ids_for_store)
                            )
                        print(f'[DELINK CONFIRM NEW] Reject child: lot={rejected_lot_id}, qty={rejected_qty}, trays={len(reject_allocation)}')

                        # Create IQF_Submitted for parent lot
                        _partial_accept_data = {
                            'label': 'PARTIAL_ACCEPT',
                            'qty': accepted_qty,
                            'total_trays': len(acc_trays_for_sub),
                            'trays': acc_trays_for_sub,
                            'accepted_lot_id': accepted_lot_id,
                        }
                        _partial_reject_data = {
                            'label': 'PARTIAL_REJECT',
                            'qty': rejected_qty,
                            'total_trays': len(reject_allocation),
                            'trays': reject_allocation,
                            'reasons': _new_rejection_details,
                            'rejected_lot_id': rejected_lot_id,
                        }
                        _submission = IQF_Submitted.objects.create(
                            lot_id=lot_id,
                            batch_id=ts.batch_id,
                            original_lot_qty=int(ts.total_stock or 0),
                            iqf_incoming_qty=rw_qty,
                            total_lot_qty=rw_qty,
                            accepted_qty=accepted_qty,
                            rejected_qty=rejected_qty,
                            remarks=remark,
                            submission_type=IQF_Submitted.SUB_PARTIAL,
                            partial_accept_data=_partial_accept_data,
                            partial_reject_data=_partial_reject_data,
                            rejection_details=_new_rejection_details,
                            is_completed=True,
                            is_draft=False,
                            created_by=request.user,
                        )
                        print(f'[DELINK CONFIRM NEW] IQF_Submitted created for parent lot={lot_id}')

                        # Populate IQF_PartialAcceptLot and IQF_PartialRejectLot tracking tables
                        _parent_batch_id_val = ts.batch_id.batch_id if ts.batch_id else ''
                        IQF_PartialAcceptLot.objects.create(
                            new_lot_id=accepted_lot_id,
                            parent_lot_id=lot_id,
                            parent_batch_id=_parent_batch_id_val,
                            parent_submission=_submission,
                            accepted_qty=accepted_qty,
                            accept_trays_count=len(acc_trays_for_sub),
                            trays_snapshot=acc_trays_for_sub,
                            created_by=request.user,
                        )
                        IQF_PartialRejectLot.objects.create(
                            new_lot_id=rejected_lot_id,
                            parent_lot_id=lot_id,
                            parent_batch_id=_parent_batch_id_val,
                            parent_submission=_submission,
                            rejected_qty=rejected_qty,
                            reject_trays_count=len(reject_allocation),
                            rejection_reasons=_new_rejection_details,
                            trays_snapshot=reject_allocation,
                            created_by=request.user,
                        )
                        print(f'[DELINK CONFIRM NEW] IQF_PartialAcceptLot + IQF_PartialRejectLot created for parent lot={lot_id}')

                        # Mark ALL parent trays as delinked — parent is consumed by child lots
                        IQFTrayId.objects.filter(lot_id=lot_id).update(delink_tray=True)
                        released_count = _release_iqf_delinked_physical_trays(delinked_set)
                        print(f'[DELINK CONFIRM NEW] Released {released_count} selected physical tray(s): {sorted(delinked_set)}')

                        # Update parent TotalStockModel — consumed, children carry the work
                        ts.iqf_few_cases_acceptance = True
                        ts.iqf_onhold_picking = False
                        ts.iqf_acceptance = False
                        ts.iqf_rejection = False
                        ts.send_brass_qc = False
                        ts.send_brass_audit_to_iqf = False
                        ts.iqf_accepted_qty = accepted_qty
                        ts.iqf_after_rejection_qty = rejected_qty
                        ts.last_process_module = 'IQF'
                        ts.current_stage = 'IQF'
                        ts.next_process_module = None
                        ts.is_split = True
                        ts.remove_lot = True
                        ts.brass_audit_rejection = False
                        ts.iqf_last_process_date_time = timezone.now()
                        ts.save(update_fields=[
                            'iqf_few_cases_acceptance', 'iqf_onhold_picking', 'iqf_acceptance', 'iqf_rejection',
                            'send_brass_qc', 'send_brass_audit_to_iqf', 'iqf_accepted_qty', 'iqf_after_rejection_qty',
                            'last_process_module', 'next_process_module', 'is_split', 'remove_lot',
                            'brass_audit_rejection', 'iqf_last_process_date_time', 'current_stage',
                        ])
                        print(f'[DELINK CONFIRM NEW] ✅ FINALIZED lot={lot_id}: accept={accepted_lot_id}(qty={accepted_qty}), reject={rejected_lot_id}(qty={rejected_qty})')

                        return Response({
                            'success': True,
                            'confirmed': True,
                            'message': 'IQF partial submission completed. Accepted lot moved to Brass QC, rejected lot to IQF Reject.',
                            'lot_id': lot_id,
                            'accepted_lot_id': accepted_lot_id,
                            'rejected_lot_id': rejected_lot_id,
                        })

                    # Update partial_reject_data with user's delink choices
                    sub.partial_reject_data = {
                        'label': 'PARTIAL_REJECT',
                        'qty': rejected_qty,
                        'total_trays': len(reject_allocation),
                        'trays': reject_allocation,
                        'reasons': sub.rejection_details or [],
                    }
                    sub.save(update_fields=['partial_reject_data'])
                    print(f'[DELINK CONFIRM] Updated IQF_Submitted.partial_reject_data: {len(reject_allocation)} reject trays')

                    # Mark delinked trays in IQFTrayId
                    if delinked_set:
                        IQFTrayId.objects.filter(lot_id=lot_id, tray_id__in=delinked_set).update(
                            delink_tray=True
                        )
                        released_count = _release_iqf_delinked_physical_trays(delinked_set)
                        print(
                            f'[DELINK CONFIRM] Marked {len(delinked_set)} trays as delinked in IQFTrayId; '
                            f'released {released_count} selected physical tray(s)'
                        )

                    # Finalize TotalStockModel flags (same as iqf_verify_trays_confirm POST)
                    ts.iqf_few_cases_acceptance = True
                    ts.iqf_onhold_picking = False
                    ts.send_brass_qc = True
                    ts.send_brass_audit_to_iqf = False
                    ts.brass_audit_rejection = False
                    ts.iqf_accepted_qty_verified = False
                    ts.next_process_module = 'Brass QC'
                    # ✅ FIX: Reset Brass QC fields for fresh cycle when lot returns from IQF
                    ts.brass_qc_accepted_qty_verified = False
                    ts.brass_qc_accptance = False
                    ts.brass_qc_rejection = False
                    ts.brass_qc_few_cases_accptance = False
                    ts.brass_draft = False
                    ts.brass_onhold_picking = False
                    ts.brass_accepted_tray_scan_status = False
                    ts.brass_physical_qty = 0
                    ts.brass_missing_qty = 0
                    ts.save(update_fields=[
                        'iqf_few_cases_acceptance', 'iqf_onhold_picking',
                        'send_brass_qc', 'send_brass_audit_to_iqf', 'brass_audit_rejection',
                        'iqf_accepted_qty_verified', 'next_process_module',
                        'brass_qc_accepted_qty_verified', 'brass_qc_accptance', 'brass_qc_rejection',
                        'brass_qc_few_cases_accptance', 'brass_draft', 'brass_onhold_picking',
                        'brass_accepted_tray_scan_status', 'brass_physical_qty', 'brass_missing_qty',
                    ])

                    print(f'[DELINK CONFIRM] ✅ FINALIZED lot={lot_id}: delinked={sorted(delinked_set)}, rejected_qty={rejected_qty}')

                return Response({
                    'success': True,
                    'confirmed': True,
                    'message': 'IQF verification completed. Lot moved to Brass QC.',
                    'lot_id': lot_id,
                })

            except Exception as e:
                print(f'[DELINK CONFIRM EXCEPTION] {e}')
                traceback.print_exc()
                # CRITICAL: If delink confirm fails, keep IQF_Submitted as-is (PARTIAL draft state)
                # Lot stays visible in pick table for user retry
                return Response({
                    'success': False,
                    'error': 'Unable to process the request. Please verify the submitted data and try again.',
                    'lot_id': lot_id,
                }, status=500)

        return Response({
            'success': True,
            'lot_id': lot_id,
            'rw_qty': rw_qty,
            'accepted_qty': accepted_qty,
            'rejected_qty': rejected_qty,
            'tray_capacity': tray_capacity,
            'original_trays': original_trays,
            'max_delink_count': max_delink_count,
            'reject_allocation': reject_allocation,
            'delinked_tray_ids': sorted(delinked_set),
            'reject_fully_allocated': remaining_to_reject == 0,
            'delink_complete': show_reject,
            'reject_pool_capacity': reject_pool_capacity,
            'accepted_tray_count': len(accepted_tray_ids),
        })

    except Exception as e:
        print(f'[IQF DELINK MODAL ERROR] {e}')
        traceback.print_exc()
        # Return informative error with context for debugging
        return Response({
            'success': False,
            'error': 'Unable to process the request. Please verify the submitted data and try again.',
            'lot_id': lot_id,
            'iqf_rejection_total': iqf_rejection_total,
        }, status=500)


# ── IQF Lot Rejection — One-click full lot rejection ──
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def iqf_lot_rejection(request):
    """Handle Lot Rejection checkbox toggle — backend single source of truth.

    POST JSON:
        { "lot_id": "LID...", "lot_rejection": true|false }

    When lot_rejection = true:
        - Treat 100% of IQF incoming qty as FULL REJECTION
        - Keep tray structure as-is (no recalculation / split)
        - Create IQF_Submitted with submission_type = LOT_REJECTION
        - Set TotalStockModel flags for rejection
        - Overrides any partial draft data

    When lot_rejection = false:
        - Clear IQF_Submitted for this lot (if LOT_REJECTION)
        - Clear IQF rejection reason store
        - Reset TotalStockModel flags to editable state
        - Restore normal audit flow
    """
    data = request.data
    lot_id = data.get('lot_id')
    lot_rejection = data.get('lot_rejection')
    remark = (data.get('remark') or '').strip()

    if not lot_id:
        return Response({'success': False, 'error': 'Missing lot_id'}, status=400)
    if lot_rejection is None:
        return Response({'success': False, 'error': 'Missing lot_rejection flag'}, status=400)

    # Remark is mandatory when activating lot rejection
    if bool(lot_rejection) and not remark:
        return Response({'success': False, 'error': 'Remark is mandatory for lot rejection', 'remark_required': True}, status=400)

    lot_rejection = bool(lot_rejection)

    try:
        ts = TotalStockModel.objects.get(lot_id=lot_id)
    except TotalStockModel.DoesNotExist:
        return Response({'success': False, 'error': f'Lot {lot_id} not found'}, status=404)

    is_iqf_eligible = (
        ts.send_brass_audit_to_iqf or
        (getattr(ts, 'brass_qc_rejection', False) and getattr(ts, 'last_process_module', '') == 'Brass QC')
    )
    if not is_iqf_eligible:
        return Response({'success': False, 'error': f'Lot {lot_id} is not eligible for IQF'}, status=400)

    try:
        with transaction.atomic():
            if lot_rejection:
                # ── ACTIVATE LOT REJECTION ──

                # 1. Resolve iqf_incoming_qty (rw_qty) — same source as iqf_submit_audit
                audit_store = Brass_Audit_Rejection_ReasonStore.objects.filter(lot_id=lot_id).order_by('-id').first()
                qc_store = Brass_QC_Rejection_ReasonStore.objects.filter(lot_id=lot_id).order_by('-id').first()
                rw_qty = 0
                if audit_store and getattr(audit_store, 'total_rejection_quantity', None) is not None:
                    rw_qty = audit_store.total_rejection_quantity
                elif qc_store and getattr(qc_store, 'total_rejection_quantity', None) is not None:
                    rw_qty = qc_store.total_rejection_quantity

                # Detect Brass QC full lot rejection
                is_full_lot_reject = False
                if audit_store and getattr(audit_store, 'batch_rejection', False):
                    is_full_lot_reject = True
                elif qc_store and getattr(qc_store, 'batch_rejection', False):
                    is_full_lot_reject = True
                if not is_full_lot_reject:
                    if getattr(ts, 'brass_qc_rejection', False) and int(getattr(ts, 'brass_qc_accepted_qty', 0) or 0) == 0:
                        is_full_lot_reject = True

                original_lot_qty = 0
                if getattr(ts, 'batch_id', None):
                    original_lot_qty = int(getattr(ts.batch_id, 'total_batch_quantity', 0) or 0)

                iqf_incoming_qty = rw_qty
                if is_full_lot_reject and iqf_incoming_qty <= 0:
                    iqf_incoming_qty = original_lot_qty

                if iqf_incoming_qty <= 0:
                    return Response({'success': False, 'error': 'No IQF incoming qty — rw_qty is 0'}, status=400)

                rejected_qty = iqf_incoming_qty
                accepted_qty = 0

                print(f'[IQF LOT REJECTION] ACTIVATE lot={lot_id}, iqf_incoming={iqf_incoming_qty}, rejected={rejected_qty}')

                # 2. Build tray snapshot — keep as-is, assign full quantities under rejection
                all_trays_qs = list(IQFTrayId.objects.filter(lot_id=lot_id, delink_tray=False).order_by('id'))
                if not all_trays_qs:
                    same_lot_rows = list(
                        IQFTrayId.objects.filter(
                            lot_id=lot_id,
                            tray_quantity__gt=0,
                        ).order_by('id')
                    )
                    same_lot_entries = _iqf_tray_entries_from_rows(same_lot_rows)
                    has_partial_lifecycle = (
                        IQF_Submitted.objects.filter(
                            lot_id=lot_id,
                            submission_type=IQF_Submitted.SUB_PARTIAL,
                            is_completed=True,
                        ).exists()
                        or IQF_PartialAcceptLot.objects.filter(parent_lot_id=lot_id).exists()
                        or IQF_PartialRejectLot.objects.filter(parent_lot_id=lot_id).exists()
                    )
                    if (
                        not has_partial_lifecycle
                        and _iqf_tray_entries_match_qty(same_lot_entries, rejected_qty)
                    ):
                        all_trays_qs = same_lot_rows
                        print(
                            f'[IQF LOT REJECTION RECOVERY] using same-lot tray rows including '
                            f'delinked rows: trays={len(all_trays_qs)}, qty={rejected_qty}'
                        )
                original_tray_list = []
                reject_trays = []
                for t in all_trays_qs:
                    raw_qty = int(getattr(t, 'tray_quantity', 0) or 0)
                    remaining = int(getattr(t, 'remaining_qty', 0) or 0)
                    tray_qty = remaining if remaining > 0 else raw_qty
                    if tray_qty <= 0:
                        continue
                    tray_entry = {
                        'tray_id': getattr(t, 'tray_id', '') or '',
                        'qty': tray_qty,
                        'top_tray': bool(getattr(t, 'top_tray', False)),
                    }
                    original_tray_list.append(tray_entry)
                    reject_trays.append(tray_entry)

                if reject_trays:
                    reject_tray_ids = [t['tray_id'] for t in reject_trays]
                    IQFTrayId.objects.filter(
                        lot_id=lot_id,
                        tray_id__in=reject_tray_ids,
                    ).update(
                        rejected_tray=True,
                        delink_tray=False,
                    )

                original_data_snapshot = {
                    'qty': original_lot_qty,
                    'tray_total': sum(t['qty'] for t in original_tray_list),
                    'total_trays': len(original_tray_list),
                    'trays': original_tray_list,
                }
                iqf_data_snapshot = {
                    'qty': iqf_incoming_qty,
                    'tray_total': sum(t['qty'] for t in reject_trays),
                    'total_trays': len(reject_trays),
                    'trays': reject_trays,
                }

                full_reject_data = {
                    'label': 'LOT_REJECTION',
                    'qty': rejected_qty,
                    'total_trays': len(reject_trays),
                    'trays': reject_trays,
                }

                # 3. Create/update IQF_Submitted
                IQF_Submitted.objects.update_or_create(
                    lot_id=lot_id,
                    defaults={
                        'batch_id': ts.batch_id,
                        'original_lot_qty': original_lot_qty,
                        'iqf_incoming_qty': iqf_incoming_qty,
                        'total_lot_qty': iqf_incoming_qty,
                        'accepted_qty': accepted_qty,
                        'rejected_qty': rejected_qty,
                        'submission_type': IQF_Submitted.SUB_LOT_REJECT,
                        'original_data': original_data_snapshot,
                        'iqf_data': iqf_data_snapshot,
                        'full_accept_data': None,
                        'partial_accept_data': None,
                        'full_reject_data': full_reject_data,
                        'partial_reject_data': None,
                        'rejection_details': None,
                        'remarks': remark,
                        'is_completed': True,
                        'is_draft': False,
                        'created_by': request.user,
                    }
                )

                # 4. Create rejection reason store (lot-level, no per-reason breakdown)
                store, _ = IQF_Rejection_ReasonStore.objects.update_or_create(
                    lot_id=lot_id,
                    defaults={
                        'user': request.user,
                        'total_rejection_quantity': rejected_qty,
                        'batch_rejection': True,
                        'lot_rejected_comment': 'Lot Rejection — full lot rejected via IQF',
                    }
                )

                # 5. Update TotalStockModel flags
                ts.iqf_rejection = True
                ts.iqf_acceptance = False
                ts.iqf_few_cases_acceptance = False
                ts.iqf_onhold_picking = False
                ts.iqf_accepted_qty = 0
                ts.iqf_after_rejection_qty = rejected_qty
                ts.last_process_module = 'IQF'
                ts.current_stage = 'IQF'
                ts.send_brass_audit_to_iqf = False
                ts.brass_audit_rejection = False
                ts.send_brass_qc = False
                ts.iqf_last_process_date_time = timezone.now()
                ts.save(update_fields=[
                    'iqf_rejection', 'iqf_acceptance', 'iqf_few_cases_acceptance',
                    'iqf_onhold_picking', 'iqf_accepted_qty', 'iqf_after_rejection_qty',
                    'last_process_module', 'send_brass_audit_to_iqf', 'brass_audit_rejection', 'send_brass_qc',
                    'iqf_last_process_date_time', 'current_stage',
                ])

                # 6. Clear any existing drafts
                IQF_Draft_Store.objects.filter(lot_id=lot_id).delete()

                print(f'[IQF LOT REJECTION] SAVED lot={lot_id}, type=LOT_REJECTION, rejected={rejected_qty}')

                return Response({
                    'success': True,
                    'lot_rejection': True,
                    'lot_id': lot_id,
                    'submission_type': 'LOT_REJECTION',
                    'iqf_incoming_qty': iqf_incoming_qty,
                    'accepted_qty': accepted_qty,
                    'rejected_qty': rejected_qty,
                    'total_trays': len(reject_trays),
                    'trays': reject_trays,
                })

            else:
                # ── DEACTIVATE LOT REJECTION ──
                print(f'[IQF LOT REJECTION] DEACTIVATE lot={lot_id}')

                # Only clear if the existing submission is LOT_REJECTION
                existing_sub = IQF_Submitted.objects.filter(lot_id=lot_id).first()
                if existing_sub and existing_sub.submission_type == IQF_Submitted.SUB_LOT_REJECT:
                    existing_sub.delete()

                # Clear lot-level rejection reason store
                IQF_Rejection_ReasonStore.objects.filter(lot_id=lot_id, batch_rejection=True).delete()

                # Reset TotalStockModel to editable state
                ts.iqf_rejection = False
                ts.iqf_acceptance = False
                ts.iqf_few_cases_acceptance = False
                ts.iqf_onhold_picking = False
                ts.iqf_accepted_qty = 0
                ts.iqf_after_rejection_qty = 0
                ts.send_brass_audit_to_iqf = True  # Re-show in IQF pick table
                ts.send_brass_qc = False
                ts.save(update_fields=[
                    'iqf_rejection', 'iqf_acceptance', 'iqf_few_cases_acceptance',
                    'iqf_onhold_picking', 'iqf_accepted_qty', 'iqf_after_rejection_qty',
                    'send_brass_audit_to_iqf', 'send_brass_qc',
                ])

                print(f'[IQF LOT REJECTION] CLEARED lot={lot_id}, restored editable state')

                return Response({
                    'success': True,
                    'lot_rejection': False,
                    'lot_id': lot_id,
                    'message': 'Lot rejection cleared. Editable state restored.',
                })

    except Exception as e:
        print(f'[IQF LOT REJECTION ERROR] {e}')
        traceback.print_exc()
        return Response({'success': False, 'error': 'Server error'}, status=500)


# ═══════════════════════════════════════════════════════════════════════════════
# ── CONSOLIDATED IQF LOT DETAILS API — SINGLE SOURCE OF TRUTH ──
# All IQF tables (Accept, Reject, Completed, View Icon) MUST use this endpoint.
# SOURCE: IQF_Submitted table ONLY. No duplicate storage, no secondary queries.
# ═══════════════════════════════════════════════════════════════════════════════
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def iqf_lot_details(request):
    """SINGLE consolidated API for ALL IQF lot data.

    SOURCE OF TRUTH: Current lot tray data ONLY (never parent lot).
    Returns accepted, rejected, delinked data + summary with status labels.

    Query params: ?lot_id=X&table=accept|reject|complete (optional filter)
    - table=accept  → only accepted trays returned
    - table=reject  → only rejected trays returned
    - table=complete → all trays, but delinked qty hidden (set to '')
    - omitted → full response (PickTable view icon)
    Response: { lot_id, accepted, rejected, delinked, rejection_reasons, summary }
    """
    # Import service layer
    from .services.selectors import (
        get_current_trays,
    )

    lot_id = request.GET.get('lot_id')
    table_filter = request.GET.get('table', '').strip().lower()
    if not lot_id:
        return Response({'success': False, 'error': 'lot_id required'}, status=400)

    print(f"[IQF LOT DETAILS] Input lot_id: {lot_id}")

    try:
        sub = IQF_Submitted.objects.select_related(
            'batch_id', 'batch_id__model_stock_no', 'created_by'
        ).filter(lot_id=lot_id, is_completed=True).last()

        if not sub:
            print(f"[IQF LOT DETAILS] No IQF_Submitted record for lot {lot_id} — loading current lot trays")

            # ── CRITICAL FIX: Use get_current_trays from service layer ──
            # This ONLY returns current lot tray data, NEVER parent lot fallback
            tray_data, source, total_qty = get_current_trays(lot_id)

            if not tray_data:
                stock_obj = TotalStockModel.objects.filter(lot_id=lot_id).first()
                if stock_obj and stock_obj.last_process_module == 'Brass QC':
                    mapped_brass_lot = None
                    try:
                        parent_stock = TotalStockModel.objects.filter(
                            Q(brass_qc_transition_accept_lot_id=lot_id) |
                            Q(brass_qc_transition_reject_lot_id=lot_id) |
                            Q(brass_qc_transition_lot_id=lot_id)
                        ).first()
                        if parent_stock and getattr(parent_stock, 'lot_id', None):
                            mapped_brass_lot = parent_stock.lot_id
                    except Exception:
                        mapped_brass_lot = None

                    candidate_lots = [lot_id]
                    if mapped_brass_lot and mapped_brass_lot != lot_id:
                        candidate_lots.append(mapped_brass_lot)

                    for candidate_lot in candidate_lots:
                        src_trays, src_source, src_total = _get_brass_qc_rejected_trays_exact(candidate_lot)
                        if not src_trays:
                            src_trays, src_source, src_total = _get_brass_qc_submission_rejected_trays_exact(candidate_lot)
                        if src_trays:
                            tray_data = src_trays
                            source = f"brass_qc_source:{candidate_lot}:{src_source}"
                            total_qty = src_total
                            print(
                                f"[IQF LOT DETAILS] Brass QC source fallback "
                                f"lot={lot_id} source_lot={candidate_lot} "
                                f"trays={len(tray_data)} source={src_source}"
                            )
                            break

            if not tray_data:
                print(f"[IQF LOT DETAILS] No tray data found for lot {lot_id}")
                return Response({
                    'success': True,
                    'lot_id': lot_id,
                    'accepted': [],
                    'rejected': [],
                    'delinked': [],
                    'rejection_reasons': [],
                    'summary': {
                        'accepted_qty': 0,
                        'rejected_qty': 0,
                        'delink_qty': 0,
                        'iqf_incoming_qty': 0,
                        'original_lot_qty': 0,
                        'submission_type': '',
                        'status_label': 'NO_DATA',
                        'remarks': '',
                        'created_by': '',
                        'created_at': '',
                    },
                    'source': source,
                })

            # Format incoming trays as accepted (since IQF hasn't processed them yet)
            incoming_trays = [
                {
                    'tray_id': t.get('tray_id', ''),
                    'qty': t.get('qty', 0),
                    'top_tray': t.get('is_top', False),
                    'status': 'INCOMING',
                }
                for t in tray_data
            ]

            print(f"[IQF LOT DETAILS] Incoming: {len(incoming_trays)} trays, total={total_qty}, source={source}")
            return Response({
                'success': True,
                'lot_id': lot_id,
                'accepted': incoming_trays,
                'rejected': [],
                'delinked': [],
                'rejection_reasons': [],
                'summary': {
                    'accepted_qty': 0,
                    'rejected_qty': 0,
                    'delink_qty': 0,
                    'iqf_incoming_qty': total_qty,
                    'original_lot_qty': 0,
                    'submission_type': '',
                    'status_label': 'NOT_PROCESSED',
                    'remarks': '',
                    'created_by': '',
                    'created_at': '',
                },
                'source': source,
            })

        print(f"[IQF LOT DETAILS] Found: type={sub.submission_type}, "
              f"accepted={sub.accepted_qty}, rejected={sub.rejected_qty}")

        # ── Extract ACCEPTED trays ──
        accepted = []
        accept_data = sub.partial_accept_data or sub.full_accept_data
        if accept_data and accept_data.get('trays'):
            for t in accept_data['trays']:
                qty = int(t.get('qty', 0))
                if qty <= 0:
                    continue
                accepted.append({
                    'tray_id': t.get('tray_id', ''),
                    'qty': qty,
                    'top_tray': bool(t.get('top_tray', t.get('is_top_tray', False))),
                    'status': 'ACCEPT',
                })

        # ── Extract REJECTED trays ──
        rejected = []
        reject_data = sub.partial_reject_data or sub.full_reject_data
        if reject_data and reject_data.get('trays'):
            for t in reject_data['trays']:
                qty = int(t.get('qty', 0))
                if qty <= 0:
                    continue
                rejected.append({
                    'tray_id': t.get('tray_id', ''),
                    'qty': qty,
                    'top_tray': bool(t.get('top_tray', False)),
                    'status': 'REJECT',
                })

        # Some completed/historical IQF rejection rows have the correct rejected
        # quantity but an empty/missing reject snapshot. Recover tray IDs only from
        # IQF-owned rejection evidence for the SAME rejection flow.
        if not rejected:
            reject_lot_ids = [lot_id]

            if sub.submission_type == 'PARTIAL':
                reject_child_lot_id = ''
                if sub.partial_reject_data:
                    reject_child_lot_id = str(
                        sub.partial_reject_data.get('rejected_lot_id') or ''
                    ).strip()

                if not reject_child_lot_id:
                    reject_child_lot_id = (
                        IQF_PartialRejectLot.objects
                        .filter(parent_lot_id=lot_id)
                        .values_list('new_lot_id', flat=True)
                        .first()
                        or ''
                    )

                if reject_child_lot_id:
                    reject_lot_ids.append(reject_child_lot_id)

            rejected_qs = IQFTrayId.objects.filter(
                lot_id__in=reject_lot_ids,
                rejected_tray=True,
                delink_tray=False,
                tray_quantity__gt=0,
            ).order_by('-top_tray', 'id')

            for t in rejected_qs:
                rejected.append({
                    'tray_id': t.tray_id,
                    'qty': int(t.tray_quantity or 0),
                    'top_tray': bool(t.top_tray),
                    'status': 'REJECT',
                })

        # FULL_REJECT / LOT_REJECTION means the complete IQF incoming tray set was
        # rejected. If an older submission has no full_reject_data tray snapshot
        # and no retained rejected IQFTrayId rows, the immutable original_data
        # snapshot is the final safe fallback. Do NOT apply this to PARTIAL.
        original_data = sub.original_data or {}
        original_trays = original_data.get('trays', [])
        if (
            not rejected
            and sub.submission_type in ('FULL_REJECT', 'LOT_REJECTION')
            and original_trays
        ):
            for t in original_trays:
                qty = int(t.get('qty', 0) or 0)
                tray_id = str(t.get('tray_id', '') or '').strip()
                if not tray_id or qty <= 0:
                    continue
                rejected.append({
                    'tray_id': tray_id,
                    'qty': qty,
                    'top_tray': bool(t.get('top_tray', t.get('is_top', False))),
                    'status': 'REJECT',
                })

        # ── Compute DELINKED trays from original snapshot ──
        if not rejected and sub.submission_type in ('FULL_REJECT', 'LOT_REJECTION'):
            same_lot_rows = list(
                IQFTrayId.objects.filter(
                    lot_id=lot_id,
                    tray_quantity__gt=0,
                ).order_by('-top_tray', 'id')
            )
            same_lot_entries = _iqf_tray_entries_from_rows(same_lot_rows)
            if (
                sub.rejected_qty > 0
                and _iqf_tray_entries_match_qty(same_lot_entries, sub.rejected_qty)
            ):
                rejected = [
                    {
                        'tray_id': t['tray_id'],
                        'qty': t['qty'],
                        'top_tray': t['top_tray'],
                        'status': 'REJECT',
                    }
                    for t in same_lot_entries
                ]

        delinked = []
        accept_tray_ids = {t['tray_id'] for t in accepted}
        reject_tray_ids = {t['tray_id'] for t in rejected}
        if original_trays:
            for t in original_trays:
                tid = t.get('tray_id', '')
                if tid and tid not in accept_tray_ids and tid not in reject_tray_ids:
                    delinked.append({
                        'tray_id': tid,
                        'qty': int(t.get('qty', 0)),
                        'top_tray': bool(t.get('top_tray', False)),
                        'status': 'DELINK',
                    })

        # Fallback: check IQFTrayId for explicitly delinked trays
        if not delinked:
            delinked_qs = IQFTrayId.objects.filter(lot_id=lot_id, delink_tray=True)
            for t in delinked_qs:
                if t.tray_id not in accept_tray_ids and t.tray_id not in reject_tray_ids:
                    delinked.append({
                        'tray_id': t.tray_id,
                        'qty': int(t.tray_quantity or 0),
                        'top_tray': bool(t.top_tray),
                        'status': 'DELINK',
                    })

        # ── Determine status label ──
        if sub.submission_type == 'FULL_ACCEPT':
            status_label = 'ACCEPT'
        elif sub.submission_type in ('FULL_REJECT', 'LOT_REJECTION'):
            status_label = 'REJECT'
        elif sub.submission_type == 'PARTIAL':
            status_label = 'PARTIAL'
        else:
            status_label = sub.submission_type

        # ── Rejection reasons from stored details ──
        rejection_reasons = []
        if sub.rejection_details:
            for rd in sub.rejection_details:
                rejection_reasons.append({
                    'reason': rd.get('reason_text', ''),
                    'reason_id': rd.get('reason_id', ''),
                    'qty': rd.get('iqf_qty', 0),
                })

        delink_qty = sum(t['qty'] for t in delinked)
        delink_tray_count = len(delinked)

        # ── Table-specific filtering ──
        # Accept table: show ONLY accepted trays
        if table_filter == 'accept':
            rejected = []
            delinked = []
        # Reject table: show ONLY the rejected trays for this IQF rejection.
        # Accepted and delinked trays remain hidden in the Reject Table context.
        elif table_filter == 'reject':
            accepted = []
            delinked = []
        # Complete table: show all, but hide delinked qty
        elif table_filter == 'complete':
            for t in delinked:
                t['qty'] = ''

        response = {
            'success': True,
            'lot_id': lot_id,
            'accepted': accepted,
            'rejected': rejected,
            'delinked': delinked,
            'rejection_reasons': rejection_reasons,
            'summary': {
                'accepted_qty': sub.accepted_qty,
                'rejected_qty': sub.rejected_qty,
                'delink_qty': delink_qty,
                'delink_tray_count': delink_tray_count,
                'iqf_incoming_qty': sub.iqf_incoming_qty,
                'original_lot_qty': sub.original_lot_qty,
                'submission_type': sub.submission_type,
                'status_label': status_label,
                'remarks': sub.remarks or '',
                'created_by': sub.created_by.username if sub.created_by else '',
                'created_at': sub.created_at.isoformat() if sub.created_at else '',
            },
            'source': 'IQF_Submitted',
        }

        print(f"[IQF LOT DETAILS] Response: accepted={len(accepted)}, "
              f"rejected={len(rejected)}, delinked={len(delinked)}, status={status_label}")
        return Response(response)

    except Exception as e:
        print(f"[IQF LOT DETAILS ERROR] {e}")
        traceback.print_exc()
        return Response({'success': False, 'error': 'Server error'}, status=500)


# ── IQF Row Hold / Unhold — Same pattern as Brass QC ──
@method_decorator(login_required, name='dispatch')
@method_decorator(csrf_exempt, name='dispatch')
class IQFSaveHoldUnholdReasonAPIView(APIView):
    """
    POST with:
    {
        "lot_id": "LID...",
        "remark": "Reason text",
        "action": "hold"  # or "unhold"
    }
    """
    def post(self, request):
        try:
            data = request.data if hasattr(request, 'data') else json.loads(request.body.decode('utf-8'))
            lot_id = data.get('lot_id')
            remark = data.get('remark', '').strip()
            action = data.get('action', '').strip().lower()

            if not lot_id or not remark or action not in ['hold', 'unhold']:
                return JsonResponse({'success': False, 'error': 'Missing or invalid parameters.'}, status=400)

            obj = TotalStockModel.objects.filter(lot_id=lot_id).first()
            if not obj:
                return JsonResponse({'success': False, 'error': 'LOT not found.'}, status=404)

            if action == 'hold':
                obj.iqf_holding_reason = remark
                obj.iqf_hold_lot = True
                obj.iqf_release_reason = ''
                obj.iqf_release_lot = False
            elif action == 'unhold':
                obj.iqf_release_reason = remark
                obj.iqf_hold_lot = False
                obj.iqf_release_lot = True

            obj.save(update_fields=['iqf_holding_reason', 'iqf_release_reason', 'iqf_hold_lot', 'iqf_release_lot'])
            return JsonResponse({'success': True, 'message': 'Reason saved.'})

        except Exception as e:
            return JsonResponse({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)


# ══ IQF Reject Table — Delink Selected Lots ══
# Frees the REJECTED trays of selected IQF reject lots so the tray IDs become
# reusable across the system (TrayId master reset to a fresh, unallocated state).
# Frontend (Iqf_RejectTable.html) posts { stock_lot_ids: [...] } and expects
# { success, updated, lots_processed, error }.
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def iqf_delink(request):
    """Delink the rejected trays of the selected IQF reject lot(s).

    For each lot_id (the reject/delink lot shown in the IQF Reject Table):
      1. Mark its active IQFTrayId rows as delinked (delink_tray=True).
      2. Free those tray IDs in the TrayId master table so they can be scanned
         again as brand-new trays (delink_tray=True, lot_id=None, batch_id=None,
         new_tray=True, scanned/verify/rejected/top flags cleared).
      3. Delink the same tray IDs in the other module tray tables
         (BrassTrayId, BrassAuditTrayId, IPTrayId) so they are no longer
         reported as "occupied in another module".

    Response fields match the frontend contract: updated, lots_processed.
    """
    try:
        stock_lot_ids = request.data.get('stock_lot_ids', []) or []
        # Backward compatibility: also accept a single lot_id or tray_ids list
        if not stock_lot_ids and request.data.get('lot_id'):
            stock_lot_ids = [request.data.get('lot_id')]

        stock_lot_ids = [str(lid).strip() for lid in stock_lot_ids if str(lid or '').strip()]

        if not stock_lot_ids:
            return Response({'success': False, 'error': 'No lots selected for delink'}, status=400)

        updated = 0
        lots_processed = 0
        not_found = []

        with transaction.atomic():
            for lot_id in stock_lot_ids:
                # Active (non-delinked) IQF trays for this reject lot — the ones to free
                tray_ids = list(
                    IQFTrayId.objects.filter(lot_id=lot_id, delink_tray=False)
                    .values_list('tray_id', flat=True)
                )
                tray_ids = [tid for tid in tray_ids if tid]

                if not tray_ids:
                    not_found.append(lot_id)
                    continue

                # 1. Delink IQF trays for this lot
                IQFTrayId.objects.filter(lot_id=lot_id, delink_tray=False).update(delink_tray=True)

                # 2. Free the tray IDs in the TrayId master → reusable as new
                freed = TrayId.objects.filter(tray_id__in=tray_ids).update(
                    delink_tray=True,
                    lot_id=None,
                    batch_id=None,
                    scanned=False,
                    IP_tray_verified=False,
                    rejected_tray=False,
                    brass_rejected_tray=False,
                    top_tray=False,
                    new_tray=True,
                )
                updated += freed

                # 3. Delink the same trays in the other module tables so they are
                #    not reported as occupied elsewhere on the next scan.
                BrassTrayId.objects.filter(tray_id__in=tray_ids).update(delink_tray=True)
                BrassAuditTrayId.objects.filter(tray_id__in=tray_ids).update(delink_tray=True)
                IPTrayId.objects.filter(tray_id__in=tray_ids).update(delink_tray=True)

                lots_processed += 1

        print(f'[IQF DELINK] lots={stock_lot_ids}, processed={lots_processed}, '
              f'trays_freed={updated}, not_found={not_found}')

        if lots_processed == 0:
            return Response({
                'success': False,
                'error': 'No rejected trays found to delink for the selected lot(s).',
                'not_found': not_found,
            }, status=400)

        return Response({
            'success': True,
            'updated': updated,
            'lots_processed': lots_processed,
            'not_found': not_found,
        })

    except Exception as e:
        print('[IQF DELINK ERROR]', str(e))
        traceback.print_exc()
        return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)
