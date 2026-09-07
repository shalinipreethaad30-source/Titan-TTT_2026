from django.db import models
from modelmasterapp.models import *
from BrassAudit.models import AQLSamplingPlan

# Create your models here.

class Nickel_AuditTrayId(models.Model):
    """
    Nickel_AuditTrayId Model
    Represents a tray identifier in the Titan Track and Traceability system.
    """
    lot_id = models.CharField(max_length=50, null=True, blank=True, help_text="Lot ID")
    tray_id = models.CharField(max_length=100,  help_text="Tray ID")
    tray_quantity = models.IntegerField(null=True, blank=True, help_text="Quantity in the tray")
    batch_id = models.ForeignKey(ModelMasterCreation, on_delete=models.CASCADE, blank=True, null=True)
    date = models.DateTimeField(default=timezone.now)
    user = models.ForeignKey(User, on_delete=models.CASCADE, blank=True, null=True)
    top_tray = models.BooleanField(default=False)


    delink_tray = models.BooleanField(default=False, help_text="Is tray delinked")
    delink_tray_qty = models.CharField(max_length=50, null=True, blank=True, help_text="Delinked quantity")

    IP_tray_verified= models.BooleanField(default=False, help_text="Is tray verified in IP")
    
    rejected_tray= models.BooleanField(default=False, help_text="Is tray rejected")

    new_tray=models.BooleanField(default=True, help_text="Is tray new")
    
    # Tray configuration fields (filled by admin)
    tray_type = models.CharField(max_length=50, null=True, blank=True, help_text="Type of tray (Jumbo, Normal, etc.) - filled by admin")
    tray_capacity = models.IntegerField(null=True, blank=True, help_text="Capacity of this specific tray - filled by admin")

    def __str__(self):
        return f"{self.tray_id} - {self.lot_id} - {self.tray_quantity}"

    @property
    def is_available_for_scanning(self):
        """
        Check if tray is available for scanning
        Available if: not scanned OR delinked (can be reused)
        """
        return not self.scanned or self.delink_tray

    @property
    def status_display(self):
        """Get human-readable status"""
        if self.delink_tray:
            return "Delinked (Reusable)"
        elif self.scanned:
            return "Already Scanned"
        elif self.batch_id:
            return "In Use"
        else:
            return "Available"

    class Meta:
        verbose_name = "Nickel_Audit Tray ID"
        verbose_name_plural = "Nickel_Audit Tray IDs"
   
   
        
# Add these new models to your models.py (if not already exist)
class Nickel_Audit_Draft_Store(models.Model):
    lot_id = models.CharField(max_length=255)
    batch_id = models.CharField(max_length=255)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    draft_type = models.CharField(max_length=50)  # 'batch_rejection' or 'tray_rejection'
    draft_data = models.JSONField()  # Store all draft data as JSON
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['lot_id', 'draft_type']
        

class Nickel_Audit_Rejection_Table(models.Model):
    rejection_reason_id = models.CharField(max_length=10, null=True, blank=True, editable=False)
    rejection_reason = models.TextField(help_text="Reason for rejection")

    def save(self, *args, **kwargs):
        if not self.rejection_reason_id:
            last = Nickel_Audit_Rejection_Table.objects.order_by('-rejection_reason_id').first()
            if last and last.rejection_reason_id.startswith('R'):
                last_num = int(last.rejection_reason_id[1:])
                new_num = last_num + 1
            else:
                new_num = 1
            self.rejection_reason_id = f"R{new_num:02d}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.rejection_reason} "


class Nickel_Audit_Rejection_ReasonStore(models.Model):
    rejection_reason = models.ManyToManyField(Nickel_Audit_Rejection_Table, blank=True)
    lot_id = models.CharField(max_length=50, null=True, blank=True, help_text="Lot ID")
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    total_rejection_quantity = models.PositiveIntegerField(help_text="Total Rejection Quantity")
    batch_rejection=models.BooleanField(default=False)
    created_at = models.DateTimeField(default=now, help_text="Timestamp of the record")
    lot_rejected_comment = models.CharField(max_length=255,null=True,blank=True)

    def __str__(self):
        return f"{self.user} - {self.total_rejection_quantity} - {self.lot_id}"


class Nickel_Audit_TopTray_Draft_Store(models.Model):
    lot_id = models.CharField(max_length=255)
    batch_id = models.CharField(max_length=255)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    tray_id = models.CharField(max_length=255, blank=True, null=True)
    tray_qty = models.IntegerField(blank=True, null=True)
    
    # ✅ UPDATED: Store delink trays with position information
    delink_trays_data = JSONField(blank=True, default=dict)  # Store structured data: {"positions": [{"position": 0, "tray_id": "JB-A00130", "original_capacity": 12}]}
    
    # ✅ KEEP for backward compatibility (but we'll use delink_trays_data going forward)
    delink_tray_ids = ArrayField(models.CharField(max_length=255), blank=True, default=list)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['lot_id']  # Only one draft per lot
        
    def __str__(self):
        return f"Top Tray Draft - {self.lot_id} - {self.tray_id}"


class Nickel_Audit_Rejected_TrayScan(models.Model):
    lot_id = models.CharField(max_length=50, null=True, blank=True, help_text="Lot ID")
    rejected_tray_quantity = models.CharField(help_text="Rejected Tray Quantity")
    rejected_tray_id= models.CharField(max_length=100, null=True, blank=True, help_text="Rejected Tray ID")
    rejection_reason = models.ForeignKey(Nickel_Audit_Rejection_Table, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    
    def __str__(self):
        return f"{self.rejection_reason} - {self.rejected_tray_quantity} - {self.lot_id}"

class Nickel_Audit_Accepted_TrayScan(models.Model):
    lot_id = models.CharField(max_length=50, null=True, blank=True, help_text="Lot ID")
    accepted_tray_quantity = models.CharField(help_text="Accepted Tray Quantity")
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    
    def __str__(self):
        return f"{self.accepted_tray_quantity} - {self.lot_id}"
    

    
class Nickel_Audit_Accepted_TrayID_Store(models.Model):
    lot_id = models.CharField(max_length=50, null=True, blank=True, help_text="Lot ID")
    tray_id = models.CharField(max_length=100, unique=True)
    tray_qty = models.IntegerField(null=True, blank=True, help_text="Quantity in the tray")
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    is_draft = models.BooleanField(default=False, help_text="Draft Save")
    is_save= models.BooleanField(default=False, help_text="Save")
    
    def __str__(self):
        return f"{self.tray_id} - {self.lot_id}"


class NickelAudit_Submission(models.Model):
    """
    Full submission record for Nickel Audit.
    Created for every accept / reject action (full or partial).
    Mirrors NickelQC_Submission pattern.
    """
    SUBMISSION_TYPES = [
        ('FULL_ACCEPT', 'Full Accept'),
        ('PARTIAL', 'Partial Accept / Partial Reject'),
        ('FULL_REJECT', 'Full Reject'),
    ]
    lot_id = models.CharField(max_length=100, db_index=True,
                              help_text="Parent lot ID from JigUnloadAfterTable")
    submission_type = models.CharField(max_length=20, choices=SUBMISSION_TYPES)
    total_lot_qty = models.IntegerField(default=0)
    accepted_qty = models.IntegerField(default=0)
    rejected_qty = models.IntegerField(default=0)
    accept_trays_data = models.JSONField(default=list, blank=True,
                                        help_text="Accept tray snapshot: [{tray_id, qty, is_top}]")
    reject_trays_data = models.JSONField(default=list, blank=True,
                                        help_text="Reject tray snapshot: [{tray_id, qty}]")
    delink_trays_data = models.JSONField(default=list, blank=True,
                                        help_text="Delink tray snapshot: [{tray_id, qty}]")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Nickel Audit Submission"
        verbose_name_plural = "Nickel Audit Submissions"
        indexes = [models.Index(fields=['lot_id'])]

    def __str__(self):
        return f"NA-Submission: {self.lot_id} ({self.submission_type})"


class NickelAudit_PartialAcceptLot(models.Model):
    """
    Partial accept child lot created when partial acceptance occurs in Nickel Audit.
    new_lot_id = the auto-generated lot_id of the child JigUnloadAfterTable row.
    """
    new_lot_id = models.CharField(max_length=100, db_index=True,
                                  help_text="Child lot ID (JigUnloadAfterTable.lot_id) for accepted portion")
    parent_lot_id = models.CharField(max_length=100, db_index=True,
                                     help_text="Original parent lot ID")
    parent_submission = models.ForeignKey(
        'NickelAudit_Submission',
        on_delete=models.CASCADE,
        related_name='partial_accept_lots',
        null=True, blank=True,
    )
    accepted_qty = models.IntegerField(help_text="Total accepted quantity")
    trays_snapshot = models.JSONField(default=list, blank=True,
                                     help_text="Accept tray snapshot: [{tray_id, qty, is_top}]")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='na_partial_accept_lots')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Nickel Audit Partial Accept Lot"
        verbose_name_plural = "Nickel Audit Partial Accept Lots"
        indexes = [
            models.Index(fields=['new_lot_id']),
            models.Index(fields=['parent_lot_id']),
        ]

    def __str__(self):
        return f"NA-PartialAccept: {self.new_lot_id} (from {self.parent_lot_id}, qty={self.accepted_qty})"


class NickelAudit_PartialRejectLot(models.Model):
    """
    Partial reject record created when partial rejection occurs in Nickel Audit.
    parent_lot_id = the original JigUnloadAfterTable lot_id.
    """
    new_lot_id = models.CharField(max_length=100, db_index=True, unique=True,
                                  help_text="Generated unique ID for this reject record")
    parent_lot_id = models.CharField(max_length=100, db_index=True,
                                     help_text="Original parent lot ID")
    parent_submission = models.ForeignKey(
        'NickelAudit_Submission',
        on_delete=models.CASCADE,
        related_name='partial_reject_lots',
        null=True, blank=True,
    )
    rejected_qty = models.IntegerField(help_text="Total rejected quantity")
    rejection_reasons = models.JSONField(default=dict, blank=True,
                                        help_text='Schema: {"<reason_id>": {"reason": "...", "qty": N}}')
    trays_snapshot = models.JSONField(default=list, blank=True,
                                     help_text="Reject tray snapshot: [{tray_id, qty}]")
    remarks = models.TextField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='na_partial_reject_lots')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Nickel Audit Partial Reject Lot"
        verbose_name_plural = "Nickel Audit Partial Reject Lots"
        indexes = [
            models.Index(fields=['new_lot_id']),
            models.Index(fields=['parent_lot_id']),
        ]

    def __str__(self):
        return f"NA-PartialReject: {self.new_lot_id} (from {self.parent_lot_id}, qty={self.rejected_qty})"


class NickelAudit_AQLSamplingPlan(AQLSamplingPlan):
    """
    Proxy onto BrassAudit.AQLSamplingPlan (table: aql_sampling_plan).
    Same shared master data Brass Audit uses — proxied here purely so it is
    registered and visible under the Nickel Audit admin section too. Django
    forbids registering one model class twice, so this proxy (own model
    class, same table, no schema change) is how both modules get their own
    admin entry against the identical AQL limits.
    """
    class Meta:
        proxy = True
        app_label = 'Nickel_Audit'
        verbose_name = 'AQL Sampling Plan'
        verbose_name_plural = 'AQL Sampling Plans'
