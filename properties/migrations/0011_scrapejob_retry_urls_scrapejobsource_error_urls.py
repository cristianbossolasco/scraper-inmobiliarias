from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("properties", "0010_scrapejob_start_page"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrapejob",
            name="retry_urls",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="scrapejobsource",
            name="error_urls",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
