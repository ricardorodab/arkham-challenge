"""Retain refresh history after account deletion and version product sessions."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("service", "0003_savedview"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="persona",
            name="session_version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AlterField(
            model_name="refreshjob",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="refresh_jobs",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
