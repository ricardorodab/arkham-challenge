import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("service", "0002_refreshjob_progress"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SavedView",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("name", models.CharField(max_length=100)),
                ("dataset_id", models.CharField(max_length=64)),
                ("sql", models.TextField()),
                ("max_rows", models.PositiveSmallIntegerField(default=1000)),
                ("chart", models.JSONField(default=dict)),
                ("share_viewer", models.BooleanField(default=False)),
                ("share_analyst", models.BooleanField(default=False)),
                ("share_admin", models.BooleanField(default=False)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("created_model_id", models.CharField(max_length=32)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="saved_views",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["owner", "created_at"], name="svc_saved_owner_created_idx"
                    ),
                    models.Index(
                        fields=["dataset_id", "created_at"], name="svc_saved_dataset_time_idx"
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("max_rows__gte", 1), ("max_rows__lte", 1000)),
                        name="svc_saved_valid_row_cap",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("revision__gte", 1)), name="svc_saved_valid_revision"
                    ),
                ],
            },
        ),
    ]
