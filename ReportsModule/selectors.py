"""
Read-only selectors for the Reports module.

Consolidated Report: one row per Plating Stock No showing the complete
journey (Day Planning -> ... -> Spider Spindle Z2), with every module
always shown as its own column (never collapsed to "current stage"
only). The exact same row builder is used by both the Preview API and
the Excel download so the two can never diverge.

Lineage note: Input Screening, Brass QC, IQF and Brass Audit can each
split a lot into an accepted child and/or a rejected child on PARTIAL
(and, for Brass QC / Brass Audit, on FULL_REJECT) submissions. Every
child row created this way keeps `TotalStockModel.batch_id` pointing at
the SAME batch as the parent (verified in each module's
`services/lot_service.py` `TotalStockModel.objects.create(batch_id=parent.batch_id, ...)`).
So the full lineage for a batch is simply every `TotalStockModel` row
sharing that `batch_id` — no need to walk parent/child lot_id chains
through the four separate `*_PartialAcceptLot`/`*_PartialRejectLot`
tables one hop at a time; each module's own completion flags are
checked across ALL of that batch's rows.
"""
import logging
from importlib import import_module
from datetime import datetime, time

from django.conf import settings
from collections import defaultdict
from types import SimpleNamespace
from django.apps import apps
from django.db.models import CharField, Max, Min, Q, Sum, Value
from django.db.models.functions import Lower, Replace
from django.utils import timezone

logger = logging.getLogger(__name__)

PLATING_SEARCH_SEPARATORS = (' ', '-', '/', '_', '.', ':')

# Stage/column order for the consolidated journey (spec order). Zone-capable
# modules get one column per zone since a lot only ever lands in one zone.
STAGE_DAY_PLANNING = 'Day Planning'
STAGE_INPUT_SCREENING = 'Input Screening'
STAGE_BRASS_QC = 'Brass QC'
STAGE_IQF = 'IQF'
STAGE_BRASS_AUDIT = 'Brass Audit'
STAGE_JIG_LOADING = 'Jig Loading'
STAGE_IP_INSPECTION = 'IP Inspection'
STAGE_JIG_UNLOADING_Z1 = 'Jig Unloading Z1'
STAGE_JIG_UNLOADING_Z2 = 'Jig Unloading Z2'
STAGE_NICKEL_WIPING_Z1 = 'Nickel Wiping Z1'
STAGE_NICKEL_WIPING_Z2 = 'Nickel Wiping Z2'
STAGE_NICKEL_AUDIT_Z1 = 'Nickel Audit Z1'
STAGE_NICKEL_AUDIT_Z2 = 'Nickel Audit Z2'
STAGE_SS_Z1 = 'Spider Spindle Z1'
STAGE_SS_Z2 = 'Spider Spindle Z2'

MODULE_COLUMNS = [
    STAGE_DAY_PLANNING,
    STAGE_INPUT_SCREENING,
    STAGE_BRASS_QC,
    STAGE_IQF,
    STAGE_BRASS_AUDIT,
    STAGE_JIG_LOADING,
    STAGE_IP_INSPECTION,
    STAGE_JIG_UNLOADING_Z1,
    STAGE_JIG_UNLOADING_Z2,
    STAGE_NICKEL_WIPING_Z1,
    STAGE_NICKEL_WIPING_Z2,
    STAGE_NICKEL_AUDIT_Z1,
    STAGE_NICKEL_AUDIT_Z2,
    STAGE_SS_Z1,
    STAGE_SS_Z2,
]

# Field-name spec for the four early modules that can split a lot into
# accept/reject children. Every one of these lives on TotalStockModel.
_EARLY_MODULE_SPECS = [
    (STAGE_INPUT_SCREENING, dict(
        accept_flag='accepted_Ip_stock', reject_flag='rejected_ip_stock',
        few_flag='few_cases_accepted_Ip_stock', onhold_flag='ip_onhold_picking',
        out_time_field='last_process_date_time',
        accepted_qty_field='total_IP_accpeted_quantity',
        rejected_qty_field='total_qty_after_rejection_IP',
        remarks_field='IP_pick_remarks',
    )),
    (STAGE_BRASS_QC, dict(
        accept_flag='brass_qc_accptance', reject_flag='brass_qc_rejection',
        few_flag='brass_qc_few_cases_accptance', onhold_flag='brass_onhold_picking',
        out_time_field='bq_last_process_date_time',
        accepted_qty_field='brass_qc_accepted_qty',
        rejected_qty_field='brass_qc_after_rejection_qty',
        remarks_field='Bq_pick_remarks',
    )),
    (STAGE_IQF, dict(
        accept_flag='iqf_acceptance', reject_flag='iqf_rejection',
        few_flag='iqf_few_cases_acceptance', onhold_flag='iqf_onhold_picking',
        out_time_field='iqf_last_process_date_time',
        accepted_qty_field='iqf_accepted_qty',
        rejected_qty_field='iqf_after_rejection_qty',
        remarks_field='IQF_pick_remarks',
    )),
    (STAGE_BRASS_AUDIT, dict(
        accept_flag='brass_audit_accptance', reject_flag='brass_audit_rejection',
        few_flag='brass_audit_few_cases_accptance', onhold_flag='brass_audit_onhold_picking',
        out_time_field='brass_audit_last_process_date_time',
        accepted_qty_field='brass_audit_accepted_qty',
        rejected_qty_field=None,  # no dedicated field; derived from lot_qty - accepted
        remarks_field='BA_pick_remarks',
    )),
]

DT_FORMAT = '%d-%b-%Y %I:%M %p'

# Per-cell stage state for the Preview UI's background indicator.
# not_reached -> lot has not entered this module yet (light red)
# current     -> lot is actively being processed at this module right now (light blue)
# completed   -> this module's processing for the lot has finished (light green)
STATE_NOT_REACHED = 'not_reached'
STATE_NOT_APPLICABLE = 'not_applicable'
STATE_CURRENT = 'current'
STATE_COMPLETED = 'completed'

_CURRENT_STAGE_STATUSES = {'In Progress', 'Pending'}


def _stage_state(status):
    """Classify a module's raw status string into a Preview UI bg state."""
    if status == 'Not Applicable':
        return STATE_NOT_APPLICABLE
    if not status or status == 'Not Reached':
        return STATE_NOT_REACHED
    if status in _CURRENT_STAGE_STATUSES:
        return STATE_CURRENT
    return STATE_COMPLETED


def _fmt(dt):
    if not dt:
        return ''
    if timezone.is_aware(dt):
        dt = timezone.localtime(dt)
    return dt.strftime(DT_FORMAT)


def _normalize_unload_lot_id(value):
    """Same normalization the Jig Unloading report uses."""
    value = str(value or '').strip().lstrip('-')
    if ':' in value:
        value = value.rsplit(':', 1)[-1].strip()
    if value.startswith('JLOT-') and '-' in value[5:]:
        value = value.rsplit('-', 1)[-1]
    return value


def _first_remark(*values):
    for value in values:
        if value and str(value).strip():
            return str(value).strip()
    return ''


def _module_cell(status, in_time=None, out_time=None, lot_qty=None, accepted_qty=None,
                  rejected_qty=None, user=None, remarks=None, accepted_label='Accepted'):
    """Format one module's cell — the same multi-line block for Preview and Excel."""
    if status in ('Not Reached', 'Not Applicable'):
        return 'IN : --\nOUT: --\nLot Qty : --\nStatus : ' + status
    lines = [f"IN : {_fmt(in_time) or '--'}", f"OUT: {_fmt(out_time) or '--'}"]
    lines.append(f"Lot Qty : {lot_qty if lot_qty is not None else '--'}")
    if accepted_qty is not None:
        lines.append(f"{accepted_label} : {accepted_qty}")
    if rejected_qty is not None:
        lines.append(f"Rejected : {rejected_qty}")
    lines.append(f"Status : {status}")
    if user:
        lines.append(f"User : {user}")
    if remarks:
        lines.append(f"Remarks : {remarks}")
    return '\n'.join(lines)


_TIME_LABELS = {'IN', 'OUT'}
_QTY_LABELS = {'Lot Qty', 'Accepted', 'Loaded Jig', 'Rejected'}
_STATUS_LABELS = {'Status'}


def _cell_line_type(label):
    """Classify a parsed cell line's label for Preview UI styling."""
    if label in _TIME_LABELS:
        return 'time'
    if label in _QTY_LABELS:
        return 'qty'
    if label in _STATUS_LABELS:
        return 'status'
    return 'muted'


def _parse_cell_lines(text):
    """Turn one `_module_cell()` text block into structured
    {label, value, type} rows so the Preview UI can render a readable
    label/value grid instead of a single text blob. Generic split on the
    deterministic 'Label : value' format `_module_cell` always produces —
    no per-stage logic needed here."""
    rows = []
    blocks = (text or '').split('\n\n')
    for index, block in enumerate(blocks):
        lines = []
        for line in block.split('\n'):
            if ':' not in line:
                continue
            label, _, value = line.partition(':')
            label, value = label.strip(), value.strip()
            lines.append({'label': label, 'value': value, 'type': _cell_line_type(label)})
        if len(blocks) > 1:
            status = next((line['value'] for line in lines if line['label'] == 'Status'), None)
            for line in lines:
                line.update(block=index, block_state=_stage_state(status))
        rows.extend(lines)

    return rows


TRAY_MODELS = {
    'Input Screening': ('InputScreening', 'IPTrayId'),
    'Brass QC': ('Brass_QC', 'BrassTrayId'),
    'IQF': ('IQF', 'IQFTrayId'),
    'Brass Audit': ('BrassAudit', 'BrassAuditTrayId'),
    'Nickel Wiping': ('Nickel_Inspection', 'NickelQcTrayId'),
    'Nickel Audit': ('Nickel_Audit', 'Nickel_AuditTrayId'),
}
DRAFT_MODELS = {
    'Input Screening': ('InputScreening', 'IP_Rejection_Draft'),
    'Brass QC': ('Brass_QC', 'Brass_QC_Draft_Store'),
    'IQF': ('IQF', 'IQF_Draft_Store'),
    'Brass Audit': ('BrassAudit', 'Brass_Audit_Draft_Store'),
    'Nickel Wiping': ('Nickel_Inspection', 'Nickel_QC_Draft_Store'),
    'Nickel Audit': ('Nickel_Audit', 'Nickel_Audit_Draft_Store'),
}
SUBMISSION_MODELS = {
    'Input Screening': ('InputScreening', 'InputScreening_Submitted'),
    'Brass QC': ('Brass_QC', 'Brass_QC_Submission'),
    'IQF': ('IQF', 'IQF_Submitted'),
    'Brass Audit': ('BrassAudit', 'Brass_Audit_Submission'),
    'Nickel Wiping': ('Nickel_Inspection', 'NickelQC_Submission'),
    'Nickel Audit': ('Nickel_Audit', 'NickelAudit_Submission'),
    'Jig Loading': ('Jig_Loading', 'JigLoadingRecord'),
}


def _earliest(*values):
    return min((v for v in values if v is not None), default=None)


class JourneyRecords:
    def __init__(self, lot_ids):
        lot_ids = set(lot_ids)
        self.entries = defaultdict(dict)
        self.transition_entries = defaultdict(dict)
        self.transition_submissions = defaultdict(lambda: defaultdict(list))
        self.submissions = defaultdict(dict)
        self.submission_history = defaultdict(lambda: defaultdict(list))
        self.rw_quantities = {}
        self.is_quantities = defaultdict(dict)
        self.dp_transfers = {}
        self.dp_batches = {}
        self.na_partial_accept_parent = {}
        self.na_partial_accept_parent_unloads = {}
        self.nq_partial_accept_parent = {}
        self.nq_partial_accept_parent_unloads = {}
        if not lot_ids:
            return
        # A partial Nickel Audit acceptance creates a technical child
        # JigUnloadAfterTable row solely to carry accepted trays to Spider
        # Spindle. Keep its parent history available and identify the child
        # so the report does not render it as another Jig Unloading pass.
        partial_accept_models = [
            ('Nickel_Audit', 'NickelAudit_PartialAcceptLot',
             self.na_partial_accept_parent),
            ('Nickel_Inspection', 'NickelQC_PartialAcceptLot',
             self.nq_partial_accept_parent),
        ]
        # A lot can be partially accepted more than once. Follow every
        # child-to-parent link, not only the latest child, so report history
        # retains each Nickel Wiping/Audit transaction.
        pending_lot_ids, seen_lot_ids = set(lot_ids), set()
        while pending_lot_ids - seen_lot_ids:
            child_lot_ids = pending_lot_ids - seen_lot_ids
            seen_lot_ids.update(child_lot_ids)
            for app_label, model_name, parent_map in partial_accept_models:
                model = apps.get_model(app_label, model_name)
                for row in model.objects.filter(new_lot_id__in=child_lot_ids).values(
                        'new_lot_id', 'parent_lot_id'):
                    parent_map[row['new_lot_id']] = row['parent_lot_id']
                    lot_ids.add(row['parent_lot_id'])
                    pending_lot_ids.add(row['parent_lot_id'])
        parent_ids = (set(self.na_partial_accept_parent.values()) |
                      set(self.nq_partial_accept_parent.values()))
        if parent_ids:
            model = apps.get_model('Jig_Unloading', 'JigUnloadAfterTable')
            parent_unloads = model.objects.filter(lot_id__in=parent_ids)
            by_parent_id = {record.lot_id: record for record in parent_unloads}
            self.na_partial_accept_parent_unloads = {
                child_id: by_parent_id[parent_id]
                for child_id, parent_id in self.na_partial_accept_parent.items()
                if parent_id in by_parent_id
            }
            self.nq_partial_accept_parent_unloads = {
                child_id: by_parent_id[parent_id]
                for child_id, parent_id in self.nq_partial_accept_parent.items()
                if parent_id in by_parent_id
            }
        # DP writes these tray transaction rows during submission, in the
        # same transaction that makes the lot visible to Input Screening.
        model = apps.get_model('DayPlanning', 'DPTrayId_History')
        for row in model.objects.filter(lot_id__in=lot_ids).values(
                'lot_id', 'batch_id_id').annotate(
                    transfer_time=Max('date'), lot_qty=Sum('tray_quantity')):
            self.dp_transfers[row['lot_id']] = row
            previous = self.dp_batches.get(row['batch_id_id'])
            self.dp_batches[row['batch_id_id']] = _latest_time(previous, row['transfer_time'])
        for stage, model_path in TRAY_MODELS.items():
            model = apps.get_model(*model_path)
            rows = model.objects.filter(lot_id__in=lot_ids).values('lot_id').annotate(
                in_time=Min('date'), lot_qty=Sum('tray_quantity'))
            for row in rows:
                self.entries[stage][row['lot_id']] = row
        for stage, model_path in DRAFT_MODELS.items():
            model = apps.get_model(*model_path)
            for row in model.objects.filter(lot_id__in=lot_ids).values('lot_id').annotate(
                    in_time=Min('created_at')):
                entry = self.entries[stage].setdefault(row['lot_id'], {})
                entry['in_time'] = _earliest(entry.get('in_time'), row['in_time'])
        for stage, model_path in SUBMISSION_MODELS.items():
            model = apps.get_model(*model_path)
            scope = Q(lot_id__in=lot_ids)
            if stage == 'Jig Loading':
                scope |= Q(is_multi_model=True)
            order = 'updated_at' if stage == 'Jig Loading' else 'created_at'
            for record in model.objects.filter(scope).order_by(order, 'pk'):
                if stage == 'Jig Loading':
                    self._loading_record(record, lot_ids)
                    continue
                self.submissions[stage][record.lot_id] = record
                self.submission_history[stage][record.lot_id].append(record)
        # A partial Jig Loading submission creates an EX-* excess lot for the
        # quantity left behind.  Its creation time is the moment that new
        # Jig Loading transaction begins, so it is the authoritative IN time
        # for the remaining-lot block in the consolidated report.
        model = apps.get_model('Jig_Loading', 'ExcessLotRecord')
        for row in model.objects.filter(new_lot_id__in=lot_ids).values(
                'new_lot_id', 'lot_qty', 'created_at'):
            entry = self.entries[STAGE_JIG_LOADING].setdefault(row['new_lot_id'], {})
            entry['in_time'] = _earliest(entry.get('in_time'), row['created_at'])
            entry.setdefault('lot_qty', row['lot_qty'])
        # A split submission belongs to its parent lot, while the physical
        # tray-entry timestamp can be stored on its accepted/rejected child.
        # Link that persisted evidence back to the parent report transaction.
        for stage, saved_records in self.submissions.items():
            for parent_lot_id, record in saved_records.items():
                for field in ('transition_lot_id', 'transition_accept_lot_id',
                              'transition_reject_lot_id'):
                    child_lot_id = getattr(record, field, None)
                    child_entry = self.entries[stage].get(child_lot_id)
                    if child_entry:
                        previous = self.transition_entries[stage].get(parent_lot_id)
                        if previous is None or _earliest(
                                previous.get('in_time'), child_entry.get('in_time')
                        ) == child_entry.get('in_time'):
                            self.transition_entries[stage][parent_lot_id] = child_entry
        # A returned child lot may not retain its parent's source timestamp.
        # Keep the immutable parent submission addressable by every transition
        # child so report handoffs use the actual completion time.
        for stage, records_by_lot in self.submission_history.items():
            for history in records_by_lot.values():
                for record in history:
                    for field in ('transition_lot_id', 'transition_accept_lot_id',
                                  'transition_reject_lot_id'):
                        child_lot_id = getattr(record, field, None)
                        if child_lot_id:
                            self.transition_submissions[stage][child_lot_id].append(record)
        for name, field in [('IS_PartialAcceptLot', 'accepted_qty'),
                            ('IS_PartialRejectLot', 'rejected_qty')]:
            model = apps.get_model('InputScreening', name)
            for row in model.objects.filter(parent_lot_id__in=lot_ids).values(
                    'parent_lot_id').annotate(quantity=Sum(field)):
                self.is_quantities[row['parent_lot_id']][field] = row['quantity']
        # IQF's incoming quantity is the receiving lot's rejection allocation,
        # not the full original batch quantity. Use the latest saved allocation.
        for app, name in [('Brass_QC', 'Brass_QC_Rejection_ReasonStore'),
                          ('BrassAudit', 'Brass_Audit_Rejection_ReasonStore')]:
            model = apps.get_model(app, name)
            for row in model.objects.filter(lot_id__in=lot_ids).order_by('created_at', 'pk'):
                previous = self.rw_quantities.get(row.lot_id)
                key = (row.created_at, row.pk)
                if previous is None or key > previous[0]:
                    self.rw_quantities[row.lot_id] = (key, row.total_rejection_quantity)
        for zone in (1, 2):
            stage = f'Spider Spindle Z{zone}'
            model = apps.get_model(f'SpiderSpindle_Z{zone}', f'SpiderSpindleZ{zone}TrayId')
            for row in model.objects.filter(lot_id__in=lot_ids).values('lot_id').annotate(
                    in_time=Min('linked_at')):
                self.entries[stage][row['lot_id']] = row
        model = apps.get_model('Jig_Unloading', 'JUSubmittedZ1')
        for row in model.objects.filter(lot_id__in=lot_ids).order_by('submitted_at', 'pk'):
            self.submissions['Jig Unloading'][row.lot_id] = row
            self.entries['Jig Unloading'][row.lot_id] = {
                'in_time': row.submitted_at, 'lot_qty': row.total_qty}
        model = apps.get_model('Jig_Unloading', 'JigUnloadDraft')
        for row in model.objects.filter(main_lot_id__in=lot_ids).order_by('created_at', 'pk'):
            entry = self.entries['Jig Unloading'].setdefault(row.main_lot_id, {})
            entry['in_time'] = _earliest(entry.get('in_time'), row.created_at)
            entry.setdefault('lot_qty', row.total_quantity)
        model = apps.get_model('Jig_Loading', 'JigLoadingManualDraft')
        for row in model.objects.filter(lot_id__in=lot_ids).order_by('updated_at', 'pk'):
            # This legacy draft has no creation timestamp. Its mutable
            # updated_at cannot truthfully stand in for the original IN time.
            self.entries['Jig Loading'].setdefault(row.lot_id, {
                'in_time': None, 'lot_qty': row.original_lot_qty})

    def _loading_record(self, record, lot_ids):
        allocations = {str(a['lot_id']): a for a in record.multi_model_allocation or []
                       if isinstance(a, dict) and a.get('lot_id')}
        for lot_id in ({record.lot_id} | allocations.keys()) & lot_ids:
            allocation = allocations.get(lot_id, {})
            quantity = allocation.get('requested_qty', allocation.get('model_lot_qty'))
            if quantity is None and lot_id == record.lot_id:
                quantity = record.lot_qty
            loaded = allocation.get('allocated_qty')
            if loaded is None and lot_id == record.lot_id:
                loaded = record.loaded_cases_qty
            self.entries['Jig Loading'][lot_id] = {
                'in_time': record.created_at, 'lot_qty': quantity}
            self.submissions['Jig Loading'][lot_id] = SimpleNamespace(
                status_flag=record.status_flag, lot_qty=quantity,
                loaded_cases_qty=loaded, updated_at=record.updated_at)

    def _receive(self, stage, lot_id, received_at, quantity):
        entry = self.entries[stage].setdefault(lot_id, {})
        entry['in_time'] = _earliest(entry.get('in_time'), received_at)
        if entry.get('lot_qty') is None:
            entry['lot_qty'] = quantity

    def add_stock_receipts(self, stocks):
        """Use the receiving pick-table gates even before local scan/draft rows."""
        for stock in stocks:
            batch = stock.batch_id
            # DP writes the destination while drafting too. IS only receives
            # it when Moved_to_D_Picker becomes true (IS selector's own gate).
            if (batch and batch.Moved_to_D_Picker and stock.tray_scan_status
                    and not stock.lot_id.startswith('EX-')):
                transfer = self.dp_transfers.get(stock.lot_id, {})
                self._receive('Input Screening', stock.lot_id,
                              transfer.get('transfer_time'),
                              transfer.get('lot_qty', stock.total_stock))
            # Jig Pick's actual eligibility is Brass Audit acceptance, not
            # existence of a later JigCompleted draft/submission.
            if (stock.brass_audit_accptance or
                    (stock.brass_audit_few_cases_accptance
                     and not stock.brass_audit_onhold_picking)):
                self._receive('Jig Loading', stock.lot_id,
                              stock.brass_audit_last_process_date_time,
                              stock.brass_audit_accepted_qty)
            # These are explicit destinations written by the submit services,
            # not an assumed linear module order. IQF acceptance returns to QC.
            routes = {
                'Input Screening': ('last_process_date_time', {'Brass QC'}),
                'Brass QC': ('bq_last_process_date_time', {'IQF', 'Brass Audit'}),
                'IQF': ('iqf_last_process_date_time', {'Brass QC'}),
                'Brass Audit': ('brass_audit_last_process_date_time',
                                {'Jig Loading', 'IQF', 'Brass QC'}),
            }
            route = routes.get(stock.last_process_module)
            destination = stock.next_process_module
            if route and destination in route[1]:
                transferred_at = getattr(stock, route[0], None)
                if transferred_at:
                    quantity = stock.total_stock
                    if stock.last_process_module == 'Input Screening' and destination == 'Brass QC':
                        # The original lot includes pieces not passed by IS.
                        # Prefer the saved accepted allocation, including zero.
                        quantity = self.is_quantities.get(stock.lot_id, {}).get(
                            'accepted_qty', stock.total_IP_accpeted_quantity)
                    if stock.last_process_module == 'Brass QC' and destination == 'Brass Audit':
                        # Brass Audit receives only the quantity accepted by
                        # Brass QC. The parent TotalStockModel can still hold
                        # the pre-QC total, so it is not receipt evidence.
                        quantity = stock.brass_qc_accepted_qty
                    if destination == 'IQF':
                        quantity = self.rw_quantities.get(
                            stock.lot_id, (None, stock.total_IP_accpeted_quantity))[1]
                    self._receive(destination, stock.lot_id, transferred_at, quantity)

    def entry(self, stage, lot_id):
        return self.entries[stage].get(lot_id)

    def transition_entry(self, stage, lot_id):
        return self.transition_entries[stage].get(lot_id)

    def submission(self, stage, lot_id):
        return self.submissions[stage].get(lot_id)

    def submissions_for(self, stage, lot_id):
        return self.submission_history[stage].get(lot_id, [])


def submission_values(stage, record):
    """Read final quantities from immutable module submission snapshots."""
    if record is None:
        return {}
    if stage == 'Input Screening':
        completed = record.is_submitted and not record.Draft_Saved
        status = ('Rejected' if record.is_full_reject else
                  'Accepted' if record.is_full_accept else 'Partially Accepted')
        # IS stores split quantities in child tables; retain stock quantities
        # for partial submissions, but full decisions are explicit snapshots.
        values = {'lot_qty': record.original_lot_qty}
        if record.is_full_accept:
            values.update(accepted_qty=record.original_lot_qty, rejected_qty=0)
        elif record.is_full_reject:
            values.update(accepted_qty=0, rejected_qty=record.original_lot_qty)
        out_time = record.submitted_at
    elif stage == 'Jig Loading':
        completed = record.status_flag == 'SUBMITTED'
        status = 'Completed'
        values = {'lot_qty': record.lot_qty, 'accepted_qty': record.loaded_cases_qty}
        out_time = record.updated_at
    elif stage == 'Jig Unloading':
        completed = not record.is_draft
        status = 'Completed'
        # total_qty is the per-lot tray quantity captured at unloading.
        values = {'lot_qty': record.total_qty, 'accepted_qty': record.total_qty}
        out_time = record.updated_at
    else:
        completed = getattr(record, 'is_completed', True) and not getattr(record, 'is_draft', False)
        status = {'FULL_ACCEPT': 'Accepted', 'FULL_REJECT': 'Rejected',
                  'LOT_REJECTION': 'Rejected', 'PARTIAL': 'Partially Accepted'}[record.submission_type]
        values = {'lot_qty': (record.iqf_incoming_qty if stage == 'IQF' else record.total_lot_qty),
                  'accepted_qty': record.accepted_qty, 'rejected_qty': record.rejected_qty}
        out_time = record.created_at
    values.update(status=status if completed else 'In Progress',
                  out_time=out_time if completed else None)
    if not completed:
        values.pop('accepted_qty', None)
        values.pop('rejected_qty', None)
    return values



def _module_transaction_blocks(history, current_values):
    """Render persisted final transactions plus a distinct current transaction."""
    transactions = list(history)
    current_key = (current_values.get('status'), current_values.get('out_time'),
                   current_values.get('lot_qty'))
    history_keys = {
        (values.get('status'), values.get('out_time'), values.get('lot_qty'))
        for values in transactions
    }
    if current_key not in history_keys:
        transactions.append(current_values)
    transactions.sort(key=lambda values: (
        values.get('status') == 'In Progress',
        values.get('out_time') or values.get('in_time') or datetime.min,
    ))
    text = '\n\n'.join(_module_cell(**values) for values in transactions)
    statuses = {values['status'] for values in transactions}
    # Callers need the business status; _stage_state() converts it to the
    # Preview colour later. Returning UI state here made route checks and
    # status output incorrect for Nickel transaction blocks.
    overall = ('In Progress' if 'In Progress' in statuses else
               next(iter(statuses)) if len(statuses) == 1 else
               'Partially Accepted')
    activity = _latest_time(*(
        stamp for values in transactions
        for stamp in (values.get('in_time'), values.get('out_time'))
    ))
    return text, overall, activity

def _early_module_status(stock, spec):
    """Return (status, out_time) for one of the four split-capable modules,
    or (None, None) if this row was never processed at that module."""
    if getattr(stock, spec['reject_flag'], False):
        return 'Rejected', getattr(stock, spec['out_time_field'], None)
    if getattr(stock, spec['accept_flag'], False):
        return 'Accepted', getattr(stock, spec['out_time_field'], None)
    few = getattr(stock, spec['few_flag'], False)
    onhold = getattr(stock, spec['onhold_flag'], False)
    if few and not onhold:
        return 'Partially Accepted', getattr(stock, spec['out_time_field'], None)
    if onhold:
        return 'In Progress', None
    return None, None


def _pick_early_module_row(stocks_for_batch, spec):
    """Among every TotalStockModel row for this batch (root + every split
    child), find the one carrying this module's own completion flags.
    Prefers the latest out-time if more than one row matches."""
    best = None
    for stock in stocks_for_batch:
        status, out_time = _early_module_status(stock, spec)
        if status is None:
            continue
        if best is None or (out_time and (not best[2] or out_time > best[2])):
            best = (stock, status, out_time)
    return best


def _latest_time(*values):
    return max((value for value in values if value is not None), default=None)


def _early_stage_handoff_time(stage, stock, out_time):
    """Return the latest completed upstream handoff for a repeated early pass.

    A lot can return to Brass QC from IQF or Brass Audit. The tray-entry map
    deliberately keeps the first receipt for a lot, so it cannot by itself
    identify a later QC/Audit pass. The persisted source-module OUT timestamp
    is the reliable lower bound for that later pass.
    """
    fields_by_source = {
        STAGE_BRASS_QC: {
            STAGE_INPUT_SCREENING: 'last_process_date_time',
            STAGE_IQF: 'iqf_last_process_date_time',
            STAGE_BRASS_AUDIT: 'brass_audit_last_process_date_time',
        },
        STAGE_BRASS_AUDIT: {STAGE_BRASS_QC: 'bq_last_process_date_time'},
    }.get(stage, {})
    source_field = fields_by_source.get(getattr(stock, 'last_process_module', None))
    source_time = getattr(stock, source_field, None) if source_field else None
    if source_time is not None and (out_time is None or source_time <= out_time):
        return source_time
    fields = tuple(fields_by_source.values())
    return max((stamp for stamp in (getattr(stock, field, None) for field in fields)
                if stamp is not None and (out_time is None or stamp <= out_time)),
               default=None)


def _early_submission_handoff_time(records, stage, lot_id, out_time):
    """Read a handoff from the immutable source-module submission ledger."""
    if not records:
        return None
    sources = {
        STAGE_BRASS_QC: (STAGE_INPUT_SCREENING, STAGE_IQF, STAGE_BRASS_AUDIT),
        STAGE_BRASS_AUDIT: (STAGE_BRASS_QC,),
    }.get(stage, ())
    return max((submission_values(source, submission).get('out_time')
                for source in sources
                for submission in (
                    records.submissions_for(source, lot_id)
                    + records.transition_submissions[source].get(lot_id, [])
                )
                if submission_values(source, submission).get('out_time') is not None
                and (out_time is None
                     or submission_values(source, submission)['out_time'] <= out_time)),
               default=None)


def _day_planning_cell(batch, transfer_time=None):
    if batch is None:
        return _module_cell('Not Reached'), None
    status = 'Completed' if batch.Moved_to_D_Picker else 'In Progress'
    # The DP transaction timestamp is also the IS receipt event: IS has no
    # separate incoming row until a later scan/draft. Never use batch IN as OUT.
    return _module_cell(status, in_time=batch.date_time,
                        out_time=transfer_time if batch.Moved_to_D_Picker else None,
                        lot_qty=batch.total_batch_quantity,
                        remarks=batch.dp_pick_remarks), status


def _early_module_cells(stocks_for_batch, prev_out_time=None, records=None):
    cells, statuses = {}, {}
    activity = None
    # When a legacy QC completion has no tray/draft row, the preceding
    # module's recorded OUT is the only persisted handoff into Brass QC.
    # Carry it through the ordered early-stage flow as a truthful fallback.
    prior_out_time = prev_out_time
    for name, spec in _EARLY_MODULE_SPECS:
        candidates = []
        has_submission = bool(records and any(
            records.submission(name, row.lot_id) for row in stocks_for_batch))
        bq_transition_lots = set()
        if name == 'Brass QC' and records:
            for parent in stocks_for_batch:
                saved = records.submission(name, parent.lot_id)
                if saved and saved.is_completed:
                    bq_transition_lots.update(
                        value for value in (
                            getattr(saved, 'transition_lot_id', None),
                            getattr(saved, 'transition_accept_lot_id', None),
                            getattr(saved, 'transition_reject_lot_id', None),
                        ) if value)
        for stock in stocks_for_batch:
            entry = records.entry(name, stock.lot_id) if records else None
            if entry is None and records:
                entry = records.transition_entry(name, stock.lot_id)
            submission = records.submission(name, stock.lot_id) if records else None
            if (name == 'Brass QC' and stock.lot_id in bq_transition_lots
                    and not submission and stock.next_process_module != 'Brass QC'
                    and not (stock.current_stage == 'Brass QC'
                             and stock.last_process_module != 'Brass QC')):
                # This is the submitted parent's destination lot, not a new QC
                # execution. Its stale current_stage must not override the final
                # snapshot. A later return to QC or own submission remains eligible.
                continue
            if (name == 'Brass Audit' and entry is None and submission is None
                    and stock.last_process_module == 'Brass Audit'
                    and stock.next_process_module in ('Brass QC', 'IQF')):
                # A return/rejection destination is not another Audit receipt.
                continue
            status, out_time = _early_module_status(stock, spec)
            if (name == STAGE_INPUT_SCREENING and has_submission and not submission
                    and (stock.last_process_module == STAGE_INPUT_SCREENING
                         or stock.next_process_module != STAGE_INPUT_SCREENING)):
                # Partial/full IS decisions create downstream child lots in the
                # same batch. They inherit flags but are not a second IS pass.
                # Keep a genuinely waiting DP lot (next -> IS) eligible.
                continue
            if has_submission and not submission and status not in (None, 'In Progress'):
                # Split children inherit upstream flags; their creation is not
                # another execution of the parent's completed module.
                continue
            reached = bool(entry is not None or submission or status or
                           getattr(stock, 'current_stage', None) == name or
                           getattr(stock, 'next_process_module', None) == name)
            if not reached:
                continue
            values = dict(status=status or 'In Progress',
                          in_time=(entry or {}).get('in_time'),
                          out_time=out_time, lot_qty=(entry or {}).get('lot_qty'),
                          remarks=getattr(stock, spec['remarks_field'], None))
            if status and status != 'In Progress':
                values['accepted_qty'] = getattr(stock, spec['accepted_qty_field'], None)
                if spec['rejected_qty_field']:
                    values['rejected_qty'] = getattr(stock, spec['rejected_qty_field'], None)
            if name == STAGE_IQF and records and stock.lot_id in records.rw_quantities:
                values['lot_qty'] = records.rw_quantities[stock.lot_id][1]
            values.update(submission_values(name, submission))
            handoff_time = _latest_time(
                _early_stage_handoff_time(name, stock, values['out_time']),
                _early_submission_handoff_time(records, name, stock.lot_id,
                                                values['out_time']),
            )
            if handoff_time is not None:
                values['in_time'] = handoff_time
            if (name == 'Brass QC' and submission and values['in_time'] is None
                    and records):
                transition_ids = (
                    getattr(submission, 'transition_lot_id', None),
                    getattr(submission, 'transition_accept_lot_id', None),
                    getattr(submission, 'transition_reject_lot_id', None),
                )
                values['in_time'] = _earliest(*(
                    (records.entry(name, lot_id) or {}).get('in_time')
                    for lot_id in transition_ids if lot_id
                ))
            if (name == STAGE_BRASS_QC and submission and values['in_time'] is None
                    and prior_out_time is not None):
                values['in_time'] = prior_out_time
            if (name == STAGE_BRASS_AUDIT and submission and values['in_time'] is None
                    and prior_out_time is not None):
                # Full/partial Audit submissions can be saved before a local
                # Audit tray row exists. The prior-stage OUT is the persisted
                # handoff into Audit and therefore its truthful IN time.
                values['in_time'] = prior_out_time
            if name == STAGE_INPUT_SCREENING and submission and records:
                if values['status'] == 'Partially Accepted':
                    values.update(records.is_quantities.get(stock.lot_id, {}))
            if values['status'] == 'In Progress':
                if values['lot_qty'] is None:
                    values['lot_qty'] = (stock.total_IP_accpeted_quantity
                                         if name == STAGE_IQF else stock.total_stock)
                values['out_time'] = None
                values.pop('accepted_qty', None)
                values.pop('rejected_qty', None)
            event = _latest_time(values['in_time'], values['out_time'])
            # A saved parent submission is authoritative over copied child
            # completion flags. Ties are stable even when timestamps coincide.
            key = (event is not None, event, submission is not None, str(stock.lot_id))
            candidates.append((key, values))
        if not candidates:
            cells[name], statuses[name] = _module_cell('Not Reached'), None
            continue
        prior_out_time = _latest_time(
            prior_out_time, *(value['out_time'] for _, value in candidates))
        if len({key[-1] for key, _ in candidates}) > 1:
            # Distinct QC/audit lots can be active and rejected simultaneously.
            # Preserve each lot's own evidence instead of choosing the newest
            # receipt or summing quantities with different outcomes.
            branches = {key[-1]: (key, value) for key, value in candidates}
            ordered = sorted(branches.items(), key=lambda item: (
                item[1][1]['status'] == 'In Progress', item[1][0]))
            cells[name] = '\n\n'.join(
                _module_cell(**value)
                for lot_id, (_, value) in ordered)
            branch_states = {value['status'] for _, (_, value) in ordered}
            statuses[name] = ('In Progress' if 'In Progress' in branch_states
                              else next(iter(branch_states)) if len(branch_states) == 1
                              else 'Partially Accepted')
            activity = _latest_time(activity, *(
                stamp for _, (_, value) in ordered
                for stamp in (value['in_time'], value['out_time'])))
            continue
        values = max(candidates, key=lambda item: item[0])[1]
        cells[name] = _module_cell(**values)
        statuses[name] = values['status']
        activity = _latest_time(activity, values['in_time'], values['out_time'])
    return cells, statuses, activity


def _jig_loading_cells(jig_record, prev_out_time=None, records=None, lot_id=None):
    entry = records.entry(STAGE_JIG_LOADING, lot_id) if records else None
    submission = records.submission(STAGE_JIG_LOADING, lot_id) if records else None
    if not jig_record and not submission and entry is None:
        return (_module_cell('Not Reached'), _module_cell('Not Reached'),
                None, None, None)
    submitted = bool(jig_record and jig_record.draft_status == 'submitted')
    values = dict(status='Completed' if submitted else 'In Progress',
                  in_time=(entry or {}).get('in_time'),
                  lot_qty=(entry or {}).get('lot_qty'), out_time=None)
    if jig_record:
        if values['lot_qty'] is None:
            values['lot_qty'] = jig_record.original_lot_qty
        values['remarks'] = _first_remark(jig_record.pick_remarks, jig_record.remarks)
    values.update(submission_values(STAGE_JIG_LOADING, submission))
    jig_cell = _module_cell(**values, accepted_label='Loaded Jig')
    activity = _latest_time(values['in_time'], values['out_time'])
    # A submitted loading record is the actual handoff into IP Inspection.
    # IP_loaded_date_time belongs to IP Inspection, never Jig Loading.
    if not submitted:
        return jig_cell, _module_cell('Not Reached'), values['status'], None, activity
    ip_done = bool(jig_record.jig_position)
    ip_out = jig_record.IP_loaded_date_time if ip_done else None
    ip_status = 'Completed' if ip_done else 'In Progress'
    ip_cell = _module_cell(ip_status, in_time=values['out_time'], out_time=ip_out,
                           lot_qty=values.get('accepted_qty', jig_record.loaded_cases_qty),
                           remarks=jig_record.remarks)
    return jig_cell, ip_cell, values['status'], ip_status, _latest_time(activity, ip_out)


def _late_module_cells(unload_record, zone_map, prev_out_time=None, records=None,
                       lot_id=None, plating_color_id=None, jig_record=None):
    columns = MODULE_COLUMNS[7:]
    cells = {name: _module_cell('Not Reached') for name in columns}
    statuses = dict.fromkeys(columns)
    zone = zone_map.get(unload_record.plating_color_id if unload_record else plating_color_id)
    activity = None
    latest_audit = None

    # IP Inspection receives the quantity accepted by the corresponding Jig
    # Loading submission.  A JigCompleted record can represent several lots,
    # so its loaded_cases_qty may be the whole jig quantity and must not be
    # used for this individual lot's Jig Unloading receipt.
    jig_loading_submission = records.submission(STAGE_JIG_LOADING, lot_id) if records else None
    ip_received_qty = submission_values(
        STAGE_JIG_LOADING, jig_loading_submission
    ).get('accepted_qty') if jig_loading_submission else None
    if ip_received_qty is None and jig_record:
        ip_received_qty = jig_record.loaded_cases_qty

    def put(name, values):
        nonlocal activity
        cells[name] = _module_cell(**values)
        statuses[name] = values['status']
        activity = _latest_time(activity, values.get('in_time'), values.get('out_time'))

    unloading_entry = records.entry('Jig Unloading', lot_id) if records else None
    unloading_submission = records.submission('Jig Unloading', lot_id) if records else None
    if (zone and jig_record and jig_record.IP_loaded_date_time
            and (jig_record.last_process_module == 'Inprocess Inspection'
                 or jig_record.jig_position)):
        # Inspection submit is the actual handoff opening Unloading Pick.
        unloading_entry = dict(unloading_entry or {})
        unloading_entry['in_time'] = _earliest(
            unloading_entry.get('in_time'), jig_record.IP_loaded_date_time)
        unloading_entry.setdefault('lot_qty', ip_received_qty)
    if zone and unloading_entry is not None:
        values = dict(status='In Progress', in_time=unloading_entry.get('in_time'),
                      lot_qty=unloading_entry.get('lot_qty'))
        values.update(submission_values('Jig Unloading', unloading_submission))
        put(f'Jig Unloading {zone.upper()}', values)
    if not unload_record:
        return cells, statuses, activity
    if zone:
        partial_wiping_parent = (
            records.nq_partial_accept_parent.get(unload_record.lot_id)
            if records else None
        )
        partial_audit_parent = (
            records.na_partial_accept_parent.get(unload_record.lot_id)
            if records else None
        )
        parent_unload = (
            records.na_partial_accept_parent_unloads.get(unload_record.lot_id)
            if records else None
        ) or (
            records.nq_partial_accept_parent_unloads.get(unload_record.lot_id)
            if records else None
        )
        # Some Zone 2 records proceed straight into Nickel Wiping without
        # updating the older Jig-Unloading completion flags.  A persisted
        # Nickel Wiping entry/submission is nevertheless an unambiguous
        # handoff out of Jig Unloading, so it must complete that stage.
        jig_unload_source = parent_unload or unload_record
        while records:
            earlier_parent = (
                records.nq_partial_accept_parent_unloads.get(jig_unload_source.lot_id)
                or records.na_partial_accept_parent_unloads.get(jig_unload_source.lot_id)
            )
            if earlier_parent is None:
                break
            jig_unload_source = earlier_parent
        jig_unload_source_id = jig_unload_source.lot_id
        jig_unloading_entry = ((records.entry('Jig Unloading', jig_unload_source_id)
                                if records else None) or unloading_entry)
        jig_unloading_submission = ((records.submission('Jig Unloading', jig_unload_source_id)
                                     if records else None) or unloading_submission)
        wiping_entry = records.entry('Nickel Wiping', jig_unload_source_id) if records else None
        wiping_lot_ids = []
        wiping_lot_id = unload_record.lot_id
        while wiping_lot_id and wiping_lot_id not in wiping_lot_ids:
            wiping_lot_ids.append(wiping_lot_id)
            wiping_lot_id = (
                records.nq_partial_accept_parent.get(wiping_lot_id)
                or records.na_partial_accept_parent.get(wiping_lot_id)
                if records else None
            )
        wiping_lot_ids.reverse()
        wiping_history = (sorted(
            (submission for value in wiping_lot_ids
             for submission in records.submissions_for('Nickel Wiping', value)),
            key=lambda submission: submission.created_at,
        ) if records else [])
        wiping_started_at = _earliest(
            (wiping_entry or {}).get('in_time'),
            *(record.created_at for record in wiping_history),
        )
        # A partial Nickel Wiping acceptance continues under a child
        # JigUnloadAfterTable lot. Its Nickel Audit record therefore may not
        # have `nq_last_process_date_time`, although the parent submission is
        # the real handoff into Nickel Audit.
        wiping_completed_at = max((record.created_at for record in wiping_history),
                                  default=None)
        ju_done = bool(jig_unload_source.unload_accepted or jig_unload_source.Un_loaded_date_time
                       or wiping_started_at)
        # Preserve the quantity handed off by IP Inspection. Later unloading
        # or Nickel allocations can be smaller, but must not rewrite the
        # historical Jig Unloading receipt quantity.
        jig_unload_lot_qty = (
            ip_received_qty
            if ip_received_qty is not None
            else jig_unload_source.total_case_qty
        )
        jig_unload_accepted_qty = submission_values(
            'Jig Unloading', jig_unloading_submission
        ).get('accepted_qty') if jig_unloading_submission else jig_unload_source.accepted_qty
        if ju_done and not jig_unload_accepted_qty:
            # The unload row defaults accepted_qty to zero.  A completed
            # unload without an explicit accepted value still accepted every
            # non-missing case, so derive that value from its receipt qty.
            jig_unload_accepted_qty = max(
                0, (jig_unload_lot_qty or 0)
                - (jig_unload_source.unload_missing_qty or 0)
            )
        put(f'Jig Unloading {zone.upper()}', dict(
            status=('Accepted' if jig_unload_source.unload_accepted else 'Completed') if ju_done else 'In Progress',
            in_time=(jig_unloading_entry or {}).get('in_time') or jig_unload_source.created_at,
            out_time=(jig_unload_source.Un_loaded_date_time or wiping_started_at)
                     if ju_done else None,
            lot_qty=jig_unload_lot_qty,
            accepted_qty=jig_unload_accepted_qty if ju_done else None))
        # Nickel Audit can be entered again after a return through Nickel
        # Wiping. Collect its ledger across the same child/parent lineage as
        # wiping so an earlier Audit transaction is never replaced by the
        # latest child-lot transaction.
        audit_history = (sorted(
            (submission for value in wiping_lot_ids
             for submission in records.submissions_for('Nickel Audit', value)),
            key=lambda submission: submission.created_at,
        ) if records else [])
        latest_audit = max(audit_history, key=lambda record: record.created_at,
                           default=None)

        def wiping_receipt_qty(at_time=None):
            """Return the quantity handed into this Wiping transaction."""
            prior_full_rejects = [
                audit for audit in audit_history
                if audit.submission_type == 'FULL_REJECT'
                and (at_time is None or audit.created_at <= at_time)
            ]
            if prior_full_rejects:
                return submission_values(
                    'Nickel Audit', prior_full_rejects[-1]
                ).get('rejected_qty')
            return jig_unload_accepted_qty

        def audit_receipt_qty(at_time=None):
            """Return the accepted Wiping quantity handed into Audit."""
            prior_wipings = [
                wiping for wiping in wiping_history
                if at_time is None or wiping.created_at <= at_time
            ]
            if prior_wipings:
                return submission_values(
                    'Nickel Wiping', prior_wipings[-1]
                ).get('accepted_qty')
            return getattr(unload_record, 'nq_qc_accepted_qty', None)

        audit_received_qty = audit_receipt_qty()
        # Mutable `na_qc_rejection` is not cleared by every later audit
        # acceptance. Prefer the append-only audit ledger whenever present.
        # Only a latest full rejection routes the lot back to Nickel Wiping.
        audit_returns_to_wiping = (
            latest_audit.submission_type == 'FULL_REJECT'
            if latest_audit is not None else bool(unload_record.na_qc_rejection)
        )
        for stage, prefix in [('Nickel Wiping', 'nq'), ('Nickel Audit', 'na')]:
            stage_lot_id = (
                partial_wiping_parent if stage == 'Nickel Wiping' and partial_wiping_parent
                else partial_audit_parent if stage == 'Nickel Audit' and partial_audit_parent
                else unload_record.lot_id
            )
            entry = records.entry(stage, stage_lot_id) if records else None
            submission = records.submission(stage, stage_lot_id) if records else None
            if stage == 'Nickel Wiping' and wiping_history:
                submission = wiping_history[-1]
            accept = getattr(unload_record, prefix + '_qc_accptance')
            reject = getattr(unload_record, prefix + '_qc_rejection')
            partial = getattr(unload_record, prefix + '_qc_few_cases_accptance')
            hold = getattr(unload_record, prefix + '_onhold_picking')
            done = accept or reject or (partial and not hold)
            # Nickel Audit can be re-entered under the same lot id. Its pick
            # table ignores rejection history older than a fresh NW acceptance.
            received = (unload_record.total_case_qty > 0 if stage == 'Nickel Wiping'
                        else (unload_record.nq_qc_accptance or
                              (unload_record.nq_qc_few_cases_accptance
                               and not unload_record.nq_onhold_picking)))
            cycle_time = unload_record.nq_last_process_date_time
            previous_out = unload_record.na_last_process_date_time
            if (stage == 'Nickel Audit' and received and cycle_time
                    and previous_out and cycle_time > previous_out):
                done = False
                if submission and submission.created_at <= cycle_time:
                    submission = None
                if entry and (not entry.get('in_time') or entry['in_time'] <= cycle_time):
                    entry = None
            transfer_time = None
            if received:
                if stage == 'Nickel Wiping':
                    transfer_time = (unload_record.Un_loaded_date_time
                                     or unload_record.created_at)
                    # Audit rejection explicitly routes this same row to NW.
                    if (audit_returns_to_wiping and previous_out
                            and (not cycle_time or previous_out > cycle_time)):
                        transfer_time = previous_out
                        done = False
                        if submission and submission.created_at <= transfer_time:
                            submission = None
                        entry = None
                else:
                    transfer_time = cycle_time or wiping_completed_at
            current = getattr(unload_record, 'current_stage', None)
            reached = (received or entry is not None or submission or done or partial or hold or
                       getattr(unload_record, prefix + '_draft') or current == stage or
                       (stage == 'Nickel Wiping' and current == 'Nickel Inspection'))
            if not reached:
                continue
            values = dict(status=('Rejected' if reject else 'Accepted' if accept else
                                   'Partially Accepted') if done else 'In Progress',
                          in_time=transfer_time or (entry or {}).get('in_time'),
                          out_time=getattr(unload_record, prefix + '_last_process_date_time') if done else None,
                          # A module displays the quantity it received from
                          # the immediately preceding module, never a later
                          # remaining quantity on the unload record.
                          lot_qty=(jig_unload_lot_qty if stage == 'Nickel Wiping'
                                   else audit_received_qty),
                          accepted_qty=getattr(unload_record, prefix + '_qc_accepted_qty') if done else None,
                          remarks=getattr(unload_record, prefix + '_pick_remarks'))
            values.update(submission_values(stage, submission))
            if stage == 'Nickel Wiping':
                # The Wiping transaction starts with the quantity actually
                # unloaded for this lot.  Its own accepted/rejected result
                # must never replace that receipt quantity.
                values['lot_qty'] = wiping_receipt_qty(
                    getattr(submission, 'created_at', None)
                )
            elif stage == 'Nickel Audit':
                values['lot_qty'] = audit_receipt_qty(
                    getattr(submission, 'created_at', None)
                )
            if values['lot_qty'] is None:
                values['lot_qty'] = (entry or {}).get('lot_qty') or unload_record.total_case_qty
            history = (wiping_history if stage == 'Nickel Wiping'
                       else audit_history if stage == 'Nickel Audit'
                       else records.submissions_for(stage, stage_lot_id) if records else [])
            if history:
                if stage == 'Nickel Wiping':
                    first_wiping_in_time = (jig_unload_source.Un_loaded_date_time
                                            or jig_unload_source.created_at)
                history_values = []
                for record in history:
                    if stage == 'Nickel Wiping':
                        # Each return from a full Nickel Audit rejection is a
                        # new wiping receipt. Its IN time is that audit's OUT
                        # time, rather than the original Jig Unloading time.
                        history_in_time = max((audit.created_at for audit in audit_history
                                               if audit.submission_type == 'FULL_REJECT'
                                               and audit.created_at <= record.created_at),
                                              default=first_wiping_in_time)
                    elif stage == 'Nickel Audit':
                        # Audit transaction N is received from the most recent
                        # completed Nickel Wiping transaction before it.
                        history_in_time = max((wiping.created_at for wiping in wiping_history
                                               if wiping.created_at <= record.created_at),
                                              default=None)
                    snapshot = dict(in_time=history_in_time,
                                    remarks=getattr(unload_record, prefix + '_pick_remarks'))
                    snapshot.update(submission_values(stage, record))
                    if stage == 'Nickel Wiping':
                        snapshot['lot_qty'] = wiping_receipt_qty(record.created_at)
                    elif stage == 'Nickel Audit':
                        snapshot['lot_qty'] = audit_receipt_qty(record.created_at)
                    history_values.append(snapshot)
                text, status, history_activity = _module_transaction_blocks(history_values, values)
                cells[f'{stage} {zone.upper()}'] = text
                statuses[f'{stage} {zone.upper()}'] = status
                activity = _latest_time(activity, history_activity)
            else:
                put(f'{stage} {zone.upper()}', values)
    for number in (1, 2):
        stage = f'Spider Spindle Z{number}'
        entry = records.entry(stage, unload_record.lot_id) if records else None
        done = getattr(unload_record, f'ss_z{number}_completed', False)
        received = (zone == f'z{number}' and unload_record.na_qc_accptance
                    and unload_record.total_case_qty > 0)
        if not done and entry is None and not received:
            continue
        latest_audit_qty = submission_values(
            'Nickel Audit', latest_audit
        ).get('accepted_qty') if latest_audit else None
        spider_received_qty = (
            latest_audit_qty
            if latest_audit_qty is not None
            else getattr(unload_record, 'na_qc_accepted_qty', None)
        )
        put(stage, dict(status='Completed' if done else 'In Progress',
                        in_time=(unload_record.na_last_process_date_time if received else None)
                                or (entry or {}).get('in_time'),
                        out_time=getattr(unload_record, f'ss_z{number}_completed_at') if done else None,
                        lot_qty=(spider_received_qty if spider_received_qty is not None
                                 else unload_record.total_case_qty),
                        remarks=unload_record.spider_pick_remarks))
    return cells, statuses, activity


def _apply_route_applicability(modules, statuses, zone=None):
    """Label bypassed stages without overwriting actual processing evidence."""
    bypassed = set()
    if statuses.get('Brass QC') == 'Accepted' and not statuses.get('IQF'):
        bypassed.add('IQF')
    if zone in ('z1', 'z2'):
        other = 'Z2' if zone == 'z1' else 'Z1'
        bypassed.update(name for name in modules if name.endswith(' ' + other))
    for name in bypassed:
        if name in modules and not statuses.get(name):
            statuses[name] = 'Not Applicable'
            modules[name] = _module_cell('Not Applicable')


def _render_stage_branches(row):
    """Keep independent downstream receipts in one original-batch row."""
    for name, branches in row.pop('_stage_branches', {}).items():
        if len(branches) < 2:
            continue
        ordered = sorted(branches.values(), key=lambda item: item[1] == STATE_CURRENT)
        text = '\n\n'.join(cell for cell, _ in ordered)
        row['modules'][name] = text
        row['module_details'][name] = _parse_cell_lines(text)
        row['module_states'][name] = (STATE_CURRENT if any(
            state == STATE_CURRENT for _, state in ordered) else STATE_COMPLETED)


_REPORT_MODULE_COLUMNS = {
    'day-planning': 'Day Planning',
    'input-screening': 'Input Screening',
    'brass-qc': 'Brass QC',
    'iqf': 'IQF',
    'brass-audit': 'Brass Audit',
    'jig-loading': 'Jig Loading',
    'inprocess-inspection': 'IP Inspection',
    'jig-unloading-z1': 'Jig Unloading Z1',
    'jig-unloading-z2': 'Jig Unloading Z2',
    'nickel-inspection-z1': 'Nickel Wiping Z1',
    'nickel-inspection-z2': 'Nickel Wiping Z2',
    'nickel-audit-z1': 'Nickel Audit Z1',
    'nickel-audit-z2': 'Nickel Audit Z2',
    'spider-spindle-z1': 'Spider Spindle Z1',
    'spider-spindle-z2': 'Spider Spindle Z2',
}


def get_consolidated_report_rows(date_from=None, date_to=None, plating_stock_no='', module=''):
    """
    Build the consolidated journey rows. One row per original planning batch, including its split children;
    independent batches with the same plating number remain separate. Every module column is
    always populated — either with its actual data or "Not Reached" — so the
    report shows the complete lifecycle rather than only the latest stage.

    date_from / date_to filter on the latest stage activity timestamp.
    plating_stock_no is a partial (icontains) match. module limits results to
    rows that have reached the selected report stage.
    """
    from modelmasterapp.models import TotalStockModel, Plating_Color, ModelMasterCreation
    from Jig_Loading.models import JigCompleted
    from Jig_Unloading.models import JigUnloadAfterTable

    stock_qs = TotalStockModel.objects.filter(
        batch_id__isnull=False,
        batch_id__total_batch_quantity__gt=0,
        remove_lot=False,
    ).select_related('batch_id').order_by('-created_at', '-pk')

    if plating_stock_no:
        stock_qs = stock_qs.filter(
            batch_id__plating_stk_no__icontains=plating_stock_no.strip()
        )

    stocks = list(stock_qs)
    lot_ids = {s.lot_id for s in stocks if s.lot_id}
    batch_ids = {s.batch_id_id for s in stocks if s.batch_id_id}

    # Every TotalStockModel row sharing a batch_id (root + every accept/reject
    # child ever created at Input Screening/Brass QC/IQF/Brass Audit) — needed
    # to follow lot splits instead of getting stuck on whichever single row
    # was picked as "most recently active."
    # Exclude synthetic EX-* excess-lot rows: Jig Loading creates its own
    # TotalStockModel row for leftover/excess quantity (sharing the same
    # batch_id), but it never actually goes through Input Screening/Brass
    # QC/IQF/Brass Audit itself — including it here can let its own stale
    # completion flags (and a coincidentally later timestamp) get picked
    # over the real lot's row, showing e.g. the excess row's tiny qty
    # instead of the genuine lot's qty for an early-stage cell.
    stocks_by_batch = {}
    if batch_ids:
        for s in TotalStockModel.objects.filter(
            batch_id_id__in=batch_ids
        ).exclude(lot_id__startswith='EX-').select_related('batch_id').order_by('created_at', 'pk'):
            stocks_by_batch.setdefault(s.batch_id_id, []).append(s)

    lot_ids.update(s.lot_id for group in stocks_by_batch.values() for s in group if s.lot_id)

    # Bulk maps — avoid N+1
    jig_by_lot = {}
    for record in JigCompleted.objects.select_related('user').order_by('updated_at', 'pk').only(
        'lot_id', 'jig_position', 'updated_at', 'pick_remarks',
        'remarks', 'unloading_remarks', 'multi_model_allocation',
        'IP_loaded_date_time', 'original_lot_qty', 'updated_lot_qty',
        'loaded_cases_qty', 'user', 'user__username', 'draft_status', 'last_process_module',
    ):
        keys = {record.lot_id}
        for allocation in record.multi_model_allocation or []:
            if isinstance(allocation, dict) and allocation.get('lot_id'):
                keys.add(str(allocation['lot_id']))
        for key in keys:
            if key in lot_ids:
                jig_by_lot[key] = record  # latest actual draft/submission wins

    unload_by_lot = {}
    for record in JigUnloadAfterTable.objects.all().order_by('created_at', 'pk'):
        keys = {_normalize_unload_lot_id(record.lot_id)}
        for combined in record.combine_lot_ids or []:
            keys.add(_normalize_unload_lot_id(combined))
        for key in keys:
            if key in lot_ids:
                unload_by_lot[key] = record

    # Zone lookup for Jig Unloading / Nickel Wiping / Nickel Audit: same
    # table, routed to Zone 1 or Zone 2 by the lot's Plating_Color flags.
    zone_map = {}
    for pc in Plating_Color.objects.all().only('id', 'jig_unload_zone_1', 'jig_unload_zone_2'):
        if pc.jig_unload_zone_1:
            zone_map[pc.id] = 'z1'
        elif pc.jig_unload_zone_2:
            zone_map[pc.id] = 'z2'

    records = JourneyRecords(lot_ids | {r.lot_id for r in unload_by_lot.values()})
    records.add_stock_receipts(row for group in stocks_by_batch.values() for row in group)

    tz_aware = timezone.is_aware(timezone.now())

    def to_dt(d, end=False):
        dt = datetime.combine(d, time.max if end else time.min)
        return timezone.make_aware(dt) if tz_aware else dt

    from_dt = to_dt(date_from) if date_from else None
    to_dt_val = to_dt(date_to, end=True) if date_to else None

    rows_by_lot = {}
    # DP Pick already contains the batch before its first tray scan creates a
    # TotalStockModel lot. Include those real planning rows, without fabricating
    # stock records or inferring any downstream module entry.
    planning = ModelMasterCreation.objects.filter(
        total_batch_quantity__gt=0,
    ).exclude(pk__in=TotalStockModel.objects.filter(
        batch_id__isnull=False).values('batch_id')).order_by('-date_time', '-pk')
    if plating_stock_no:
        planning = planning.filter(plating_stk_no__icontains=plating_stock_no.strip())
    for batch in planning:
        stk_no = (batch.plating_stk_no or '').strip()
        activity = batch.date_time
        if (not stk_no or (from_dt and (not activity or activity < from_dt))
                or (to_dt_val and activity and activity > to_dt_val)):
            continue
        dp_cell, dp_status = _day_planning_cell(batch)
        modules = {name: _module_cell('Not Reached') for name in MODULE_COLUMNS}
        modules[STAGE_DAY_PLANNING] = dp_cell
        states = {name: STATE_NOT_REACHED for name in MODULE_COLUMNS}
        states[STAGE_DAY_PLANNING] = _stage_state(dp_status)
        rows_by_lot[('batch', batch.pk)] = {
            'plating_stk_no': stk_no, 'lot_qty': batch.total_batch_quantity,
            'modules': modules, 'module_states': states,
            'module_details': {name: _parse_cell_lines(cell) for name, cell in modules.items()},
            'remarks': batch.dp_pick_remarks or '', '_activity': activity,
        }
    for stock in stocks:
        batch = stock.batch_id
        stk_no = (batch.plating_stk_no or '').strip()
        if not stk_no:
            continue

        jig_record = jig_by_lot.get(stock.lot_id)
        unload_record = unload_by_lot.get(stock.lot_id)
        stocks_for_batch = stocks_by_batch.get(stock.batch_id_id) or [stock]

        dp_cell, dp_status = _day_planning_cell(batch, records.dp_batches.get(batch.pk))

        early_cells, early_statuses, running_out = _early_module_cells(
            stocks_for_batch, records=records
        )
        early_activity = running_out
        jig_cell, ip_cell, jig_status, ip_status, running_out = _jig_loading_cells(
            jig_record, records=records, lot_id=stock.lot_id
        )
        jig_activity = running_out
        late_cells, late_statuses, running_out = _late_module_cells(
            unload_record, zone_map, records=records, lot_id=stock.lot_id,
            plating_color_id=stock.plating_color_id, jig_record=jig_record
        )

        modules = {STAGE_DAY_PLANNING: dp_cell}
        modules.update(early_cells)
        modules[STAGE_JIG_LOADING] = jig_cell
        modules[STAGE_IP_INSPECTION] = ip_cell
        modules.update(late_cells)

        statuses = {STAGE_DAY_PLANNING: dp_status}
        statuses.update(early_statuses)
        statuses[STAGE_JIG_LOADING] = jig_status
        statuses[STAGE_IP_INSPECTION] = ip_status
        statuses.update(late_statuses)
        _apply_route_applicability(modules, statuses, zone_map.get(
            unload_record.plating_color_id if unload_record else stock.plating_color_id))
        module_states = {name: _stage_state(status) for name, status in statuses.items()}
        module_details = {name: _parse_cell_lines(text) for name, text in modules.items()}

        activity = _latest_time(running_out, early_activity, jig_activity,
                                batch.date_time, stock.created_at)

        # Date-range filter on latest stage activity
        if from_dt and (not activity or activity < from_dt):
            continue
        if to_dt_val and activity and activity > to_dt_val:
            continue

        remarks = _first_remark(
            getattr(unload_record, 'spider_pick_remarks', None) if unload_record else None,
            getattr(unload_record, 'na_pick_remarks', None) if unload_record else None,
            getattr(unload_record, 'nq_pick_remarks', None) if unload_record else None,
            getattr(jig_record, 'unloading_remarks', None) if jig_record else None,
            getattr(jig_record, 'pick_remarks', None) if jig_record else None,
            getattr(jig_record, 'remarks', None) if jig_record else None,
            stock.BA_pick_remarks, stock.IQF_pick_remarks,
            stock.Bq_pick_remarks, stock.IP_pick_remarks,
            batch.dp_pick_remarks,
        )

        row = {
            'plating_stk_no': stk_no,
            'lot_qty': int(batch.total_batch_quantity or 0),
            'modules': modules,
            'module_states': module_states,
            'module_details': module_details,
            'remarks': remarks,
            '_activity': activity,
        }

        row['_stage_branches'] = {
            name: {(('unload', unload_record.pk) if unload_record and name in MODULE_COLUMNS[7:]
                    else ('stock', stock.pk)): (modules[name], module_states[name])}
            for name in MODULE_COLUMNS[5:]
            if module_states[name] in (STATE_CURRENT, STATE_COMPLETED)
        }

        lot_key = ('batch', batch.pk)
        existing = rows_by_lot.get(lot_key)
        if existing is None:
            rows_by_lot[lot_key] = row
        else:
            # Split children share an upstream journey but may reach different
            # downstream stages. Retain actual evidence from both branches.
            newer = bool(row['_activity'] and (
                not existing['_activity'] or row['_activity'] > existing['_activity']))
            for name, branches in row['_stage_branches'].items():
                existing['_stage_branches'].setdefault(name, {}).update(branches)
            for name in MODULE_COLUMNS:
                state = row['module_states'][name]
                previous = existing['module_states'][name]
                actual = state in (STATE_CURRENT, STATE_COMPLETED)
                previous_actual = previous in (STATE_CURRENT, STATE_COMPLETED)
                if actual and (not previous_actual or newer):
                    for field in ('modules', 'module_states', 'module_details'):
                        existing[field][name] = row[field][name]
            existing['_activity'] = _latest_time(existing['_activity'], row['_activity'])
            if newer and row['remarks']:
                existing['remarks'] = row['remarks']

    sentinel = datetime.min
    if tz_aware:
        sentinel = timezone.make_aware(datetime(1, 1, 2))
    rows = sorted(
        rows_by_lot.values(),
        key=lambda r: (r['_activity'] or sentinel, r['plating_stk_no']),
        reverse=True,
    )
    # ``module`` controls which transaction column the Preview displays.  Do
    # not discard lots whose selected-stage state is Not Reached or Not
    # Applicable: those states are part of the report and must remain visible.
    for idx, row in enumerate(rows, start=1):
        row['s_no'] = idx
        row.pop('_activity', None)
        _render_stage_branches(row)
    return rows


def _normalize_plating_search(value):
    return ''.join(ch for ch in str(value or '').lower() if ch.isalnum())


def _plating_search_key_expression():
    expression = Lower('plating_stk_no')
    for separator in PLATING_SEARCH_SEPARATORS:
        expression = Replace(
            expression,
            Value(separator),
            Value(''),
            output_field=CharField(),
        )
    return expression


def _rank_plating_matches(values, query, limit):
    q_lower = query.lower()
    normalized_query = _normalize_plating_search(query)

    def sort_key(value):
        value_lower = value.lower()
        normalized_value = _normalize_plating_search(value)
        return (
            not value_lower.startswith(q_lower),
            not (normalized_query and normalized_value.startswith(normalized_query)),
            value_lower,
        )

    return sorted({value for value in values if value}, key=sort_key)[:limit]


def search_plating_stock(query, limit=15):
    """
    Autocomplete for Plating Stock No. Uses Elasticsearch when configured
    (settings.ELASTICSEARCH_URL + elasticsearch package installed),
    otherwise falls back to an indexed DB partial match.
    """
    from modelmasterapp.models import ModelMasterCreation

    query = (query or '').strip()
    if not query:
        return []

    results = set()
    es_url = getattr(settings, 'ELASTICSEARCH_URL', None)
    if es_url:
        try:
            Elasticsearch = import_module('elasticsearch').Elasticsearch
            client = Elasticsearch(es_url, request_timeout=2)
            response = client.search(
                index=getattr(settings, 'ELASTICSEARCH_PLATING_INDEX', 'plating_stock'),
                query={
                    'bool': {
                        'should': [
                            {'match_phrase_prefix': {'plating_stk_no': query}},
                            {'wildcard': {
                                'plating_stk_no.keyword': {
                                    'value': f'*{query}*',
                                    'case_insensitive': True,
                                },
                            }},
                            {'wildcard': {
                                'plating_stk_no': {
                                    'value': f'*{query}*',
                                    'case_insensitive': True,
                                },
                            }},
                        ],
                        'minimum_should_match': 1,
                    },
                },
                size=limit,
            )
            hits = [
                hit['_source'].get('plating_stk_no')
                for hit in response.get('hits', {}).get('hits', [])
            ]
            results.update(h for h in hits if h)
        except Exception:
            logger.warning('Elasticsearch autocomplete failed; using DB fallback', exc_info=True)

    # DB fallback: union of batch stock numbers (ModelMasterCreation, the
    # source the consolidated report uses) and the ModelMaster catalogue,
    # so every known plating stock number is suggested while typing.
    from modelmasterapp.models import ModelMaster

    normalized_query = _normalize_plating_search(query)

    def _matches(model):
        queryset = model.objects.exclude(
            plating_stk_no__isnull=True
        ).exclude(
            plating_stk_no=''
        ).annotate(
            _plating_search_key=_plating_search_key_expression()
        )
        filters = Q(plating_stk_no__icontains=query)
        if normalized_query:
            filters |= Q(_plating_search_key__contains=normalized_query)
        return queryset.filter(filters).values_list('plating_stk_no', flat=True).distinct()

    results.update(_matches(ModelMasterCreation))
    results.update(_matches(ModelMaster))
    # prefix matches first, including punctuation-insensitive prefixes, then alphabetical
    return _rank_plating_matches(results, query, limit)