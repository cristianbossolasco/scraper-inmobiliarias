import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("properties", "0023_scrapejobsource_discovery_finished_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrapejob",
            name="from_latest_discovery",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="scrapejob",
            name="phases",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="scrapejob",
            name="reprocess_mode",
            field=models.CharField(
                choices=[
                    ("incomplete", "Incompletas"),
                    ("stale", "Antiguas"),
                    ("all", "Todas"),
                ],
                default="all",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="scrapejob",
            name="reprocess_stale_days",
            field=models.PositiveIntegerField(default=30),
        ),
        migrations.CreateModel(
            name="ScrapeJobListing",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("external_id", models.CharField(max_length=160)),
                ("url", models.URLField(max_length=1000)),
                ("position", models.PositiveIntegerField(default=0)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("discovered", "Descubierta"),
                            ("new_pending", "Nueva pendiente"),
                            ("existing_pending", "Existente pendiente"),
                            ("processed", "Procesada"),
                            ("skipped_existing", "Existente omitida"),
                            ("gone", "Retirada"),
                            ("error", "Error"),
                        ],
                        db_index=True,
                        default="discovered",
                        max_length=24,
                    ),
                ),
                (
                    "discovered_at",
                    models.DateTimeField(db_index=True, default=django.utils.timezone.now),
                ),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("error", models.TextField(blank=True)),
                (
                    "job_source",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="snapshot_listings",
                        to="properties.scrapejobsource",
                    ),
                ),
                (
                    "listing",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="scrape_job_items",
                        to="properties.listing",
                    ),
                ),
                (
                    "source",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="scrape_job_listings",
                        to="properties.source",
                    ),
                ),
            ],
            options={
                "ordering": ["job_source_id", "position", "id"],
                "indexes": [
                    models.Index(fields=["source", "status"], name="scrape_item_src_status_idx"),
                    models.Index(fields=["job_source", "status"], name="scrape_item_job_status_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("job_source", "external_id"),
                        name="unique_scrape_job_listing",
                    )
                ],
            },
        ),
    ]
