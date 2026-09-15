from rest_framework import serializers
from modelmasterapp.models import *
from InputScreening.models import *
from Brass_QC.models import *
from BrassAudit.models import *
from Nickel_Audit.models import *
from Nickel_Inspection.models import *
from rest_framework.exceptions import ValidationError
from django.conf import settings
from django.utils import timezone

class PolishFinishTypeSerializer(serializers.ModelSerializer):
    polish_finish = serializers.CharField(
        error_messages={
            'unique': "A polish finish with this name already exists. Please enter a unique name."
        }
    )
    polish_internal = serializers.CharField(
        error_messages={
            'unique': "A polish internal ID with this value already exists. Please enter a unique internal ID."
        }
    )

    class Meta:
        model = PolishFinishType
        fields = '__all__'
    
    @staticmethod
    def _validate_plain_text(value, label):
        value = value.strip()
        if not value:
            raise serializers.ValidationError(f"{label} cannot be empty.")
        # Reject markup delimiters rather than stripping and saving altered data.
        # Display code must continue to escape values, including older records.
        if '<' in value or '>' in value:
            raise serializers.ValidationError(f"HTML tags are not allowed in {label}.")
        return value

    def validate_polish_finish(self, value):
        return self._validate_plain_text(value, "Polish Finish")

    def validate_polish_internal(self, value):
        return self._validate_plain_text(value, "Internal Code")

    def validate(self, data):
        polish_finish = data.get('polish_finish', '').strip()
        polish_internal = data.get('polish_internal', '').strip()
        qs = PolishFinishType.objects.all()
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if polish_finish and qs.filter(polish_finish__iexact=polish_finish).exists():
            raise serializers.ValidationError({
                'polish_finish': "A polish finish with this name already exists. Please enter a unique name."
            })
        if polish_internal and qs.filter(polish_internal__iexact=polish_internal).exists():
            raise serializers.ValidationError({
                'polish_internal': "A polish internal ID with this value already exists. Please enter a unique internal ID."
            })
        return data

    def create(self, validated_data):
        validated_data['date_time'] = timezone.now()
        return super().create(validated_data)

class PlatingColorSerializer(serializers.ModelSerializer):
    plating_color = serializers.CharField(
        error_messages={
            'unique': "A plating color with this name already exists. Please enter a unique name."
        }
    )

    class Meta:
        model = Plating_Color
        fields = '__all__'
    
    def validate_plating_color(self, value):
        if not value.strip():
            raise serializers.ValidationError("Plating color name cannot be empty.")
        return value.strip()

    def validate(self, data):
        plating_color = data.get('plating_color', '').strip()
        qs = Plating_Color.objects.all()
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if plating_color and qs.filter(plating_color__iexact=plating_color).exists():
            raise serializers.ValidationError({
                'plating_color': "A plating color with this name already exists. Please enter a unique name."
            })
        return data

    def create(self, validated_data):
        # Ensure date_time is set to current time when creating
        validated_data['date_time'] = timezone.now()
        return super().create(validated_data)

class TrayTypeSerializer(serializers.ModelSerializer):
    tray_type = serializers.CharField(
        error_messages={
            'unique': "A tray type with this name already exists. Please enter a unique name."
        }
    )

    class Meta:
        model = TrayType
        fields = '__all__'
    
    def validate_tray_type(self, value):
        if not value.strip():
            raise serializers.ValidationError("Tray type name cannot be empty.")
        return value.strip()
    
    def validate_tray_capacity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Tray capacity must be greater than 0.")
        return value

    def validate(self, data):
        tray_type = data.get('tray_type', '').strip()
        qs = TrayType.objects.all()
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if tray_type and qs.filter(tray_type__iexact=tray_type).exists():
            raise serializers.ValidationError({
                'tray_type': "A tray type with this name already exists. Please enter a unique name."
            })
        return data

    def create(self, validated_data):
        # Ensure date_time is set to current time when creating
        validated_data['date_time'] = timezone.now()
        return super().create(validated_data)

class ModelImageSerializer(serializers.ModelSerializer):
    # Allowed image MIME types and extensions (Issue #24).
    # Kept in sync with adminportal/views.py ModelImageAPIView._validate_image_file().
    # This is now the canonical validation layer; the view-level check is left in
    # place as a defense-in-depth duplicate, not the source of truth.
    _ALLOWED_IMAGE_MIME = frozenset({
        'image/jpeg', 'image/png', 'image/gif',
    })
    _ALLOWED_IMAGE_EXT = frozenset({
        '.jpg', '.jpeg', '.png', '.gif',
    })
    # Dangerous intermediate extensions that must not appear anywhere in the filename
    _DANGEROUS_EXT = frozenset({
        '.exe', '.php', '.sh', '.bat', '.cmd', '.ps1', '.js', '.py',
        '.rb', '.pl', '.asp', '.aspx', '.jsp', '.cgi', '.dll', '.so',
    })

    # Project-owned magic-number (file signature) checks for each allowed
    # image format. These inspect actual file bytes, independent of the
    # filename extension or the client-supplied Content-Type header, so a
    # mismatched/spoofed extension or MIME cannot mask non-image content.
    # Values are lists of possible byte signatures at offset 0, since some
    # formats (GIF, WEBP) have more than one valid header variant.
    _IMAGE_SIGNATURES = {
        'PNG': [b'\x89PNG\r\n\x1a\n'],
        'JPEG': [b'\xff\xd8\xff'],
        'GIF': [b'GIF87a', b'GIF89a'],
    }

    # Maps each detected signature format to the extension(s) and MIME type
    # that are allowed to accompany it. Used to enforce extension/MIME/
    # signature consistency (Task 4): the detected format must match BOTH
    # the claimed extension AND the claimed Content-Type, not just be "some
    # allowed format".
    _FORMAT_TO_EXT = {
        'PNG': {'.png'},
        'JPEG': {'.jpg', '.jpeg'},
        'GIF': {'.gif'},
    }
    _FORMAT_TO_MIME = {
        'PNG': 'image/png',
        'JPEG': 'image/jpeg',
        'GIF': 'image/gif',
    }

    class Meta:
        model = ModelImage
        fields = '__all__'

    @classmethod
    def _detect_image_format(cls, value):
        """
        Reads the first bytes of the uploaded file and identifies which
        known image format (if any) the byte signature matches.

        Returns a tuple: (detected_format_or_None, error_or_None).
        - If content matches a recognized signature: (format_name, None)
        - If content matches no recognized signature: (None, error_string)

        The file pointer is always reset to the start afterwards so that
        downstream consumers (Pillow verification, model save) read the
        complete, untouched file.
        """
        try:
            value.seek(0)
            header = value.read(16)
        finally:
            value.seek(0)

        if not header:
            return None, 'Uploaded file is empty or unreadable.'

        is_png = header.startswith(cls._IMAGE_SIGNATURES['PNG'][0])
        is_jpeg = header.startswith(cls._IMAGE_SIGNATURES['JPEG'][0])
        is_gif = any(header.startswith(sig) for sig in cls._IMAGE_SIGNATURES['GIF'])
        if is_png:
            return 'PNG', None
        if is_jpeg:
            return 'JPEG', None
        if is_gif:
            return 'GIF', None

        return None, (
            'File content does not match a recognized image format '
            '(PNG, JPEG, GIF). The file may be corrupted or '
            'not a genuine image.'
        )

    @classmethod
    def _check_signature_consistency(cls, value, ext, content_type):
        """
        Verifies that the detected image signature, the claimed file
        extension, and the claimed Content-Type all agree on the same
        format. Returns an error string if any of the three disagree, or
        None if they are fully consistent.
        """
        detected_format, detect_error = cls._detect_image_format(value)
        if detect_error:
            return detect_error

        allowed_exts = cls._FORMAT_TO_EXT[detected_format]
        expected_mime = cls._FORMAT_TO_MIME[detected_format]

        if ext not in allowed_exts:
            return (
                f'File extension "{ext}" does not match the detected image '
                f'format ({detected_format}). Expected extension(s): '
                f'{", ".join(sorted(allowed_exts))}.'
            )

        if content_type and content_type != expected_mime:
            return (
                f'Content-Type "{content_type}" does not match the detected '
                f'image format ({detected_format}). Expected Content-Type: '
                f'{expected_mime}.'
            )

        return None

    def validate_master_image(self, value):
        """
        Canonical file validation for ModelImage uploads. Runs regardless of
        which view/caller uses this serializer.

        Layers (same logic as ModelImageAPIView._validate_image_file, plus
        added size, signature, and consistency checks):
          1. Dangerous intermediate extension denylist (e.g. sample.exe.png)
          2. Final extension allowlist
          3. MIME type allowlist
          4. Max upload size (settings.MODEL_IMAGE_MAX_UPLOAD_SIZE)
          5. Magic-number detection + extension/MIME/signature consistency
             (project-owned, reads raw bytes)
          6. Valid image content (delegated to ImageField -> Pillow .verify())
        """
        import os

        name = value.name or ''
        stem = os.path.splitext(name)[0].lower()
        _, ext = os.path.splitext(name.lower())

        for dext in self._DANGEROUS_EXT:
            if stem.endswith(dext) or f'{dext}.' in stem:
                raise serializers.ValidationError(
                    f'File "{name}" contains a disallowed intermediate extension.'
                )

        if ext not in self._ALLOWED_IMAGE_EXT:
            raise serializers.ValidationError(
                f'File extension "{ext}" is not allowed. Allowed: jpg, jpeg, png, gif.'
            )

        content_type = getattr(value, 'content_type', '') or ''
        if content_type and content_type not in self._ALLOWED_IMAGE_MIME:
            raise serializers.ValidationError(
                f'File type "{content_type}" is not allowed. Only image files are accepted.'
            )

        max_size = settings.MODEL_IMAGE_MAX_UPLOAD_SIZE
        size = getattr(value, 'size', None)
        if size is not None and size > max_size:
            max_mb = max_size / (1024 * 1024)
            raise serializers.ValidationError(
                f'Image exceeds maximum allowed size of {max_mb:g} MB.'
            )

        signature_error = self._check_signature_consistency(value, ext, content_type)
        if signature_error:
            raise serializers.ValidationError(signature_error)

        # Content verification: ModelImage.master_image is an ImageField, so
        # ImageField.to_internal_value() has already run Pillow's
        # Image.open(file).verify() before validate_master_image() is called.
        # Non-image bytes (even with a spoofed extension/MIME) are rejected
        # upstream of this point with a DRF "Upload a valid image" error.

        return value

    def create(self, validated_data):
        # Ensure date_time is set to current time when creating
        validated_data['date_time'] = timezone.now()

        uploaded_file = validated_data.get('master_image')
        if uploaded_file and not validated_data.get('original_filename'):
            import os
            validated_data['original_filename'] = os.path.basename(
                uploaded_file.name or ''
            )

        return super().create(validated_data)

class ModelMasterSerializer(serializers.ModelSerializer):
    model_no = serializers.CharField()  # No unique error here

    class Meta:
        model = ModelMaster
        fields = '__all__'
    
    def validate(self, data):
        model_no = data.get('model_no', '').strip()
        polish_finish = data.get('polish_finish')
        ep_bath_type = data.get('ep_bath_type')
        version = data.get('version')

        qs = ModelMaster.objects.all()
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        # Check for duplicate combination
        if qs.filter(
            model_no__iexact=model_no,
            polish_finish=polish_finish,
            ep_bath_type=ep_bath_type,
            version=version
        ).exists():
            raise serializers.ValidationError(
                "A model with this combination of Model No, Polish Finish, EP Bath Type, and Version already exists. Please enter a unique combination."
            )
        return data

    def create(self, validated_data):
        validated_data['date_time'] = timezone.now()
        model_no = validated_data.get('model_no', '')
        polish_finish = validated_data.get('polish_finish')  # This is a PolishFinishType instance
        version = validated_data.get('version')  # This is a string or int

        plating_stk_no = f"{model_no}X{polish_finish.polish_internal}{version}"
        validated_data['plating_stk_no'] = plating_stk_no
        return super().create(validated_data)

class LocationSerializer(serializers.ModelSerializer):
    location_name = serializers.CharField(
        error_messages={
            'unique': "A location with this name already exists. Please enter a unique name."
        }
    )

    class Meta:
        model = Location
        fields = '__all__'

    def validate_location_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Location name cannot be empty.")
        return value.strip()

    def validate(self, data):
        location_name = data.get('location_name', '').strip()
        qs = Location.objects.all()
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if location_name and qs.filter(location_name__iexact=location_name).exists():
            raise serializers.ValidationError({
                'location_name': "A location with this name already exists. Please enter a unique name."
            })
        return data

    def create(self, validated_data):
        # Ensure date_time is set to current time when creating
        validated_data['date_time'] = timezone.now()
        return super().create(validated_data)

class TrayIdSerializer(serializers.ModelSerializer):
    tray_type = serializers.PrimaryKeyRelatedField(queryset=TrayType.objects.all(), required=True)
    tray_id = serializers.CharField(
        error_messages={
            'unique': "A tray ID with this value already exists. Please enter a unique tray ID."
        }
    )

    class Meta:
        model = TrayId
        fields = '__all__'

    def validate_tray_id(self, value):
        if not value.strip():
            raise serializers.ValidationError("Tray ID cannot be empty.")
        return value.strip()

    def validate(self, data):
        tray_id = data.get('tray_id', '').strip()
        tray_type = data.get('tray_type')
        tray_capacity = data.get('tray_capacity')
        
        # Check for duplicate tray_id
        qs = TrayId.objects.all()
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if tray_id and qs.filter(tray_id__iexact=tray_id).exists():
            raise serializers.ValidationError({
                'tray_id': f"A tray ID '{tray_id}' already exists. Please enter a unique tray ID."
            })
        
        # Validate tray capacity matches tray type
        if tray_type and tray_capacity is not None:
            if tray_type.tray_capacity != tray_capacity:
                raise serializers.ValidationError({
                    'tray_capacity': f"Tray capacity must match the selected tray type's capacity ({tray_type.tray_capacity})."
                })
        
        return data

    def create(self, validated_data):
        # Ensure date_time is set to current time when creating
        validated_data['date'] = timezone.now()
        return super().create(validated_data)

class CategorySerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(
        error_messages={
            'unique': "A category with this name already exists. Please enter a unique name."
        }
    )

    class Meta:
        model = Category
        fields = '__all__'

    def validate_category_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Category name cannot be empty.")
        return value.strip()

    def validate(self, data):
        category_name = data.get('category_name', '').strip()
        qs = Category.objects.all()
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if category_name and qs.filter(category_name__iexact=category_name).exists():
            raise serializers.ValidationError({
                'category_name': "A category with this name already exists. Please enter a unique name."
            })
        return data

    def create(self, validated_data):
        # Ensure date_time is set to current time when creating
        validated_data['date_time'] = timezone.now()
        return super().create(validated_data)
    
class IPRejectionSerializer(serializers.ModelSerializer):
    rejection_reason = serializers.CharField(
        error_messages={
            'unique': "A rejection reason with this text already exists. Please enter a unique reason."
        }
    )

    class Meta:
        model = IP_Rejection_Table
        fields = '__all__'

    def validate_rejection_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError("Rejection reason cannot be empty.")
        return value.strip()

    def validate(self, data):
        rejection_reason = data.get('rejection_reason', '').strip()
        qs = IP_Rejection_Table.objects.all()
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if rejection_reason and qs.filter(rejection_reason__iexact=rejection_reason).exists():
            raise serializers.ValidationError({
                'rejection_reason': "A rejection reason with this text already exists. Please enter a unique reason."
            })
        return data

    def create(self, validated_data):
        # Optionally set fields like date_time if needed
        return super().create(validated_data)


class BrassIQFRejectionSerializer(serializers.Serializer):
    rejection_reason = serializers.CharField()
    rejection_reason_id = serializers.CharField(required=False, allow_blank=True)

    def create(self, validated_data):
        # Only save rejection_reason_id, rejection_reason, and date_time
        fields = {
            'rejection_reason_id': validated_data.get('rejection_reason_id'),
            'rejection_reason': validated_data['rejection_reason'],
            'date_time': timezone.now()
        }
        qc_obj = Brass_QC_Rejection_Table.objects.create(**fields)
        audit_obj = Brass_Audit_Rejection_Table.objects.create(**fields)
        iqf_obj = IQF_Rejection_Table.objects.create(**fields)
        return {
            'qc': qc_obj,
            'audit': audit_obj,
            'iqf': iqf_obj
        }

    def to_representation(self, instance):
        return {
            'qc_id': instance['qc'].id,
            'audit_id': instance['audit'].id,
            'iqf_id': instance['iqf'].id,
            'rejection_reason_id': instance['qc'].rejection_reason_id,
            'rejection_reason': instance['qc'].rejection_reason,
        }
        
class NickelAuditQCRejectionSerializer(serializers.Serializer):
    rejection_reason = serializers.CharField()

    def create(self, validated_data):
        # Only save rejection_reason
        fields = {
            'rejection_reason': validated_data['rejection_reason']
        }
        audit_obj = Nickel_Audit_Rejection_Table.objects.create(**fields)
        qc_obj = Nickel_QC_Rejection_Table.objects.create(**fields)
        return {
            'audit': audit_obj,
            'qc': qc_obj
        }

    def to_representation(self, instance):
        return {
            'audit_id': instance['audit'].id,
            'qc_id': instance['qc'].id,
            'rejection_reason': instance['qc'].rejection_reason,
        }
