"""Regression tests for exact active IQF F2 tray lookup."""
import json
from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.utils import timezone

from adminportal.global_scan import GlobalTraySearchView
from Brass_QC.models import BrassTrayId
from modelmasterapp.models import ModelMaster, ModelMasterCreation, TotalStockModel, Version
from .models import IQFTrayId
from .services.selectors import is_current_iqf_scan_tray


class IQFF2ScanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        model = ModelMaster.objects.create(model_no='F2-TEST')
        version = Version.objects.create(version_name='F2-TEST')
        cls.batch = ModelMasterCreation.objects.create(
            batch_id='F2-SHARED-BATCH', model_stock_no=model,
            version=version, total_batch_quantity=21,
        )
        TotalStockModel.objects.bulk_create([
            TotalStockModel(
                lot_id=lot, batch_id=cls.batch, model_stock_no=model,
                version=version, total_stock=qty, next_process_module='IQF',
                last_process_module='Brass QC', send_brass_audit_to_iqf=True,
            ) for lot, qty in [('LOT-31', 16), ('LOT-32', 5)]
        ])
        for tray, lot, qty in [('NB-A00031', 'LOT-31', 16), ('NB-A00032', 'LOT-32', 5)]:
            IQFTrayId.objects.create(tray_id=tray, lot_id=lot, tray_quantity=qty, batch_id=cls.batch)
            BrassTrayId.objects.create(tray_id=tray, lot_id=lot, tray_quantity=qty, batch_id=cls.batch)

    def scan(self, tray, path='/iqf/iqf_picktable/'):
        request = RequestFactory().post(
            '/adminportal/global_tray_search/',
            data=json.dumps({'tray_id': tray, 'current_path': path}),
            content_type='application/json',
        )
        request.user = User(username='scan-user', is_superuser=True)
        view = GlobalTraySearchView()
        with ExitStack() as stack:
            stack.enter_context(patch('adminportal.global_scan.find_active_excess_lot_by_tray', return_value=None))
            # Tray queries and IQF checks are real. Other modules are not
            # eligible in this fixture and have separate regression tests.
            for name in ('inprocess_inspection', 'jig_unloading', 'brass_audit',
                         'brass_qc', 'input_screening', 'day_planning', 'jig_loading'):
                stack.enter_context(patch.object(view, '_check_lot_in_' + name, return_value=None))
            stack.enter_context(patch.object(view, '_check_batch_in_day_planning', return_value=None))
            stack.enter_context(patch.object(view, '_user_can_access_result', return_value=True))
            return json.loads(view.post(request).content)

    def test_each_tray_returns_own_lot_even_with_shared_batch(self):
        for tray, lot in [('NB-A00031', 'LOT-31'), ('NB-A00032', 'LOT-32'), (' nb-a00031\r\n', 'LOT-31')]:
            with self.subTest(tray=tray):
                result = self.scan(tray)
                self.assertTrue(result['found'])
                self.assertEqual(result['lot_id'], lot)
                self.assertEqual(result['module'], 'IQF')

    def test_partial_and_padded_variant_are_not_exact_iqf_trays(self):
        for tray in ('NB-A0003', 'NB-A0031', 'NB-A000310', 'NB-A99999'):
            with self.subTest(tray=tray):
                self.assertFalse(self.scan(tray)['found'])

    def test_stale_upstream_lot_cannot_match_another_trays_iqf_record(self):
        BrassTrayId.objects.filter(tray_id='NB-A00031').update(lot_id='LOT-32')
        self.assertEqual(self.scan('NB-A00031')['lot_id'], 'LOT-31')
        IQFTrayId.objects.filter(tray_id='NB-A00031').delete()
        result = self.scan('NB-A00031')
        self.assertFalse(result['found'])
        self.assertEqual(result['message'], 'Tray ID does not exist')
        self.assertNotIn('lot_id', result)

    def test_latest_release_does_not_resurrect_older_active_record(self):
        IQFTrayId.objects.create(
            tray_id='NB-A00031', lot_id='LOT-32', tray_quantity=16,
            delink_tray=True, date=timezone.now() + timedelta(seconds=1),
        )
        self.assertFalse(self.scan('NB-A00031')['found'])

    def test_latest_assignment_wins_and_ties_use_pk(self):
        original = IQFTrayId.objects.get(tray_id='NB-A00031')
        IQFTrayId.objects.create(
            tray_id='nb-a00031', lot_id='LOT-32', tray_quantity=16,
            batch_id=self.batch, date=original.date,
        )
        self.assertEqual(self.scan('NB-A00031')['lot_id'], 'LOT-32')

    def test_inactive_tray_states_do_not_resolve(self):
        for changes in ({'delink_tray': True}, {'rejected_tray': True},
                        {'tray_quantity': 0}, {'lot_id': None}):
            with self.subTest(changes=changes):
                IQFTrayId.objects.filter(tray_id='NB-A00031').update(**changes)
                self.assertFalse(self.scan('NB-A00031')['found'])
                IQFTrayId.objects.filter(tray_id='NB-A00031').update(
                    delink_tray=False, rejected_tray=False, tray_quantity=16, lot_id='LOT-31',
                )

    def test_completed_removed_and_split_lots_are_not_current(self):
        for flag in ('iqf_acceptance', 'iqf_rejection', 'is_split', 'remove_lot'):
            with self.subTest(flag=flag):
                TotalStockModel.objects.filter(lot_id='LOT-31').update(**{flag: True})
                self.assertFalse(is_current_iqf_scan_tray('NB-A00031', 'LOT-31'))
                TotalStockModel.objects.filter(lot_id='LOT-31').update(**{flag: False})

    def test_other_modules_keep_existing_not_found_message(self):
        self.assertEqual(self.scan('NB-A99999', '/brass_qc/brass_picktable/')['message'], 'Not Exists')
