import re


_TRAY_SERIES_RE = re.compile(r'^([A-Z]{2})-A\d{5}$')
_JUMBO_TRAY_TYPE_CODES = {'JR', 'JD', 'JB', 'JL', 'JUMBO'}


def _nq_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _tray_id(value):
    return str(value or '').strip().upper()


def _tray_sort_key(tray_id):
    return _tray_id(tray_id)


def extract_tray_series_from_tray_id(tray_id):
    tray_key = _tray_id(tray_id)
    match = _TRAY_SERIES_RE.match(tray_key)
    return match.group(1) if match else ''


def _resolve_nickel_wiping_tray_type(tray_type, plating_stk_no=None):
    """
    Return the authoritative model tray type for a Nickel Wiping lot.

    JigUnloadAfterTable.tray_type is only a snapshot captured at jig-unload
    time. For models added or re-classified in ModelMaster afterwards (e.g. the
    pilot-model bulk load) that snapshot can be blank or stale, which previously
    caused the rejection allocation to silently default to Normal (NB) for a
    Jumbo model. ModelMaster is the source of truth (CLAUDE.md - "Backend is the
    only source of truth"), so resolve from it by plating stock number first and
    fall back to the snapshot only when the master lookup yields nothing.
    """
    resolved = str(tray_type or '').strip()
    if plating_stk_no:
        try:
            from Jig_Unloading.tray_utils import get_model_master_tray_info
            master_type, _ = get_model_master_tray_info(plating_stk_no, resolved)
            if master_type:
                resolved = str(master_type).strip()
        except Exception:  # pragma: no cover - defensive, never block on lookup
            pass
    return resolved


def get_nickel_wiping_rejection_tray_allocation(tray_type, plating_stk_no=None):
    """
    Resolve Nickel Wiping rejection tray allocation from model master tray type.

    Nickel Wiping receives the configured model tray type from the upstream
    model master flow via JigUnloadAfterTable.tray_type. Normal-family models
    use NB rejection trays; Jumbo-family models use JB rejection trays.

    When plating_stk_no is supplied the tray type is re-resolved from ModelMaster
    so a blank/stale JigUnloadAfterTable.tray_type snapshot cannot downgrade a
    Jumbo model to Normal reject trays.
    """
    tray_type_key = _resolve_nickel_wiping_tray_type(tray_type, plating_stk_no).upper()
    if 'JUMBO' in tray_type_key or tray_type_key in _JUMBO_TRAY_TYPE_CODES:
        return 'JB', 12
    return 'NB', 16


def validate_nickel_wiping_rejection_tray_series(tray_id, tray_type, plating_stk_no=None):
    allowed_prefix, _ = get_nickel_wiping_rejection_tray_allocation(tray_type, plating_stk_no)
    scanned_prefix = extract_tray_series_from_tray_id(tray_id)

    if not scanned_prefix:
        return (
            False,
            f'Invalid tray ID format. Expected {allowed_prefix}-A00001 format.',
            allowed_prefix,
        )
    if scanned_prefix != allowed_prefix:
        return (
            False,
            f'This model is allocated to {allowed_prefix} trays. Please scan a {allowed_prefix} tray.',
            allowed_prefix,
        )
    return True, '', allowed_prefix


def release_tray_master_for_reuse(tray_id, *, delink_qty=None):
    """
    Release the global TrayId row for a tray that has been explicitly delinked.

    The caller must run inside transaction.atomic() when this release is paired
    with module-level delink updates.
    """
    tray_key = _tray_id(tray_id)
    if not tray_key:
        return False

    from modelmasterapp.models import TrayId

    tray_master = (
        TrayId.objects.select_for_update()
        .filter(tray_id__iexact=tray_key)
        .first()
    )
    if not tray_master:
        return False

    tray_master.lot_id = None
    tray_master.batch_id = None
    tray_master.tray_quantity = None
    tray_master.delink_tray = True
    tray_master.delink_tray_qty = str(delink_qty) if delink_qty is not None else None
    tray_master.scanned = False
    tray_master.top_tray = False
    tray_master.IP_tray_verified = False
    tray_master.rejected_tray = False
    tray_master.brass_rejected_tray = False
    tray_master.new_tray = True
    tray_master.save(update_fields=[
        'lot_id',
        'batch_id',
        'tray_quantity',
        'delink_tray',
        'delink_tray_qty',
        'scanned',
        'top_tray',
        'IP_tray_verified',
        'rejected_tray',
        'brass_rejected_tray',
        'new_tray',
    ])
    return True


def is_nickel_wiping_tray_master_released(tray_id):
    """
    Return True when the master tray row has the explicit released/reusable
    state created by Nickel Wiping delink.
    """
    tray_key = _tray_id(tray_id)
    if not tray_key:
        return False

    from modelmasterapp.models import TrayId

    tray_master = TrayId.objects.filter(tray_id__iexact=tray_key).first()
    if not tray_master:
        return False

    return bool(
        tray_master.delink_tray
        and not tray_master.scanned
        and not tray_master.lot_id
        and not tray_master.batch_id_id
        and not tray_master.rejected_tray
        and not tray_master.brass_rejected_tray
    )


def _snapshot_rows(value):
    return value if isinstance(value, list) else []


def _normalize_reject_event_rows(rows):
    reject_qty_by_id = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        tray_id = _tray_id(row.get('tray_id') or row.get('rejected_tray_id'))
        if not tray_id:
            continue
        qty = _nq_int(
            row.get(
                'qty',
                row.get('tray_quantity', row.get('rejected_tray_quantity', 0)),
            )
        )
        if qty <= 0:
            continue
        reject_qty_by_id[tray_id] = qty
    return [
        {'tray_id': tray_id, 'qty': qty}
        for tray_id, qty in reject_qty_by_id.items()
    ]


def get_current_nickel_wiping_reject_trays(lot_id):
    """
    Resolve the current/latest Nickel Wiping rejection-event trays for a lot.

    Event precedence intentionally matches the delink flow: latest
    NickelQC_Submission first, then the newest append-only reject record, then
    legacy direct reject scans. Old historical events are not combined with a
    newer event for the same lot.
    """
    lot_key = str(lot_id or '').strip()
    if not lot_key:
        return []

    from Nickel_Inspection.models import (
        Nickel_QC_Rejected_TrayScan,
        NickelQC_Submission,
        NickelWiping_FullRejectRecord,
        NickelWiping_PartialRejectRecord,
    )

    submission = (
        NickelQC_Submission.objects
        .filter(lot_id=lot_key, submission_type__in=['PARTIAL', 'FULL_REJECT'])
        .order_by('-created_at', '-id')
        .first()
    )
    if submission is not None:
        return _normalize_reject_event_rows(
            _snapshot_rows(submission.reject_trays_data)
        )

    partial_record = (
        NickelWiping_PartialRejectRecord.objects
        .filter(source_lot_id=lot_key)
        .order_by('-created_at', '-id')
        .first()
    )
    full_record = (
        NickelWiping_FullRejectRecord.objects
        .filter(source_lot_id=lot_key)
        .order_by('-created_at', '-id')
        .first()
    )
    candidates = [r for r in (partial_record, full_record) if r is not None]
    if candidates:
        latest = max(candidates, key=lambda r: (r.created_at, r.id))
        return _normalize_reject_event_rows(_snapshot_rows(latest.reject_trays))

    legacy_rows = [
        {
            'tray_id': scan.rejected_tray_id,
            'qty': _nq_int(scan.rejected_tray_quantity),
        }
        for scan in Nickel_QC_Rejected_TrayScan.objects.filter(lot_id=lot_key)
    ]
    return _normalize_reject_event_rows(legacy_rows)


def has_unreleased_nickel_wiping_reject_trays(lot_id):
    """
    Return True when the current reject event has at least one tray that has
    not been explicitly marked delinked in Nickel Wiping local event state.

    Master TrayId release state is intentionally not used here: a master tray
    can already look reusable for historical reasons, but a newly created
    Nickel Wiping reject event must remain actionable until the operator runs
    Delink Selected for this event.
    """
    from Nickel_Inspection.models import NickelQcTrayId

    for row in get_current_nickel_wiping_reject_trays(lot_id):
        tray_id = row.get('tray_id')
        tray_obj = (
            NickelQcTrayId.objects
            .filter(lot_id=lot_id, tray_id__iexact=tray_id)
            .first()
        )
        if tray_obj is None or not tray_obj.delink_tray:
            return True
    return False


def has_releasable_nickel_wiping_reject_trays(lot_id):
    return has_unreleased_nickel_wiping_reject_trays(lot_id)


def _nickel_wiping_active_lot_ids(current_lot_id=None):
    from django.db.models import Q
    from Jig_Unloading.models import JigUnloadAfterTable

    current_lot = str(current_lot_id or '').strip()
    active_lots = JigUnloadAfterTable.objects.filter(
        Q(nq_draft=True)
        | Q(nq_onhold_picking=True)
        | Q(nq_qc_rejection=True)
        | Q(nq_qc_few_cases_accptance=True)
        | Q(current_stage__iexact='Nickel Wiping')
    )
    if current_lot:
        active_lots = active_lots.exclude(lot_id=current_lot)
    return list(active_lots.values_list('lot_id', flat=True))


def _tray_rows_contain_tray_id(rows, tray_key):
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if _tray_id(row.get('tray_id') or row.get('rejected_tray_id')) == tray_key:
            return True
    return False


def validate_nickel_wiping_rejection_tray_available(
    tray_id,
    current_lot_id=None,
    *,
    lock_master=False,
):
    """
    Return (is_available, message) for Nickel Wiping rejection tray reuse.

    Historical reject snapshots are considered blocking only while their owner
    lot is still active in Nickel Wiping. The current lot is excluded so draft
    resume and revalidation of the same lot's trays remain valid.
    """
    tray_key = _tray_id(tray_id)
    if not tray_key:
        return False, 'Tray ID required'

    from modelmasterapp.models import TrayId
    from Nickel_Inspection.models import (
        Nickel_QC_Draft_Store,
        Nickel_QC_Rejected_TrayScan,
        NickelQC_Submission,
        NickelWiping_FullRejectRecord,
        NickelWiping_PartialRejectRecord,
    )

    master_qs = TrayId.objects
    if lock_master:
        master_qs = master_qs.select_for_update()
    master_tray = master_qs.filter(tray_id__iexact=tray_key).first()
    if not master_tray:
        return False, f'Tray {tray_key} not found in master'

    # Cross-module guard for Nickel Wiping Z1/Z2. Both zones use this same
    # service through the shared nq_action handler, so one lifecycle-aware
    # check protects scan-time validation and final reject submission.
    #
    # Keep the current-lot exception: an original tray already belonging to
    # this Nickel Wiping lot may legitimately be reused as its own reject
    # container. Explicitly delinked/released trays also remain reusable.
    current_lot = str(current_lot_id or '').strip()

    try:
        from Brass_QC.services.validators import (
            validate_tray_cross_module_occupancy,
            is_tray_rejected_in_brass_qc,
        )

        occupied_module, occupancy_error = validate_tray_cross_module_occupancy(
            tray_key, current_lot or None
        )
        if occupancy_error:
            return False, 'Tray is occupied.'

        # Brass QC reject identity is snapshot-backed, so it can outlive the
        # module mirror/master assignment. Reuse its release-aware helper.
        if is_tray_rejected_in_brass_qc(tray_key):
            return False, 'Tray is occupied.'
    except ImportError:
        # Preserve existing Nickel Wiping behavior if Brass QC services are not
        # importable during an isolated app-management operation.
        pass

    # Brass Audit rejected trays can likewise be represented only in completed
    # submission / partial-reject snapshots. Do not let a free-looking master
    # row make such a tray reusable unless it was explicitly released/delinked.
    explicitly_released = bool(
        master_tray.delink_tray and not master_tray.scanned
    )
    if not explicitly_released:
        try:
            from BrassAudit.models import (
                Brass_Audit_Submission,
                BrassAudit_PartialRejectLot,
            )

            ba_submissions = Brass_Audit_Submission.objects.filter(
                submission_type__in=['PARTIAL', 'FULL_REJECT'],
                is_completed=True,
            )
            if current_lot:
                ba_submissions = ba_submissions.exclude(lot_id=current_lot)
            for submission in ba_submissions.only(
                'lot_id', 'partial_reject_data', 'full_reject_data'
            ):
                snapshot = submission.partial_reject_data or submission.full_reject_data or {}
                if _tray_rows_contain_tray_id(snapshot.get('trays'), tray_key):
                    return False, 'Tray is occupied.'

            ba_reject_lots = BrassAudit_PartialRejectLot.objects.exclude(
                trays_snapshot__isnull=True
            )
            if current_lot:
                ba_reject_lots = ba_reject_lots.exclude(new_lot_id=current_lot)
            for reject_lot in ba_reject_lots.only('new_lot_id', 'trays_snapshot'):
                if _tray_rows_contain_tray_id(reject_lot.trays_snapshot, tray_key):
                    return False, 'Tray is occupied.'
        except ImportError:
            pass

        # Nickel Audit Z1 and Z2 share these models. Exclude the current lot so
        # an intentional NA -> NW rework return can continue, while reject trays
        # owned by any other Nickel Audit lot remain unavailable.
        try:
            from Nickel_Audit.models import (
                Nickel_AuditTrayId,
                Nickel_Audit_Rejected_TrayScan,
                NickelAudit_Submission,
                NickelAudit_PartialRejectLot,
            )

            na_live = Nickel_AuditTrayId.objects.filter(
                tray_id__iexact=tray_key,
                delink_tray=False,
                lot_id__isnull=False,
            )
            if current_lot:
                na_live = na_live.exclude(lot_id=current_lot)
            if na_live.exists():
                return False, 'Tray is occupied.'

            na_scans = Nickel_Audit_Rejected_TrayScan.objects.filter(
                rejected_tray_id__iexact=tray_key
            )
            if current_lot:
                na_scans = na_scans.exclude(lot_id=current_lot)
            if na_scans.exists():
                return False, 'Tray is occupied.'

            na_submissions = NickelAudit_Submission.objects.filter(
                submission_type__in=['PARTIAL', 'FULL_REJECT']
            )
            if current_lot:
                na_submissions = na_submissions.exclude(lot_id=current_lot)
            for submission in na_submissions.only('lot_id', 'reject_trays_data'):
                if _tray_rows_contain_tray_id(submission.reject_trays_data, tray_key):
                    return False, 'Tray is occupied.'

            na_reject_lots = NickelAudit_PartialRejectLot.objects.exclude(
                trays_snapshot__isnull=True
            )
            if current_lot:
                na_reject_lots = na_reject_lots.exclude(new_lot_id=current_lot)
            for reject_lot in na_reject_lots.only('new_lot_id', 'trays_snapshot'):
                if _tray_rows_contain_tray_id(reject_lot.trays_snapshot, tray_key):
                    return False, 'Tray is occupied.'
        except ImportError:
            pass

    # An explicitly delinked/released tray is reusable after the live
    # cross-module ownership checks above have passed. Nickel Wiping's
    # historical reject scans/submission snapshots are audit history and must
    # not keep that released tray occupied.
    if explicitly_released:
        return True, ''

    active_lot_ids = _nickel_wiping_active_lot_ids(current_lot_id)
    if not active_lot_ids:
        return True, ''

    if Nickel_QC_Rejected_TrayScan.objects.filter(
        rejected_tray_id__iexact=tray_key,
        lot_id__in=active_lot_ids,
    ).exists():
        return False, f'Tray {tray_key} is already assigned in Nickel Wiping.'

    if str(current_lot_id or '').strip():
        draft_qs = Nickel_QC_Draft_Store.objects.filter(
            draft_type='batch_rejection',
            lot_id__in=active_lot_ids,
        ).only('lot_id', 'draft_data')
        for draft in draft_qs:
            draft_data = draft.draft_data or {}
            if not isinstance(draft_data, dict):
                continue
            if _tray_rows_contain_tray_id(draft_data.get('reject_trays'), tray_key):
                return False, f'Tray {tray_key} is already reserved in Nickel Wiping.'
            if _tray_rows_contain_tray_id(draft_data.get('reject_slots'), tray_key):
                return False, f'Tray {tray_key} is already reserved in Nickel Wiping.'

    submission_qs = NickelQC_Submission.objects.filter(
        lot_id__in=active_lot_ids,
        submission_type__in=['PARTIAL', 'FULL_REJECT'],
    ).only('lot_id', 'reject_trays_data')
    for submission in submission_qs:
        if _tray_rows_contain_tray_id(submission.reject_trays_data, tray_key):
            return False, f'Tray {tray_key} is already assigned in Nickel Wiping.'

    full_reject_qs = NickelWiping_FullRejectRecord.objects.filter(
        source_lot_id__in=active_lot_ids,
    ).only('source_lot_id', 'reject_trays')
    for record in full_reject_qs:
        if _tray_rows_contain_tray_id(record.reject_trays, tray_key):
            return False, f'Tray {tray_key} is already assigned in Nickel Wiping.'

    partial_reject_qs = NickelWiping_PartialRejectRecord.objects.filter(
        source_lot_id__in=active_lot_ids,
    ).only('source_lot_id', 'reject_trays')
    for record in partial_reject_qs:
        if _tray_rows_contain_tray_id(record.reject_trays, tray_key):
            return False, f'Tray {tray_key} is already assigned in Nickel Wiping.'

    return True, ''


def _row_get(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _clean_tray_rows(rows, qty_keys=('qty', 'tray_quantity')):
    clean_rows = []
    for index, row in enumerate(rows or []):
        tray_id = _tray_id(_row_get(row, 'tray_id', ''))
        qty = 0
        for key in qty_keys:
            qty = _nq_int(_row_get(row, key, 0))
            if qty:
                break
        if not tray_id or qty <= 0:
            continue
        clean_rows.append({
            'tray_id': tray_id,
            'qty': qty,
            'is_top': bool(_row_get(row, 'is_top', False) or _row_get(row, 'top_tray', False)),
            '_index': index,
        })
    return clean_rows


def _mark_top_by_smallest_qty(rows):
    if not rows:
        return []
    top_index = min(
        range(len(rows)),
        key=lambda index: (rows[index]['qty'], _tray_sort_key(rows[index]['tray_id']), rows[index].get('_index', index)),
    )
    normalized = []
    for index, row in enumerate(rows):
        item = {
            'tray_id': row['tray_id'],
            'qty': row['qty'],
            'is_top': index == top_index,
        }
        item['top_tray'] = item['is_top']
        normalized.append(item)
    return sorted(normalized, key=lambda row: (not row['is_top'], _tray_sort_key(row['tray_id'])))


def build_reject_slots(rejected_qty, reject_capacity):
    rejected_qty = _nq_int(rejected_qty)
    reject_capacity = max(_nq_int(reject_capacity), 1)
    quantities = []
    remaining_qty = rejected_qty
    while remaining_qty > 0:
        slot_qty = min(remaining_qty, reject_capacity)
        quantities.append(slot_qty)
        remaining_qty -= slot_qty

    sorted_quantities = sorted(enumerate(quantities), key=lambda item: (item[1], item[0]))
    return [
        {
            'qty': qty,
            'is_top': index == 0,
            'slot_no': index + 1,
        }
        for index, (_, qty) in enumerate(sorted_quantities)
    ]


def _build_accept_slot_quantities(accepted_qty, accept_capacity):
    accepted_qty = _nq_int(accepted_qty)
    accept_capacity = _nq_int(accept_capacity)
    if accepted_qty <= 0 or accept_capacity <= 0:
        return []

    full_count, remainder = divmod(accepted_qty, accept_capacity)
    quantities = []
    if remainder:
        quantities.append(remainder)
    quantities.extend([accept_capacity] * full_count)
    return quantities


def _sort_accept_candidates_for_capacity(rows):
    if not rows:
        return []

    top_row = min(
        rows,
        key=lambda row: (
            row.get('source_qty', row['qty']),
            _tray_sort_key(row['tray_id']),
            row.get('_index', 0),
        ),
    )
    top_tray_id = top_row['tray_id']
    sorted_rows = [row for row in rows if row['tray_id'] == top_tray_id]
    sorted_rows.extend(
        sorted(
            [row for row in rows if row['tray_id'] != top_tray_id],
            key=lambda row: (
                bool(row.get('_partial_remainder', False)),
                _tray_sort_key(row['tray_id']),
                row.get('_index', 0),
            ),
        )
    )
    return sorted_rows


def _apply_accept_capacity_shape(accept_rows, accepted_qty, accept_capacity):
    quantities = _build_accept_slot_quantities(accepted_qty, accept_capacity)
    if not quantities:
        return [], []

    ordered_rows = _sort_accept_candidates_for_capacity(accept_rows)
    selected_rows = ordered_rows[:len(quantities)]
    surplus_rows = ordered_rows[len(quantities):]

    shaped_rows = []
    for index, (row, qty) in enumerate(zip(selected_rows, quantities)):
        item = {
            'tray_id': row['tray_id'],
            'qty': qty,
            'is_top': index == 0,
        }
        item['top_tray'] = item['is_top']
        shaped_rows.append(item)

    surplus_delink_slots = [
        {
            'tray_id': row['tray_id'],
            'qty': row.get('source_qty', row['qty']),
            'is_required': True,
        }
        for row in surplus_rows
    ]
    return shaped_rows, surplus_delink_slots


def _validate_accept_capacity_shape(rows, accepted_qty=None, accept_capacity=None):
    clean_rows = _mark_top_by_smallest_qty(rows)
    accept_capacity = _nq_int(accept_capacity)
    if not clean_rows or accept_capacity <= 0:
        return clean_rows

    top_seen = False
    for row in clean_rows:
        qty = _nq_int(row.get('qty'))
        if qty > accept_capacity:
            raise ValueError(f"Accept tray {row['tray_id']} qty exceeds max {accept_capacity}.")
        if row.get('is_top'):
            top_seen = True
        elif qty != accept_capacity:
            raise ValueError('Only the accept top tray may have a partial quantity.')

    if clean_rows and not top_seen:
        raise ValueError('Accept top tray is required.')

    if accepted_qty is not None and tray_qty_total(clean_rows) != _nq_int(accepted_qty):
        raise ValueError('Accept tray total does not match accepted qty.')

    return clean_rows


def build_nq_rejection_allocation(
    original_trays,
    rejected_qty,
    reject_capacity,
    accept_capacity=None,
    accepted_qty=None,
):
    original_rows = _clean_tray_rows(original_trays)
    rejected_qty = _nq_int(rejected_qty)
    accepted_qty = _nq_int(accepted_qty)
    accept_capacity = _nq_int(accept_capacity)
    if accepted_qty <= 0:
        accepted_qty = max(sum(row['qty'] for row in original_rows) - rejected_qty, 0)
    remaining_reject_qty = rejected_qty
    delink_slots = []
    accept_auto_trays = []

    for row in original_rows:
        if remaining_reject_qty <= 0:
            accept_auto_trays.append({
                'tray_id': row['tray_id'],
                'qty': row['qty'],
                'source_qty': row['qty'],
                '_index': row.get('_index', 0),
            })
        elif row['qty'] <= remaining_reject_qty:
            delink_slots.append({
                'tray_id': row['tray_id'],
                'qty': row['qty'],
                'is_required': True,
            })
            remaining_reject_qty -= row['qty']
        else:
            accept_auto_trays.append({
                'tray_id': row['tray_id'],
                'qty': row['qty'] - remaining_reject_qty,
                'source_qty': row['qty'],
                '_index': row.get('_index', 0),
                '_partial_remainder': True,
            })
            remaining_reject_qty = 0

    if accept_capacity > 0:
        accept_auto_trays, surplus_delink_slots = _apply_accept_capacity_shape(
            accept_auto_trays,
            accepted_qty,
            accept_capacity,
        )
        delink_slots.extend(surplus_delink_slots)
    else:
        accept_auto_trays = _mark_top_by_smallest_qty(accept_auto_trays)

    accept_slots = [
        {
            'qty': row['qty'],
            'is_top': row['is_top'],
            'slot_no': index + 1,
        }
        for index, row in enumerate(accept_auto_trays)
    ]

    return {
        'reject_slots': build_reject_slots(rejected_qty, reject_capacity),
        'delink_slots': delink_slots,
        'auto_delink_tray_ids': [row['tray_id'] for row in delink_slots],
        'accept_auto_trays': accept_auto_trays,
        'accept_slots': accept_slots,
    }


def normalize_reject_trays(reject_trays, expected_slots):
    rows = _clean_tray_rows(reject_trays)
    expected_qtys = [slot['qty'] for slot in expected_slots or []]
    if len(rows) != len(expected_qtys):
        raise ValueError('Scan all reject tray slots before submitting.')

    seen_trays = set()
    for row in rows:
        if row['tray_id'] in seen_trays:
            raise ValueError(f"Duplicate reject tray {row['tray_id']} scanned.")
        seen_trays.add(row['tray_id'])

    rows = sorted(rows, key=lambda row: (row['qty'], row['_index']))
    if [row['qty'] for row in rows] != expected_qtys:
        raise ValueError('Reject tray quantities do not match backend allocation.')

    normalized = []
    for index, row in enumerate(rows):
        item = {
            'tray_id': row['tray_id'],
            'qty': row['qty'],
            'is_top': index == 0,
            'slot_no': index + 1,
        }
        item['top_tray'] = item['is_top']
        normalized.append(item)
    return normalized


def normalize_delink_trays(delink_trays, expected_delink_slots):
    expected_slots = expected_delink_slots or []
    expected_ids = [_tray_id(slot.get('tray_id')) for slot in expected_slots]
    expected_id_set = set(expected_ids)

    submitted_ids = []
    for row in delink_trays or []:
        tray_id = _tray_id(row.get('tray_id') if isinstance(row, dict) else row)
        if tray_id:
            submitted_ids.append(tray_id)

    if not expected_ids:
        if submitted_ids:
            raise ValueError('No delink trays are required for this rejection.')
        return []

    submitted_id_set = set(submitted_ids)
    if len(submitted_ids) != len(submitted_id_set):
        raise ValueError('Duplicate delink tray scanned.')

    if submitted_id_set != expected_id_set:
        missing_ids = [tray_id for tray_id in expected_ids if tray_id not in submitted_id_set]
        extra_ids = [tray_id for tray_id in submitted_ids if tray_id not in expected_id_set]
        details = []
        if missing_ids:
            details.append('missing ' + ', '.join(missing_ids))
        if extra_ids:
            details.append('unexpected ' + ', '.join(extra_ids))
        raise ValueError('Scan/tap all required delink trays: ' + '; '.join(details))

    return [
        {
            'tray_id': slot['tray_id'],
            'qty': slot['qty'],
            'is_delinked': True,
            'slot_no': index + 1,
        }
        for index, slot in enumerate(expected_slots)
    ]


def normalize_operator_delink_trays(delink_trays, expected_delink_slots, original_trays):
    expected_slots = expected_delink_slots or []
    submitted_ids = []

    for row in delink_trays or []:
        if isinstance(row, dict):
            raw_tray_id = row.get('tray_id', '')
        else:
            raw_tray_id = _row_get(row, 'tray_id', row)
        tray_id = _tray_id(raw_tray_id)
        if tray_id:
            submitted_ids.append(tray_id)

    if len(submitted_ids) != len(expected_slots):
        raise ValueError('Scan all delink tray slots before submitting.')

    original_rows = _clean_tray_rows(original_trays)
    original_qty_by_id = {row['tray_id']: row['qty'] for row in original_rows}
    seen_trays = set()
    normalized = []

    for index, tray_id in enumerate(submitted_ids):
        if tray_id in seen_trays:
            raise ValueError(f"Duplicate delink tray {tray_id} scanned.")
        if tray_id not in original_qty_by_id:
            raise ValueError(f"Delink tray {tray_id} is not an original tray for this lot.")
        seen_trays.add(tray_id)
        normalized.append({
            'tray_id': tray_id,
            'qty': original_qty_by_id[tray_id],
            'is_delinked': True,
            'slot_no': index + 1,
        })
    return normalized


def normalize_accept_trays(
    accept_trays,
    expected_accept_trays,
    original_trays=None,
    delink_trays=None,
    accepted_qty=None,
    accept_capacity=None,
):
    rows = _clean_tray_rows(accept_trays)
    expected_rows = _clean_tray_rows(expected_accept_trays)
    expected_qty_by_id = {row['tray_id']: row['qty'] for row in expected_rows}

    if original_trays is not None:
        if len(rows) != len(expected_rows):
            raise ValueError('Scan the accept top tray and all auto-filled accept trays before submitting.')
        if sorted(row['qty'] for row in rows) != sorted(row['qty'] for row in expected_rows):
            raise ValueError('Accept tray quantities do not match backend allocation.')

        original_rows = _clean_tray_rows(original_trays)
        original_qty_by_id = {row['tray_id']: row['qty'] for row in original_rows}
        delink_ids = {
            _tray_id(row.get('tray_id') if isinstance(row, dict) else row)
            for row in (delink_trays or [])
        }

        seen_trays = set()
        for row in rows:
            tray_id = row['tray_id']
            if tray_id in seen_trays:
                raise ValueError(f"Duplicate accept tray {tray_id} scanned.")
            if tray_id in delink_ids:
                raise ValueError(f"Accept tray {tray_id} is already selected as delink tray.")
            if tray_id not in original_qty_by_id:
                raise ValueError(f"Accept tray {tray_id} is not an original tray for this lot.")
            seen_trays.add(tray_id)
        return _validate_accept_capacity_shape(rows, accepted_qty, accept_capacity)

    if expected_rows:
        if len(rows) != len(expected_rows):
            raise ValueError('Scan the accept top tray and all auto-filled accept trays before submitting.')
        submitted_ids = {row['tray_id'] for row in rows}
        expected_ids = set(expected_qty_by_id.keys())
        if submitted_ids != expected_ids:
            missing_ids = [tray_id for tray_id in expected_qty_by_id if tray_id not in submitted_ids]
            extra_ids = [row['tray_id'] for row in rows if row['tray_id'] not in expected_ids]
            details = []
            if missing_ids:
                details.append('missing ' + ', '.join(missing_ids))
            if extra_ids:
                details.append('unexpected ' + ', '.join(extra_ids))
            raise ValueError('Accept tray list does not match backend allocation: ' + '; '.join(details))
        for row in rows:
            if row['qty'] != expected_qty_by_id[row['tray_id']]:
                raise ValueError(f"Accept tray {row['tray_id']} qty must be {expected_qty_by_id[row['tray_id']]}")

    seen_trays = set()
    for row in rows:
        if row['tray_id'] in seen_trays:
            raise ValueError(f"Duplicate accept tray {row['tray_id']} scanned.")
        seen_trays.add(row['tray_id'])
    return _validate_accept_capacity_shape(rows, accepted_qty, accept_capacity)


def validate_original_tray_coverage(accept_trays, delink_trays, original_trays, reject_trays=None):
    original_ids = {
        row['tray_id']
        for row in _clean_tray_rows(original_trays)
    }
    # A freed original tray reused directly as its own reject container (instead
    # of being delinked separately) also counts as covering that original tray —
    # only reused original IDs count here, not brand-new NB/JB reject trays.
    reused_original_ids = {
        row['tray_id']
        for row in _clean_tray_rows(reject_trays)
        if row['tray_id'] in original_ids
    }
    submitted_ids = {
        row['tray_id']
        for row in _clean_tray_rows(accept_trays)
    } | {
        row['tray_id']
        for row in _clean_tray_rows(delink_trays)
    } | reused_original_ids

    missing_ids = sorted(original_ids - submitted_ids)
    extra_ids = sorted(submitted_ids - original_ids)
    if missing_ids or extra_ids:
        details = []
        if missing_ids:
            details.append('missing ' + ', '.join(missing_ids))
        if extra_ids:
            details.append('unexpected ' + ', '.join(extra_ids))
        raise ValueError('Accept and delink trays must cover the original lot trays: ' + '; '.join(details))


def tray_qty_total(rows):
    return sum(_nq_int(row.get('qty')) for row in rows or [])
