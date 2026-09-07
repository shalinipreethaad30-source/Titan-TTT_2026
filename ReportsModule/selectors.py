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
STATE_CURRENT = 'current'
STATE_COMPLETED = 'completed'

_CURRENT_STAGE_STATUSES = {'In Progress', 'Pending'}


def _stage_state(status):
    """Classify a module's raw status string into a Preview UI bg state."""
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
                  rejected_qty=None, user=None, remarks=None):
    """Format one module's cell — the same multi-line block for Preview and Excel."""
    if status == 'Not Reached':
        return 'IN : --\nOUT: --\nLot Qty : --\nStatus : Not Reached'
    lines = [f"IN : {_fmt(in_time) or '--'}", f"OUT: {_fmt(out_time) or '--'}"]
    lines.append(f"Lot Qty : {lot_qty if lot_qty is not None else '--'}")
    if accepted_qty is not None:
        lines.append(f"Accepted : {accepted_qty}")
    if rejected_qty is not None:
        lines.append(f"Rejected : {rejected_qty}")
    lines.append(f"Status : {status}")
    if user:
        lines.append(f"User : {user}")
    if remarks:
        lines.append(f"Remarks : {remarks}")
    return '\n'.join(lines)


_TIME_LABELS = {'IN', 'OUT'}
_QTY_LABELS = {'Lot Qty', 'Accepted', 'Rejected'}
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
    for line in (text or '').split('\n'):
        if ':' not in line:
            continue
        label, _, value = line.partition(':')
        label = label.strip()
        value = value.strip()
        rows.append({'label': label, 'value': value, 'type': _cell_line_type(label)})
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
        self.entries = defaultdict(dict)
        self.submissions = defaultdict(dict)
        self.rw_quantities = {}
        self.is_quantities = defaultdict(dict)
        self.dp_transfers = {}
        self.dp_batches = {}
        if not lot_ids:
            return
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
                    if destination == 'IQF':
                        quantity = self.rw_quantities.get(
                            stock.lot_id, (None, stock.total_IP_accpeted_quantity))[1]
                    self._receive(destination, stock.lot_id, transferred_at, quantity)

    def entry(self, stage, lot_id):
        return self.entries[stage].get(lot_id)

    def submission(self, stage, lot_id):
        return self.submissions[stage].get(lot_id)


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
        values = {'lot_qty': record.total_qty}
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
            submission = records.submission(name, stock.lot_id) if records else None
            if (name == 'Brass QC' and stock.lot_id in bq_transition_lots
                    and not submission and stock.next_process_module != 'Brass QC'
                    and not (stock.current_stage == 'Brass QC'
                             and stock.last_process_module != 'Brass QC')):
                # This is the submitted parent's destination lot, not a new QC
                # execution. Its stale current_stage must not override the final
                # snapshot. A later return to QC or own submission remains eligible.
                continue
            status, out_time = _early_module_status(stock, spec)
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
    jig_cell = _module_cell(**values)
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
        unloading_entry.setdefault('lot_qty', jig_record.loaded_cases_qty)
    if zone and unloading_entry is not None:
        values = dict(status='In Progress', in_time=unloading_entry.get('in_time'),
                      lot_qty=unloading_entry.get('lot_qty'))
        values.update(submission_values('Jig Unloading', unloading_submission))
        put(f'Jig Unloading {zone.upper()}', values)
    if not unload_record:
        return cells, statuses, activity
    if zone:
        ju_done = bool(unload_record.unload_accepted or unload_record.Un_loaded_date_time)
        put(f'Jig Unloading {zone.upper()}', dict(
            status=('Accepted' if unload_record.unload_accepted else 'Completed') if ju_done else 'In Progress',
            in_time=(unloading_entry or {}).get('in_time') or unload_record.created_at,
            out_time=unload_record.Un_loaded_date_time if ju_done else None,
            lot_qty=unload_record.total_case_qty,
            accepted_qty=unload_record.accepted_qty if ju_done else None))
        for stage, prefix in [('Nickel Wiping', 'nq'), ('Nickel Audit', 'na')]:
            entry = records.entry(stage, unload_record.lot_id) if records else None
            submission = records.submission(stage, unload_record.lot_id) if records else None
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
                    if (unload_record.na_qc_rejection and previous_out
                            and (not cycle_time or previous_out > cycle_time)):
                        transfer_time = previous_out
                        done = False
                        if submission and submission.created_at <= transfer_time:
                            submission = None
                        entry = None
                else:
                    transfer_time = cycle_time
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
                          lot_qty=(entry or {}).get('lot_qty'),
                          accepted_qty=getattr(unload_record, prefix + '_qc_accepted_qty') if done else None,
                          remarks=getattr(unload_record, prefix + '_pick_remarks'))
            values.update(submission_values(stage, submission))
            if values['lot_qty'] is None:
                values['lot_qty'] = unload_record.total_case_qty
            put(f'{stage} {zone.upper()}', values)
    for number in (1, 2):
        stage = f'Spider Spindle Z{number}'
        entry = records.entry(stage, unload_record.lot_id) if records else None
        done = getattr(unload_record, f'ss_z{number}_completed', False)
        received = (zone == f'z{number}' and unload_record.na_qc_accptance
                    and unload_record.total_case_qty > 0)
        if not done and entry is None and not received:
            continue
        put(stage, dict(status='Completed' if done else 'In Progress',
                        in_time=(unload_record.na_last_process_date_time if received else None)
                                or (entry or {}).get('in_time'),
                        out_time=getattr(unload_record, f'ss_z{number}_completed_at') if done else None,
                        lot_qty=unload_record.total_case_qty,
                        remarks=unload_record.spider_pick_remarks))
    return cells, statuses, activity


def get_consolidated_report_rows(date_from=None, date_to=None, plating_stock_no=''):
    """
    Build the consolidated journey rows. One row per stock lot, or per
    planning batch before stock creation; plating numbers may repeat. Every module column is
    always populated — either with its actual data or "Not Reached" — so the
    report shows the complete lifecycle rather than only the latest stage.

    date_from / date_to filter on the latest stage activity timestamp.
    plating_stock_no is a partial (icontains) match.
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
            'lot_qty': int(stock.total_stock or batch.total_batch_quantity or 0),
            'modules': modules,
            'module_states': module_states,
            'module_details': module_details,
            'remarks': remarks,
            '_activity': activity,
        }

        lot_key = ('lot', stock.lot_id) if stock.lot_id else ('stock', stock.pk)
        existing = rows_by_lot.get(lot_key)
        if (
            existing is None
            or (row['_activity'] and not existing['_activity'])
            or (row['_activity'] and existing['_activity']
                and row['_activity'] > existing['_activity'])
        ):
            rows_by_lot[lot_key] = row

    sentinel = datetime.min
    if tz_aware:
        sentinel = timezone.make_aware(datetime(1, 1, 2))
    rows = sorted(
        rows_by_lot.values(),
        key=lambda r: (r['_activity'] or sentinel, r['plating_stk_no']),
        reverse=True,
    )
    for idx, row in enumerate(rows, start=1):
        row['s_no'] = idx
        row.pop('_activity', None)
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
