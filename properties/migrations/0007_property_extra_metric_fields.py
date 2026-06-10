from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("properties", "0006_property_detected_address_property_detected_latitude_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="property",
            name="toilets",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="property",
            name="uncovered_area",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name="property",
            name="semicovered_area",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name="property",
            name="front_width",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True),
        ),
        migrations.AddField(
            model_name="property",
            name="lot_depth",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True),
        ),
        migrations.AddField(
            model_name="property",
            name="building_floors",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
    ]
