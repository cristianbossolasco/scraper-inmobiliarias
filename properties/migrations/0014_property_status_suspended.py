from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("properties", "0013_property_data_manually_corrected_at_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="property",
            name="status",
            field=models.CharField(
                choices=[
                    ("active", "Activa"),
                    ("reserved", "Reservada"),
                    ("sold", "Vendida"),
                    ("suspended", "Suspendida"),
                    ("removed", "Retirada"),
                ],
                db_index=True,
                default="active",
                max_length=20,
            ),
        ),
    ]
