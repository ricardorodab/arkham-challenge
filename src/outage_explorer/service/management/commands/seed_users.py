"""Seed product identities from supplied passwords without shipping credentials.

web.local init supplies its already-generated passwords then calls this command. Django stores
password hashes. Existing passwords are preserved unless the operator explicitly requests
reset; existing privilege/persona inconsistencies require reconciliation rather than an
implicit privilege or identity rewrite.
"""

import json
import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from outage_explorer.service.models import Persona


class Command(BaseCommand):
    """Create validated Viewer/Analyst/Admin demo accounts in one transaction."""

    help = "Create Viewer/Analyst/Admin accounts from environment-supplied passwords."
    requires_system_checks = []

    def add_arguments(self, parser):
        """Expose an explicit reset switch; ordinary repeated initialization is idempotent."""
        parser.add_argument("--reset-passwords", action="store_true")

    def handle(self, *args, **options):
        """Validate all required passwords/identities before committing account changes.

        Read environment passwords only for new or explicitly reset users, apply Django
        password policy, create hashes through create_user/set_password and assign the product
        Persona without staff/superuser privilege. Reject setup conflicts without a partial
        transaction. Return JSON action summaries, never passwords.
        """
        user_model = get_user_model()
        prepared = []
        for role in Persona.Role.values:
            user = user_model.objects.filter(username=role).first()
            if user is not None:
                persona = Persona.objects.filter(user=user).first()
                if user.is_staff or user.is_superuser or persona is None or persona.role != role:
                    raise CommandError(
                        "An existing demo identity requires explicit operator reconciliation."
                    )
            password = None
            if user is None or options["reset_passwords"]:
                password = os.environ.get(f"OUTAGE_{role.upper()}_PASSWORD", "")
                if not password:
                    raise CommandError(f"OUTAGE_{role.upper()}_PASSWORD is required.")
                try:
                    validate_password(password, user=user or user_model(username=role))
                except ValidationError:
                    raise CommandError(
                        f"OUTAGE_{role.upper()}_PASSWORD does not meet password policy."
                    ) from None
            prepared.append((role, user, password))

        results = []
        try:
            with transaction.atomic():
                for role, user, password in prepared:
                    if user is None:
                        user = user_model.objects.create_user(
                            username=role,
                            password=password,
                            is_active=True,
                            is_staff=False,
                            is_superuser=False,
                        )
                        Persona.objects.create(user=user, role=role)
                        action = "created"
                    elif password is not None:
                        user.set_password(password)
                        user.save(update_fields=["password"])
                        action = "password_reset"
                    else:
                        action = "unchanged"
                    results.append({"username": role, "action": action})
        except IntegrityError:
            raise CommandError(
                "Demo account setup conflicted with another operation; no partial change was saved."
            ) from None
        return json.dumps({"accounts": results})
