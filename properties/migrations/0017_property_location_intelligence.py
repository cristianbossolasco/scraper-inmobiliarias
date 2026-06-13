from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("properties", "0016_operation_jobs_and_scrape_mark_missing"),
    ]

    operations = [
        migrations.AlterField(
            model_name="operationjob",
            name="kind",
            field=models.CharField(
                choices=[
                    ("pipeline", "Pipeline"),
                    ("scrape", "Scraping"),
                    ("geocode", "Geocoding"),
                    ("infer_zones", "Inferencia de zonas"),
                    ("score_security", "Scoring seguridad"),
                    (
                        "score_location_intelligence",
                        "Scoring inteligencia territorial",
                    ),
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
        migrations.AlterField(
            model_name="operationjobstep",
            name="kind",
            field=models.CharField(
                choices=[
                    ("pipeline", "Pipeline"),
                    ("scrape", "Scraping"),
                    ("geocode", "Geocoding"),
                    ("infer_zones", "Inferencia de zonas"),
                    ("score_security", "Scoring seguridad"),
                    (
                        "score_location_intelligence",
                        "Scoring inteligencia territorial",
                    ),
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
        migrations.CreateModel(
            name="PropertyLocationIntelligence",
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
                    "overall_score",
                    models.FloatField(blank=True, db_index=True, null=True),
                ),
                ("level", models.CharField(blank=True, db_index=True, max_length=20)),
                (
                    "zone_name",
                    models.CharField(blank=True, db_index=True, max_length=120),
                ),
                (
                    "match_method",
                    models.CharField(
                        choices=[
                            ("coordinates", "Coordenadas"),
                            ("zone", "Zona inferida"),
                            ("none", "Sin match"),
                        ],
                        db_index=True,
                        default="none",
                        max_length=20,
                    ),
                ),
                (
                    "confidence",
                    models.CharField(blank=True, db_index=True, max_length=40),
                ),
                (
                    "transport_score",
                    models.FloatField(blank=True, db_index=True, null=True),
                ),
                (
                    "education_score",
                    models.FloatField(blank=True, db_index=True, null=True),
                ),
                (
                    "health_score",
                    models.FloatField(blank=True, db_index=True, null=True),
                ),
                (
                    "flood_penalty_score",
                    models.FloatField(blank=True, db_index=True, null=True),
                ),
                (
                    "urban_informality_score",
                    models.FloatField(blank=True, db_index=True, null=True),
                ),
                (
                    "environmental_penalty_score",
                    models.FloatField(blank=True, null=True),
                ),
                (
                    "development_potential_score",
                    models.FloatField(blank=True, null=True),
                ),
                (
                    "in_flood_risk_zone",
                    models.BooleanField(blank=True, db_index=True, null=True),
                ),
                ("nearest_renabap_m", models.FloatField(blank=True, null=True)),
                ("nearest_sube_point_m", models.FloatField(blank=True, null=True)),
                ("nearest_school_m", models.FloatField(blank=True, null=True)),
                (
                    "nearest_health_center_m",
                    models.FloatField(blank=True, null=True),
                ),
                ("components", models.JSONField(blank=True, default=dict)),
                ("risks", models.JSONField(blank=True, default=dict)),
                ("evidence", models.JSONField(blank=True, default=dict)),
                ("source_signature", models.CharField(blank=True, max_length=300)),
                (
                    "scored_at",
                    models.DateTimeField(blank=True, db_index=True, null=True),
                ),
                (
                    "property",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="location_intelligence",
                        to="properties.property",
                    ),
                ),
            ],
            options={
                "ordering": ["-overall_score", "zone_name"],
            },
        ),
        migrations.AddIndex(
            model_name="propertylocationintelligence",
            index=models.Index(
                fields=["overall_score", "level"],
                name="properties__locati_b9d0c4_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="propertylocationintelligence",
            index=models.Index(
                fields=["zone_name", "overall_score"],
                name="properties__locati_79cf14_idx",
            ),
        ),
    ]
