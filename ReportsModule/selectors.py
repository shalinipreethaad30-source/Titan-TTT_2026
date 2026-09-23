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

_CURRENT_STAGE_STATUSES = {'In Progress', 'Pending', 'Yet to be Loaded'}


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


def _combined_remarks(*values):
    """Return every distinct stage remark, retaining its source module."""
    remarks = []
    seen = set()
    for stage, value in values:
        text = str(value or '').strip()
        if not text:
            continue
        rendered = f'{stage}: {text}' if stage else text
        if rendered not in seen:
            seen.add(rendered)
            remarks.append(rendered)
    return '\n'.join(remarks)


def _first_remark(*values):
    for value in values:
        if value and str(value).strip():
            return str(value).strip()
    return ''


def _module_cell(status, in_time=None, out_time=None, lot_qty=None, accepted_qty=None,
                  rejected_qty=None, shortage_qty=None, missing_qty=None, user=None, remarks=None,
                  accepted_label='Accepted', remarks_label='Remarks', details=None):
    """Format one module's cell — the same multi-line block for Preview and Excel."""
    if status in ('Not Reached', 'Not Applicable'):
        return 'IN : --\nOUT: --\nLot Qty : --\nStatus : ' + status
    lines = [f"IN : {_fmt(in_time) or '--'}", f"OUT: {_fmt(out_time) or '--'}"]
    lines.append(f"Lot Qty : {lot_qty if lot_qty is not None else '--'}")
    if accepted_qty is not None:
        lines.append(f"{accepted_label} : {accepted_qty}")
    if rejected_qty is not None:
        lines.append(f"Rejected : {rejected_qty}")
    if shortage_qty:
        lines.append(f"Shortage : {shortage_qty}")
    if missing_qty:
        lines.append(f"Missing Qty : {missing_qty}")
    for label, value in details or []:
        lines.append(f"{label} : {value if value else '--'}")
    lines.append(f"Status : {status}")
    if user:
        lines.append(f"User : {user}")
    if remarks:
        lines.append(f"{remarks_label} : {remarks}")
    return '\n'.join(lines)


def _append_module_remarks(cell, *values):
    """Append distinct saved remarks to their own module cell."""
    if 'Status : Not Reached' in cell or 'Status : Not Applicable' in cell:
        return cell
    for value in values:
        text = str(value or '').strip()
        if text and f'Remarks : {text}' not in cell:
            cell += f'\nRemarks : {text}'
    return cell


_TIME_LABELS = {'IN', 'OUT'}
_QTY_LABELS = {'Lot Qty', 'Accepted', 'Loaded Jig', 'Unloaded Qty', 'Rejected', 'Shortage', 'Missing Qty'}
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


def _wiping_source_history(cell, sources):
    """Show the saved unloading origins and the last-model rejection rule."""
    rendered = []
    for block in cell.split('\n\n'):
        fields = dict(line.split(' : ', 1) for line in block.splitlines() if ' : ' in line)
        quantities = [source['qty'] for source in sources]
        if fields.get('Lot Qty') != str(sum(quantities)):
            # Do not attach an old allocation to a different rework receipt.
            rendered.append(block)
            continue
        history = [f'Previous History : Lot Qty {sum(quantities)}']
        for index, source in enumerate(sources, 1):
            history.append(f'M{index} : {source["model"]} - {source["jig_id"]} = {source["qty"]}')
        history.append('Total Lot Qty : ' + ' + '.join(map(str, quantities)) + f' = {sum(quantities)}')
        lines = block.splitlines()
        insert_at = next((i + 1 for i, line in enumerate(lines) if line.startswith('Lot Qty : ')), len(lines))
        lines[insert_at:insert_at] = history
        if fields.get('Status') in ('Accepted', 'Partially Accepted', 'Rejected'):
            accepted = int(fields.get('Accepted', '0'))
            rejected = int(fields.get('Rejected', '0'))
            action_at = next((i for i, line in enumerate(lines) if line.startswith(('Accepted : ', 'Rejected : '))), len(lines))
            lines.insert(action_at, 'Wiping Action : Accept / Reject')
            if accepted == 0 and rejected == sum(quantities):
                current = [0] * len(quantities)
            elif rejected <= quantities[-1] and accepted + rejected == sum(quantities):
                current = quantities[:-1] + [quantities[-1] - rejected]
            else:
                current = None
            lines.append(f'Current Info : Lot Qty {accepted}')
            if current is not None:
                for index, (source, qty) in enumerate(zip(sources, current), 1):
                    lines.append(f'M{index} : {source["model"]} - {source["jig_id"]} = {qty}')
            else:
                lines.append('Note : Rejection exceeds the last model quantity; model allocation requires verification.')
            lines.append(f'Total Lot Qty : {accepted}')
        rendered.append('\n'.join(lines))
    return '\n\n'.join(rendered)


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
                title = line.strip()
                if title:
                    lines.append({'label': '', 'value': title, 'type': 'heading'})
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
        self.ba_rejection_remarks = {}
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
        # Shortage is stored in the Input Screening rejection-reasons snapshot,
        # but intentionally excluded from IS_PartialRejectLot.rejected_qty
        # because it has no physical reject tray. Preserve that distinction in
        # the report by displaying it as its own quantity line.
        reject_model = apps.get_model('InputScreening', 'IS_PartialRejectLot')
        for row in reject_model.objects.filter(parent_lot_id__in=lot_ids).values(
                'parent_lot_id', 'rejection_reasons'):
            shortage_qty = sum(
                int(reason.get('qty') or 0)
                for reason in (row['rejection_reasons'] or {}).values()
                if isinstance(reason, dict) and (
                    reason.get('is_shortage')
                    or 'shortage' in str(reason.get('reason') or '').lower()
                )
            )
            if shortage_qty:
                quantities = self.is_quantities[row['parent_lot_id']]
                quantities['shortage_qty'] = (
                    int(quantities.get('shortage_qty') or 0) + shortage_qty
                )
        model = apps.get_model('BrassAudit', 'Brass_Audit_Rejection_ReasonStore')
        for row in model.objects.filter(
                lot_id__in=lot_ids,
                lot_rejected_comment__isnull=False,
        ).exclude(lot_rejected_comment='').order_by('created_at', 'pk').values(
                'lot_id', 'lot_rejected_comment'):
            self.ba_rejection_remarks[row['lot_id']] = row['lot_rejected_comment']
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


def _jig_loading_cells(jig_record, prev_out_time=None, records=None, lot_id=None,
                       plating_stock_no=None, _history_item=False):
    # A lot can be loaded in more than one jig.  Keep every loading event in
    # the report so a completed first load is not hidden when its remaining
    # quantity is later used as an added model in another jig.
    history = getattr(jig_record, '_report_jig_history', None) if jig_record else None
    if not _history_item and history and len(history) > 1:
        results = [
            _jig_loading_cells(record, prev_out_time=prev_out_time,
                               records=records, lot_id=lot_id,
                               plating_stock_no=plating_stock_no,
                               _history_item=True)
            for record in history
        ]
        jig_cells = [result[0] for result in results if result[0]]
        ip_cells = [result[1] for result in results
                    if result[1] and 'Status : Not Reached' not in result[1]]
        jig_statuses = [result[2] for result in results if result[2]]
        ip_statuses = [result[3] for result in results if result[3]]
        activities = [result[4] for result in results if result[4] is not None]
        return (
            '\n\n'.join(jig_cells),
            '\n\n'.join(ip_cells) if ip_cells else _module_cell('Not Reached'),
            'In Progress' if 'In Progress' in jig_statuses else
            (jig_statuses[-1] if jig_statuses else None),
            'In Progress' if 'In Progress' in ip_statuses else
            (ip_statuses[-1] if ip_statuses else None),
            _latest_time(*activities),
        )
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
    # Before loading is completed, the report has only the received lot.
    # Model role, jig details, broken hooks, and loaded quantity are final
    # transaction data and must not be shown yet.
    if not submitted:
        jig_cell = '\n'.join([
            f'IN : {_fmt((entry or {}).get("in_time")) or "--"}',
            'OUT: --',
            f'Lot Qty : {values.get("lot_qty") if values.get("lot_qty") is not None else "--"}',
            'Status : In Progress',
        ])
        activity = _latest_time(values['in_time'], values['out_time'])
        return jig_cell, _module_cell('Not Reached'), 'In Progress', None, activity
    allocations = [allocation for allocation in (getattr(jig_record, 'multi_model_allocation', None) or [])
                   if isinstance(allocation, dict) and allocation.get('lot_id')]
    primary = None
    primary_lot_id = None
    loaded_qty = getattr(jig_record, 'loaded_cases_qty', 0) or 0
    is_excess_lot = str(lot_id or '').startswith('EX-')

    def allocation_qty(allocation):
        return int(allocation.get('allocated_qty') or 0)

    # Both the primary and balance-lot report paths need this shared combined
    # jig identity for their IP Inspection note and 98 quantity.
    if len(allocations) > 1:
        primary = next((allocation for allocation in allocations
                        if allocation.get('role') == 'primary'), allocations[0])
        primary_lot_id = str(primary.get('lot_id') or '')
        loaded_qty = (getattr(jig_record, 'loaded_cases_qty', 0)
                      or sum(allocation_qty(allocation) for allocation in allocations))
    # An added model is identified from the saved allocation, not from its
    # lot-id prefix.  This makes the report work for a normal 4-qty lot added
    # to a 140-qty primary lot as well as generated EX balance lots.
    is_added_model_lot = (
        len(allocations) > 1
        and str(lot_id or '') != primary_lot_id
    )

    # A balance lot is rendered beneath its original lot, never as a separate
    # duplicate report row.
    if is_added_model_lot:
        secondary = next((allocation for allocation in allocations
                          if str(allocation.get('lot_id') or '') == str(lot_id)), None)
        secondary_index = allocations.index(secondary) + 1 if secondary else None
        heading = (f'Added Model - Model {secondary_index}' if secondary else
                   'Primary Model')
        if not submitted:
            jig_cell = '\n'.join([
                f'IN : {_fmt((entry or {}).get("in_time")) or "--"}',
                'OUT: --',
                f'Lot Qty : {allocation_qty(secondary) if secondary else (values.get("lot_qty") if values.get("lot_qty") is not None else "--")}',
                'Status : Yet to be Loaded',
            ])
        elif secondary:
            source_entry = records.entry(STAGE_JIG_LOADING, lot_id) if records else None
            source_submission = records.submission(STAGE_JIG_LOADING, lot_id) if records else None
            source_values = submission_values(STAGE_JIG_LOADING, source_submission)
            jig_cell = '\n'.join([
                heading,
                f'PLATING STK NO : {plating_stock_no or secondary.get("model") or secondary.get("model_no") or "--"}',
                f'IN : {_fmt((source_entry or {}).get("in_time")) or _fmt((entry or {}).get("in_time")) or "--"}',
                f'OUT: {_fmt(source_values.get("out_time")) or _fmt(values.get("out_time")) or "--"}',
                f'JIG ID : {getattr(jig_record, "jig_id", None) or "--"}',
                f'Lot Qty : {allocation_qty(secondary)}',
                f'Broken Hook : {getattr(jig_record, "broken_hooks", 0) or 0}',
                f'Loaded Qty : {allocation_qty(secondary)}',
                'Status : Completed',
            ])
        else:
            jig_cell = _module_cell(**values, accepted_label='Loaded Jig')
    elif not allocations:
        model = (plating_stock_no or getattr(jig_record, 'plating_stock_num', None)
                 or '--')
        jig_cell = '\n'.join([
            f'PLATING STK NO : {model}',
            f'IN : {_fmt((entry or {}).get("in_time")) or "--"}',
            f'OUT: {_fmt(values.get("out_time")) or "--"}',
            'Primary Model',
            f'JIG ID : {getattr(jig_record, "jig_id", None) or "--"}',
            f'Lot Qty : {values.get("lot_qty") if values.get("lot_qty") is not None else "--"}',
            f'Broken Hook : {getattr(jig_record, "broken_hooks", 0) or 0}',
            f'Loaded Qty : {getattr(jig_record, "loaded_cases_qty", 0) or 0}',
            f'Status : {values["status"]}',
        ])
    else:
        jig_cell = _module_cell(**values, accepted_label='Loaded Jig')
    # A completed primary lot in a multi-model jig remains one primary-model
    # transaction. Its added lot is reported from that added lot's own
    # balance history, rather than creating a second block here.
    primary_compact = False
    if len(allocations) > 1 and not is_added_model_lot:
        primary = next((allocation for allocation in allocations
                        if allocation.get('role') == 'primary'), allocations[0])
        primary_lot_id = str(primary.get('lot_id') or '')
        loaded_qty = (getattr(jig_record, 'loaded_cases_qty', 0)
                      or sum(allocation_qty(allocation) for allocation in allocations))
        if str(lot_id or '') == primary_lot_id:
            primary_model = primary.get('model') or primary.get('model_no') or plating_stock_no or '--'
            source_entry = records.entry(STAGE_JIG_LOADING, primary_lot_id) if records else None
            source_submission = records.submission(STAGE_JIG_LOADING, primary_lot_id) if records else None
            source_values = submission_values(STAGE_JIG_LOADING, source_submission)
            jig_cell = '\n'.join([
                f'PLATING STK NO : {primary_model}',
                f'IN : {_fmt((source_entry or {}).get("in_time")) or "--"}',
                f'OUT: {_fmt(source_values.get("out_time")) or "--"}',
                'Primary Model',
                f'JIG ID : {getattr(jig_record, "jig_id", None) or "--"}',
                f'Lot Qty : {allocation_qty(primary)}',
                f'Broken Hook : {getattr(jig_record, "broken_hooks", 0) or 0}',
                f'Loaded Qty : {allocation_qty(primary)}',
                f'Status : {values["status"]}',
            ])
            primary_compact = True
    display_values = dict(values)
    if len(allocations) > 1:
        # Individual source quantities are not independently loaded; both
        # sources share the final combined Jig Loaded Qty.
        display_values.pop('accepted_qty', None)
    if len(allocations) > 1 and not is_added_model_lot and not primary_compact:
        primary = next((allocation for allocation in allocations
                        if allocation.get('role') == 'primary'), allocations[0])
        primary_lot_id = str(primary.get('lot_id') or '')
        quantity = allocation_qty
        loaded_qty = (getattr(jig_record, 'loaded_cases_qty', 0)
                      or sum(quantity(allocation) for allocation in allocations))
        jig_capacity = (getattr(jig_record, 'effective_capacity', 0)
                        or getattr(jig_record, 'jig_capacity', 0))
        ordered = [primary] + [a for a in allocations if a is not primary]
        blocks = []
        for index, allocation in enumerate(ordered, 1):
            source = str(allocation.get('lot_id') or '')
            source_entry = records.entry(STAGE_JIG_LOADING, source) if records else None
            source_submission = records.submission(STAGE_JIG_LOADING, source) if records else None
            source_values = submission_values(STAGE_JIG_LOADING, source_submission)
            lines = [
                f"IN : {_fmt((source_entry or {}).get('in_time')) or '--'}",
                f"OUT: {_fmt(source_values.get('out_time')) or '--'}",
            ]
            if index == 1:
                lines.append(f'Jig Cap : {jig_capacity or "--"}')
            else:
                lines.append(f'Add Model - Model {index}')
            lines.extend([
                f'Primary : {primary.get("model") or primary.get("model_no") or "--"}',
                f'Model : {allocation.get("model") or allocation.get("model_no") or "--"}',
                f'Jig ID : {getattr(jig_record, "jig_id", None) or "--"}',
                f'Lot Qty : {quantity(allocation)}',
            ])
            if index == 1 and jig_capacity:
                lines.append(f'Calc : {jig_capacity} - {quantity(primary)} = {max(0, jig_capacity - quantity(primary))}')
            if index == 1:
                # The primary is already identified by its Primary label.
                lines = [line for line in lines if not line.startswith('Model : ')]
            else:
                # Added sources are a breakdown of this jig, not another
                # primary transaction inside the primary report row.
                lines = [
                    f"IN : {_fmt((source_entry or {}).get('in_time')) or '--'}",
                    f"OUT: {_fmt(source_values.get('out_time')) or '--'}",
                    f'Add Model - Model {index} : {allocation.get("model") or allocation.get("model_no") or "--"}',
                    f'Jig ID : {getattr(jig_record, "jig_id", None) or "--"}',
                    f'Broken Hook : {getattr(jig_record, "broken_hooks", 0) or 0}',
                ]
            if index > 1:
                lines.append(f'Added Qty : {quantity(allocation)}')
                lines.append(
                    f'Calc : {quantity(allocation)} + {quantity(primary)} = {loaded_qty}'
                )
            blocks.append('\n'.join(lines))
        # Every source row displays the same combined Jig Loading record in
        # the same order: primary section, added-model section, then total.
        excess_qty = max(0, sum(quantity(a) for a in ordered) - int(jig_capacity or 0))
        blocks[-1] += f'\nJig Loaded Qty : {loaded_qty}'
        if excess_qty:
            operands = ' + '.join(str(quantity(a)) for a in ordered)
            blocks[-1] += f'\nExcess : {operands} - {jig_capacity} = {excess_qty}'
        else:
            blocks[-1] += '\nExcess : No Excess'
        blocks[-1] += f"\nStatus : {values['status']}"
        if values.get('remarks'):
            blocks[-1] += f"\nRemarks : {values['remarks']}"
        jig_cell = '\n\n'.join(blocks)
    activity = _latest_time(values['in_time'], values['out_time'])
    # A submitted loading record is the actual handoff into IP Inspection.
    # IP_loaded_date_time belongs to IP Inspection, never Jig Loading.
    if not submitted:
        return jig_cell, _module_cell('Not Reached'), values['status'], None, activity
    ip_done = bool(jig_record.jig_position)
    ip_out = jig_record.IP_loaded_date_time if ip_done else None
    ip_status = 'Completed' if ip_done else 'In Progress'
    ip_remarks = jig_record.remarks
    if primary is not None:
        primary_model = str(primary.get('model') or primary.get('model_no') or '--')
        if str(lot_id or '') == primary_lot_id:
            combined_note = (
                'Combined with Added Model; Plating Stk No: '
                + str(plating_stock_no or primary_model)
            )
        else:
            combined_note = (
                'Combined with Primary Model; Plating Stk No: '
                + str(plating_stock_no or primary_model)
            )
        ip_remarks = _combined_remarks(
            ('', ip_remarks),
            ('', combined_note),
        )
    combined_source = next((allocation for allocation in allocations
                            if str(allocation.get('lot_id') or '') == str(lot_id or '')), None)
    ip_lot_qty = (allocation_qty(combined_source)
                  if combined_source is not None else
                  (loaded_qty if len(allocations) > 1
                   else values.get('accepted_qty', jig_record.loaded_cases_qty)))
    ip_details = []
    if len(allocations) > 1:
        ip_details.append(('Jig Qty', loaded_qty))
    ip_details.append(('Jig ID', getattr(jig_record, 'jig_id', None)))
    bath_number = getattr(getattr(jig_record, 'bath_numbers', None), 'bath_number', None)
    if bath_number:
        ip_details.append(('Bath No', bath_number))
    ip_cell = _module_cell(ip_status, in_time=values['out_time'], out_time=ip_out,
                           lot_qty=ip_lot_qty,
                           remarks=ip_remarks, remarks_label='Note',
                           details=ip_details)
    return jig_cell, ip_cell, values['status'], ip_status, _latest_time(activity, ip_out)


def _late_module_cells(unload_record, zone_map, prev_out_time=None, records=None,
                       lot_id=None, plating_color_id=None, jig_record=None):
    columns = MODULE_COLUMNS[7:]
    cells = {name: _module_cell('Not Reached') for name in columns}
    statuses = dict.fromkeys(columns)
    zone = zone_map.get(unload_record.plating_color_id if unload_record else plating_color_id)
    activity = None
    latest_audit = None
    source_quantities = getattr(unload_record, '_report_source_quantities', {})
    source_qty = source_quantities.get(lot_id)
    combined_qty = sum(source_quantities.values())

    jig_allocations = [allocation for allocation in (
        getattr(jig_record, 'multi_model_allocation', None) or []
    ) if isinstance(allocation, dict) and allocation.get('lot_id')]
    jig_primary = next((allocation for allocation in jig_allocations
                        if allocation.get('role') == 'primary'),
                       jig_allocations[0] if jig_allocations else None)
    # A primary model that had another model added during Jig Loading uses
    # the normal compact Nickel Wiping receipt. The detailed source-history
    # format remains for models combined during Jig Unloading.
    is_jig_loading_multi_model_primary = bool(
        jig_primary
        and len(jig_allocations) > 1
        and str(jig_primary.get('lot_id') or '') == str(lot_id or '')
    )
    jig_source = next((allocation for allocation in jig_allocations
                       if str(allocation.get('lot_id') or '') == str(lot_id or '')), None)
    combined_jig_qty = getattr(jig_record, 'loaded_cases_qty', 0) or 0
    source_jig_qty = (int(jig_source.get('allocated_qty') or 0)
                      if jig_source is not None else None)
    # IP Inspection receives the quantity accepted by the corresponding Jig
    # Loading submission.  A JigCompleted record can represent several lots,
    # so its loaded_cases_qty may be the whole jig quantity and must not be
    # used for this individual lot's Jig Unloading receipt.
    jig_loading_submission = records.submission(STAGE_JIG_LOADING, lot_id) if records else None
    ip_received_qty = (
        source_jig_qty if source_jig_qty is not None else
        (submission_values(STAGE_JIG_LOADING, jig_loading_submission).get('accepted_qty')
         if jig_loading_submission else None)
    )
    if ip_received_qty is None and jig_record:
        ip_received_qty = jig_record.loaded_cases_qty
    unloading_lot_qty = source_jig_qty if source_jig_qty is not None else ip_received_qty

    def put(name, values):
        nonlocal activity
        cells[name] = _module_cell(**values)
        statuses[name] = values['status']
        activity = _latest_time(activity, values.get('in_time'), values.get('out_time'))

    # IP Inspection has not handed the jig to Unloading until its own submit
    # creates the completed jig position. Do not expose an unloading receipt,
    # or any later stage, while that IP transaction remains in progress.
    if not (jig_record and jig_record.jig_position):
        return cells, statuses, activity

    unloading_entry = records.entry('Jig Unloading', lot_id) if records else None
    unloading_submission = records.submission('Jig Unloading', lot_id) if records else None
    if zone and jig_record and jig_record.IP_loaded_date_time:
        # Inspection submit is the actual handoff opening Unloading Pick.
        unloading_entry = dict(unloading_entry or {})
        unloading_entry['in_time'] = _earliest(
            unloading_entry.get('in_time'), jig_record.IP_loaded_date_time)
        unloading_entry.setdefault('lot_qty', unloading_lot_qty)
    if zone and unloading_entry is not None:
        values = dict(status='In Progress', in_time=unloading_entry.get('in_time'),
                      lot_qty=unloading_entry.get('lot_qty'))
        # Saving tray rows is not the final unloading action.  A Jig Unloading
        # submission becomes complete only when Submit All creates its
        # JigUnloadAfterTable handoff record.
        if unload_record:
            values.update(submission_values('Jig Unloading', unloading_submission))
        if source_jig_qty is not None:
            values['lot_qty'] = source_jig_qty
            values['details'] = [('Jig Qty', combined_jig_qty)]
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
        # An unload timestamp or a later module must not complete Jig
        # Unloading. Only its own final submitted record is confirmation that
        # the lot was actually unloaded.
        ju_done = bool(
            jig_unloading_submission
            and not getattr(jig_unloading_submission, 'is_draft', False)
        )
        # Preserve the quantity handed off by IP Inspection. Later unloading
        # or Nickel allocations can be smaller, but must not rewrite the
        # historical Jig Unloading receipt quantity.
        jig_unload_lot_qty = (
            source_jig_qty
            if source_jig_qty is not None else
            (ip_received_qty if ip_received_qty is not None
             else jig_unload_source.total_case_qty)
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
        if source_qty is not None:
            # Nickel ledgers describe the combined transfer; project its
            # outcome only after matching the full saved receipt quantity.
            jig_unload_accepted_qty = combined_qty
        unload_display_qty = (source_qty if source_qty is not None
                              else jig_unload_accepted_qty)
        jig_unload_missing_qty = max(
            0, (jig_unload_lot_qty or 0) - (unload_display_qty or 0)
        )
        combined_remark = None
        if source_qty is not None and len(source_quantities) > 1:
            other_qty = combined_qty - source_qty
            combined_remark = (
                f'Added model qty: {other_qty}. '
                f'Combined unloading qty: {combined_qty}.'
            )
        put(f'Jig Unloading {zone.upper()}', dict(
            status='Completed' if ju_done else 'In Progress',
            in_time=(jig_unloading_entry or {}).get('in_time') or jig_unload_source.created_at,
            out_time=(jig_unload_source.Un_loaded_date_time or wiping_started_at)
                     if ju_done else None,
            lot_qty=jig_unload_lot_qty,
            accepted_qty=unload_display_qty if ju_done else None,
            accepted_label='Unloaded Qty',
            missing_qty=jig_unload_missing_qty if ju_done else None,
            details=[
                ('Jig ID', getattr(jig_record, 'jig_id', None)),
                ('Jig Qty', getattr(jig_record, 'loaded_cases_qty', None)),
            ],
            remarks=combined_remark))
        combined_cells = getattr(unload_record, '_report_combined_unloading_cells', {})
        # A whole-jig add-model transaction (for example 98 + 98) takes
        # precedence over the legacy per-source blocks built from its models.
        combined_cell = getattr(unload_record, '_report_combined_unloading_cell', None)
        combined_cell = combined_cell or (
            combined_cells.get(str(lot_id or ''))
            if isinstance(combined_cells, dict) else None
        )
        if ju_done and combined_cell:
            cells[f'Jig Unloading {zone.upper()}'] = combined_cell
        if getattr(unload_record, '_report_combined_secondary', False):
            # Keep this source's own Jig Unloading block, then carry the
            # primary combined Nickel history into it.  The caller appends
            # that history beside a hidden balance lot's unloading block.
            combined_transfer = getattr(unload_record, '_report_combined_transfer', None)
            if combined_transfer:
                primary_unload, primary_lot_id = combined_transfer
                primary_jig = None
                if records:
                    # The primary submission's Jig ID is already present in
                    # the current report map; find it from the source mapping
                    # through the passed JigCompleted relationship instead of
                    # changing the displayed added-lot jig.
                    primary_jig = jig_record
                    allocations = getattr(jig_record, 'multi_model_allocation', None) or []
                    if not any(str(a.get('lot_id') or '') == str(primary_lot_id)
                               for a in allocations if isinstance(a, dict)):
                        primary_jig = None
                primary_cells, primary_statuses, primary_activity = _late_module_cells(
                    primary_unload, zone_map, records=records,
                    lot_id=primary_lot_id, plating_color_id=plating_color_id,
                    jig_record=primary_jig or jig_record,
                )
                for name in ('Nickel Wiping Z1', 'Nickel Wiping Z2',
                             'Nickel Audit Z1', 'Nickel Audit Z2'):
                    cells[name] = primary_cells[name]
                    statuses[name] = primary_statuses[name]
                activity = _latest_time(activity, primary_activity)
            return cells, statuses, activity
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
                if is_jig_loading_multi_model_primary:
                    # A primary Jig Loading model hands the entire combined
                    # jig to Nickel Wiping. Its own source share (for example
                    # 40) must not replace the 98-qty Jig Unloading receipt.
                    values['lot_qty'] = combined_jig_qty
                    values['in_time'] = (
                        getattr(jig_unload_source, 'Un_loaded_date_time', None)
                        or values.get('in_time')
                    )
                # Child/source unload records can point at an earlier parent.
                # Preserve the total from the combined Add Model transaction
                # that selected this report row (for example 98+98+98=294).
                combined_unload_qty = (
                    getattr(unload_record, '_report_combined_jig_qty', None)
                    or getattr(jig_unload_source, '_report_combined_jig_qty', None)
                )
                if combined_unload_qty:
                    values['lot_qty'] = combined_unload_qty
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
    wiping_sources = getattr(unload_record, '_report_wiping_sources', None)
    if wiping_sources and zone and not is_jig_loading_multi_model_primary:
        name = f'Nickel Wiping {zone.upper()}'
        cells[name] = _wiping_source_history(cells[name], wiping_sources)
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
    from Jig_Loading.models import JigCompleted, ExcessLotRecord
    from Jig_Unloading.models import JigUnloadAfterTable

    stock_qs = TotalStockModel.objects.filter(
        batch_id__isnull=False,
        batch_id__total_batch_quantity__gt=0,
        remove_lot=False,
    ).select_related('batch_id').order_by('-created_at', '-pk')

    stocks = list(stock_qs)
    lot_ids = {s.lot_id for s in stocks if s.lot_id}
    split_records = list(ExcessLotRecord.objects.select_related('jig_loading_record'))
    split_quantities = {r.new_lot_id: r.lot_qty for r in split_records}
    split_sources = {}
    for split in split_records:
        loading = split.jig_loading_record
        # A multi-model jig's primary lot need not own the remainder.
        sources = {}
        for tray in loading.tray_data or []:
            source = tray.get('source_lot_id')
            qty = int(tray.get('excess_qty', 0) or 0)
            if source and qty > 0:
                sources[source] = sources.get(source, 0) + qty
        if not sources:
            for allocation in loading.multi_model_allocation or []:
                source = allocation.get('lot_id')
                requested = allocation.get('requested_qty', allocation.get('model_lot_qty'))
                if source and requested is not None:
                    qty = int(requested or 0) - int(allocation.get('allocated_qty', 0) or 0)
                    if qty > 0:
                        sources[source] = sources.get(source, 0) + qty
        if len(sources) == 1 and sum(sources.values()) == split.lot_qty:
            split_sources[split.new_lot_id] = next(iter(sources))
        elif not loading.is_multi_model:
            split_sources[split.new_lot_id] = split.parent_lot_id
    source_batches = dict(TotalStockModel.objects.filter(
        lot_id__in=set(split_sources.values())).values_list('lot_id', 'batch_id_id'))
    split_batches = {}
    for child in split_sources:
        source, seen = child, set()
        while source in split_sources and source not in seen:
            seen.add(source)
            source = split_sources[source]
        if source not in split_quantities and source_batches.get(source):
            split_batches[child] = source_batches[source]
    source_batch_objects = ModelMasterCreation.objects.in_bulk(set(split_batches.values()))
    if plating_stock_no:
        matching_batch_ids = set(ModelMasterCreation.objects.filter(
            plating_stk_no__icontains=plating_stock_no.strip()
        ).values_list('pk', flat=True))
        stocks = [
            stock for stock in stocks
            if split_batches.get(stock.lot_id, stock.batch_id_id) in matching_batch_ids
        ]
    batch_ids = ({s.batch_id_id for s in stocks if s.batch_id_id}
                 | set(split_batches.get(s.lot_id) for s in stocks if s.lot_id in split_batches))

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
    jig_history_by_lot = {}
    for record in JigCompleted.objects.select_related('user', 'bath_numbers').order_by('updated_at', 'pk').only(
        'lot_id', 'jig_position', 'updated_at', 'pick_remarks',
        'remarks', 'unloading_remarks', 'multi_model_allocation',
        'IP_loaded_date_time', 'original_lot_qty', 'updated_lot_qty',
        'loaded_cases_qty', 'jig_id', 'jig_capacity', 'effective_capacity', 'excess_qty',
        'broken_hooks', 'plating_stock_num',
        'bath_numbers__bath_number',
        'user', 'user__username', 'draft_status', 'last_process_module',
    ):
        keys = {record.lot_id}
        for allocation in record.multi_model_allocation or []:
            if isinstance(allocation, dict) and allocation.get('lot_id'):
                keys.add(str(allocation['lot_id']))
        for key in keys:
            if key in lot_ids:
                jig_history_by_lot.setdefault(key, []).append(record)
                jig_by_lot[key] = record  # latest record drives downstream state

    for key, history in jig_history_by_lot.items():
        # The display uses the full sequence; downstream modules still use
        # the newest record for their current-state calculations.
        jig_by_lot[key]._report_jig_history = history

    # Show generated balance lots inside their source lot's Jig Loading cell.
    # This is the one place where the balance belongs in the consolidated
    # journey; rendering its TotalStock row separately duplicates the quantity.
    balance_lots_by_parent = {}
    balance_child_ids = set()
    for split in split_records:
        parent_lot_id = split_sources.get(split.new_lot_id, split.parent_lot_id)
        if parent_lot_id:
            balance_lots_by_parent.setdefault(str(parent_lot_id), []).append(split)
            balance_child_ids.add(str(split.new_lot_id))

    def balance_descendants(parent_lot_id, seen=None):
        """Yield every later Jig Loading balance from one original lot."""
        seen = set() if seen is None else seen
        for balance in balance_lots_by_parent.get(str(parent_lot_id), []):
            child_lot_id = str(balance.new_lot_id)
            if child_lot_id in seen:
                continue
            seen.add(child_lot_id)
            yield balance
            yield from balance_descendants(child_lot_id, seen)

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

    # Older unload rows sometimes retain only the primary source ID, while
    # the submitted tray snapshot preserves every lot in the combined load.
    combined_unloads = JigUnloadAfterTable.objects.filter(jig_qr_id__contains=',')
    unload_candidates = {r.pk: r for r in unload_by_lot.values()}
    unload_candidates.update({r.pk: r for r in combined_unloads})
    for unload in list(unload_candidates.values()):
        jig_ids = [item.strip() for item in str(unload.jig_qr_id or '').split(',') if item.strip()]
        # Jig IDs are reused over time; select the newest completed record
        # for each ID in this combined unloading transaction.
        whole_jigs_by_id = {}
        if len(jig_ids) > 1:
            for candidate in JigCompleted.objects.filter(jig_id__in=jig_ids).order_by('-pk'):
                whole_jigs_by_id.setdefault(candidate.jig_id, candidate)
        whole_jigs = [whole_jigs_by_id[jig_id] for jig_id in jig_ids
                      if jig_id in whole_jigs_by_id]
        if len(whole_jigs) == len(jig_ids) and sum(j.loaded_cases_qty or 0 for j in whole_jigs) == unload.total_case_qty:
            lines = ['Jig Unloading - Add Model', 'Plating Stk No : ' + str(getattr(whole_jigs[0], 'plating_stock_num', None) or '--')]
            combined_sources = set(unload.combine_lot_ids or [])
            for index, jig in enumerate(sorted(whole_jigs, key=lambda j: jig_ids.index(j.jig_id)), 1):
                lines += ['IN TIME : ' + (_fmt(getattr(jig, 'IP_loaded_date_time', None)) or '--'), 'OUT TIME: ' + (_fmt(unload.Un_loaded_date_time) or '--'), f'Jig ID-{index} : {jig.jig_id}', f'Jig ID-{index} Qty : {jig.loaded_cases_qty}']
                combined_sources.update(str(a.get('lot_id')) for a in (jig.multi_model_allocation or []) if a.get('lot_id'))
            lines += [f'Unloaded Qty : {unload.total_case_qty}', 'Status : Completed']
            unload._report_combined_unloading_cell = '\n'.join(lines)
            unload._report_combined_jig_qty = unload.total_case_qty
            for source in combined_sources:
                if source in lot_ids: unload_by_lot[source] = unload
        # A combined unloading can contain multi-model jigs. Collect every
        # source mapping first so two complete 98 jigs (50+48 and 96+2) are
        # treated as one 196 add-model unloading transaction.
        all_mappings = []
        for raw_source in unload.combine_lot_ids or []:
            submitted = records.submission('Jig Unloading', _normalize_unload_lot_id(raw_source))
            for tray in getattr(submitted, 'tray_data', None) or []:
                all_mappings.extend((tray.get('_source_metadata') or {}).get('source_mappings', []))
        by_jig = {}
        for mapping in all_mappings:
            jig_key = str(mapping.get('jig_completed_id') or mapping.get('jig_id') or '')
            if jig_key:
                by_jig.setdefault(jig_key, []).append(mapping)
        if len(by_jig) > 1:
            jigs = JigCompleted.objects.in_bulk({int(k) for k in by_jig if k.isdigit()})
            details = []
            sources = set()
            for key, mappings in by_jig.items():
                jig = jigs.get(int(key)) if key.isdigit() else None
                qty = sum(int(m.get('qty') or 0) for m in mappings)
                sources.update(str(m.get('lot_id')) for m in mappings if m.get('lot_id'))
                details.append((jig, mappings[0], qty))
            if len(details) > 1 and all((j.loaded_cases_qty if j else 0) == qty for j, _, qty in details):
                lines = ['Jig Unloading - Add Model', 'Plating Stk No : ' + str(getattr(details[0][0], 'plating_stock_num', None) or '--')]
                for index, (jig, mapping, qty) in enumerate(details, 1):
                    lines += ['IN TIME : ' + (_fmt(getattr(jig, 'IP_loaded_date_time', None)) or '--'), 'OUT TIME: ' + (_fmt(unload.Un_loaded_date_time) or '--'), f'Jig ID-{index} : ' + str(mapping.get('jig_id') or '--'), f'Jig ID-{index} Qty : {qty}']
                lines += ['Unloaded Qty : ' + str(sum(qty for _, _, qty in details)), 'Status : Completed']
                unload._report_combined_unloading_cell = '\n'.join(lines)
                unload._report_combined_jig_qty = sum(qty for _, _, qty in details)
                for source in sources:
                    if source in lot_ids: unload_by_lot[source] = unload
        for raw_source in unload.combine_lot_ids or []:
            submission = records.submission('Jig Unloading', _normalize_unload_lot_id(raw_source))
            for tray in getattr(submission, 'tray_data', None) or []:
                source_metadata = tray.get('_source_metadata') or {}
                mappings = source_metadata.get('source_mappings', [])
                quantities = {str(m['lot_id']): int(m.get('qty') or 0)
                              for m in mappings if m.get('lot_id')}
                if len(quantities) < 2 or any(q <= 0 for q in quantities.values()):
                    continue
                if sum(quantities.values()) != submission.total_qty:
                    continue
                unload._report_source_quantities = quantities
                # Nickel partial acceptance creates child unload rows without
                # an unloading timestamp. Keep the original handoff time.
                receipt_unload = unload
                seen_unload_ids = set()
                while receipt_unload.lot_id not in seen_unload_ids:
                    seen_unload_ids.add(receipt_unload.lot_id)
                    parent_unload = (
                        records.nq_partial_accept_parent_unloads.get(receipt_unload.lot_id)
                        or records.na_partial_accept_parent_unloads.get(receipt_unload.lot_id)
                    )
                    if parent_unload is None:
                        break
                    receipt_unload = parent_unload
                unloading_out_time = receipt_unload.Un_loaded_date_time
                # Preserve the individual receipts in this unloading merge.
                # Reuse the same display on every source report row.
                source_blocks = []
                wiping_sources = []
                receipt_quantities = []
                mapped_jigs = JigCompleted.objects.in_bulk({
                    int(m['jig_completed_id']) for m in mappings
                    if m.get('jig_completed_id')
                })
                jig_groups = {}
                seen_sources = set()
                for mapping in mappings:
                    source = str(mapping.get('lot_id') or '')
                    if source not in quantities or source in seen_sources:
                        continue
                    seen_sources.add(source)
                    source_jig = (mapped_jigs.get(int(mapping['jig_completed_id']))
                                  if mapping.get('jig_completed_id') else jig_by_lot.get(source))
                    # Two models can share one physical jig.  They still
                    # require separate unloading report blocks, one per
                    # source lot (for example 54 and 44), rather than being
                    # merged under the jig's primary key.
                    key = source
                    group = jig_groups.setdefault(key, {
                        'source': source, 'jig': source_jig, 'mapping': mapping,
                        'unloaded_qty': 0,
                    })
                    group['unloaded_qty'] += quantities[source]
                source_cells = {}
                source_jig_details = []
                for index, group in enumerate(jig_groups.values(), start=1):
                    source = group['source']
                    source_jig = group['jig']
                    mapping = group['mapping']
                    loading = records.submission(STAGE_JIG_LOADING, source)
                    jig_qty = (source_jig.loaded_cases_qty if source_jig
                               else getattr(loading, 'loaded_cases_qty', None))
                    # The shared jig holds the combined quantity, but each
                    # report row retains this source model's own lot quantity.
                    lot_qty = quantities[source]
                    source_submission = records.submission('Jig Unloading', source)
                    source_entry = records.entry('Jig Unloading', source) or {}
                    unloaded_qty = group['unloaded_qty']
                    receipt_quantities.append(lot_qty)
                    # The unloading operation is against the complete shared
                    # jig.  Show that completed jig quantity in each source
                    # model's block, while Lot Qty remains the source amount.
                    displayed_unloaded_qty = jig_qty if jig_qty is not None else unloaded_qty
                    missing_qty = max(0, (jig_qty or 0) - displayed_unloaded_qty)
                    # A combined unloading is reported per source jig.  Keep
                    # the field order fixed so each completed block reads as
                    # one physical jig-unloading transaction.
                    block = '\n'.join([
                        'IN : ' + (_fmt(getattr(source_jig, 'IP_loaded_date_time', None)
                                         or source_entry.get('in_time')) or '--'),
                        'OUT: ' + (_fmt(unloading_out_time) or '--'),
                        'Jig ID : ' + str(mapping.get('jig_id') or '--'),
                        'Jig Qty : ' + str(jig_qty if jig_qty is not None else '--'),
                        'Lot Qty : ' + str(lot_qty),
                        'Unloaded Jig : ' + str(displayed_unloaded_qty),
                        'Missing Qty : ' + str(missing_qty),
                        'Status : Completed',
                    ])
                    model_no = (getattr(source_submission, 'model_no', None)
                                or getattr(submission, 'model_no', None) or '--')
                    wiping_sources.append({'model': model_no,
                                           'jig_id': mapping.get('jig_id') or '--',
                                           'qty': unloaded_qty})
                    source_blocks.append(block)
                    source_cells[source] = block
                    source_jig_details.append({
                        'source': source,
                        'stock_no': getattr(source_jig, 'plating_stock_num', None),
                        'in_time': (getattr(source_jig, 'IP_loaded_date_time', None)
                                    or source_entry.get('in_time')),
                        'out_time': unloading_out_time,
                        'jig_id': mapping.get('jig_id'),
                        'jig_qty': jig_qty,
                        'lot_qty': lot_qty,
                    })
                combined_cell = '\n\n'.join(source_blocks)
                # When separate full jigs are added in one Jig Unloading
                # transaction (for example, 98 + 98), display the add-model
                # transaction as one two-jig record. Partial model shares
                # such as 54 + 44 retain their individual source blocks.
                is_full_jig_add_model = (
                    len(source_jig_details) > 1
                    and all(
                        detail['jig_qty'] is not None
                        and detail['lot_qty'] == detail['jig_qty']
                        for detail in source_jig_details
                    )
                )
                if is_full_jig_add_model:
                    first = source_jig_details[0]
                    add_model_lines = [
                        'Jig Unloading - Add Model',
                        'Plating Stk No : ' + str(first['stock_no'] or '--'),
                    ]
                    for index, detail in enumerate(source_jig_details, start=1):
                        add_model_lines.extend([
                            'IN TIME : ' + (_fmt(detail['in_time']) or '--'),
                            'OUT TIME: ' + (_fmt(detail['out_time']) or '--'),
                            f'Jig ID-{index} : ' + str(detail['jig_id'] or '--'),
                            f'Jig ID-{index} Qty : ' + str(detail['jig_qty']),
                        ])
                    add_model_lines.extend([
                        'Unloaded Qty : ' + str(sum(detail['lot_qty'] for detail in source_jig_details)),
                        'Status : Completed',
                    ])
                    combined_cell = '\n'.join(add_model_lines)
                    source_cells = {
                        detail['source']: combined_cell
                        for detail in source_jig_details
                    }
                unload._report_wiping_sources = wiping_sources
                for source in quantities:
                    if source in lot_ids and source not in unload_by_lot:
                        unload_by_lot[source] = unload
                    if source in unload_by_lot:
                        target = unload_by_lot[source]
                        existing = getattr(target, '_report_combined_unloading_cells', {})
                        target._report_combined_unloading_cells = {
                            **(existing if isinstance(existing, dict) else {}),
                            source: source_cells.get(source, combined_cell),
                        }
                # Submit All writes an auxiliary unloading row for each added
                # source lot.  That row records its Jig Unloading history,
                # but it does not create a separate Nickel Wiping lot: the
                # primary combined transfer is the only downstream lot.
                primary_source = str(source_metadata.get('primary_lot_id') or '')
                for source in quantities:
                    if (source != primary_source and source in unload_by_lot
                            and unload_by_lot[source] is not unload):
                        unload_by_lot[source]._report_combined_secondary = True
                        unload_by_lot[source]._report_combined_transfer = (unload, primary_source)
                break

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
        if str(stock.lot_id) in balance_child_ids:
            continue
        # Jig Loading remainder rows inherit the primary jig batch in their
        # stock record. Their persisted tray source is the truthful model for
        # filtering and report display.
        batch = source_batch_objects.get(split_batches.get(stock.lot_id), stock.batch_id)
        is_jig_split = stock.lot_id in split_quantities or stock.lot_id.startswith('EX-')
        stk_no = (batch.plating_stk_no or '').strip()
        if not stk_no:
            continue

        jig_record = jig_by_lot.get(stock.lot_id)
        unload_record = unload_by_lot.get(stock.lot_id)
        stocks_for_batch = stocks_by_batch.get(stock.batch_id_id) or [stock]
        loading_allocations = [
            allocation for allocation in
            (getattr(jig_record, 'multi_model_allocation', None) or [])
            if isinstance(allocation, dict) and allocation.get('lot_id')
        ]
        loading_primary = next(
            (allocation for allocation in loading_allocations
             if allocation.get('role') == 'primary'),
            loading_allocations[0] if loading_allocations else None,
        )
        # A lot added in Jig Loading remains a secondary source model. Its
        # Nickel Wiping history belongs on the primary model's report row.
        is_jig_loading_added_model = bool(
            loading_primary
            and len(loading_allocations) > 1
            and str(stock.lot_id) != str(loading_primary.get('lot_id') or '')
        )
        # A direct Jig Loading lot is the primary model even when it has no
        # multi-model allocation saved. Secondary summaries belong only on
        # rows that do not have such a primary loading record.
        has_jig_loading_primary_model = bool(
            jig_record and (
                not loading_allocations
                or (loading_primary and
                    str(stock.lot_id) == str(loading_primary.get('lot_id') or ''))
            )
        )

        dp_cell, dp_status = _day_planning_cell(batch, records.dp_batches.get(batch.pk))

        early_cells, early_statuses, running_out = _early_module_cells(
            stocks_for_batch, records=records
        )
        early_activity = running_out
        jig_cell, ip_cell, jig_status, ip_status, running_out = _jig_loading_cells(
            jig_record, records=records, lot_id=stock.lot_id,
            plating_stock_no=stk_no,
        )
        balance_blocks = []
        balance_ip_blocks = []
        balance_jig_records = []
        for balance in balance_descendants(stock.lot_id):
            balance_jig = jig_by_lot.get(balance.new_lot_id)
            if balance_jig:
                balance_jig_records.append((balance, balance_jig))
                balance_cell, balance_ip_cell, _, _, _ = _jig_loading_cells(
                    balance_jig, records=records, lot_id=balance.new_lot_id,
                    plating_stock_no=stk_no,
                )
                if 'Status : Not Reached' not in balance_ip_cell:
                    balance_ip_blocks.append(balance_ip_cell)
            else:
                balance_entry = records.entry(STAGE_JIG_LOADING, balance.new_lot_id)
                balance_cell = '\n'.join([
                    f'IN : {_fmt((balance_entry or {}).get("in_time")) or "--"}',
                    'OUT: --',
                    f'Excess Lot Qty : {balance.lot_qty}',
                    'Status : Yet to be Loaded',
                ])
            balance_blocks.append(balance_cell)
        if balance_blocks:
            jig_cell = '\n\n'.join([jig_cell, *balance_blocks])
        if balance_ip_blocks:
            # The original lot retains its earlier IP record and receives a
            # second 98 record when its balance joins another primary model.
            ip_cell = '\n\n'.join([ip_cell, *balance_ip_blocks])
        jig_activity = running_out
        late_cells, late_statuses, running_out = _late_module_cells(
            unload_record, zone_map, records=records, lot_id=stock.lot_id,
            plating_color_id=stock.plating_color_id, jig_record=jig_record
        )
        for balance, balance_jig in balance_jig_records:
            balance_late_cells, balance_late_statuses, balance_activity = _late_module_cells(
                unload_by_lot.get(balance.new_lot_id), zone_map, records=records,
                lot_id=balance.new_lot_id, plating_color_id=stock.plating_color_id,
                jig_record=balance_jig,
            )
            # A remaining balance lot that becomes a secondary model keeps
            # its Jig Unloading history only. Its Nickel Wiping flow belongs
            # to the new primary model and must not be shown here. A separate
            # small lot added as a model renders its own short summary below.
            for name in ('Jig Unloading Z1', 'Jig Unloading Z2'):
                balance_unloading = balance_late_cells[name]
                if 'Status : Not Reached' in balance_unloading:
                    continue
                if 'Status : Not Reached' in late_cells[name]:
                    late_cells[name] = balance_unloading
                else:
                    late_cells[name] += '\n\n' + balance_unloading
                if balance_late_statuses[name] == 'In Progress':
                    late_statuses[name] = 'In Progress'
            balance_allocations = [
                allocation for allocation in
                (getattr(balance_jig, 'multi_model_allocation', None) or [])
                if isinstance(allocation, dict) and allocation.get('lot_id')
            ]
            balance_primary = next(
                (allocation for allocation in balance_allocations
                 if allocation.get('role') == 'primary'),
                balance_allocations[0] if balance_allocations else None,
            )
            balance_secondary = next(
                (allocation for allocation in balance_allocations
                 if str(allocation.get('lot_id') or '') == str(balance.new_lot_id)),
                None,
            )
            if (
                not has_jig_loading_primary_model
                and balance_primary
                and balance_secondary
                and str(balance_primary.get('lot_id') or '') != str(balance.new_lot_id)
            ):
                secondary_qty = balance_secondary.get(
                    'allocated_qty', balance_secondary.get(
                        'requested_qty', balance_secondary.get('model_lot_qty', balance.lot_qty)
                    )
                )
                primary_qty = balance_primary.get(
                    'allocated_qty', balance_primary.get(
                        'requested_qty', balance_primary.get('model_lot_qty', '--')
                    )
                )
                primary_model = (
                    balance_primary.get('model')
                    or balance_primary.get('model_no')
                    or stk_no
                )
                balance_zone = zone_map.get(
                    (unload_by_lot.get(balance.new_lot_id).plating_color_id
                     if unload_by_lot.get(balance.new_lot_id) else stock.plating_color_id)
                )
                if balance_zone:
                    name = f'Nickel Wiping {balance_zone.upper()}'
                    balance_wiping = '\n'.join([
                        f'PLATING STK NO : {stk_no}',
                        f'Lot Qty : {secondary_qty}',
                        'Note : Added with Primary Model : '
                        f'{primary_model}; Lot Qty : {primary_qty}',
                    ])
                    if 'Status : Not Reached' in late_cells[name]:
                        late_cells[name] = balance_wiping
                    else:
                        late_cells[name] += '\n\n' + balance_wiping
            running_out = _latest_time(running_out, balance_activity)
        combined_transfer = getattr(unload_record, '_report_combined_transfer', None)
        if combined_transfer and not is_jig_loading_added_model:
            primary_unload, primary_source = combined_transfer
            shared_cells, shared_statuses, shared_activity = _late_module_cells(
                primary_unload, zone_map, records=records, lot_id=primary_source,
                plating_color_id=stock.plating_color_id,
                jig_record=jig_by_lot.get(primary_source),
            )
            # Jig Unloading remains source-specific: an added 4-qty lot must
            # not be replaced with its 140-qty primary lot.  Only downstream
            # Nickel stages describe the shared combined transfer.
            for name in ('Nickel Wiping Z1', 'Nickel Wiping Z2',
                         'Nickel Audit Z1', 'Nickel Audit Z2'):
                late_cells[name] = shared_cells[name]
                late_statuses[name] = shared_statuses[name]
            running_out = _latest_time(running_out, shared_activity)
        elif is_jig_loading_added_model:
            # A secondary Jig Loading model records only its own handoff to
            # Nickel Wiping. The physical wiping transaction belongs to its
            # primary model, so do not copy the combined quantity, Wiping
            # history, or any later Nickel Audit transaction to this row.
            secondary_loading = next(
                (allocation for allocation in loading_allocations
                 if str(allocation.get('lot_id') or '') == str(stock.lot_id)),
                {},
            )
            secondary_qty = secondary_loading.get(
                'allocated_qty', secondary_loading.get(
                    'requested_qty', secondary_loading.get('model_lot_qty', stock.total_stock)
                )
            )
            primary_qty = loading_primary.get(
                'allocated_qty', loading_primary.get(
                    'requested_qty', loading_primary.get('model_lot_qty', '--')
                )
            )
            primary_model = (
                loading_primary.get('model')
                or loading_primary.get('model_no')
                or stk_no
            )
            secondary_wiping = '\n'.join([
                f'PLATING STK NO : {stk_no}',
                f'Lot Qty : {secondary_qty}',
                'Note : Added with Primary Model : '
                f'{primary_model}; Lot Qty : {primary_qty}',
            ])
            secondary_zone = zone_map.get(
                unload_record.plating_color_id if unload_record else stock.plating_color_id
            )
            if secondary_zone:
                name = f'Nickel Wiping {secondary_zone.upper()}'
                late_cells[name] = secondary_wiping
                # Keep the green completed state without adding a Status line
                # to the displayed secondary-model summary.
                late_statuses[name] = 'Completed'
            for name in ('Nickel Audit Z1', 'Nickel Audit Z2'):
                late_cells[name] = _module_cell('Not Reached')
                late_statuses[name] = 'Not Reached'

        modules = {STAGE_DAY_PLANNING: dp_cell}
        modules.update(early_cells)
        modules[STAGE_JIG_LOADING] = jig_cell
        modules[STAGE_IP_INSPECTION] = ip_cell
        modules.update(late_cells)

        # Keep each saved remark beside the transaction it belongs to.
        modules[STAGE_DAY_PLANNING] = _append_module_remarks(
            modules[STAGE_DAY_PLANNING], batch.dp_pick_remarks)
        modules[STAGE_INPUT_SCREENING] = _append_module_remarks(
            modules[STAGE_INPUT_SCREENING], stock.IP_pick_remarks)
        modules[STAGE_BRASS_QC] = _append_module_remarks(
            modules[STAGE_BRASS_QC], stock.Bq_pick_remarks)
        modules[STAGE_IQF] = _append_module_remarks(
            modules[STAGE_IQF], stock.IQF_pick_remarks)
        modules[STAGE_BRASS_AUDIT] = _append_module_remarks(
            modules[STAGE_BRASS_AUDIT], stock.BA_pick_remarks,
            *(records.ba_rejection_remarks.get(candidate.lot_id)
              for candidate in stocks_for_batch))
        modules[STAGE_JIG_LOADING] = _append_module_remarks(
            modules[STAGE_JIG_LOADING],
            getattr(jig_record, 'pick_remarks', None) if jig_record else None,
            getattr(jig_record, 'unloading_remarks', None) if jig_record else None,
            getattr(jig_record, 'remarks', None) if jig_record else None)
        report_zone = zone_map.get(
            unload_record.plating_color_id if unload_record else stock.plating_color_id)
        if unload_record and report_zone:
            modules[f'Nickel Wiping {report_zone.upper()}'] = _append_module_remarks(
                modules[f'Nickel Wiping {report_zone.upper()}'],
                unload_record.nq_pick_remarks)
            modules[f'Nickel Audit {report_zone.upper()}'] = _append_module_remarks(
                modules[f'Nickel Audit {report_zone.upper()}'],
                unload_record.na_pick_remarks)

        statuses = {STAGE_DAY_PLANNING: dp_status}
        statuses.update(early_statuses)
        statuses[STAGE_JIG_LOADING] = jig_status
        statuses[STAGE_IP_INSPECTION] = ip_status
        statuses.update(late_statuses)
        if is_jig_split:
            # This lot starts at Jig Loading; earlier history belongs to its
            # parent batch and must not be repeated as this lot's processing.
            for name in MODULE_COLUMNS[:5]:
                modules[name] = _module_cell('Not Applicable')
                statuses[name] = 'Not Applicable'
        _apply_route_applicability(modules, statuses, zone_map.get(
            unload_record.plating_color_id if unload_record else stock.plating_color_id))
        # Keep the stock identifier visible at the top of every process
        # module.  Several specialized multi-model cells already include it,
        # so do not add a duplicate line.
        for name, cell in modules.items():
            if 'PLATING STK NO :' not in (cell or '').upper():
                modules[name] = f'PLATING STK NO : {stk_no or "--"}\n{cell}'
        module_states = {name: _stage_state(status) for name, status in statuses.items()}
        module_details = {name: _parse_cell_lines(text) for name, text in modules.items()}

        activity = _latest_time(running_out, early_activity, jig_activity,
                                batch.date_time, stock.created_at)

        # Date-range filter on latest stage activity
        if from_dt and (not activity or activity < from_dt):
            continue
        if to_dt_val and activity and activity > to_dt_val:
            continue

        # Module remarks are displayed in their own module cells. Keep this
        # column blank while retaining it in the report layout.
        remarks = ''

        row = {
            'plating_stk_no': stk_no,
            'lot_qty': int(split_quantities.get(stock.lot_id, stock.total_stock) or 0)
                       if is_jig_split else int(batch.total_batch_quantity or 0),
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

        # Render a known remainder as another block in its source lot's row.
        # Unresolved mixed-source leftovers must not inherit the primary model.
        lot_key = (('batch', split_batches[stock.lot_id])
                   if stock.lot_id in split_batches else
                   ('jig_split', stock.lot_id) if is_jig_split else ('batch', batch.pk))
        existing = rows_by_lot.get(lot_key)
        if existing is None:
            rows_by_lot[lot_key] = row
        else:
            if not is_jig_split:
                existing['plating_stk_no'] = row['plating_stk_no']
                existing['lot_qty'] = row['lot_qty']
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
            if row['remarks']:
                existing['remarks'] = _combined_remarks(
                    ('', existing['remarks']), ('', row['remarks'])
                )

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
