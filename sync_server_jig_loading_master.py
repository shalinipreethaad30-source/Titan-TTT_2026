"""
Compare/apply server JigLoadingMaster data to the LOCAL database by plating_stk_no.

Default: DRY RUN (no database changes)
Apply:   python sync_server_jig_loading_master.py --apply

Place this script in the Django project root beside manage.py.
Keep these two exported JSON files in the project's Doc folder:
  Doc/server_dp_master_data(4).json
  Doc/server_jig_loading_master(1).json
"""

import os
import sys
import json
from pathlib import Path
from collections import Counter

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "watchcase_tracker.settings")

import django
django.setup()

from django.db import transaction
from modelmasterapp.models import ModelMaster
from Jig_Loading.models import JigLoadingMaster


BASE_DIR = Path(__file__).resolve().parent
DP_MASTER_JSON = BASE_DIR / "server_dp_master_data.json"
JIG_LOADING_JSON = BASE_DIR / "server_jig_loading_master.json"

APPLY = "--apply" in sys.argv


def load_json(path):
    if not path.exists():
        raise FileNotFoundError(f"Missing file: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


dp_data = load_json(DP_MASTER_JSON)
server_jig_rows = load_json(JIG_LOADING_JSON)

# Server ModelMaster PK -> identifying data.
server_models = {
    row["pk"]: row["fields"]
    for row in dp_data
    if row.get("model") == "modelmasterapp.modelmaster"
}

stats = Counter()
details = []
server_psns = set()

for row in server_jig_rows:
    server_model_id = row.get("model_stock_no_id")
    fields = server_models.get(server_model_id)

    if not fields:
        stats["SERVER_MODEL_MISSING"] += 1
        details.append(("SERVER_MODEL_MISSING", server_model_id))
        continue

    psn = str(fields.get("plating_stk_no") or "").strip()
    if not psn:
        stats["SERVER_PSN_BLANK"] += 1
        details.append(("SERVER_PSN_BLANK", server_model_id))
        continue

    if psn.lower() in server_psns:
        stats["SERVER_DUPLICATE_PSN"] += 1
        details.append(("SERVER_DUPLICATE_PSN", psn, server_model_id))
        continue
    server_psns.add(psn.lower())

    local_matches = ModelMaster.objects.filter(plating_stk_no__iexact=psn)
    local_count = local_matches.count()

    if local_count == 0:
        stats["LOCAL_MODEL_MISSING"] += 1
        details.append(("LOCAL_MODEL_MISSING", psn, server_model_id))
        continue

    if local_count > 1:
        stats["LOCAL_MODEL_DUPLICATE"] += 1
        details.append(("LOCAL_MODEL_DUPLICATE", psn, local_count))
        continue

    local_model = local_matches.first()

    desired = {
        "jig_type": row.get("jig_type"),
        "jig_capacity": row.get("jig_capacity"),
        "forging_info": row.get("forging_info"),
    }

    local_jigs = JigLoadingMaster.objects.filter(model_stock_no=local_model)
    local_jig_count = local_jigs.count()

    if local_jig_count > 1:
        stats["LOCAL_JIG_DUPLICATE"] += 1
        details.append(("LOCAL_JIG_DUPLICATE", psn, local_jig_count))
        continue

    if local_jig_count == 0:
        stats["CREATE"] += 1
        details.append(("CREATE", psn, desired))
        continue

    local_jig = local_jigs.first()
    current = {
        "jig_type": local_jig.jig_type,
        "jig_capacity": local_jig.jig_capacity,
        "forging_info": local_jig.forging_info,
    }

    if current == desired:
        stats["ALREADY_MATCHING"] += 1
    else:
        stats["UPDATE"] += 1
        details.append(("UPDATE", psn, current, desired))


print("\n=== SERVER -> LOCAL JIG LOADING MASTER COMPARISON ===")
print("Mode                  :", "APPLY" if APPLY else "DRY RUN")
print("Server Jig mappings   :", len(server_jig_rows))
print("Server ModelMaster    :", len(server_models))
print("Unique server PSNs    :", len(server_psns))
print("CREATE                :", stats["CREATE"])
print("UPDATE                :", stats["UPDATE"])
print("ALREADY MATCHING      :", stats["ALREADY_MATCHING"])
print("LOCAL MODEL MISSING   :", stats["LOCAL_MODEL_MISSING"])
print("LOCAL MODEL DUPLICATE :", stats["LOCAL_MODEL_DUPLICATE"])
print("LOCAL JIG DUPLICATE   :", stats["LOCAL_JIG_DUPLICATE"])
print("SERVER MODEL MISSING  :", stats["SERVER_MODEL_MISSING"])
print("SERVER PSN BLANK      :", stats["SERVER_PSN_BLANK"])
print("SERVER DUPLICATE PSN  :", stats["SERVER_DUPLICATE_PSN"])
print("Local JigMaster total :", JigLoadingMaster.objects.count())

print("\n=== 1824BAA02 CHECK ===")
server_1824 = None
for row in server_jig_rows:
    fields = server_models.get(row.get("model_stock_no_id"))
    if fields and str(fields.get("plating_stk_no") or "").strip().upper() == "1824BAA02":
        server_1824 = (fields, row)
        break

if server_1824:
    fields, row = server_1824
    print("Server Model PK       :", row.get("model_stock_no_id"))
    print("Model No              :", fields.get("model_no"))
    print("Plating Stock No      :", fields.get("plating_stk_no"))
    print("Jig Type              :", row.get("jig_type"))
    print("Jig Capacity          :", row.get("jig_capacity"))
    print("Forging Info          :", row.get("forging_info"))
    cap = row.get("jig_capacity")
    print("Expected Prefix       :", f"J{int(cap):03d}-" if cap is not None else None)
else:
    print("NOT FOUND IN SERVER EXPORT")

blocking = (
    stats["SERVER_MODEL_MISSING"]
    + stats["SERVER_PSN_BLANK"]
    + stats["SERVER_DUPLICATE_PSN"]
    + stats["LOCAL_MODEL_MISSING"]
    + stats["LOCAL_MODEL_DUPLICATE"]
    + stats["LOCAL_JIG_DUPLICATE"]
)

if not APPLY:
    print("\nNO DATABASE CHANGES MADE.")
    print("Blocking issues       :", blocking)
    print("If Blocking issues = 0, review the counts before running with --apply.")
    sys.exit(0)

if blocking:
    raise SystemExit(
        f"\nABORTED: {blocking} blocking mapping issue(s) found. "
        "No changes were applied."
    )

created = 0
updated = 0
unchanged = 0

with transaction.atomic():
    for row in server_jig_rows:
        fields = server_models[row["model_stock_no_id"]]
        psn = str(fields["plating_stk_no"]).strip()

        local_model = ModelMaster.objects.get(plating_stk_no__iexact=psn)

        desired = {
            "jig_type": row.get("jig_type"),
            "jig_capacity": row.get("jig_capacity"),
            "forging_info": row.get("forging_info"),
        }

        obj = JigLoadingMaster.objects.filter(model_stock_no=local_model).first()

        if obj is None:
            JigLoadingMaster.objects.create(
                model_stock_no=local_model,
                **desired,
            )
            created += 1
            continue

        changed = False
        for field, value in desired.items():
            if getattr(obj, field) != value:
                setattr(obj, field, value)
                changed = True

        if changed:
            obj.save(update_fields=["jig_type", "jig_capacity", "forging_info"])
            updated += 1
        else:
            unchanged += 1

print("\n=== APPLY COMPLETE ===")
print("Created               :", created)
print("Updated               :", updated)
print("Unchanged             :", unchanged)
print("Final JigMaster total :", JigLoadingMaster.objects.count())
