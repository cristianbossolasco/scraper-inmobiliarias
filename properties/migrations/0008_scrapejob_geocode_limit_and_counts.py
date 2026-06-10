from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("properties", "0007_property_extra_metric_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrapejob",
            name="geocode_limit",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="scrapejobsource",
            name="geocode_failed",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="scrapejobsource",
            name="geocode_pending",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="scrapejobsource",
            name="geocoded",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
