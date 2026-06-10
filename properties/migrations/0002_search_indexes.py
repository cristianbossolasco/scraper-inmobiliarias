from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("properties", "0001_initial")]

    operations = [
        migrations.RunSQL(
            sql="""
                CREATE VIRTUAL TABLE property_fts
                USING fts5(content, tokenize='unicode61 remove_diacritics 2');
                CREATE VIRTUAL TABLE property_location_rtree
                USING rtree(id, min_lat, max_lat, min_lng, max_lng);
            """,
            reverse_sql="""
                DROP TABLE IF EXISTS property_fts;
                DROP TABLE IF EXISTS property_location_rtree;
            """,
        )
    ]
