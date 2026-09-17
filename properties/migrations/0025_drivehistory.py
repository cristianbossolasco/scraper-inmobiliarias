import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0024_scrape_pipeline_phases_and_snapshot"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DriveHistory",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("client_session_id", models.CharField(max_length=100)),
                ("started_at", models.DateTimeField()),
                ("ended_at", models.DateTimeField()),
                ("distance_m", models.FloatField(default=0)),
                ("point_count", models.PositiveIntegerField(default=0)),
                ("encounter_count", models.PositiveIntegerField(default=0)),
                ("favorite_count", models.PositiveIntegerField(default=0)),
                ("filters", models.JSONField(default=dict)),
                ("session", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="drive_history", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-started_at", "-id"],
                "indexes": [models.Index(fields=["owner", "-started_at"], name="drive_owner_started_idx")],
                "constraints": [models.UniqueConstraint(fields=("owner", "client_session_id"), name="unique_owner_drive_session")],
            },
        ),
    ]
