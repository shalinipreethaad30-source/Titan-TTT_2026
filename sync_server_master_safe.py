#!/usr/bin/env python
"""
Safe master-data sync from server_dp_master_data.json.

Default: DRY RUN (no database changes)
Apply:   py sync_local_master_from_server_json.py --apply

Safety:
- Does NOT delete records.
- Does NOT update existing records.
- Creates missing reference-master records by business key.
- Creates missing ModelMaster rows by case-insensitive plating_stk_no.
- Maps foreign keys by source business values, not source PKs.
- Ignores images and server user IDs.
"""

import argparse
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "watchcase_tracker.settings")

import django
django.setup()

from django.db import transaction
from modelmasterapp.models import (
    ModelMaster,
    PolishFinishType,
    TrayType,
    Version,
    Plating_Color,
    Category,
    Vendor,
    Location,
)

DEFAULT_JSON = "server_dp_master_data.json"


def norm(v):
    return str(v).strip().casefold() if v is not None else ""


def rows_for(data, label):
    return [x for x in data if x.get("model", "").casefold() == label.casefold()]


def field_names(model):
    return {f.name for f in model._meta.get_fields()}


def first_ci(model, field, value):
    if value is None:
        return None
    return model.objects.filter(**{f"{field}__iexact": str(value).strip()}).first()


def source_map(rows):
    return {x["pk"]: x.get("fields", {}) for x in rows}


def get_or_plan(model, lookup_field, lookup_value, defaults, apply, stats, label):
    obj = first_ci(model, lookup_field, lookup_value)
    if obj:
        stats[label]["existing"] += 1
        return obj

    stats[label]["missing"] += 1
    if not apply:
        return None

    allowed = field_names(model)
    clean_defaults = {
        k: v for k, v in defaults.items()
        if k in allowed and k not in {lookup_field, "id", "pk", "date_time", "createdby"}
    }
    obj = model.objects.create(**{lookup_field: lookup_value}, **clean_defaults)
    stats[label]["created"] += 1
    return obj


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually create missing records")
    parser.add_argument("--json", default=DEFAULT_JSON, help="Path to server master JSON")
    args = parser.parse_args()

    json_path = Path(args.json)
    if not json_path.exists():
        print(f"ERROR: JSON file not found: {json_path.resolve()}")
        sys.exit(1)

    with json_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    labels = {
        "Version": ("modelmasterapp.version", Version, "version_internal"),
        "Plating_Color": ("modelmasterapp.plating_color", Plating_Color, "plating_color"),
        "PolishFinishType": ("modelmasterapp.polishfinishtype", PolishFinishType, "polish_finish"),
        "Category": ("modelmasterapp.category", Category, "category_name"),
        "Vendor": ("modelmasterapp.vendor", Vendor, "vendor_internal"),
        "Location": ("modelmasterapp.location", Location, "location_name"),
        "TrayType": ("modelmasterapp.traytype", TrayType, "tray_type"),
    }

    stats = {
        k: {"source": 0, "existing": 0, "missing": 0, "created": 0}
        for k in [*labels.keys(), "ModelMaster"]
    }

    source_rows = {}
    for name, (fixture_label, _, _) in labels.items():
        source_rows[name] = rows_for(data, fixture_label)
        stats[name]["source"] = len(source_rows[name])

    mm_rows = rows_for(data, "modelmasterapp.modelmaster")
    stats["ModelMaster"]["source"] = len(mm_rows)

    print("=" * 76)
    print("MODE:", "APPLY" if args.apply else "DRY RUN")
    print("JSON:", json_path.resolve())
    print("Source ModelMaster rows:", len(mm_rows))
    print("=" * 76)

    # Build source PK -> source fields maps for FK translation.
    polish_src = source_map(source_rows["PolishFinishType"])
    tray_src = source_map(source_rows["TrayType"])
    vendor_src = source_map(source_rows["Vendor"])

    def do_sync():
        # Reference masters first.
        for name, (_, model, key_field) in labels.items():
            for item in source_rows[name]:
                f = item.get("fields", {})
                key = f.get(key_field)
                if key is None or str(key).strip() == "":
                    continue
                defaults = dict(f)
                defaults.pop(key_field, None)
                get_or_plan(
                    model, key_field, str(key).strip(), defaults,
                    args.apply, stats, name
                )

        mm_fields = field_names(ModelMaster)

        for item in mm_rows:
            f = item.get("fields", {})
            sku = f.get("plating_stk_no")
            if sku is None or not str(sku).strip():
                continue
            sku = str(sku).strip()

            existing = ModelMaster.objects.filter(plating_stk_no__iexact=sku).first()
            if existing:
                stats["ModelMaster"]["existing"] += 1
                continue

            stats["ModelMaster"]["missing"] += 1
            if not args.apply:
                continue

            kwargs = {}
            simple_fields = [
                "model_no", "ep_bath_type", "tray_capacity", "tray_code",
                "brand", "gender", "wiping_required", "version",
                "plating_color_code",
            ]
            for name in simple_fields:
                if name in mm_fields and name in f:
                    kwargs[name] = f.get(name)

            # Foreign keys are mapped by business key, never by server PK.
            polish_pk = f.get("polish_finish")
            if "polish_finish" in mm_fields and polish_pk is not None:
                sf = polish_src.get(polish_pk)
                if sf:
                    obj = first_ci(PolishFinishType, "polish_finish", sf.get("polish_finish"))
                    if obj:
                        kwargs["polish_finish"] = obj

            tray_pk = f.get("tray_type")
            if "tray_type" in mm_fields and tray_pk is not None:
                sf = tray_src.get(tray_pk)
                if sf:
                    obj = first_ci(TrayType, "tray_type", sf.get("tray_type"))
                    if obj:
                        kwargs["tray_type"] = obj

            vendor_pk = f.get("vendor_internal")
            if "vendor_internal" in mm_fields and vendor_pk is not None:
                sf = vendor_src.get(vendor_pk)
                if sf:
                    obj = first_ci(Vendor, "vendor_internal", sf.get("vendor_internal"))
                    if obj:
                        kwargs["vendor_internal"] = obj

            # Some project versions contain a plating_color field on ModelMaster.
            # The fixture is normally null; only map it if it has a usable value.
            if "plating_color" in mm_fields and f.get("plating_color"):
                pc_source = source_map(source_rows["Plating_Color"]).get(f.get("plating_color"))
                if pc_source:
                    pc = first_ci(Plating_Color, "plating_color", pc_source.get("plating_color"))
                    if pc:
                        kwargs["plating_color"] = pc

            # Never copy server createdby or images.
            ModelMaster.objects.create(plating_stk_no=sku, **kwargs)
            stats["ModelMaster"]["created"] += 1

    if args.apply:
        with transaction.atomic():
            do_sync()
    else:
        do_sync()

    print("\n=== SUMMARY ===")
    for name, s in stats.items():
        print(
            f"{name:18} source={s['source']:4} "
            f"existing={s['existing']:4} missing={s['missing']:4} "
            f"created={s['created']:4}"
        )

    print("\nModelMaster total now:", ModelMaster.objects.count())
    print(
        "Unique ModelMaster SKUs:",
        ModelMaster.objects.exclude(plating_stk_no__isnull=True)
        .exclude(plating_stk_no="")
        .values("plating_stk_no").distinct().count()
    )

    if not args.apply:
        print("\nDRY RUN ONLY - no database changes were made.")
        print("If the summary is correct, run:")
        print("  py sync_local_master_from_server_json.py --apply")
    else:
        print("\nAPPLY COMPLETE - only missing records were created; nothing was deleted or updated.")


if __name__ == "__main__":
    main()
