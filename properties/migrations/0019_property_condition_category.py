from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("properties", "0018_listing_identity"),
    ]

    operations = [
        migrations.AddField(
            model_name="property",
            name="condition_category",
            field=models.CharField(
                choices=[
                    ("new", "A estrenar"),
                    ("renovated", "Refaccionada"),
                    ("used", "Usada"),
                    ("needs_work", "A refaccionar"),
                    ("unknown", "Sin dato"),
                ],
                db_index=True,
                default="unknown",
                max_length=20,
            ),
        ),
    ]
