from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("properties", "0011_scrapejob_retry_urls_scrapejobsource_error_urls"),
    ]

    operations = [
        migrations.AddField(
            model_name="property",
            name="inferred_neighborhood",
            field=models.CharField(blank=True, db_index=True, max_length=120),
        ),
        migrations.AddField(
            model_name="property",
            name="inferred_neighborhood_method",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="property",
            name="inferred_neighborhood_distance_m",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="property",
            name="zone_conflict",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="property",
            name="zone_needs_review",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="property",
            name="zone_inference_evidence",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="property",
            name="zone_inferred_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
