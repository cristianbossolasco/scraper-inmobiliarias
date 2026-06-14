from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("properties", "0020_backfill_property_condition_category"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="property",
            index=models.Index(
                fields=["operation", "status", "is_hidden", "last_seen_at"],
                name="prop_op_status_hidden_seen_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="property",
            index=models.Index(
                fields=["property_type", "condition_category", "age_years"],
                name="prop_type_cond_age_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="listing",
            index=models.Index(
                fields=["source", "source_status", "active"],
                name="listing_src_status_active_idx",
            ),
        ),
    ]
