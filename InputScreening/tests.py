from django.contrib.auth.models import User
from django.test import TestCase

from modelmasterapp.models import (
    ModelMaster,
    ModelMasterCreation,
    PolishFinishType,
    TotalStockModel,
    TrayType,
    Version,
)

from .models import InputScreening_Submitted
from .selectors import pick_table_queryset
from .services_reject import _mark_lot_submitted_flags


class InputScreeningPickTableCompletionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="is_user",
            password="testpass123",
        )
        self.tray_type = TrayType.objects.create(
            tray_type="Jumbo",
            tray_capacity=12,
        )
        self.version = Version.objects.create(
            version_name="V1",
            version_internal="V1",
        )
        self.polish_finish = PolishFinishType.objects.create(
            polish_finish="PF1",
            polish_internal="PF1",
        )
        self.model_master = ModelMaster.objects.create(
            model_no="1805",
            ep_bath_type="EP",
            tray_type=self.tray_type,
            tray_capacity=12,
            version="V1",
            plating_stk_no="1805SAA02",
        )

    def _create_batch(self, batch_id="BATCH-IS-001", qty=60):
        return ModelMasterCreation.objects.create(
            batch_id=batch_id,
            model_stock_no=self.model_master,
            polish_finish="PF1",
            ep_bath_type="EP",
            tray_type="Jumbo",
            tray_capacity=12,
            version=self.version,
            total_batch_quantity=qty,
            no_of_trays=5,
            plating_stk_no="1805SAA02",
            Moved_to_D_Picker=True,
        )

    def _create_stock(self, batch, lot_id, qty=60, **flags):
        defaults = {
            "batch_id": batch,
            "model_stock_no": self.model_master,
            "version": self.version,
            "total_stock": qty,
            "polish_finish": self.polish_finish,
            "lot_id": lot_id,
        }
        defaults.update(flags)
        return TotalStockModel.objects.create(**defaults)

    def test_partial_submit_marks_tray_scan_completed_without_full_accept_flag(self):
        batch = self._create_batch()
        self._create_stock(batch, "LOT-PARTIAL", qty=60)

        _mark_lot_submitted_flags("LOT-PARTIAL", accepted_qty=50)

        stock = TotalStockModel.objects.get(lot_id="LOT-PARTIAL")
        self.assertTrue(stock.few_cases_accepted_Ip_stock)
        self.assertTrue(stock.accepted_tray_scan_status)
        self.assertFalse(stock.accepted_Ip_stock)
        self.assertEqual(stock.total_IP_accpeted_quantity, 50)
        self.assertEqual(stock.last_process_module, "Input Screening")
        self.assertEqual(stock.next_process_module, "Brass QC")

    def test_finalized_submission_is_excluded_by_batch_when_latest_stock_is_child(self):
        batch = self._create_batch()
        self._create_stock(
            batch,
            "LOT-PARENT",
            qty=60,
            few_cases_accepted_Ip_stock=True,
            total_IP_accpeted_quantity=50,
            last_process_module="Input Screening",
            next_process_module="Brass QC",
        )
        InputScreening_Submitted.objects.create(
            lot_id="LOT-PARENT",
            batch_id=batch.batch_id,
            module_name="Input Screening",
            plating_stock_no="1805SAA02",
            model_no="1805",
            tray_type="Jumbo",
            tray_capacity=12,
            original_lot_qty=60,
            active_trays_count=6,
            is_partial_accept=True,
            is_partial_reject=True,
            is_active=True,
            is_submitted=True,
            created_by=self.user,
        )
        self._create_stock(
            batch,
            "LOT-DOWNSTREAM",
            qty=50,
            last_process_module="Brass Audit",
            next_process_module="Jig Loading",
        )

        rows = list(pick_table_queryset().values_list("batch_id", flat=True))

        self.assertNotIn(batch.batch_id, rows)

    def test_draft_submission_does_not_hide_unprocessed_pick_row(self):
        batch = self._create_batch(batch_id="BATCH-IS-DRAFT")
        self._create_stock(batch, "LOT-DRAFT", qty=60)
        InputScreening_Submitted.objects.create(
            lot_id="LOT-DRAFT",
            batch_id=batch.batch_id,
            module_name="Input Screening",
            plating_stock_no="1805SAA02",
            model_no="1805",
            tray_type="Jumbo",
            tray_capacity=12,
            original_lot_qty=60,
            active_trays_count=6,
            Draft_Saved=True,
            is_active=True,
            is_submitted=False,
            created_by=self.user,
        )

        rows = list(pick_table_queryset().values_list("batch_id", flat=True))

        self.assertIn(batch.batch_id, rows)
