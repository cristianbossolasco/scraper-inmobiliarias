from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("properties", "0008_scrapejob_geocode_limit_and_counts"),
    ]

    operations = [
        migrations.AlterField(
            model_name="property",
            name="operation",
            field=models.CharField(db_index=True, default="sale", max_length=20),
        ),
        migrations.AlterField(
            model_name="property",
            name="bathrooms",
            field=models.DecimalField(
                blank=True, db_index=True, decimal_places=1, max_digits=4, null=True
            ),
        ),
        migrations.AlterField(
            model_name="property",
            name="garages",
            field=models.PositiveSmallIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AlterField(
            model_name="property",
            name="covered_area",
            field=models.DecimalField(
                blank=True, db_index=True, decimal_places=2, max_digits=10, null=True
            ),
        ),
        migrations.AlterField(
            model_name="property",
            name="land_area",
            field=models.DecimalField(
                blank=True, db_index=True, decimal_places=2, max_digits=10, null=True
            ),
        ),
        migrations.AlterField(
            model_name="property",
            name="last_seen_at",
            field=models.DateTimeField(auto_now=True, db_index=True),
        ),
        migrations.AddIndex(
            model_name="property",
            index=models.Index(
                fields=["operation", "is_hidden", "last_seen_at"],
                name="properties__operat_47d71a_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="property",
            index=models.Index(
                fields=["operation", "price"],
                name="properties__operat_976d5e_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="property",
            index=models.Index(
                fields=["operation", "land_area"],
                name="properties__operat_224f57_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="property",
            index=models.Index(
                fields=["operation", "covered_area"],
                name="properties__operat_86aa71_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="listing",
            index=models.Index(
                fields=["property", "active"],
                name="properties__propert_c585c9_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="listing",
            index=models.Index(
                fields=["agency", "active"],
                name="properties__agency__9a4f75_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="listing",
            index=models.Index(
                fields=["source", "active"],
                name="properties__source__e7ad06_idx",
            ),
        ),
    ]
