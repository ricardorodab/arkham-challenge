"""Retain public progress without rewriting existing job outcomes."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("service", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="refreshjob",
            name="progress",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
