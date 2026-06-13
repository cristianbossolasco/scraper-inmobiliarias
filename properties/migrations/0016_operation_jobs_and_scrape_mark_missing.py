from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("properties", "0015_property_security_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrapejob",
            name="mark_missing",
            field=models.BooleanField(default=True),
        ),
        migrations.CreateModel(
            name="OperationJob",
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
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("pipeline", "Pipeline"),
                            ("scrape", "Scraping"),
                            ("geocode", "Geocoding"),
                            ("infer_zones", "Inferencia de zonas"),
                            ("score_security", "Scoring seguridad"),
                            ("repair_addresses", "Reparar direcciones"),
                            ("repair_neighborhoods", "Reparar barrios"),
                            ("repair_localities", "Reparar localidades"),
                            ("repair_agencies", "Reparar agencias"),
                            ("repair_metrics", "Reparar metricas"),
                            ("repair_merged_listings", "Separar fusiones"),
                            ("merge_properties", "Fusionar duplicados"),
                        ],
                        db_index=True,
                        default="pipeline",
                        max_length=40,
                    ),
                ),
                ("title", models.CharField(blank=True, max_length=160)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pendiente"),
                            ("running", "En curso"),
                            ("success", "Correcta"),
                            ("partial", "Parcial"),
                            ("failed", "Fallida"),
                            ("cancelled", "Cancelada"),
                            ("interrupted", "Interrumpida"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=20,
                    ),
                ),
                (
                    "mode",
                    models.CharField(
                        choices=[("dry_run", "Simulacion"), ("apply", "Aplicar")],
                        db_index=True,
                        default="dry_run",
                        max_length=16,
                    ),
                ),
                ("scope", models.JSONField(blank=True, default=dict)),
                ("params", models.JSONField(blank=True, default=dict)),
                ("result_summary", models.JSONField(blank=True, default=dict)),
                ("total_steps", models.PositiveIntegerField(default=0)),
                ("completed_steps", models.PositiveIntegerField(default=0)),
                ("processed", models.PositiveIntegerField(default=0)),
                ("changed", models.PositiveIntegerField(default=0)),
                ("errors", models.PositiveIntegerField(default=0)),
                ("logs", models.TextField(blank=True)),
                ("cancel_requested", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "source_job",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="apply_jobs",
                        to="properties.operationjob",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="OperationJobStep",
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
                ("order", models.PositiveSmallIntegerField(default=0)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("pipeline", "Pipeline"),
                            ("scrape", "Scraping"),
                            ("geocode", "Geocoding"),
                            ("infer_zones", "Inferencia de zonas"),
                            ("score_security", "Scoring seguridad"),
                            ("repair_addresses", "Reparar direcciones"),
                            ("repair_neighborhoods", "Reparar barrios"),
                            ("repair_localities", "Reparar localidades"),
                            ("repair_agencies", "Reparar agencias"),
                            ("repair_metrics", "Reparar metricas"),
                            ("repair_merged_listings", "Separar fusiones"),
                            ("merge_properties", "Fusionar duplicados"),
                        ],
                        max_length=40,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pendiente"),
                            ("running", "En curso"),
                            ("success", "Correcta"),
                            ("partial", "Parcial"),
                            ("failed", "Fallida"),
                            ("cancelled", "Cancelada"),
                            ("interrupted", "Interrumpida"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=20,
                    ),
                ),
                (
                    "mode",
                    models.CharField(
                        choices=[("dry_run", "Simulacion"), ("apply", "Aplicar")],
                        default="dry_run",
                        max_length=16,
                    ),
                ),
                ("params", models.JSONField(blank=True, default=dict)),
                ("total", models.PositiveIntegerField(default=0)),
                ("processed", models.PositiveIntegerField(default=0)),
                ("changed", models.PositiveIntegerField(default=0)),
                ("skipped", models.PositiveIntegerField(default=0)),
                ("errors", models.PositiveIntegerField(default=0)),
                ("logs", models.TextField(blank=True)),
                ("error_log", models.TextField(blank=True)),
                ("result_summary", models.JSONField(blank=True, default=dict)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "job",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="steps",
                        to="properties.operationjob",
                    ),
                ),
            ],
            options={
                "ordering": ["job_id", "order", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="operationjobstep",
            constraint=models.UniqueConstraint(
                fields=("job", "order"), name="unique_operation_step_order"
            ),
        ),
    ]
