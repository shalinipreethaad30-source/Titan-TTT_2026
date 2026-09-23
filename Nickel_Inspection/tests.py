from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from Jig_Unloading.models import JigUnloadAfterTable
from modelmasterapp.models import TrayId
from Nickel_Inspection.models import (
    Nickel_QC_Draft_Store,
    Nickel_QC_Rejected_TrayScan,
    Nickel_QC_Rejection_Table,
    NickelQC_Submission,
    NickelQcTrayId,
    NickelWiping_FullRejectRecord,
)
from Nickel_Inspection.services import (
    get_current_nickel_wiping_reject_trays,
    get_nickel_wiping_rejection_tray_allocation,
    has_unreleased_nickel_wiping_reject_trays,
    validate_nickel_wiping_rejection_tray_available,
    validate_nickel_wiping_rejection_tray_series,
)
from Nickel_Inspection.views import (
    _nq_normalize_full_reject_event_trays,
    nq_action,
    nq_delink_selected_trays,
)


class NickelWipingFullRejectLifecycleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='full-reject-user')
        self.factory = APIRequestFactory()
        self.lot_id = 'UNLOT-FULL-REJECT-288'
        self.juat = JigUnloadAfterTable.objects.create(
            jig_qr_id='JIG-FULL-REJECT',
            lot_id=self.lot_id,
            total_case_qty=288,
            tray_type='Jumbo',
            tray_capacity=12,
            current_stage='Nickel Wiping',
        )
        self.tray_ids = [f'JL-A{i:05d}' for i in range(201, 225)]
        for index, tray_id in enumerate(self.tray_ids):
            NickelQcTrayId.objects.create(
                lot_id=self.lot_id,
                tray_id=tray_id,
                tray_quantity=12,
                top_tray=index == 0,
                tray_type='Jumbo',
                tray_capacity=12,
            )
            TrayId.objects.create(
                tray_id=tray_id,
                lot_id=self.lot_id,
                tray_quantity=12,
                scanned=True,
            )

    def _submit_full_reject(self):
        request = self.factory.post('/nickle_inspection/api/action/', {
            'action': 'SUBMIT_REJECT',
            'lot_id': self.lot_id,
            'full_lot_rejection': True,
            'rejected_qty': 288,
            'remarks': 'Full lot rejected during Nickel Wiping',
            'reject_trays': [],
            'accept_trays': [],
            'delink_trays': [],
        }, format='json')
        force_authenticate(request, user=self.user)
        return nq_action(request)

    def test_full_reject_snapshot_and_delink_lifecycle(self):
        response = self._submit_full_reject()
        self.assertEqual(200, response.status_code)

        submission = NickelQC_Submission.objects.get(lot_id=self.lot_id)
        full_record = NickelWiping_FullRejectRecord.objects.get(source_lot_id=self.lot_id)
        expected = [{'tray_id': tray_id, 'qty': 12} for tray_id in self.tray_ids]
        self.assertEqual('FULL_REJECT', submission.submission_type)
        self.assertEqual(expected, submission.reject_trays_data)
        self.assertEqual(expected, full_record.reject_trays)
        self.assertEqual(24, len(submission.reject_trays_data))
        self.assertEqual(288, sum(row['qty'] for row in submission.reject_trays_data))
        self.assertEqual(expected, get_current_nickel_wiping_reject_trays(self.lot_id))
        self.assertTrue(has_unreleased_nickel_wiping_reject_trays(self.lot_id))
        self.assertFalse(
            NickelQcTrayId.objects.filter(lot_id=self.lot_id, delink_tray=True).exists()
        )
        self.assertEqual(
            24,
            TrayId.objects.filter(tray_id__in=self.tray_ids, lot_id=self.lot_id).count(),
        )

        request = self.factory.post('/nickle_inspection/nickel_qc_delink_selected_trays/', {
            'stock_lot_ids': [self.lot_id],
        }, format='json')
        force_authenticate(request, user=self.user)
        response = nq_delink_selected_trays(request)
        self.assertEqual(200, response.status_code)
        self.assertEqual(24, response.data['updated'])
        self.assertFalse(has_unreleased_nickel_wiping_reject_trays(self.lot_id))
        self.assertEqual(
            24,
            NickelQcTrayId.objects.filter(
                lot_id=self.lot_id,
                rejected_tray=True,
                delink_tray=True,
                tray_quantity=0,
                delink_tray_qty='12',
            ).count(),
        )

        repeat_request = self.factory.post('/nickle_inspection/nickel_qc_delink_selected_trays/', {
            'stock_lot_ids': [self.lot_id],
        }, format='json')
        force_authenticate(repeat_request, user=self.user)
        repeat_response = nq_delink_selected_trays(repeat_request)
        self.assertEqual(200, repeat_response.status_code)
        self.assertEqual(0, repeat_response.data['updated'])

    def test_full_reject_incomplete_coverage_fails_without_rejection_state(self):
        NickelQcTrayId.objects.filter(
            lot_id=self.lot_id,
            tray_id=self.tray_ids[-1],
        ).update(tray_quantity=0)

        response = self._submit_full_reject()

        self.assertEqual(400, response.status_code)
        self.assertFalse(NickelQC_Submission.objects.filter(lot_id=self.lot_id).exists())
        self.assertFalse(
            NickelWiping_FullRejectRecord.objects.filter(source_lot_id=self.lot_id).exists()
        )
        self.juat.refresh_from_db()
        self.assertFalse(self.juat.nq_qc_rejection)

    def test_full_reject_snapshot_validation_rejects_duplicates_and_zero_qty(self):
        with self.assertRaisesMessage(ValueError, 'Duplicate original tray ID'):
            _nq_normalize_full_reject_event_trays([
                {'tray_id': 'JL-A00201', 'qty': 6},
                {'tray_id': 'jl-a00201', 'qty': 6},
            ], 12, 12)

        with self.assertRaisesMessage(ValueError, 'positive quantity'):
            _nq_normalize_full_reject_event_trays([
                {'tray_id': 'JL-A00201', 'qty': 0},
            ], 12, 12)


class NickelWipingRejectionTrayAvailabilityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='nq-user')
        self.reason = Nickel_QC_Rejection_Table.objects.create(
            rejection_reason='Surface defect',
        )
        TrayId.objects.create(tray_id='NB-A00009')
        TrayId.objects.create(tray_id='NB-A00010')

    def _create_nq_lot(self, lot_id, **kwargs):
        defaults = {
            'jig_qr_id': f'JIG-{lot_id}',
            'lot_id': lot_id,
            'total_case_qty': 10,
            'current_stage': 'Nickel Wiping',
        }
        defaults.update(kwargs)
        return JigUnloadAfterTable.objects.create(**defaults)

    def test_other_active_rejection_scan_blocks_tray(self):
        self._create_nq_lot('LOT-A', nq_qc_rejection=True)
        Nickel_QC_Rejected_TrayScan.objects.create(
            lot_id='LOT-A',
            rejected_tray_id='NB-A00009',
            rejected_tray_quantity='10',
            rejection_reason=self.reason,
            user=self.user,
        )

        available, message = validate_nickel_wiping_rejection_tray_available(
            'NB-A00009',
            current_lot_id='LOT-B',
        )

        self.assertFalse(available)
        self.assertIn('already assigned', message)

    def test_same_lot_rejection_scan_is_allowed(self):
        self._create_nq_lot('LOT-A', nq_qc_rejection=True)
        Nickel_QC_Rejected_TrayScan.objects.create(
            lot_id='LOT-A',
            rejected_tray_id='NB-A00009',
            rejected_tray_quantity='10',
            rejection_reason=self.reason,
            user=self.user,
        )

        available, message = validate_nickel_wiping_rejection_tray_available(
            'NB-A00009',
            current_lot_id='LOT-A',
        )

        self.assertTrue(available)
        self.assertEqual('', message)

    def test_other_active_draft_blocks_tray(self):
        self._create_nq_lot('LOT-A', nq_draft=True)
        Nickel_QC_Draft_Store.objects.create(
            lot_id='LOT-A',
            batch_id='BATCH-A',
            user=self.user,
            draft_type='batch_rejection',
            draft_data={
                'reject_trays': [{'tray_id': 'NB-A00009', 'qty': 10}],
            },
        )

        available, message = validate_nickel_wiping_rejection_tray_available(
            'NB-A00009',
            current_lot_id='LOT-B',
        )

        self.assertFalse(available)
        self.assertIn('already reserved', message)

    def test_same_lot_draft_is_allowed(self):
        self._create_nq_lot('LOT-A', nq_draft=True)
        Nickel_QC_Draft_Store.objects.create(
            lot_id='LOT-A',
            batch_id='BATCH-A',
            user=self.user,
            draft_type='batch_rejection',
            draft_data={
                'reject_trays': [{'tray_id': 'NB-A00009', 'qty': 10}],
            },
        )

        available, message = validate_nickel_wiping_rejection_tray_available(
            'NB-A00009',
            current_lot_id='LOT-A',
        )

        self.assertTrue(available)
        self.assertEqual('', message)

    def test_historical_released_lot_does_not_block_reuse(self):
        self._create_nq_lot(
            'LOT-A',
            current_stage='Nickel Audit',
            nq_qc_rejection=False,
            nq_qc_few_cases_accptance=False,
            nq_draft=False,
            nq_onhold_picking=False,
        )
        Nickel_QC_Rejected_TrayScan.objects.create(
            lot_id='LOT-A',
            rejected_tray_id='NB-A00009',
            rejected_tray_quantity='10',
            rejection_reason=self.reason,
            user=self.user,
        )

        available, message = validate_nickel_wiping_rejection_tray_available(
            'NB-A00009',
            current_lot_id='LOT-B',
        )

        self.assertTrue(available)
        self.assertEqual('', message)

    def test_different_available_tray_is_allowed(self):
        self._create_nq_lot('LOT-A', nq_qc_rejection=True)
        Nickel_QC_Rejected_TrayScan.objects.create(
            lot_id='LOT-A',
            rejected_tray_id='NB-A00009',
            rejected_tray_quantity='10',
            rejection_reason=self.reason,
            user=self.user,
        )

        available, message = validate_nickel_wiping_rejection_tray_available(
            'NB-A00010',
            current_lot_id='LOT-B',
        )

        self.assertTrue(available)
        self.assertEqual('', message)


class NickelWipingRejectionTraySeriesTests(TestCase):
    def test_nb_allocated_model_allows_nb_rejection_tray(self):
        valid, message, allowed_prefix = validate_nickel_wiping_rejection_tray_series(
            'NB-A00001',
            'Normal',
        )

        self.assertTrue(valid)
        self.assertEqual('', message)
        self.assertEqual('NB', allowed_prefix)

    def test_nb_allocated_model_blocks_jb_rejection_tray(self):
        valid, message, allowed_prefix = validate_nickel_wiping_rejection_tray_series(
            'JB-A00001',
            'Normal',
        )

        self.assertFalse(valid)
        self.assertIn('NB trays', message)
        self.assertEqual('NB', allowed_prefix)

    def test_jb_allocated_model_allows_jb_rejection_tray(self):
        valid, message, allowed_prefix = validate_nickel_wiping_rejection_tray_series(
            'JB-A00001',
            'Jumbo',
        )

        self.assertTrue(valid)
        self.assertEqual('', message)
        self.assertEqual('JB', allowed_prefix)

    def test_jb_allocated_model_blocks_nb_rejection_tray(self):
        valid, message, allowed_prefix = validate_nickel_wiping_rejection_tray_series(
            'NB-A00001',
            'Jumbo',
        )

        self.assertFalse(valid)
        self.assertIn('JB trays', message)
        self.assertEqual('JB', allowed_prefix)

    def test_nb_allocated_model_blocks_nr_nd_and_jr_rejection_trays(self):
        for tray_id in ('NR-A00001', 'ND-A00001', 'JR-A00001'):
            valid, message, allowed_prefix = validate_nickel_wiping_rejection_tray_series(
                tray_id,
                'Normal',
            )

            self.assertFalse(valid)
            self.assertIn('NB trays', message)
            self.assertEqual('NB', allowed_prefix)

    def test_jb_allocated_model_blocks_nr_nd_and_jr_rejection_trays(self):
        for tray_id in ('NR-A00001', 'ND-A00001', 'JR-A00001'):
            valid, message, allowed_prefix = validate_nickel_wiping_rejection_tray_series(
                tray_id,
                'Jumbo',
            )

            self.assertFalse(valid)
            self.assertIn('JB trays', message)
            self.assertEqual('JB', allowed_prefix)

    def test_2648_normal_model_resolves_to_nb_through_master_tray_type(self):
        allowed_prefix, reject_capacity = get_nickel_wiping_rejection_tray_allocation('Normal')

        self.assertEqual('NB', allowed_prefix)
        self.assertEqual(16, reject_capacity)

        valid, _, _ = validate_nickel_wiping_rejection_tray_series(
            'NB-A00001',
            'Normal',
        )
        self.assertTrue(valid)

        valid, message, _ = validate_nickel_wiping_rejection_tray_series(
            'JB-A00001',
            'Normal',
        )
        self.assertFalse(valid)
        self.assertIn('NB trays', message)
