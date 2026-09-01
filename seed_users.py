import os
import django

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "watchcase_tracker.settings"
)

django.setup()

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction

from adminportal.services import (
    ensure_module_registry_seeded,
    sync_user_module_provisions_from_group,
    invalidate_user_modules_cache,
)

User = get_user_model()


USERS = [
    # Day Planning
    ("DPUser1", "DPUser@123", "DP User"),
    ("DPUser2", "DPUser@123", "DP User"),

    # Input Screening
    ("ISUser1", "ISUser@123", "IS User"),
    ("ISUser2", "ISUser@123", "IS User"),

    # Brass QC
    ("BQUser1", "BQUser@123", "BQC User"),
    ("BQUser2", "BQUser@123", "BQC User"),

    # IQF
    ("IQFUser1", "IQFUser@123", "IQF User"),
    ("IQFUser2", "IQFUser@123", "IQF User"),

    # Brass Audit
    ("BAUser1", "BAUser@123", "BA User"),
    ("BAUser2", "BAUser@123", "BA User"),

    # Jig Loading
    ("JLUser1", "JLUser@123", "JIG-L User"),
    ("JLUser2", "JLUser@123", "JIG-L User"),

    # Inprocess Inspection
    ("IIUser1", "IIUser@123", "IP User"),
    ("IIUser2", "IIUser@123", "IP User"),

    # Jig Unloading Z1
    ("JULUserz1", "JULUser@123", "JIG-UL User"),
    ("JULUserz2", "JULUser@123", "JIG-UL User"),

    # Jig Unloading Z2
    ("JULUserz01", "JULUser@123", "JIG-UL-Z2 User"),
    ("JULUserz02", "JULUser@123", "JIG-UL-Z2 User"),

    # Nickel Wiping / Nickel Inspection Z1
    ("NWUserz1", "NWUser@123", "NQ User"),
    ("NWUserz2", "NWUser@123", "NQ User"),

    # Nickel Wiping / Nickel Inspection Z2
    ("NWUserz01", "NWUser@123", "Nickel Inspection Zone 2 User"),
    ("NWUserz02", "NWUser@123", "Nickel Inspection Zone 2 User"),

    # Nickel Audit Z1
    ("NAUserz1", "NAUser@123", "NA User"),
    ("NAUserz2", "NAUser@123", "NA User"),

    # Nickel Audit Z2
    ("NAUserz01", "NAUser@123", "Nickel Audit Zone 2 User"),
    ("NAUserz02", "NAUser@123", "Nickel Audit Zone 2 User"),

    # Spider Spindle Z1
    ("SSUserz1", "SSUser@123", "SP-Z1 User"),
    ("SSUserz2", "SSUser@123", "SP-Z1 User"),

    # Spider Spindle Z2
    ("SSUserz01", "SSUser@123", "SP-Z2 User"),
    ("SSUserz02", "SSUser@123", "SP-Z2 User"),
]


@transaction.atomic
def seed_users():
    print("\n========================================")
    print("Track & Trace User Seed")
    print("========================================\n")

    # Creates/updates all modules and user-category groups
    # based on module_registry.py
    ensure_module_registry_seeded()

    created_count = 0
    updated_count = 0
    error_count = 0

    for username, password, group_name in USERS:
        try:
            group = Group.objects.get(name=group_name)

            user, created = User.objects.get_or_create(
                username=username
            )

            # Make sure these are standard application users
            user.is_active = True
            user.is_staff = False
            user.is_superuser = False

            # Django-safe password hashing
            user.set_password(password)
            user.save()

            # Each seeded user gets exactly one operational category
            user.groups.set([group])

            # Create UserModuleProvision rows from the assigned group
            synced = sync_user_module_provisions_from_group(user)

            # Clear cached permissions
            invalidate_user_modules_cache(user.id)

            if created:
                created_count += 1
                status_text = "CREATED"
            else:
                updated_count += 1
                status_text = "UPDATED"

            module_names = list(
                user.module_provisions.values_list(
                    "module_name",
                    flat=True
                )
            )

            print(
                f"[{status_text}] "
                f"{username:<15} "
                f"-> {group_name:<30} "
                f"Modules: {', '.join(module_names)}"
            )

            if not synced:
                print(
                    f"  [WARNING] No module mapping was found "
                    f"for {group_name}"
                )

        except Group.DoesNotExist:
            error_count += 1
            print(
                f'[ERROR] {username}: '
                f'Group "{group_name}" does not exist.'
            )

        except Exception as exc:
            error_count += 1
            print(
                f"[ERROR] {username}: "
                f"{exc.__class__.__name__}: {exc}"
            )

    print("\n========================================")
    print(f"Total Users : {len(USERS)}")
    print(f"Created     : {created_count}")
    print(f"Updated     : {updated_count}")
    print(f"Errors      : {error_count}")
    print("========================================\n")


if __name__ == "__main__":
    seed_users()