"""
Set ModelMaster.tray_code (and keep tray_type / tray_capacity consistent)
straight from the source of truth spreadsheet:

    Doc/All SKU and Details.xlsx   (Sheet1, headers row 1, data row 2..2585)

TRAY CODE = <type prefix><colour letter>

    type  : Traycate column   Normal -> 'N'   Jumbo -> 'J'   '?' -> 'N' (default)
    colour: In process Tray Color column
                Red      -> 'R'
                Blue     -> 'B'
                D.Green  -> 'D'
                L.Green  -> 'L'

    e.g.  Normal + Red     -> NR
          Jumbo  + Blue    -> JB
          Normal + D.Green -> ND
          Normal + L.Green -> NL

Valid codes (modelmasterapp/tray_code_mapping.py): NR ND NB NL JR JD JB JL

The script matches rows by `plating_stk_no` == spreadsheet "Plating Stock No".
It also (re)writes tray_type FK and tray_capacity (Normal 16 / Jumbo 12) from
the same row so the three fields never disagree.

Rows whose colour is blank/unknown, or whose plating_stk_no is not in the
sheet, are LEFT UNCHANGED and reported.

Usage:
    python set_tray_code_from_excel.py
"""
import os

import django
import openpyxl

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "watchcase_tracker.settings")
django.setup()

from django.db import transaction  # noqa: E402

from modelmasterapp.models import ModelMaster, ModelMasterCreation, TrayType  # noqa: E402

# ─────────────────────────── knobs ───────────────────────────
DRY_RUN = False
XLSX_PATH = r"E:\Titan-TTT_2026\Doc\All SKU and Details.xlsx"
SHEET = "Sheet1"
COL_PLATING_STK = 1        # "Plating Stock No"   (0-based)
COL_TRAYCATE = 13          # "Traycate"
COL_TRAY_COLOR = 18        # "In process Tray Color"

DEFAULT_TYPE = "Normal"    # used when Traycate is '?'/blank
CASCADE_TO_BATCHES = True  # also refresh ModelMasterCreation.tray_capacity / no_of_trays

TYPE_PREFIX = {"normal": "N", "jumbo": "J"}
COLOR_LETTER = {
    "red": "R",
    "blue": "B",
    "d.green": "D",
    "dark green": "D",
    "l.green": "L",
    "light green": "L",
}
CAPACITY = {"Normal": 16, "Jumbo": 12}
# ─────────────────────────────────────────────────────────────


def load_sheet():
    wb = openpyxl.load_workbook(XLSX_PATH, read_only=True, data_only=True)
    ws = wb[SHEET]
    out = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        p = r[COL_PLATING_STK]
        if p is None or str(p).strip() == "":
            continue
        out[str(p).strip()] = {
            "traycate": (str(r[COL_TRAYCATE]).strip() if r[COL_TRAYCATE] is not None else ""),
            "color": (str(r[COL_TRAY_COLOR]).strip() if r[COL_TRAY_COLOR] is not None else ""),
        }
    return out


def resolve(row):
    """row -> (tray_code, tray_type_str) or (None, reason)."""
    tc = (row["traycate"] or "").strip().lower()
    if tc in ("", "?"):
        type_str = DEFAULT_TYPE
    elif tc in TYPE_PREFIX:
        type_str = tc.capitalize()
    else:
        return None, f"unknown Traycate {row['traycate']!r}"

    prefix = TYPE_PREFIX[type_str.lower()]
    letter = COLOR_LETTER.get((row["color"] or "").strip().lower())
    if not letter:
        return None, f"unknown/blank Tray Color {row['color']!r}"

    return prefix + letter, type_str


def main():
    sheet = load_sheet()
    tray_types = {t.tray_type.strip().lower(): t for t in TrayType.objects.all()}

    models = list(ModelMaster.objects.select_related("tray_type").all().order_by("id"))

    updates = []          # (model, new_code, new_type_str)
    unchanged = 0
    not_in_sheet = []
    unresolved = []

    for m in models:
        key = (m.plating_stk_no or "").strip()
        row = sheet.get(key)
        if not row:
            not_in_sheet.append(f"#{m.id} {m} (plating_stk_no={key!r})")
            continue
        code, type_str = resolve(row)
        if code is None:
            unresolved.append(f"#{m.id} {m}: {type_str}")
            continue
        cur_type = m.tray_type.tray_type if m.tray_type else None
        if m.tray_code == code and cur_type == type_str and m.tray_capacity == CAPACITY[type_str]:
            unchanged += 1
            continue
        updates.append((m, code, type_str))

    print("=" * 70)
    print(f"ModelMaster rows scanned : {len(models)}")
    print(f"Already correct          : {unchanged}")
    print(f"To be updated            : {len(updates)}")
    print(f"Not found in spreadsheet : {len(not_in_sheet)}")
    print(f"Colour/type unresolved   : {len(unresolved)}")
    print("=" * 70)
    for m, code, type_str in updates[:60]:
        old = m.tray_code or "-"
        print(f"  #{m.id:<6} {str(m)[:34]:<34} {old:>3} -> {code}  ({type_str} {CAPACITY[type_str]})")
    if len(updates) > 60:
        print(f"  ... and {len(updates) - 60} more")
    if not_in_sheet:
        print("\nNOT IN SPREADSHEET (left unchanged):")
        for line in not_in_sheet:
            print(f"  - {line}")
    if unresolved:
        print("\nUNRESOLVED colour/type (left unchanged):")
        for line in unresolved:
            print(f"  - {line}")

    if DRY_RUN:
        print("\nDRY_RUN = True -> nothing written. Set DRY_RUN = False to apply.")
        return

    with transaction.atomic():
        for m, code, type_str in updates:
            m.tray_code = code
            m.tray_type = tray_types.get(type_str.lower(), m.tray_type)
            m.tray_capacity = CAPACITY[type_str]
            m.save(update_fields=["tray_code", "tray_type", "tray_capacity"])
        print(f"ModelMaster rows updated  : {len(updates)}")

        if CASCADE_TO_BATCHES:
            import math
            fixed = 0
            for b in ModelMasterCreation.objects.select_related(
                "model_stock_no", "model_stock_no__tray_type"
            ):
                src = b.model_stock_no
                if not src.tray_type:
                    continue
                cap = CAPACITY.get(src.tray_type.tray_type)
                if not cap:
                    continue
                new_trays = (
                    math.ceil(b.total_batch_quantity / cap)
                    if b.total_batch_quantity else b.no_of_trays
                )
                if b.tray_capacity != cap or b.no_of_trays != new_trays:
                    ModelMasterCreation.objects.filter(pk=b.pk).update(
                        tray_capacity=cap, no_of_trays=new_trays
                    )
                    fixed += 1
            print(f"ModelMasterCreation rows updated: {fixed}")

    print("\nDONE.")


if __name__ == "__main__":
    main()
