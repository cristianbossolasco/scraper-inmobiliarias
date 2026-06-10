from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("properties", "0009_performance_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrapejob",
            name="start_page",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
