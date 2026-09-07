"""
Shared utility for fetching tray data from upstream tables when no tray records
exist in the current stage (NickelQcTrayId, JigUnload_TrayId, etc.).

Used by: Nickel Inspection (Z1/Z2), Nickel Audit (Z1/Z2) PickTrayIdList views.
"""
import logging
import re

from django.db.models import Q

logger = logging.getLogger(__name__)


TRAY_ID_FORMAT_PATTERN = re.compile(r'^[A-Z]+-A\d{5}$')


def normalize_jig_unload_tray_id(raw_tray_id):
    return str(raw_tray_id or '').strip().upper()


def normalize_combine_lot_id(combined):
    """Normalise a JigUnloadAfterTable.combine_lot_ids entry to its plain source lot_id.

    Zone 1 stores plain lot_ids (e.g. 'LID210820252112300004') while Zone 2 stores
    prefixed variants (e.g. 'JLOT-8CEDE491A4A3-LID210820252112300004' or
    'JLOT-8CEDE491A4A3:LID210820252112300004'). Without normalising both formats to
    the same plain lot_id, the same source lot submitted from both zones is treated
    as two unrelated lots instead of being merged into a single combined lot.
    """
    if not combined:
        return combined
    s = str(combined).strip().lstrip('-')
    if ':' in s:
        possible_lot = s.rsplit(':', 1)[-1].strip()
        if possible_lot:
            return possible_lot
    if s.startswith('JLOT-') and '-' in s[5:]:
        return s.rsplit('-', 1)[1]
    return s


def is_valid_jig_unload_tray_id_format(raw_tray_id):
    return bool(TRAY_ID_FORMAT_PATTERN.match(normalize_jig_unload_tray_id(raw_tray_id)))


def _tray_id_variants(raw_tray_id):
    tray_id = normalize_jig_unload_tray_id(raw_tray_id)
    if not tray_id:
        return set()

    variants = {tray_id}
    match = re.match(r'^([A-Z]+)-A(\d+)$', tray_id)
    if match:
        prefix, digits = match.groups()
        if len(digits) <= 5:
            variants.add(f'{prefix}-A{digits.zfill(5)}')
    return variants


def _lot_id_aliases(raw_lot_id):
    value = str(raw_lot_id or '').strip()
    if not value:
        return set()

    aliases = {value}
    if ':' in value:
        aliases.add(value.rsplit(':', 1)[-1].strip())
    if value.startswith('JLOT-') and '-' in value[5:]:
        aliases.add(value.rsplit('-', 1)[-1].strip())
    return {alias for alias in aliases if alias}


def _allowed_lot_aliases(allowed_lot_ids):
    aliases = set()
    for lot_id in allowed_lot_ids or []:
        aliases.update(_lot_id_aliases(lot_id))
    return aliases


def _iter_payload_dicts(payload):
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            if isinstance(value, (dict, list)):
                yield from _iter_payload_dicts(value)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, (dict, list)):
                yield from _iter_payload_dicts(item)


def _payload_contains_tray_id(payload, tray_variants):
    for entry in _iter_payload_dicts(payload):
        entry_tray_id = entry.get('tray_id') or entry.get('trayId')
        if not entry_tray_id:
            continue
        entry_tray_id = normalize_jig_unload_tray_id(entry_tray_id)
        if not is_valid_jig_unload_tray_id_format(entry_tray_id):
            continue
        if _tray_id_variants(entry_tray_id) & tray_variants:
            return True
    return False


def _collect_lot_aliases_from_payload(payload):
    aliases = set()
    scalar_keys = ('lot_id', 'main_lot_id', 'source_lot_id', 'primary_lot_id')
    list_keys = ('combined_lot_ids', 'source_lot_ids')

    for entry in _iter_payload_dicts(payload):
        for key in scalar_keys:
            aliases.update(_lot_id_aliases(entry.get(key)))
        for key in list_keys:
            value = entry.get(key)
            if isinstance(value, list):
                for lot_id in value:
                    aliases.update(_lot_id_aliases(lot_id))
    return aliases


def _variant_query(field_name, tray_variants):
    query = Q()
    for tray_id in tray_variants:
        query |= Q(**{f'{field_name}__iexact': tray_id})
    return query


def _make_tray_conflict(tray_id, source, linked_lot='', record_id=None):
    linked_lot_text = linked_lot or 'another lot'
    return {
        'occupied': True,
        'tray_id': tray_id,
        'source': source,
        'linked_lot': linked_lot,
        'record_id': record_id,
        'message': (
            f'Tray "{tray_id}" is already reserved for {linked_lot_text} in {source}. '
            'Please use a free tray or release/delink the existing tray first.'
        ),
    }


def _jig_unload_safe_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _jig_unload_reject_conflict(tray_id, source):
    return {
        'occupied': True,
        'tray_id': tray_id,
        'source': source,
        'linked_lot': '',
        'message': 'Tray is occupied.',
    }


def _is_jig_unload_tray_master_released(tray_id):
    from modelmasterapp.models import TrayId

    return TrayId.objects.filter(
        tray_id__iexact=tray_id,
        delink_tray=True,
        scanned=False,
    ).exists()


def _is_iqf_lot_active_for_jig_unload_reject(lot_id, current_lot_id=None):
    lot_key = str(lot_id or '').strip()
    current_lot = str(current_lot_id or '').strip()
    if not lot_key or (current_lot and lot_key == current_lot):
        return False

    from modelmasterapp.models import TotalStockModel

    return TotalStockModel.objects.filter(
        Q(lot_id=lot_key) | Q(brass_qc_transition_reject_lot_id=lot_key),
    ).filter(
        Q(current_stage__iexact='IQF') | Q(iqf_onhold_picking=True)
    ).exists()


def _iqf_reject_snapshot_contains_tray(payload, tray_id):
    tray_key = normalize_jig_unload_tray_id(tray_id)
    if not tray_key:
        return False

    for entry in _iter_payload_dicts(payload):
        entry_tray_id = (
            entry.get('tray_id')
            or entry.get('trayId')
            or entry.get('rejected_tray_id')
        )
        if normalize_jig_unload_tray_id(entry_tray_id) != tray_key:
            continue
        if _jig_unload_safe_int(
            entry.get('qty')
            or entry.get('tray_qty')
            or entry.get('tray_quantity')
            or entry.get('rejected_tray_quantity')
            or entry.get('remaining_qty')
        ) > 0:
            return True
    return False


def _is_iqf_reject_snapshot_active_for_jig_unload(tray_id, current_lot_id=None):
    """Return True when IQF reject history still owns this tray for JU."""
    tray_key = normalize_jig_unload_tray_id(tray_id)
    if not tray_key:
        return False

    try:
        from IQF.models import (
            IQF_Submitted,
            IQF_PartialRejectLot,
            IQF_Rejected_TrayScan,
        )
    except ImportError:
        logger.warning(
            "IQF reject snapshot models unavailable for Jig Unloading tray_id=%s",
            tray_key,
        )
        return False

    if _is_jig_unload_tray_master_released(tray_key):
        return False

    submissions = IQF_Submitted.objects.filter(
        is_completed=True,
        rejected_qty__gt=0,
    ).exclude(
        full_reject_data__isnull=True,
        partial_reject_data__isnull=True,
    ).only('lot_id', 'full_reject_data', 'partial_reject_data')
    for submission in submissions.iterator():
        if not _is_iqf_lot_active_for_jig_unload_reject(
            submission.lot_id,
            current_lot_id=current_lot_id,
        ):
            continue
        if (
            _iqf_reject_snapshot_contains_tray(submission.full_reject_data, tray_key)
            or _iqf_reject_snapshot_contains_tray(submission.partial_reject_data, tray_key)
        ):
            return True

    reject_lots = IQF_PartialRejectLot.objects.filter(
        rejected_qty__gt=0,
    ).exclude(trays_snapshot__isnull=True).only(
        'new_lot_id',
        'parent_lot_id',
        'trays_snapshot',
    )
    for reject_lot in reject_lots.iterator():
        if not (
            _is_iqf_lot_active_for_jig_unload_reject(
                reject_lot.new_lot_id,
                current_lot_id=current_lot_id,
            )
            or _is_iqf_lot_active_for_jig_unload_reject(
                reject_lot.parent_lot_id,
                current_lot_id=current_lot_id,
            )
        ):
            continue
        if _iqf_reject_snapshot_contains_tray(reject_lot.trays_snapshot, tray_key):
            return True

    rejected_scans = IQF_Rejected_TrayScan.objects.filter(
        tray_id__iexact=tray_key,
    ).only('lot_id', 'rejected_tray_quantity')
    for rejected_scan in rejected_scans.iterator():
        if not _is_iqf_lot_active_for_jig_unload_reject(
            rejected_scan.lot_id,
            current_lot_id=current_lot_id,
        ):
            continue
        if _jig_unload_safe_int(rejected_scan.rejected_tray_quantity) > 0:
            return True

    return False


def validate_jig_unload_nickel_reject_tray(raw_tray_id, current_lot_id=None):
    """Return a JU-style conflict when an active reject tray is still owned."""
    tray_id = normalize_jig_unload_tray_id(raw_tray_id)
    if not tray_id:
        return None

    try:
        from Brass_QC.services.validators import (
            has_active_input_screening_reject_occupancy,
            validate_tray_not_rejected_in_brass_qc,
        )

        if has_active_input_screening_reject_occupancy(
            tray_id,
            str(current_lot_id or '').strip() or None,
        ):
            return _jig_unload_reject_conflict(
                tray_id,
                'Input Screening reject tray',
            )
        if validate_tray_not_rejected_in_brass_qc(tray_id):
            return _jig_unload_reject_conflict(tray_id, 'Brass QC reject tray')
    except ImportError:
        logger.warning(
            "Brass QC/Input Screening validator unavailable for Jig Unloading tray_id=%s",
            tray_id,
        )

    try:
        from IQF.services.validators import _is_iqf_rejected_tray_active_elsewhere

        if _is_iqf_rejected_tray_active_elsewhere(
            tray_id,
            str(current_lot_id or '').strip(),
        ):
            return _jig_unload_reject_conflict(tray_id, 'IQF reject tray')
    except ImportError:
        logger.warning(
            "IQF reject tray validator unavailable for Jig Unloading tray_id=%s",
            tray_id,
        )

    if _is_iqf_reject_snapshot_active_for_jig_unload(
        tray_id,
        current_lot_id=current_lot_id,
    ):
        return _jig_unload_reject_conflict(tray_id, 'IQF reject tray')

    try:
        from modelmasterapp.models import TrayId
        from Nickel_Inspection.services import validate_nickel_wiping_rejection_tray_available

        if TrayId.objects.filter(tray_id__iexact=tray_id).exists():
            available, _message = validate_nickel_wiping_rejection_tray_available(
                tray_id,
                current_lot_id=current_lot_id,
                lock_master=False,
            )
            if not available:
                return _jig_unload_reject_conflict(
                    tray_id,
                    'Nickel reject tray',
                )
    except ImportError:
        logger.exception(
            "Nickel reject tray validator unavailable for Jig Unloading tray_id=%s",
            tray_id,
        )
        return None

    return None


def _has_allowed_lot(record_lot_aliases, allowed_aliases):
    return bool(record_lot_aliases and allowed_aliases and record_lot_aliases & allowed_aliases)


def find_jig_unload_tray_conflict(raw_tray_id, allowed_lot_ids=None, include_tray_master=False):
    """Return a conflict dict when a tray is reserved by another active lot.

    Jig Unloading keeps in-progress tray scans in JSON-backed draft/autosave
    records before final submit. Those records must reserve tray IDs just like
    final tray rows, otherwise the same physical tray can be scanned into a
    second lot while the first lot is still pending submission.
    """
    tray_id = normalize_jig_unload_tray_id(raw_tray_id)
    tray_variants = _tray_id_variants(tray_id)
    if not tray_variants:
        return None

    allowed_aliases = _allowed_lot_aliases(allowed_lot_ids)

    from modelmasterapp.models import TrayId
    from Jig_Unloading.models import JigUnload_TrayId, JigUnloadDraft, JigUnloadAutoSave, JUSubmittedZ1
    from Jig_Loading.models import ExcessLotTray

    # A tray that has been officially delinked in the TrayId master (CLAUDE.md
    # §7 "Delink Rules" — the authoritative state update) is free for reuse even
    # if an older submitted/draft/autosave JSON payload still lists it. The
    # JSON-backed branches below have no per-tray delink flag of their own
    # (unlike the JigUnload_TrayId branch, which already skips delinked rows),
    # so honour the master delink state and skip them to avoid blocking a
    # legitimately released tray forever. Live occupancy is still enforced by
    # the JigUnload_TrayId branch, which only matches non-delinked rows.
    master_delinked = TrayId.objects.filter(
        _variant_query('tray_id', tray_variants), delink_tray=True
    ).exists()

    if include_tray_master:
        for excess_tray in ExcessLotTray.objects.filter(_variant_query('tray_id', tray_variants)).select_related(
            'excess_lot'
        ).only(
            'id', 'tray_id', 'lot_id', 'qty', 'excess_lot__new_lot_id', 'excess_lot__parent_lot_id'
        ):
            record_lots = _lot_id_aliases(excess_tray.lot_id)
            excess_lot = getattr(excess_tray, 'excess_lot', None)
            if excess_lot:
                record_lots.update(_lot_id_aliases(excess_lot.new_lot_id))
            if _has_allowed_lot(record_lots, allowed_aliases):
                continue
            return _make_tray_conflict(
                tray_id,
                'Jig Loading excess tray',
                next(iter(record_lots), ''),
                excess_tray.id,
            )

    if include_tray_master:
        for tray in TrayId.objects.filter(_variant_query('tray_id', tray_variants)).only(
            'id', 'tray_id', 'lot_id', 'scanned', 'delink_tray'
        ):
            if tray.delink_tray:
                continue
            record_lots = _lot_id_aliases(tray.lot_id)
            if _has_allowed_lot(record_lots, allowed_aliases):
                continue
            if tray.scanned or record_lots:
                return _make_tray_conflict(
                    tray_id,
                    'Tray master',
                    next(iter(record_lots), ''),
                    tray.id,
                )

    for tray in JigUnload_TrayId.objects.filter(_variant_query('tray_id', tray_variants)).only(
        'id', 'tray_id', 'lot_id', 'delink_tray'
    ):
        if tray.delink_tray:
            continue
        record_lots = _lot_id_aliases(tray.lot_id)
        if _has_allowed_lot(record_lots, allowed_aliases):
            continue
        return _make_tray_conflict(
            tray_id,
            'Jig Unloading submitted trays',
            next(iter(record_lots), ''),
            tray.id,
        )

    if master_delinked:
        # Tray was officially released in the master — the stale JSON-backed
        # references below no longer represent an active reservation.
        return None

    submitted_rows = JUSubmittedZ1.objects.exclude(tray_data__isnull=True).only(
        'id', 'jig_completed_id', 'lot_id', 'tray_data', 'is_draft'
    )
    for submitted in submitted_rows.iterator():
        if not _payload_contains_tray_id(submitted.tray_data, tray_variants):
            continue
        record_lots = _lot_id_aliases(submitted.lot_id)
        record_lots.update(_collect_lot_aliases_from_payload(submitted.tray_data))
        if _has_allowed_lot(record_lots, allowed_aliases):
            continue
        source = 'Jig Unloading draft/model save' if submitted.is_draft else 'Jig Unloading model save'
        return _make_tray_conflict(tray_id, source, next(iter(record_lots), ''), submitted.id)

    draft_rows = JigUnloadDraft.objects.exclude(draft_data__isnull=True).only(
        'draft_id', 'main_lot_id', 'combined_lot_ids', 'draft_data'
    )
    for draft in draft_rows.iterator():
        if not _payload_contains_tray_id(draft.draft_data, tray_variants):
            continue
        record_lots = _lot_id_aliases(draft.main_lot_id)
        for combined_lot_id in draft.combined_lot_ids or []:
            record_lots.update(_lot_id_aliases(combined_lot_id))
        record_lots.update(_collect_lot_aliases_from_payload(draft.draft_data))
        if _has_allowed_lot(record_lots, allowed_aliases):
            continue
        return _make_tray_conflict(
            tray_id,
            'Jig Unloading draft',
            next(iter(record_lots), ''),
            draft.draft_id,
        )

    autosave_rows = JigUnloadAutoSave.objects.exclude(tray_data__isnull=True).only(
        'id', 'main_lot_id', 'combined_lot_ids', 'tray_data', 'updated_at'
    )
    for autosave in autosave_rows.iterator():
        if autosave.is_expired() or not autosave.has_meaningful_data():
            continue
        if not _payload_contains_tray_id(autosave.tray_data, tray_variants):
            continue
        record_lots = _lot_id_aliases(autosave.main_lot_id)
        for combined_lot_id in autosave.combined_lot_ids or []:
            record_lots.update(_lot_id_aliases(combined_lot_id))
        record_lots.update(_collect_lot_aliases_from_payload(autosave.tray_data))
        if _has_allowed_lot(record_lots, allowed_aliases):
            continue
        return _make_tray_conflict(
            tray_id,
            'Jig Unloading autosave',
            next(iter(record_lots), ''),
            autosave.id,
        )

    return None


def get_model_master_tray_info(plating_stk_no, fallback_type='', fallback_cap=0):
    """
    Dynamically look up tray type code from ModelMaster by plating stock number.
    Returns (tray_type_str, tray_capacity_int).
    Falls back to provided defaults if lookup fails.
    """
    if plating_stk_no:
        from modelmasterapp.models import ModelMaster
        mm = ModelMaster.objects.select_related('tray_type').filter(
            plating_stk_no=plating_stk_no
        ).first()
        if mm and mm.tray_type:
            return mm.tray_type.tray_type, mm.tray_capacity or fallback_cap
    return fallback_type, fallback_cap


def get_upstream_tray_distribution(lot_id):
    """
    When no tray records exist in the current-stage tables, look up the
    JigUnloadAfterTable for combine_lot_ids, then fetch REAL tray IDs from
    the nearest upstream table that has data.

    Quantities are redistributed to match JigUnloadAfterTable.total_case_qty.

    Returns:
        (list[dict], str) — (tray_data_list, tray_source) on success
        (None, None)      — when no upstream data is available
    """
    from Jig_Unloading.models import JigUnloadAfterTable, JigUnload_TrayId, JUSubmittedZ1
    from BrassAudit.models import BrassAuditTrayId, Brass_Audit_Accepted_TrayID_Store
    from Brass_QC.models import BrassTrayId
    from Jig_Loading.models import JigLoadTrayId

    # 1. Get JigUnloadAfterTable record for this UNLOT lot_id
    juat = JigUnloadAfterTable.objects.filter(lot_id=lot_id).first()
    if not juat:
        return None, None

    combine_lot_ids = juat.combine_lot_ids or []
    if not combine_lot_ids:
        return None, None

    total_qty = juat.total_case_qty or 0
    tray_capacity = juat.tray_capacity or 16

    if total_qty <= 0:
        return None, None

    # 2. Try JigUnload_TrayId with combine_lot_ids first
    for lid in combine_lot_ids:
        trays = JigUnload_TrayId.objects.filter(lot_id=lid).order_by('id')
        if trays.exists():
            data = []
            for idx, t in enumerate(trays, 1):
                data.append({
                    's_no': idx,
                    'tray_id': t.tray_id,
                    'tray_quantity': t.tray_qty or 0,
                    'top_tray': t.top_tray,
                    'delink_tray': t.delink_tray,
                    'rejected_tray': t.rejected_tray,
                })
            logger.info(
                "[upstream_tray] Found %d trays in JigUnload_TrayId for %s (via %s)",
                len(data), lot_id, lid,
            )
            return data, "JigUnload_TrayId (via combine_lot_ids)"

    # 2b. Try JUSubmittedZ1.tray_data (Zone 1 Jig Unloading stores tray scans here)
    for lid in combine_lot_ids:
        ju_sub = JUSubmittedZ1.objects.filter(lot_id=lid, is_draft=False).order_by('-submitted_at').first()
        if ju_sub and ju_sub.tray_data:
            data = []
            for idx, t in enumerate(ju_sub.tray_data, 1):
                tray_id = t.get('tray_id', '')
                if not tray_id:
                    continue
                data.append({
                    's_no': idx,
                    'tray_id': tray_id,
                    'tray_quantity': t.get('qty', t.get('tray_qty', 0)) or 0,
                    'top_tray': t.get('is_top_tray', False),
                    'delink_tray': False,
                    'rejected_tray': False,
                })
            if data:
                logger.info(
                    "[upstream_tray] Found %d trays in JUSubmittedZ1 for %s (via %s)",
                    len(data), lot_id, lid,
                )
                print(f"✅ Found {len(data)} trays from JUSubmittedZ1 (via combine_lot_ids)")
                return data, "JUSubmittedZ1 (via combine_lot_ids)"

    # 3. Search upstream tables for REAL tray IDs
    #    Priority: closest upstream stage → farthest
    upstream_sources = [
        (BrassAuditTrayId, 'tray_quantity', "BrassAuditTrayId"),
        (Brass_Audit_Accepted_TrayID_Store, 'tray_qty', "Brass_Audit_Accepted_TrayID_Store"),
        (BrassTrayId, 'tray_quantity', "BrassTrayId"),
        (JigLoadTrayId, None, "JigLoadTrayId"),  # JigLoadTrayId may not have qty
    ]

    upstream_trays = []
    tray_source = None

    for SourceModel, qty_field, source_name in upstream_sources:
        for lid in combine_lot_ids:
            trays = SourceModel.objects.filter(lot_id=lid).order_by('id')
            if trays.exists():
                upstream_trays = list(trays)
                tray_source = source_name
                break
        if upstream_trays:
            break

    if not upstream_trays:
        logger.warning(
            "[upstream_tray] No upstream tray data for %s (combine_lot_ids=%s)",
            lot_id, combine_lot_ids,
        )
        return None, None

    # 4. Extract real tray IDs (prefer non-rejected, non-delinked)
    active_tray_ids = []
    top_tray_id = None

    for t in upstream_trays:
        is_rejected = getattr(t, 'rejected_tray', False)
        is_delinked = getattr(t, 'delink_tray', False)
        is_top = getattr(t, 'top_tray', False)

        if is_rejected or is_delinked:
            continue

        if is_top:
            top_tray_id = t.tray_id
        else:
            active_tray_ids.append(t.tray_id)

    # If all trays were filtered out, use all of them
    if not active_tray_ids and not top_tray_id:
        for t in upstream_trays:
            is_top = getattr(t, 'top_tray', False)
            if is_top:
                top_tray_id = t.tray_id
            else:
                active_tray_ids.append(t.tray_id)

    # 5. Redistribute quantities based on total_case_qty & tray_capacity
    num_full = total_qty // tray_capacity
    remainder = total_qty % tray_capacity
    num_trays_needed = num_full + (1 if remainder > 0 else 0)

    # Build ordered list of tray IDs: top tray first, then full trays
    ordered_ids = []
    if remainder > 0 and top_tray_id:
        ordered_ids.append(top_tray_id)
    elif remainder > 0 and active_tray_ids:
        # Use first active tray as top if no dedicated top tray
        ordered_ids.append(active_tray_ids.pop(0))

    ordered_ids.extend(active_tray_ids)

    data = []
    for i in range(num_trays_needed):
        if i >= len(ordered_ids):
            break  # Don't fabricate tray IDs

        if remainder > 0 and i == 0:
            qty = remainder
            is_top = True
        else:
            qty = tray_capacity
            is_top = False

        data.append({
            's_no': i + 1,
            'tray_id': ordered_ids[i],
            'tray_quantity': qty,
            'top_tray': is_top,
            'delink_tray': False,
            'rejected_tray': False,
        })

    logger.info(
        "[upstream_tray] Built %d trays from %s for %s (total_qty=%d, cap=%d)",
        len(data), tray_source, lot_id, total_qty, tray_capacity,
    )
    return data, f"upstream ({tray_source})"
