"""Preserve populated account state and legacy sessions through the account migration."""

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.utils.crypto import salted_hmac


@pytest.mark.django_db(transaction=True)
def test_populated_v3_accounts_jobs_views_and_sessions_survive_upgrade():
    """Exercise the actual SQLite table rebuild and version-one compatibility boundary."""
    previous = [("service", "0003_savedview")]
    current = [("service", "0004_user_sessions_and_refresh_owner")]
    executor = MigrationExecutor(connection)
    executor.migrate(previous)
    try:
        apps = executor.loader.project_state(previous).apps
        old_user = apps.get_model("auth", "User").objects.create(
            username="legacy-admin-upgrade", password=make_password("Synthetic-Upgrade-Only-123!")
        )
        old_persona = apps.get_model("service", "Persona").objects.create(
            user_id=old_user.pk, role="admin"
        )
        old_job = apps.get_model("service", "RefreshJob").objects.create(
            owner_id=old_user.pk, mode="recent", status="succeeded"
        )
        old_view = apps.get_model("service", "SavedView").objects.create(
            owner_id=old_user.pk,
            name="Retained fleet query",
            dataset_id="fleet_daily",
            sql="SELECT period, outage_mw FROM fleet_daily",
            created_model_id="1" * 32,
        )
        auth_hash = get_user_model()(
            pk=old_user.pk, password=old_user.password
        ).get_session_auth_hash()
        session = SessionStore()
        session.update(
            {
                "_auth_user_id": str(old_user.pk),
                "_auth_user_backend": "django.contrib.auth.backends.ModelBackend",
                "_auth_user_hash": auth_hash,
            }
        )
        session.save()
        binding = salted_hmac(
            "outage-actor", f"{old_user.pk}:admin:{session.session_key}:{auth_hash}"
        ).hexdigest()
        expected_scope = salted_hmac(
            "outage-frontend-session", binding, algorithm="sha256"
        ).hexdigest()

        MigrationExecutor(connection).migrate(current)
        from outage_explorer.service.models import Persona, RefreshJob, SavedView

        persona = Persona.objects.get(pk=old_persona.pk)
        assert persona.user_id == old_user.pk and persona.role == "admin"
        assert persona.session_version == 1
        assert get_user_model().objects.get(pk=old_user.pk).password == old_user.password
        assert RefreshJob.objects.get(pk=old_job.pk).owner_id == old_user.pk
        assert SavedView.objects.get(pk=old_view.pk).name == "Retained fleet query"
        client = Client()
        client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key
        reply = client.get("/api/v1/auth/session")
        assert reply.status_code == 200
        assert reply.json()["session_scope"] == expected_scope

        get_user_model().objects.get(pk=old_user.pk).delete()
        assert RefreshJob.objects.get(pk=old_job.pk).owner_id is None
        assert not SavedView.objects.filter(pk=old_view.pk).exists()
    finally:
        MigrationExecutor(connection).migrate(current)
