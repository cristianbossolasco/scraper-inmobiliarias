from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("properties", "0014_property_status_suspended"),
    ]

    operations = [
        migrations.AddField(
            model_name="property",
            name="security_coverage_score",
            field=models.FloatField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="property",
            name="security_risk_score",
            field=models.FloatField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="property",
            name="security_level",
            field=models.CharField(blank=True, db_index=True, max_length=20),
        ),
        migrations.AddField(
            model_name="property",
            name="security_zone_label",
            field=models.CharField(blank=True, db_index=True, max_length=120),
        ),
        migrations.AddField(
            model_name="property",
            name="security_source",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="property",
            name="security_evidence",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="property",
            name="security_scored_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
