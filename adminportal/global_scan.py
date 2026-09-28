"""
Global Tray Search View
-----------------------
POST /adminportal/global_tray_search/
Body: { "tray_id": "JB-A00001" }

Searches active tray tables across all modules in workflow order
(newest stage first). Returns the first match with module name,
pick-table URL, and the lot_id to highlight.

ARCHITECTURE: Uses the SAME datasources as View Icon popups.
Each module check queries the active pick table queryset, not just raw tray tables.
This ensures Global Scan respects workflow state, submission flags, and module ownership.
"""
import json
import logging
import re

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.urls import reverse
from django.views import View
from django.db.models import F, Q

from adminportal.middleware import _MODULE_URL_MAP
from adminportal.services import get_user_allowed_module_names, is_admin_user

logger = logging.getLogger(__name__)

SCAN_TAG = '[GLOBAL_SCAN_API]'

def _normalize_excess_scan(value):
    return ''.join(str(value or '').split()).upper()


def _excess_entries(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _legacy_source_lot(source):
    """Match JigView's source-lot fallback for older multi-model snapshots."""
    if source.is_multi_model:
        draft = source.draft_data if isinstance(source.draft_data, dict) else {}
        tray_map = {
            item['tray_id']: item for item in _excess_entries(draft.get('tray_data'))
            if item.get('tray_id')
        }
        if not tray_map:
            tray_map = {
                item['tray_id']: item for item in _excess_entries(source.delink_tray_info)
                if item.get('tray_id')
            }
        for item in _excess_entries(source.half_filled_tray_info):
            original = tray_map.get(item.get('tray_id'), {})
            if original.get('source_lot_id'):
                return original['source_lot_id']
    return source.lot_id


def find_active_excess_lot_by_tray(tray_ids):
    """Return current excess ownership, never the historical parent lot.

    Use the same submitted snapshot window and parent/batch/jig mapping as
    JigView. Excess lots do not need the parent's Brass Audit acceptance flag.
    This read-only lookup does not authorize loading or change tray quantities.
    """
    from Jig_Loading.models import ExcessLotRecord, ExcessLotTray, JigCompleted

    variants = {_normalize_excess_scan(value) for value in tray_ids if _normalize_excess_scan(value)}
    if not variants:
        return None
    sources = list(JigCompleted.objects.filter(
        draft_status='submitted', half_filled_tray_qty__gt=0,
    ).only(
        'lot_id', 'batch_id', 'jig_id', 'half_filled_tray_info',
        'is_multi_model', 'draft_data', 'delink_tray_info',
    )[:50])
    if not sources:
        return None

    records = {}
    for record in ExcessLotRecord.objects.filter(
        parent_lot_id__in={source.lot_id for source in sources},
    ).order_by('-created_at'):
        records.setdefault(
            (record.parent_lot_id, record.parent_batch_id, record.jig_id), record,
        )

    tray_query = Q()
    for value in variants:
        tray_query |= Q(tray_id__iexact=value)
    matching_records = set(ExcessLotTray.objects.filter(
        tray_query, excess_lot_id__in=[record.pk for record in records.values()],
        qty__gt=0,
    ).values_list('excess_lot_id', flat=True))

    for source in sources:
        record = records.get((source.lot_id, source.batch_id, source.jig_id))
        snapshot = _excess_entries(source.half_filled_tray_info)
        matches = any(_normalize_excess_scan(item.get('tray_id')) in variants for item in snapshot)
        # Match the view-icon fallback only when no snapshot trays are available.
        if not matches and not (not snapshot and record and record.pk in matching_records):
            continue
        lot_id = record.new_lot_id if record else _legacy_source_lot(source)
        later_submissions = JigCompleted.objects.filter(draft_status='submitted').exclude(pk=source.pk)
        if later_submissions.filter(lot_id=lot_id).exists():
            continue
        consumed_as_secondary = any(
            str(item.get('lot_id') or '') == str(lot_id)
            for allocation in later_submissions.filter(is_multi_model=True).values_list(
                'multi_model_allocation', flat=True,
            )
            for item in _excess_entries(allocation)
        )
        if consumed_as_secondary:
            continue
        return {'lot_id': str(lot_id), 'batch_id': str(source.batch_id or ''), 'source': 'JigLoadingExcess'}
    return None


# Global Scan - F2 shortcut in header triggers a POST request to this view with the scanned tray_id.
class GlobalTraySearchView(LoginRequiredMixin, View):
    """
    Searches for a tray_id across all active module tray tables.
    Priority order (user-specified):
        Jig Loading > Jig Unloading > Nickel Wiping > Nickel Audit
        > Spider Spindle > IQF > Brass Audit > Brass QC > Input Screening
    """

    def post(self, request, *args, **kwargs):
        try:
            body = json.loads(request.body)
            tray_id = body.get('tray_id', '').strip().upper()
            current_path = body.get('current_path', '').strip()
        except (ValueError, KeyError, TypeError):
            tray_id = request.POST.get('tray_id', '').strip().upper()
            current_path = request.POST.get('current_path', '').strip()

        # Normalize: remove any whitespace, newlines, carriage returns
        tray_id = ''.join(tray_id.split())

        if not tray_id:
            return JsonResponse({'success': False, 'error': 'No tray_id provided'}, status=400)

        logger.info(
            '%s search_started tray_id=%s user=%s',
            SCAN_TAG,
            tray_id,
            request.user.username,
        )

        result = self._search_all_modules(tray_id, current_path=current_path, user=request.user)

        if result:
            if not self._user_can_access_result(request.user, result):
                module_name = result.get('module') or 'another'
                message = f"Currently it is available in '{module_name}' module"
                logger.info(
                    '%s search_restricted tray_id=%s module=%s user=%s',
                    SCAN_TAG,
                    tray_id,
                    module_name,
                    request.user.username,
                )
                return JsonResponse({
                    'success': False,
                    'found': True,
                    'restricted': True,
                    'tray_id': tray_id,
                    'message': message,
                }, status=403)

            # Add tray_id to response for frontend highlight
            result['tray_id'] = tray_id
            logger.info(
                '%s search_found tray_id=%s module=%s lot_id=%s url=%s',
                SCAN_TAG,
                tray_id,
                result['module'],
                result.get('lot_id', 'N/A'),
                result['url'],
            )
            return JsonResponse({
                'success': True,
                'found': True,
                **result
            })

        logger.info('%s search_not_found tray_id=%s', SCAN_TAG, tray_id)
        return JsonResponse({
            'success': False,
            'found': False,
            'tray_id': tray_id,
            'message': ('Tray ID does not exist'
                        if self._normalize_path(current_path).startswith('/iqf/')
                        else 'Not Exists')
        })


    @staticmethod
    def _normalize_path(path_value):
        normalized = str(path_value or '/').split('?', 1)[0].rstrip('/').lower()
        return normalized or '/'

    def _path_matches(self, response_url, current_path):
        if not current_path:
            return False
        return self._normalize_path(response_url) == self._normalize_path(current_path)

    def _required_modules_for_result(self, result):
        response_url = (result or {}).get('url')
        if not response_url:
            return set()
        normalized_url = self._normalize_path(response_url).lstrip('/')
        for prefix, modules in _MODULE_URL_MAP.items():
            if normalized_url.startswith(prefix.lower()):
                return set(modules)
        return set()

    def _user_can_access_result(self, user, result):
        if is_admin_user(user):
            return True
        required_modules = self._required_modules_for_result(result)
        if not required_modules:
            return True
        allowed_modules = set(get_user_allowed_module_names(user))
        return bool(allowed_modules.intersection(required_modules))

    def _tray_id_variants(self, tray_id):
        """Return exact tray id plus safe zero-padded suffix variants.

        Some upstream scans arrive as ND-A0002 while stored trays use ND-A00002.
        Keep matching conservative: only pad the final numeric suffix.
        """
        normalized = ''.join(str(tray_id or '').split()).upper()
        variants = {normalized} if normalized else set()
        match = re.match(r'^(.*?)(\d+)$', normalized)
        if match:
            prefix, digits = match.groups()
            number = digits.lstrip('0') or '0'
            for width in {len(digits), 5}:
                variants.add(f'{prefix}{number.zfill(width)}')
        return sorted(variants)

    def _tray_query(self, variants, field_name='tray_id'):
        query = Q()
        for candidate in variants:
            query |= Q(**{f'{field_name}__iexact': candidate})
        return query

    def _resolve_active_tray_lot_ids(self, tray_id):
        """Return active physical-tray assignments before Nickel Wiping.

        Nickel Wiping releases a tray by clearing and delinking its global
        ``TrayId`` master record.  Some valid reuse paths create the new active
        module tray row without reactivating that legacy master row, so F2 must
        also read the active module assignment.  It deliberately never reads
        Nickel Wiping or later records: those belong to a released lifecycle.
        """
        from modelmasterapp.models import TrayId
        from InputScreening.models import IPTrayId
        from Brass_QC.models import BrassTrayId
        from BrassAudit.models import BrassAuditTrayId
        from IQF.models import IQFTrayId
        from Jig_Loading.models import JigLoadTrayId
        from Jig_Unloading.models import JigUnload_TrayId

        tray_query = self._tray_query(self._tray_id_variants(tray_id))
        lot_ids = set()
        batch_ids = set()

        def collect(records):
            for record in records:
                if record.lot_id:
                    lot_ids.add(str(record.lot_id))
                batch_id = getattr(record, 'batch_id_id', None)
                if batch_id:
                    batch_ids.add(str(batch_id))

        # Keep the normal global assignment path, when the master was updated.
        collect(
            TrayId.objects.filter(
                tray_query, delink_tray=False, lot_id__isnull=False,
            ).exclude(lot_id='').only('lot_id', 'batch_id')
        )

        # A reused tray can be represented only by its current module row.
        # These are active, pre-release stages; history and Nickel records are
        # intentionally excluded.
        active_filters = {
            'delink_tray': False,
            'rejected_tray': False,
            'lot_id__isnull': False,
        }
        for tray_model in (IPTrayId, BrassTrayId, BrassAuditTrayId, IQFTrayId, JigLoadTrayId):
            collect(
                tray_model.objects.filter(tray_query, **active_filters)
                .exclude(lot_id='')
                .only('lot_id', 'batch_id')
            )

        collect(
            JigUnload_TrayId.objects.filter(
                tray_query,
                delink_tray=False,
                rejected_tray=False,
                lot_id__isnull=False,
            ).exclude(lot_id='').only('lot_id')
        )
        return lot_ids, batch_ids

    def _resolve_active_nickel_wiping_lot_ids(self, tray_id):
        """Return lots explicitly linked to a Nickel Wiping tray identifier.

        Nickel Wiping keeps its tray rows after the normal physical-tray
        lifecycle releases the tray for reuse.  It must therefore not share
        the pre-Nickel ``delink_tray=False`` rule used by
        :meth:`_resolve_active_tray_lot_ids`.  Current Nickel rows may also
        retain their source trays only in the Jig Unloading submission
        snapshot, so both sources are read with exact tray matching.  Callers
        still use the Nickel Wiping pick-table query to decide whether each
        discovered lot is currently active.
        """
        from Nickel_Inspection.models import NickelQcTrayId

        tray_query = self._tray_query(self._tray_id_variants(tray_id))
        lot_ids = []
        seen_lot_ids = set()

        def add_lot_id(lot_id):
            normalized_lot_id = str(lot_id or '').strip()
            if normalized_lot_id and normalized_lot_id not in seen_lot_ids:
                seen_lot_ids.add(normalized_lot_id)
                lot_ids.append(normalized_lot_id)
        rows = (
            NickelQcTrayId.objects.filter(tray_query, lot_id__isnull=False)
            .exclude(lot_id='')
            .order_by('-date', '-pk')
            .values_list('lot_id', flat=True)
        )
        for lot_id in rows:
            add_lot_id(lot_id)

        # Zone 1 and Zone 2 Nickel Wiping can display original tray IDs from
        # the final Jig Unloading submission rather than NickelQcTrayId. Map
        # the exact matching source lot to its generated unload lot, which is
        # the row key used by the Nickel pick tables.
        try:
            from Jig_Unloading.models import JigUnloadAfterTable, JUSubmittedZ1

            tray_variants = self._tray_id_variants(tray_id)
            submissions = (
                JUSubmittedZ1.objects.filter(is_draft=False)
                .exclude(tray_data__isnull=True)
                .only('lot_id', 'tray_data', 'updated_at')
                .order_by('-updated_at', '-pk')
            )
            for submission in submissions.iterator():
                if not self._tray_id_in_payload(submission.tray_data, tray_variants):
                    continue
                source_lot_id = str(submission.lot_id or '').strip()
                if not source_lot_id:
                    continue
                unload_lot_ids = JigUnloadAfterTable.objects.filter(
                    combine_lot_ids__contains=[source_lot_id],
                ).order_by('-created_at', '-pk').values_list('lot_id', flat=True)
                for unload_lot_id in unload_lot_ids:
                    add_lot_id(unload_lot_id)
        except Exception as e:
            logger.debug('%s Nickel Wiping submission-tray probe failed: %s', SCAN_TAG, e)
        return lot_ids

    def _resolve_active_brass_audit_lot_ids(self, tray_id):
        """Return current Brass Audit pick-table lots containing this tray.

        Brass Audit presents tray data through ``_resolve_lot_trays_audit``.
        That resolver can legitimately use a Brass QC submission snapshot after
        its physical tray rows have been delinked for reuse.  Use the same
        resolver here, but only for the current Brass Audit Pick Table lots,
        so F2 neither misses visible trays nor revives historical rows.
        """
        from BrassAudit.selectors import get_picktable_base_queryset
        from BrassAudit.views import _resolve_lot_trays_audit

        tray_variants = self._tray_id_variants(tray_id)
        lot_ids = []
        for lot_id in get_picktable_base_queryset().values_list('lot_id', flat=True):
            if not lot_id:
                continue
            tray_data, _, _ = _resolve_lot_trays_audit(lot_id)
            if self._tray_id_in_payload(tray_data, tray_variants):
                lot_ids.append(str(lot_id))
        return lot_ids

    def _resolve_nickel_audit_lot_ids(self, tray_id, nickel_wiping_lot_ids=None):
        """Return exact tray-linked candidates for the Nickel Audit pick tables.

        Nickel Audit Zone 1 and Zone 2 keep separate tray models.  A Nickel
        Audit row can also inherit a tray only through the preceding Nickel
        Wiping lot, so include the already exact-resolved Nickel candidates.
        The Nickel Audit pick-table checkers determine active ownership.
        """
        from Nickel_Audit.models import Nickel_AuditTrayId
        from nickel_audit_zone_two.models import NickelQcTrayId as NickelAuditZ2TrayId

        tray_query = self._tray_query(self._tray_id_variants(tray_id))
        lot_ids = []
        seen_lot_ids = set()
        for tray_model in (Nickel_AuditTrayId, NickelAuditZ2TrayId):
            for lot_id in tray_model.objects.filter(tray_query, lot_id__isnull=False).exclude(
                lot_id=''
            ).order_by('-date', '-pk').values_list('lot_id', flat=True):
                normalized_lot_id = str(lot_id).strip()
                if normalized_lot_id and normalized_lot_id not in seen_lot_ids:
                    seen_lot_ids.add(normalized_lot_id)
                    lot_ids.append(normalized_lot_id)

        for lot_id in nickel_wiping_lot_ids or []:
            normalized_lot_id = str(lot_id or '').strip()
            if normalized_lot_id and normalized_lot_id not in seen_lot_ids:
                seen_lot_ids.add(normalized_lot_id)
                lot_ids.append(normalized_lot_id)
        return lot_ids

    def _tray_id_in_payload(self, payload, variants):
        variant_set = {str(value or '').upper() for value in variants if value}
        if not payload or not variant_set:
            return False

        def _iter_entries(value):
            if isinstance(value, list):
                for item in value:
                    yield item
            elif isinstance(value, dict):
                yield value

        for entry in _iter_entries(payload):
            if not isinstance(entry, dict):
                continue
            tray_value = entry.get('tray_id') or entry.get('trayId') or entry.get('id')
            if tray_value and ''.join(str(tray_value).split()).upper() in variant_set:
                return True
        return False

    def _add_jig_unload_candidate_lots(self, lot_ids, submitted_record):
        if not submitted_record:
            return
        if submitted_record.lot_id:
            lot_ids.add(str(submitted_record.lot_id))
            return

        try:
            from Jig_Loading.models import JigCompleted
            jig = JigCompleted.objects.filter(id=submitted_record.jig_completed_id).first()
            if not jig:
                return
            if jig.lot_id:
                lot_ids.add(str(jig.lot_id))
        except Exception as e:
            logger.debug('%s Jig Unloading submitted candidate expansion failed: %s', SCAN_TAG, e)

    def _resolve_candidate_lot_ids(self, tray_id, user=None):
        """LOT-FIRST RESOLVER: Scan EVERY tray table to discover all lot_ids
        that this tray belongs to (currently or historically).

        A tray inherited from upstream may not exist in the current module's
        own tray table (e.g. Brass QC inherits IPTrayId from Input Screening).
        So we collect all candidate lot_ids and let the pick-table checkers
        decide which module currently owns the lot.

        Returns: set of lot_id strings (may be empty)
        Also returns: set of batch_ids (for Day Planning / IS resolution)
        """
        lot_ids = set()
        batch_ids = set()

        # Jig ID lookup: a scanned Jig ID (the "JIG ID" column shown in the
        # Jig Unloading Pick/Completed tables) identifies every lot ever loaded
        # onto that physical jig, whether or not it has been unloaded yet.
        try:
            from Jig_Loading.models import JigCompleted
            for lid in JigCompleted.objects.filter(jig_id__iexact=tray_id).values_list('lot_id', flat=True):
                if lid:
                    lot_ids.add(str(lid))
        except Exception as e:
            logger.debug('%s JigCompleted jig_id probe failed: %s', SCAN_TAG, e)

        # Jig Loading draft lookup: a scanned Jig ID identifies the drafted lot,
        # even before any tray table contains the scanned value.
        try:
            from Jig_Loading.selectors import find_active_draft_by_jig_id
            jig_draft = find_active_draft_by_jig_id(tray_id, user=user)
            if jig_draft and jig_draft.lot_id:
                lot_ids.add(str(jig_draft.lot_id))
                if jig_draft.batch_id:
                    batch_ids.add(str(jig_draft.batch_id))
        except Exception as e:
            logger.debug('%s Jig Loading draft jig probe failed: %s', SCAN_TAG, e)

        tray_variants = self._tray_id_variants(tray_id)
        tray_query = self._tray_query(tray_variants)

        # Direct lot_id lookup: the scanned value may itself be a lot_id
        # rather than a tray_id — e.g. a source lot_id printed/shown on a
        # consolidated Nickel Wiping row, or any lot label scanned straight
        # off a lot. Check the lot tables directly so these resolve without
        # requiring the value to appear in any tray table first.
        try:
            from modelmasterapp.models import TotalStockModel, ModelMasterCreation
            lot_variant_query = self._tray_query(tray_variants, field_name='lot_id')
            for lid in TotalStockModel.objects.filter(lot_variant_query).values_list('lot_id', flat=True):
                if lid:
                    lot_ids.add(lid)
            for lid in ModelMasterCreation.objects.filter(lot_variant_query).values_list('lot_id', flat=True):
                if lid:
                    lot_ids.add(lid)
        except Exception as e:
            logger.debug('%s direct lot_id probe failed: %s', SCAN_TAG, e)

        def _safe_collect(qs, attr='lot_id'):
            try:
                for val in qs.values_list(attr, flat=True):
                    if val:
                        lot_ids.add(val)
            except Exception as e:
                logger.debug('%s tray-table probe failed: %s', SCAN_TAG, e)

        # ?? Day Planning / Input Screening source tables (have batch_id) ??
        try:
            from modelmasterapp.models import TrayId, DraftTrayId, TotalStockModel
            for t in TrayId.objects.filter(tray_query):
                if getattr(t, 'lot_id', None):
                    lot_ids.add(t.lot_id)
                if t.batch_id_id:
                    batch_ids.add(t.batch_id_id)
            for t in DraftTrayId.objects.filter(tray_query):
                if getattr(t, 'lot_id', None):
                    lot_ids.add(t.lot_id)
                if getattr(t, 'batch_id_id', None):
                    batch_ids.add(t.batch_id_id)
        except Exception as e:
            logger.debug('%s TrayId probe failed: %s', SCAN_TAG, e)

        # Input Screening pick/verification uses DPTrayId_History as the live
        # tray source, so include it before batch-to-lot resolution runs.
        try:
            from DayPlanning.models import DPTrayId_History
            for t in DPTrayId_History.objects.filter(tray_query):
                if getattr(t, 'lot_id', None):
                    lot_ids.add(t.lot_id)
                if getattr(t, 'batch_id_id', None):
                    batch_ids.add(t.batch_id_id)
        except Exception as e:
            logger.debug('%s DPTrayId_History probe failed: %s', SCAN_TAG, e)

        # Resolve batch_ids ? lot_ids via TotalStockModel
        if batch_ids:
            try:
                from modelmasterapp.models import TotalStockModel
                for lid in TotalStockModel.objects.filter(
                    batch_id_id__in=batch_ids
                ).values_list('lot_id', flat=True):
                    if lid:
                        lot_ids.add(lid)
            except Exception as e:
                logger.debug('%s TotalStockModel batch probe failed: %s', SCAN_TAG, e)

        # ?? Input Screening tray tables ??
        try:
            from InputScreening.models import IPTrayId, IP_Accepted_TrayID_Store, IP_TrayVerificationStatus
            _safe_collect(IPTrayId.objects.filter(tray_query))
            _safe_collect(IP_Accepted_TrayID_Store.objects.filter(tray_query))
            _safe_collect(IP_TrayVerificationStatus.objects.filter(
                tray_query,
                is_verified=True,
                verification_status='pass',
            ))
        except Exception as e:
            logger.debug('%s IS tray probe failed: %s', SCAN_TAG, e)

        # ?? Brass QC tray tables ??
        try:
            from Brass_QC.models import BrassTrayId, Brass_Qc_Accepted_TrayID_Store
            _safe_collect(BrassTrayId.objects.filter(tray_query))
            _safe_collect(Brass_Qc_Accepted_TrayID_Store.objects.filter(tray_query))
        except Exception as e:
            logger.debug('%s Brass QC tray probe failed: %s', SCAN_TAG, e)

        # ?? Brass Audit tray tables ??
        try:
            from BrassAudit.models import BrassAuditTrayId, Brass_Audit_Accepted_TrayID_Store
            _safe_collect(BrassAuditTrayId.objects.filter(tray_query))
            _safe_collect(Brass_Audit_Accepted_TrayID_Store.objects.filter(tray_query))
        except Exception as e:
            logger.debug('%s Brass Audit tray probe failed: %s', SCAN_TAG, e)

        # ?? IQF tray tables ??
        try:
            from IQF.models import IQFTrayId, IQF_Accepted_TrayID_Store
            _safe_collect(IQFTrayId.objects.filter(tray_query))
            _safe_collect(IQF_Accepted_TrayID_Store.objects.filter(tray_query))
        except Exception as e:
            logger.debug('%s IQF tray probe failed: %s', SCAN_TAG, e)

        # ?? Jig Loading / Unloading ??
        try:
            from Jig_Loading.models import JigLoadTrayId
            _safe_collect(JigLoadTrayId.objects.filter(tray_query))
        except Exception as e:
            logger.debug('%s Jig Loading tray probe failed: %s', SCAN_TAG, e)

        try:
            from Jig_Loading.selectors import find_active_draft_by_scanned_tray
            for candidate in tray_variants:
                draft = find_active_draft_by_scanned_tray(candidate, user=user)
                if draft and draft.lot_id:
                    lot_ids.add(str(draft.lot_id))
                    if draft.batch_id:
                        batch_ids.add(str(draft.batch_id))
        except Exception as e:
            logger.debug('%s Jig Loading draft tray probe failed: %s', SCAN_TAG, e)

        try:
            from Jig_Unloading.models import JigUnload_TrayId, JUSubmittedZ1
            _safe_collect(JigUnload_TrayId.objects.filter(tray_query))
            submitted_rows = JUSubmittedZ1.objects.exclude(tray_data__isnull=True).only(
                'jig_completed_id', 'lot_id', 'tray_data', 'is_draft'
            )
            for submitted in submitted_rows.iterator():
                if self._tray_id_in_payload(submitted.tray_data, tray_variants):
                    self._add_jig_unload_candidate_lots(lot_ids, submitted)
        except Exception as e:
            logger.debug('%s Jig Unloading tray probe failed: %s', SCAN_TAG, e)

        # ?? Nickel modules ??
        try:
            from Nickel_Inspection.models import NickelQcTrayId
            _safe_collect(NickelQcTrayId.objects.filter(tray_query))
        except Exception as e:
            logger.debug('%s Nickel Insp tray probe failed: %s', SCAN_TAG, e)

        try:
            from nickel_inspection_zone_two.models import NickelQcTrayId as NQZ2
            _safe_collect(NQZ2.objects.filter(tray_query))
        except Exception as e:
            logger.debug('%s Nickel Insp Z2 tray probe failed: %s', SCAN_TAG, e)

        try:
            from Nickel_Audit.models import Nickel_AuditTrayId
            _safe_collect(Nickel_AuditTrayId.objects.filter(tray_query))
        except Exception as e:
            logger.debug('%s Nickel Audit tray probe failed: %s', SCAN_TAG, e)

        try:
            from nickel_audit_zone_two.models import NickelQcTrayId as NAZ2
            _safe_collect(NAZ2.objects.filter(tray_query))
        except Exception as e:
            logger.debug('%s Nickel Audit Z2 tray probe failed: %s', SCAN_TAG, e)

        # ?? Spider Spindle ??
        try:
            from SpiderSpindle_Z1.models import SpiderSpindleZ1TrayId
            _safe_collect(SpiderSpindleZ1TrayId.objects.filter(tray_query))
        except Exception as e:
            logger.debug('%s SS Z1 tray probe failed: %s', SCAN_TAG, e)

        try:
            from SpiderSpindle_Z2.models import SpiderSpindleZ2TrayId
            _safe_collect(SpiderSpindleZ2TrayId.objects.filter(tray_query))
        except Exception as e:
            logger.debug('%s SS Z2 tray probe failed: %s', SCAN_TAG, e)

        # Current Nickel Wiping rows may inherit tray IDs from upstream unload
        # snapshots. Map source lot IDs to the active JigUnloadAfterTable UNLOT.
        try:
            from Jig_Unloading.models import JigUnloadAfterTable
            for source_lot_id in list(lot_ids):
                _safe_collect(
                    JigUnloadAfterTable.objects.filter(
                        combine_lot_ids__contains=[source_lot_id]
                    )
                )
        except Exception as e:
            logger.debug('%s JigUnloadAfterTable combine-lot probe failed: %s', SCAN_TAG, e)

        return lot_ids, batch_ids

    def _search_all_modules(self, tray_id, current_path='', user=None):
        """LOT-FIRST search.

        1. Resolve all candidate lot_ids by scanning EVERY tray table
        2. For each module (reverse workflow order), check if any candidate
           lot_id is currently active in that module's pick table
        3. Return first module where lot is active

        Workflow: Day Planning ? Input Screening ? Brass QC ? Brass Audit ? IQF
                  ? Jig Loading ? Jig Unloading ? Nickel Wiping.  Normal tray
                  assignment lookup stops before Nickel because the physical
                  tray may be reused; Nickel has its own exact-ID resolver.
        """
        # Excess trays remain in Jig Loading after their parent jig moves on.
        # Resolve that physical tray ownership before historical lot candidates.
        excess = find_active_excess_lot_by_tray(self._tray_id_variants(tray_id))
        if excess:
            return {'module': 'Jig Loading', 'url': reverse('JigView'), **excess}

        # F2 also supports scanning a jig label on Jig Unloading screens. Keep
        # that separate from physical-tray lookup; this resolver itself filters
        # to active Jig Unloading records.
        if self._is_jig_id_format(tray_id):
            jig_result = self._resolve_jig_unloading_by_jig_id(tray_id)
            if jig_result:
                return jig_result

            # A Jig Loading draft is the current owner before the jig reaches
            # Inprocess Inspection/Jig Unloading.  Reuse the existing draft
            # selector so F2 follows the same active-draft rules as JigView.
            try:
                from Jig_Loading.selectors import find_active_draft_by_jig_id
                draft = find_active_draft_by_jig_id(tray_id, user=user)
                if draft and draft.lot_id:
                    result = self._check_lot_in_jig_loading(draft.lot_id)
                    if result and self._is_main_or_pick_result(result):
                        return result
            except Exception as e:
                logger.error('%s active Jig Loading jig lookup failed: %s', SCAN_TAG, e)
            return None

        # Nickel IDs are retained independently of reusable physical trays.
        # Resolve them by exact ID and return only rows that are still visible
        # in one of the existing Nickel Wiping pick tables.
        nickel_lot_ids = self._resolve_active_nickel_wiping_lot_ids(tray_id)
        requested_path = self._normalize_path(current_path) if current_path else ''
        nickel_fallback = None
        for label, check in (
            ('Nickel Wiping', self._check_lot_in_nickel_wiping),
            ('Nickel Wiping Z2', self._check_lot_in_nickel_wiping_z2),
        ):
            try:
                for lid in nickel_lot_ids:
                    result = check(lid)
                    if not result or not self._is_main_or_pick_result(result):
                        continue
                    logger.info('%s nickel_module_match module=%s lot_id=%s', SCAN_TAG, label, lid)
                    if nickel_fallback is None:
                        nickel_fallback = result
                    if self._path_matches(result.get('url'), requested_path):
                        return result
            except Exception as e:
                logger.error('%s Unexpected error in %s: %s', SCAN_TAG, label, e)

        nickel_audit_fallback = None
        nickel_audit_lot_ids = []
        try:
            nickel_audit_lot_ids = self._resolve_nickel_audit_lot_ids(
                tray_id, nickel_wiping_lot_ids=nickel_lot_ids,
            )
            for label, check in (
                ('Nickel Audit', self._check_lot_in_nickel_audit_z1),
                ('Nickel Audit Z2', self._check_lot_in_nickel_audit_z2),
            ):
                for lid in nickel_audit_lot_ids:
                    result = check(lid)
                    # F2 navigates only to active Pick Tables.  A tray may
                    # have an older completed Nickel Audit record as well as
                    # a newer current row; never select the completed row.
                    if not result or not self._is_main_or_pick_result(result):
                        continue
                    logger.info('%s nickel_audit_module_match module=%s lot_id=%s', SCAN_TAG, label, lid)
                    if nickel_audit_fallback is None:
                        nickel_audit_fallback = result
        except Exception as e:
            logger.error('%s Unexpected error in Nickel Audit lookup: %s', SCAN_TAG, e)

        # Spider Spindle uses the same Nickel Audit lot ID.  Resolve it here
        # for JR/NR/ND identifiers, whose tray association is no longer in a
        # normal physical-tray table by the time it reaches this stage.
        spider_spindle_fallback = None
        for label, check in (
            ('Spider Spindle Z1', self._check_lot_in_ss_z1),
            ('Spider Spindle Z2', self._check_lot_in_ss_z2),
        ):
            for lid in nickel_audit_lot_ids:
                try:
                    result = check(lid)
                    if result and self._is_main_or_pick_result(result):
                        logger.info('%s spider_spindle_module_match module=%s lot_id=%s', SCAN_TAG, label, lid)
                        if spider_spindle_fallback is None:
                            spider_spindle_fallback = result
                except Exception as e:
                    logger.error('%s Unexpected error in %s: %s', SCAN_TAG, label, e)

        # JR/NR/ND remain Nickel-specific.  Prefer their current Nickel Audit
        # successor Pick Table, then fall back through Nickel Audit and Wiping.
        if self._is_nickel_specific_tray_id(tray_id):
            return spider_spindle_fallback or nickel_audit_fallback or nickel_fallback

        # Brass Audit may display a Brass QC acceptance snapshot after the
        # physical tray has been delinked from the upstream tables.  Resolve
        # only current Brass Audit rows using that page's existing tray source.
        brass_audit_lot_ids = self._resolve_active_brass_audit_lot_ids(tray_id)
        brass_audit_fallback = None
        for lid in brass_audit_lot_ids:
            try:
                result = self._check_lot_in_brass_audit(lid)
                if not result or not self._is_main_or_pick_result(result):
                    continue
                logger.info('%s brass_audit_snapshot_match lot_id=%s', SCAN_TAG, lid)
                if brass_audit_fallback is None:
                    brass_audit_fallback = result
                if self._path_matches(result.get('url'), requested_path):
                    return result
            except Exception as e:
                logger.error('%s Unexpected error in Brass Audit snapshot lookup: %s', SCAN_TAG, e)
        # Step 1: Resolve the current physical-tray assignment only. The legacy
        # resolver intentionally reads history, which is not valid for F2.
        lot_ids, batch_ids = self._resolve_active_tray_lot_ids(tray_id)
        logger.info(
            '%s candidates_resolved tray_id=%s lot_ids=%s batch_ids=%s',
            SCAN_TAG,
            tray_id,
            sorted(lot_ids),
            sorted(batch_ids),
        )

        if not lot_ids and not batch_ids:
            logger.info('%s no_candidates tray_id=%s', SCAN_TAG, tray_id)
            return brass_audit_fallback or nickel_audit_fallback or nickel_fallback

        # Step 2: Check each module's eligible Main/Pick table only.
        # Completed/Reject/history tables are deliberately not eligible for
        # global scan navigation/highlighting.
        checks = [
            ('Spider Spindle Z1', self._check_lot_in_ss_z1),
            ('Spider Spindle Z2', self._check_lot_in_ss_z2),
            ('Inprocess Inspection', self._check_lot_in_inprocess_inspection),
            ('Jig Unloading',   self._check_lot_in_jig_unloading),
            ('IQF',             self._check_lot_in_iqf),
            ('Brass Audit',     self._check_lot_in_brass_audit),
            ('Brass QC',        self._check_lot_in_brass_qc),
            ('Input Screening', self._check_lot_in_input_screening),
            ('Day Planning',    self._check_lot_in_day_planning),
            ('Jig Loading',     self._check_lot_in_jig_loading),
        ]

        fallback_result = None
        for label, check in checks:
            try:
                for lid in lot_ids:
                    result = check(lid, tray_id=tray_id) if label == 'IQF' else check(lid)
                    if result:
                        if not self._is_main_or_pick_result(result):
                            logger.info(
                                '%s module_match_skipped_non_pick_main module=%s lot_id=%s url=%s',
                                SCAN_TAG,
                                result.get('module'),
                                lid,
                                result.get('url'),
                            )
                            continue
                        logger.info('%s module_match module=%s lot_id=%s', SCAN_TAG, label, lid)
                        if fallback_result is None:
                            fallback_result = result
                        if self._path_matches(result.get('url'), requested_path):
                            return result
            except Exception as e:
                logger.error('%s Unexpected error in %s: %s', SCAN_TAG, label, e)

        # No exact same-page match was found above. That does NOT mean the
        # tray doesn't exist - it usually means the tray belongs to a
        # DIFFERENT page than the one the user is currently on (e.g.
        # scanning from the Dashboard for a tray that lives in DP Completed
        # Table). That is a normal cross-page case and must still resolve so
        # the frontend can navigate there and highlight the row. Previously
        # this fallback only fired when requested_path was empty, which is
        # almost never true since the frontend always sends current_path -
        # so real cross-page matches were being silently discarded here.
        if fallback_result:
            return fallback_result

        # Day Planning batch fallback (when lot_id not yet created)
        if batch_ids:
            try:
                result = self._check_batch_in_day_planning(batch_ids)
                if result:
                    return result
            except Exception as e:
                logger.error('%s DP batch fallback error: %s', SCAN_TAG, e)

        # No new active lifecycle exists. A current Brass Audit snapshot is
        # next, followed by the prior Nickel Wiping lifecycle. This ordering
        # prevents an old Nickel lot from overriding a reused normal tray.
        if brass_audit_fallback:
            return brass_audit_fallback
        if spider_spindle_fallback:
            return spider_spindle_fallback
        if nickel_audit_fallback:
            return nickel_audit_fallback
        if nickel_fallback:
            return nickel_fallback

        return None

    # -- Per-lot pick-table checkers ----------------------------------------
    # Each accepts a candidate lot_id and returns module match dict if
    # that lot is currently active in the module's pick table, else None.
    # NOTE: We use the SAME queryset as the module's UI Pick Table so
    # Global Scan ownership matches what the user sees on screen.

    def _wide_date_range_query(self):
        """Query string spanning from year 2000 through today (IST).

        Several Completed/Reject table pages (Brass QC, Brass Audit,
        Nickel Audit, Spider Spindle) default to a "yesterday to today"
        date range when no from_date/to_date is given. A tray scan can
        correctly resolve to one of these pages yet still land on an
        empty-looking table if the row is older than that default window.
        Appending this wide range to the navigation URL guarantees the
        matched row is actually visible after navigating there.
        """
        try:
            from django.utils import timezone
            import pytz
            tz = pytz.timezone('Asia/Kolkata')
            today = timezone.now().astimezone(tz).date()
            return f'from_date=2000-01-01&to_date={today.isoformat()}'
        except Exception:
            return ''

    def _stock_for(self, lot_id):
        from modelmasterapp.models import TotalStockModel
        return TotalStockModel.objects.filter(lot_id=lot_id).first()

    def _is_main_or_pick_result(self, result):
        module_name = str((result or {}).get('module') or '').lower()
        if 'completed' in module_name or 'complete' in module_name or 'reject' in module_name:
            return False

        response_url = (result or {}).get('url')
        if not response_url:
            return False
        normalized_url = self._normalize_path(response_url)
        excluded_terms = ('completed', 'complete', 'reject', 'rejection')
        return not any(term in normalized_url for term in excluded_terms)

    def _page_for_value(self, queryset, field_name, value, page_size=10):
        if not value:
            return None
        values = list(queryset.values_list(field_name, flat=True))
        target = str(value)
        for index, candidate in enumerate(values):
            if str(candidate) == target:
                return (index // page_size) + 1
        return None

    def _batch_str(self, stock, fallback):
        try:
            return stock.batch_id.batch_id if stock and stock.batch_id else fallback
        except Exception:
            return fallback

    def _is_jig_id_format(self, value):
        normalized = ''.join(str(value or '').split()).upper()
        return bool(re.match(r'^(JL-[A-Z]\d{5}|J\d{3}-\d{4})$', normalized))

    @staticmethod
    def _is_nickel_specific_tray_id(value):
        normalized = ''.join(str(value or '').split()).upper()
        return normalized.startswith(('JR-', 'NR-', 'ND-'))

    def _resolve_jig_unloading_by_jig_id(self, jig_id, candidate_lot_ids=None):
        try:
            from Jig_Loading.models import JigCompleted

            normalized_jig_id = ''.join(str(jig_id or '').split()).upper()
            lot_ids = {str(lot_id) for lot_id in (candidate_lot_ids or []) if lot_id}
            active_jigs = JigCompleted.objects.filter(
                jig_id__iexact=normalized_jig_id,
                last_process_module='Inprocess Inspection',
            ).order_by('-IP_loaded_date_time', '-updated_at')

            jig = None
            if lot_ids:
                jig = active_jigs.filter(lot_id__in=lot_ids).first()
            if not jig:
                jig = active_jigs.first()
            if not jig:
                return None

            stock = self._stock_for(jig.lot_id)
            module_name, module_url = self._jig_unload_route(jig)
            page = self._page_for_jig_unload_jig(jig, module_url)
            return {
                'module': module_name,
                'url': module_url,
                'lot_id': jig.lot_id,
                'stock_lot_id': jig.lot_id,
                'jig_completed_id': jig.id,
                'jig_id': jig.jig_id or normalized_jig_id,
                'batch_id': self._batch_str(stock, getattr(jig, 'batch_id', jig.lot_id)),
                'page': page,
            }
        except Exception as e:
            logger.error('%s _resolve_jig_unloading_by_jig_id: %s', SCAN_TAG, e)
            return None

    def _page_for_jig_unload_jig(self, target_jig, target_url, page_size=10):
        try:
            from Jig_Loading.models import JigCompleted

            active_jigs = JigCompleted.objects.filter(
                last_process_module='Inprocess Inspection',
            ).order_by('-IP_loaded_date_time', '-updated_at').only(
                'id', 'lot_id', 'batch_id', 'draft_data', 'jig_id'
            )
            position = 0
            for candidate in active_jigs.iterator():
                _, candidate_url = self._jig_unload_route(candidate)
                if self._normalize_path(candidate_url) != self._normalize_path(target_url):
                    continue
                position += 1
                if candidate.id == target_jig.id:
                    return ((position - 1) // page_size) + 1
        except Exception as e:
            logger.debug('%s _page_for_jig_unload_jig failed: %s', SCAN_TAG, e)
        return None

    def _jig_unload_route(self, jig):
        draft_data = getattr(jig, 'draft_data', {}) or {}
        plating_color = ''
        if isinstance(draft_data, dict):
            plating_color = draft_data.get('plating_color') or ''
        if not plating_color:
            stock = self._stock_for(getattr(jig, 'lot_id', None))
            if stock and getattr(stock, 'plating_color', None):
                plating_color = getattr(stock.plating_color, 'plating_color', '') or ''
        if not plating_color:
            try:
                from modelmasterapp.models import ModelMasterCreation
                mmc = ModelMasterCreation.objects.filter(batch_id=getattr(jig, 'batch_id', None)).first()
                plating_color = getattr(mmc, 'plating_color', '') or ''
            except Exception:
                plating_color = ''

        normalized_color = str(plating_color or '').upper().replace('IP-', '').strip()
        try:
            from modelmasterapp.models import Plating_Color
            color = (
                Plating_Color.objects.filter(
                    Q(plating_color__iexact=normalized_color) |
                    Q(plating_color__iexact=f'IP-{normalized_color}')
                ).first()
            )
            if color and getattr(color, 'jig_unload_zone_2', False):
                return 'Jig Unloading Zone 2', reverse('JU_Zone_MainTable')
            if color and getattr(color, 'jig_unload_zone_1', False):
                return 'Jig Unloading', reverse('Jig_Unloading_MainTable')
        except Exception as e:
            logger.debug('%s jig unload color route lookup failed: %s', SCAN_TAG, e)

        if normalized_color == 'IPS':
            return 'Jig Unloading', reverse('Jig_Unloading_MainTable')
        return 'Jig Unloading Zone 2', reverse('JU_Zone_MainTable')

    def _find_active_jig_unload_for_lot(self, lot_id):
        from Jig_Loading.models import JigCompleted
        from Jig_Unloading.models import JUSubmittedZ1

        active_jigs = JigCompleted.objects.filter(last_process_module='Inprocess Inspection')
        jig = active_jigs.filter(lot_id=lot_id).first()
        if jig:
            return jig

        submitted = JUSubmittedZ1.objects.filter(lot_id=lot_id).order_by('-updated_at').first()
        if submitted:
            jig = active_jigs.filter(id=submitted.jig_completed_id).first()
            if jig:
                return jig

        for candidate in active_jigs.only('id', 'lot_id', 'batch_id', 'draft_data'):
            draft_data = candidate.draft_data or {}
            if not isinstance(draft_data, dict):
                continue
            for item in draft_data.get('multi_model_allocation', []) or []:
                if isinstance(item, dict) and str(item.get('lot_id') or '') == str(lot_id):
                    return candidate
            for item in draft_data.get('tray_data', []) or []:
                if isinstance(item, dict) and str(item.get('source_lot_id') or '') == str(lot_id):
                    return candidate
        return None

    def _check_lot_in_jig_loading(self, lot_id):
        try:
            stock = self._stock_for(lot_id)

            # 1) Already submitted -> sitting in the Jig Loading Completed
            #    Table (JigCompletedTable shows every draft_status='submitted'
            #    row, regardless of downstream jig_position progress). This is
            #    a historical fact about the JigCompleted record itself, so it
            #    must be checked BEFORE the stock-eligibility gate below —
            #    that gate only decides whether the lot should still appear on
            #    the active Jig Loading work screen, and wrongly hid already
            #    -submitted jigs whose current stock flags no longer matched it.
            if not stock:
                return None
            eligible = (
                stock.brass_audit_accptance or
                (getattr(stock, 'brass_audit_few_cases_accptance', False)
                 and not getattr(stock, 'brass_audit_onhold_picking', False))
            )
            if not eligible:
                return None

            # 2) Still being worked on in the active Jig Loading screen
            return {
                'module': 'Jig Loading',
                'url': reverse('JigView'),
                'lot_id': lot_id,
                'batch_id': self._batch_str(stock, lot_id),
            }
        except Exception as e:
            logger.error('%s _check_lot_in_jig_loading: %s', SCAN_TAG, e)
            return None

    def _find_completed_jig_unload_for_lot(self, lot_id):
        from Jig_Loading.models import JigCompleted
        return JigCompleted.objects.filter(
            lot_id=lot_id, last_process_module='Jig Unloading'
        ).order_by('-updated_at').first()

    def _check_lot_in_jig_unloading(self, lot_id):
        try:
            jig = self._find_active_jig_unload_for_lot(lot_id)
            if jig:
                stock = self._stock_for(lot_id)
                module_name, module_url = self._jig_unload_route(jig)
                return {
                    'module': module_name,
                    'url': module_url,
                    'lot_id': jig.lot_id,
                    'stock_lot_id': lot_id,
                    'jig_completed_id': jig.id,
                    'batch_id': self._batch_str(stock, getattr(jig, 'batch_id', lot_id)),
                }

            # Not pending pick anymore - check if it already finished unloading
            # and is sitting in the Completed table instead.
            completed_jig = self._find_completed_jig_unload_for_lot(lot_id)
            if not completed_jig:
                return None
            stock = self._stock_for(lot_id)
            module_name, module_url = self._jig_unload_route(completed_jig)
            completed_url_name = (
                'JigUnloading_Completedtable' if module_name == 'Jig Unloading' else 'JU_Zone_Completedtable'
            )
            return {
                'module': module_name,
                'url': reverse(completed_url_name),
                'lot_id': completed_jig.lot_id,
                'stock_lot_id': lot_id,
                'jig_completed_id': completed_jig.id,
                'batch_id': self._batch_str(stock, getattr(completed_jig, 'batch_id', lot_id)),
            }
        except Exception as e:
            logger.error('%s _check_lot_in_jig_unloading: %s', SCAN_TAG, e)
            return None

    def _check_lot_in_nickel_wiping(self, lot_id):
        try:
            return self._check_lot_in_nickel_wiping_zone(
                lot_id,
                zone_field='jig_unload_zone_1',
                module_name='Nickel Wiping',
                url_name='Nickel_Inspection',
                completed_url_name='NI_Completed',
            )
        except Exception as e:
            logger.error('%s _check_lot_in_nickel_wiping: %s', SCAN_TAG, e)
            return None

    def _check_lot_in_nickel_wiping_z2(self, lot_id):
        try:
            return self._check_lot_in_nickel_wiping_zone(
                lot_id,
                zone_field='jig_unload_zone_2',
                module_name='Nickel Wiping Z2',
                url_name='NQ_Zone_PickTable',
                completed_url_name='NQ_Zone_Completed',
            )
        except Exception as e:
            logger.error('%s _check_lot_in_nickel_wiping_z2: %s', SCAN_TAG, e)
            return None

    def _check_lot_in_nickel_wiping_zone(self, lot_id, zone_field, module_name, url_name, completed_url_name):
        from Jig_Unloading.models import JigUnloadAfterTable
        from modelmasterapp.models import Plating_Color

        allowed_color_ids = Plating_Color.objects.filter(
            **{zone_field: True}
        ).values_list('id', flat=True)
        stock = self._stock_for(lot_id)

        # 1) Still active / pending in the Nickel Wiping Pick Table
        active_filter = (
            (
                (Q(nq_qc_accptance__isnull=True) | Q(nq_qc_accptance=False))
                & (Q(nq_qc_rejection__isnull=True) | Q(nq_qc_rejection=False))
                & ~Q(nq_qc_few_cases_accptance=True, nq_onhold_picking=False)
                & Q(total_case_qty__gt=0)
            )
            | Q(send_to_nickel_brass=True)
            | Q(rejected_nickle_ip_stock=True, nq_onhold_picking=True)
        )
        if JigUnloadAfterTable.objects.filter(
            lot_id=lot_id,
            total_case_qty__gt=0,
            plating_color_id__in=allowed_color_ids,
        ).filter(active_filter).exists():
            return {
                'module': module_name,
                'url': reverse(url_name),
                'lot_id': lot_id,
                'batch_id': self._batch_str(stock, lot_id),
            }

        # 2) Already submitted -> Completed Table. The Completed table is
        #    built from the append-only NickelWiping_*Record event history
        #    (Full Accept / Full Reject / Partial Accept), keyed by the
        #    JigUnloadAfterTable lot_id (source_lot_id / child_lot_id), not
        #    the live JigUnloadAfterTable row state. Confirm the lot belongs
        #    to this zone via its plating colour.
        from Nickel_Inspection.models import (
            NickelWiping_FullAcceptRecord,
            NickelWiping_FullRejectRecord,
            NickelWiping_PartialAcceptRecord,
        )
        has_completed_event = (
            NickelWiping_FullAcceptRecord.objects.filter(source_lot_id=lot_id).exists() or
            NickelWiping_FullRejectRecord.objects.filter(source_lot_id=lot_id).exists() or
            NickelWiping_PartialAcceptRecord.objects.filter(
                Q(source_lot_id=lot_id) | Q(child_lot_id=lot_id)
            ).exists()
        )
        if has_completed_event and JigUnloadAfterTable.objects.filter(
            lot_id=lot_id, plating_color_id__in=allowed_color_ids
        ).exists():
            return {
                'module': f'{module_name} (Completed)',
                'url': reverse(completed_url_name),
                'lot_id': lot_id,
                'batch_id': self._batch_str(stock, lot_id),
            }

        return None

    @staticmethod
    def _nickel_audit_source_lot_ids(jig_unload_obj):
        source_lots = []
        for raw_lot_id in getattr(jig_unload_obj, 'combine_lot_ids', None) or []:
            source_lot = str(raw_lot_id or '').strip()
            if '-' in source_lot:
                source_lot = source_lot.rsplit('-', 1)[-1]
            if source_lot:
                source_lots.append(source_lot)
        fallback_lot = str(getattr(jig_unload_obj, 'lot_id', '') or '').strip()
        return source_lots or ([fallback_lot] if fallback_lot else [])

    def _nickel_audit_completed_source_lot_ids(self, allowed_color_ids):
        from Jig_Unloading.models import JigUnloadAfterTable

        completed_filter = (
            Q(na_qc_accptance=True)
            | Q(na_qc_rejection=True)
            | Q(na_qc_few_cases_accptance=True, na_onhold_picking=False)
        )
        completed_sources = set()
        completed_rows = (
            JigUnloadAfterTable.objects.filter(
                total_case_qty__gt=0,
                plating_color_id__in=allowed_color_ids,
            )
            .filter(completed_filter)
            .only('lot_id', 'combine_lot_ids')
        )
        for completed_row in completed_rows:
            completed_sources.update(self._nickel_audit_source_lot_ids(completed_row))
        return completed_sources

    def _check_lot_in_nickel_audit_zone(self, lot_id, zone_field, module_name, url_name, completed_url_name):
        from Jig_Unloading.models import JigUnloadAfterTable
        from modelmasterapp.models import Plating_Color
        from Nickel_Audit.models import NickelAudit_Submission

        allowed_color_ids = list(
            Plating_Color.objects.filter(**{zone_field: True}).values_list('id', flat=True)
        )
        active_filter = (
            (
                (Q(na_qc_accptance__isnull=True) | Q(na_qc_accptance=False))
                & (
                    Q(na_qc_rejection__isnull=True)
                    | Q(na_qc_rejection=False)
                    # Match the Nickel Audit Pick Table: a rejection from a
                    # previous cycle is stale after a newer Nickel Wiping
                    # re-acceptance.
                    | (
                        Q(na_qc_rejection=True)
                        & Q(nq_last_process_date_time__gt=F('na_last_process_date_time'))
                    )
                )
                & ~Q(na_qc_few_cases_accptance=True, na_onhold_picking=False)
                & (
                    Q(nq_qc_accptance=True)
                    | Q(nq_qc_few_cases_accptance=True, nq_onhold_picking=False)
                )
            )
            | Q(na_qc_rejection=True, na_onhold_picking=True)
        )
        pick_row = (
            JigUnloadAfterTable.objects.filter(
                lot_id=lot_id,
                total_case_qty__gt=0,
                plating_color_id__in=allowed_color_ids,
            )
            .filter(active_filter)
            .only('lot_id', 'combine_lot_ids')
            .first()
        )
        stock = self._stock_for(lot_id)

        if pick_row:
            # The Pick Table only treats a submission from the current Nickel
            # Wiping cycle as completed.  Previous Audit-cycle submissions
            # remain in history after rework under the same lot ID.
            already_submitted = NickelAudit_Submission.objects.filter(
                lot_id=pick_row.lot_id,
                created_at__gt=pick_row.nq_last_process_date_time,
            ).exists()
            if not already_submitted:
                return {
                    'module': module_name,
                    'url': reverse(url_name),
                    'lot_id': lot_id,
                    'batch_id': self._batch_str(stock, lot_id),
                }

        # Not (or no longer) active in the Pick Table - check the Completed
        # Table via its NickelAudit_Submission event, so scans of trays that
        # already finished Nickel Audit still resolve instead of "Not Exists".
        if (
            JigUnloadAfterTable.objects.filter(lot_id=lot_id, plating_color_id__in=allowed_color_ids).exists()
            and NickelAudit_Submission.objects.filter(lot_id=lot_id).exists()
        ):
            return {
                'module': f'{module_name} (Completed)',
                'url': reverse(completed_url_name) + '?' + self._wide_date_range_query(),
                'lot_id': lot_id,
                'batch_id': self._batch_str(stock, lot_id),
            }

        return None

    def _check_lot_in_nickel_audit_z1(self, lot_id):
        try:
            return self._check_lot_in_nickel_audit_zone(
                lot_id,
                zone_field='jig_unload_zone_1',
                module_name='Nickel Audit',
                url_name='NA_PickTable',
                completed_url_name='NA_Completed',
            )
        except Exception as e:
            logger.error('%s _check_lot_in_nickel_audit_z1: %s', SCAN_TAG, e)
            return None

    def _check_lot_in_nickel_audit_z2(self, lot_id):
        try:
            return self._check_lot_in_nickel_audit_zone(
                lot_id,
                zone_field='jig_unload_zone_2',
                module_name='Nickel Audit Z2',
                url_name='NA_Zone_PickTable',
                completed_url_name='NA_Zone_Completed',
            )
        except Exception as e:
            logger.error('%s _check_lot_in_nickel_audit_z2: %s', SCAN_TAG, e)
            return None

    def _check_lot_in_spider_spindle_zone(
        self, lot_id, zone_field, completed_field, module_name, url_name, completed_url_name
    ):
        from Jig_Unloading.models import JigUnloadAfterTable
        from modelmasterapp.models import Plating_Color

        allowed_color_ids = Plating_Color.objects.filter(
            **{zone_field: True}
        ).values_list('id', flat=True)
        completed_filter = Q(**{completed_field: False}) | Q(**{f'{completed_field}__isnull': True})
        pick_row = (
            JigUnloadAfterTable.objects.filter(
                lot_id=lot_id,
                total_case_qty__gt=0,
                plating_color_id__in=allowed_color_ids,
                na_qc_accptance=True,
            )
            .filter(completed_filter)
            .only('lot_id')
            .first()
        )
        if pick_row:
            stock = self._stock_for(pick_row.lot_id)
            return {
                'module': module_name,
                'url': reverse(url_name),
                'lot_id': pick_row.lot_id,
                'batch_id': self._batch_str(stock, pick_row.lot_id),
            }

        # Not (or no longer) active in the Pick Table - the row may have
        # already finished Spider Spindle (completed_field=True) and moved
        # to its Completed Table. Check that directly so the scan still
        # resolves instead of "Not Exists".
        completed_row = (
            JigUnloadAfterTable.objects.filter(
                lot_id=lot_id,
                plating_color_id__in=allowed_color_ids,
                na_qc_accptance=True,
                **{completed_field: True},
            )
            .only('lot_id')
            .first()
        )
        if completed_row:
            stock = self._stock_for(completed_row.lot_id)
            return {
                'module': f'{module_name} (Completed)',
                'url': reverse(completed_url_name) + '?' + self._wide_date_range_query(),
                'lot_id': completed_row.lot_id,
                'batch_id': self._batch_str(stock, completed_row.lot_id),
            }

        return None

    def _check_lot_in_ss_z1(self, lot_id):
        try:
            return self._check_lot_in_spider_spindle_zone(
                lot_id,
                zone_field='jig_unload_zone_1',
                completed_field='ss_z1_completed',
                module_name='Spider Spindle Z1',
                url_name='ss_z1_pick_table',
                completed_url_name='ss_z1_completed',
            )
        except Exception as e:
            logger.error('%s _check_lot_in_ss_z1: %s', SCAN_TAG, e)
            return None

    def _check_lot_in_ss_z2(self, lot_id):
        try:
            return self._check_lot_in_spider_spindle_zone(
                lot_id,
                zone_field='jig_unload_zone_2',
                completed_field='ss_z2_completed',
                module_name='Spider Spindle Z2',
                url_name='ss_z2_pick_table',
                completed_url_name='ss_z2_completed',
            )
        except Exception as e:
            logger.error('%s _check_lot_in_ss_z2: %s', SCAN_TAG, e)
            return None

    def _check_lot_in_iqf(self, lot_id, tray_id):
        try:
            from IQF.services.selectors import is_current_iqf_scan_tray
            if not is_current_iqf_scan_tray(tray_id, lot_id):
                return None
            from IQF.services.selectors import get_iqf_picktable_base_queryset
            stock = self._stock_for(lot_id)

            # 1) Still sitting in the IQF Pick Table (not yet submitted)
            if get_iqf_picktable_base_queryset().filter(lot_id=lot_id).exists():
                return {
                    'module': 'IQF',
                    'url': reverse('iqf_picktable'),
                    'lot_id': lot_id,
                    'batch_id': self._batch_str(stock, lot_id),
                }

            # 2) Submitted -> Completed / Reject Table. IQF_Submitted is the
            #    single source of truth for both (no date-range default, so
            #    no wide date range needed here unlike Brass QC/Audit).
            from IQF.models import IQF_Submitted

            submission = IQF_Submitted.objects.filter(lot_id=lot_id, is_completed=True).first()
            if submission:
                if getattr(submission, 'rejected_qty', 0):
                    return {
                        'module': 'IQF (Reject)',
                        'url': reverse('iqf_rejection_table'),
                        'lot_id': lot_id,
                        'batch_id': self._batch_str(stock, lot_id),
                    }
                return {
                    'module': 'IQF (Completed)',
                    'url': reverse('iqf_completed_table'),
                    'lot_id': lot_id,
                    'batch_id': self._batch_str(stock, lot_id),
                }

            return None
        except Exception as e:
            logger.error('%s _check_lot_in_iqf: %s', SCAN_TAG, e)
            return None

    def _check_lot_in_brass_audit(self, lot_id):
        try:
            from BrassAudit.selectors import get_picktable_base_queryset

            stock = self._stock_for(lot_id)

            # 1) Still sitting in the Brass Audit Pick Table (not yet submitted)
            if get_picktable_base_queryset().filter(lot_id=lot_id).exists():
                return {
                    'module': 'Brass Audit',
                    'url': reverse('brass_audit_picktable'),
                    'lot_id': lot_id,
                    'batch_id': self._batch_str(stock, lot_id),
                }

            from modelmasterapp.models import TotalStockModel
            from BrassAudit.models import Brass_Audit_Submission

            # 2) Submitted and accepted/rejected -> Completed Table. Mirrors
            #    BrassAuditCompletedView's filter, minus the default date
            #    range, so a tray scan finds the row regardless of when it
            #    was processed.
            processed = TotalStockModel.objects.filter(
                lot_id=lot_id,
                batch_id__total_batch_quantity__gt=0,
            ).filter(
                Q(brass_audit_accptance=True) |
                Q(brass_audit_rejection=True) |
                Q(brass_audit_few_cases_accptance=True, brass_audit_onhold_picking=False)
            ).exists()
            has_submission = Brass_Audit_Submission.objects.filter(
                lot_id=lot_id, is_completed=True
            ).exists()
            if processed and has_submission:
                return {
                    'module': 'Brass Audit (Completed)',
                    'url': reverse('brass_audit_completed') + '?' + self._wide_date_range_query(),
                    'lot_id': lot_id,
                    'batch_id': self._batch_str(stock, lot_id),
                }

            # 3) Rejected (full or partial) -> Reject Table. Mirrors
            #    BrassAuditRejectTableView's filter, minus the date range.
            rejected = TotalStockModel.objects.filter(
                lot_id=lot_id,
                batch_id__total_batch_quantity__gt=0,
            ).filter(
                Q(brass_audit_rejection=True) | Q(brass_audit_few_cases_accptance=True)
            ).exists()
            if rejected:
                return {
                    'module': 'Brass Audit (Reject)',
                    'url': reverse('brass_audit_rejection') + '?' + self._wide_date_range_query(),
                    'lot_id': lot_id,
                    'batch_id': self._batch_str(stock, lot_id),
                }

            return None
        except Exception as e:
            logger.error('%s _check_lot_in_brass_audit: %s', SCAN_TAG, e)
            return None

    def _check_lot_in_brass_qc(self, lot_id):
        try:
            from Brass_QC.services.selectors import get_picktable_base_queryset
            stock = self._stock_for(lot_id)

            # 1) Still sitting in the Brass QC Pick Table (not yet submitted)
            # Use SAME queryset as Brass QC Pick Table UI (TotalStockModel-based)
            if get_picktable_base_queryset().filter(lot_id=lot_id).exists():
                return {
                    'module': 'Brass QC',
                    'url': reverse('BrassPickTableView'),
                    'lot_id': lot_id,
                    'batch_id': self._batch_str(stock, lot_id),
                }

            # 2) Submitted (accepted or rejected) -> Completed Table. Brass QC
            #    has a single Completed Table that also lists rejected lots,
            #    unlike other modules which split accept/reject. Mirrors
            #    get_completed_base_queryset's filter, minus the date range.
            from modelmasterapp.models import TotalStockModel
            processed = TotalStockModel.objects.filter(
                lot_id=lot_id,
                batch_id__total_batch_quantity__gt=0,
            ).filter(
                Q(brass_qc_accptance=True) |
                Q(brass_qc_rejection=True) |
                Q(brass_qc_few_cases_accptance=True, brass_onhold_picking=False)
            ).exists()
            if processed:
                return {
                    'module': 'Brass QC (Completed)',
                    'url': reverse('BrassCompletedView') + '?' + self._wide_date_range_query(),
                    'lot_id': lot_id,
                    'batch_id': self._batch_str(stock, lot_id),
                }

            return None
        except Exception as e:
            logger.error('%s _check_lot_in_brass_qc: %s', SCAN_TAG, e)
            return None

    def _check_lot_in_input_screening(self, lot_id):
        try:
            from InputScreening.selectors import pick_table_queryset
            stock = self._stock_for(lot_id)

            # 1) Still sitting in the IS Pick Table (not yet submitted)
            # IS Pick Table renders the TotalStockModel lot as the annotated
            # stock_lot_id, while ModelMasterCreation.lot_id is often empty.
            if pick_table_queryset().filter(
                Q(stock_lot_id=lot_id) | Q(lot_id=lot_id)
            ).exists():
                page = self._page_for_value(pick_table_queryset(), 'stock_lot_id', lot_id)
                return {
                    'module': 'Input Screening',
                    'url': reverse('IS_PickTable'),
                    'lot_id': lot_id,
                    'stock_lot_id': lot_id,
                    'batch_id': self._batch_str(stock, lot_id),
                    'page': page,
                }

            # 2) Submitted (accepted/partial) -> Completed Table
            from InputScreening.models import InputScreening_Submitted, IS_PartialRejectLot

            if InputScreening_Submitted.objects.filter(
                lot_id=lot_id, is_submitted=True, is_active=True
            ).exists():
                return {
                    'module': 'Input Screening (Completed)',
                    'url': reverse('IS_Completed_Table'),
                    'lot_id': lot_id,
                    'batch_id': self._batch_str(stock, lot_id),
                }

            # 3) Rejected (full or partial) -> Reject Table
            if IS_PartialRejectLot.objects.filter(parent_lot_id=lot_id).exists():
                return {
                    'module': 'Input Screening (Reject)',
                    'url': reverse('IS_RejectTable'),
                    'lot_id': lot_id,
                    'batch_id': self._batch_str(stock, lot_id),
                }

            return None
        except Exception as e:
            logger.error('%s _check_lot_in_input_screening: %s', SCAN_TAG, e)
            return None

    def _check_lot_in_inprocess_inspection(self, lot_id):
        try:
            from Jig_Loading.models import JigCompleted
            stock = self._stock_for(lot_id)

            # 1) Completed (jig_position assigned) -> Inprocess Inspection Completed
            if JigCompleted.objects.filter(
                lot_id=lot_id, jig_position__isnull=False
            ).exists():
                return {
                    'module': 'Inprocess Inspection (Completed)',
                    'url': reverse('inprocess_inspection_complete'),
                    'lot_id': lot_id,
                    'batch_id': self._batch_str(stock, lot_id),
                }

            # 2) Submitted from Jig Loading, awaiting jig position -> main table
            if JigCompleted.objects.filter(
                lot_id=lot_id, draft_status='submitted', jig_position__isnull=True
            ).exists():
                page = self._page_for_value(
                    JigCompleted.objects.filter(
                        draft_status='submitted',
                        jig_position__isnull=True,
                    ).order_by('-updated_at'),
                    'lot_id',
                    lot_id,
                )
                return {
                    'module': 'Inprocess Inspection',
                    'url': reverse('inprocess_inspection_main'),
                    'lot_id': lot_id,
                    'batch_id': self._batch_str(stock, lot_id),
                    'page': page,
                }

            return None
        except Exception as e:
            logger.error('%s _check_lot_in_inprocess_inspection: %s', SCAN_TAG, e)
            return None

    def _check_lot_in_day_planning(self, lot_id):
        try:
            from modelmasterapp.models import ModelMasterCreation
            stock = self._stock_for(lot_id)

            base_queryset = ModelMasterCreation.objects.filter(total_batch_quantity__gt=0)

            def _resolve(queryset):
                if stock and stock.batch_id_id:
                    return queryset.filter(pk=stock.batch_id_id).first()
                return queryset.filter(lot_id=lot_id).first()

            # 1) Still sitting in the DP Pick Table (not yet released/scanned)
            batch = _resolve(base_queryset.filter(Moved_to_D_Picker=False))
            if batch:
                return {
                    'module': 'Day Planning',
                    'url': reverse('dp_pick_table'),
                    'lot_id': batch.lot_id or lot_id,
                    'batch_id': batch.batch_id,
                    'stock_lot_id': batch.lot_id or lot_id,
                }

            # 2) Already released/scanned into the DP Completed Table. A tray
            #    here may not yet have moved into any downstream module's own
            #    pick table (e.g. released but not picked up by Input
            #    Screening yet), so without this branch the scan reports
            #    "Not Exists" even though the tray is visible on-screen in
            #    the DP Completed Table.
            batch = _resolve(base_queryset.filter(Moved_to_D_Picker=True))
            if batch:
                return {
                    'module': 'Day Planning (Completed)',
                    'url': reverse('dp_completed_table'),
                    'lot_id': batch.lot_id or lot_id,
                    'batch_id': batch.batch_id,
                    'stock_lot_id': batch.lot_id or lot_id,
                }

            return None
        except Exception as e:
            logger.error('%s _check_lot_in_day_planning: %s', SCAN_TAG, e)
            return None

    def _check_batch_in_day_planning(self, batch_ids):
        """Last-resort batch-level fallback when no lot_id was found."""
        try:
            from modelmasterapp.models import ModelMasterCreation
            base_queryset = ModelMasterCreation.objects.filter(
                pk__in=batch_ids,
                total_batch_quantity__gt=0,
            )

            batch = base_queryset.filter(Moved_to_D_Picker=False).first()
            if batch:
                return {
                    'module': 'Day Planning',
                    'url': reverse('dp_pick_table'),
                    'lot_id': batch.lot_id or batch.batch_id,
                    'batch_id': batch.batch_id,
                    'stock_lot_id': batch.lot_id or batch.batch_id,
                }

            batch = base_queryset.filter(Moved_to_D_Picker=True).first()
            if batch:
                return {
                    'module': 'Day Planning (Completed)',
                    'url': reverse('dp_completed_table'),
                    'lot_id': batch.lot_id or batch.batch_id,
                    'batch_id': batch.batch_id,
                    'stock_lot_id': batch.lot_id or batch.batch_id,
                }

            return None
        except Exception as e:
            logger.error('%s _check_batch_in_day_planning: %s', SCAN_TAG, e)
            return None
