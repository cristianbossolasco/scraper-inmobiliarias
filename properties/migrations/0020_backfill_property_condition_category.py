import re
import unicodedata

from django.db import migrations


CONDITION_PATTERNS = (
    (
        "needs_work",
        (
            r"\ba\s+refaccionar\b",
            r"\bpara\s+refaccionar\b",
            r"\ba\s+reciclar\b",
            r"\bpara\s+reciclar\b",
            r"\ba\s+demoler\b",
            r"\bpara\s+demoler\b",
            r"\bmal\s+estado\b",
            r"\bdeteriorad",
        ),
    ),
    (
        "new",
        (
            r"\ba\s+estrenar\b",
            r"\bestrenar\b",
            r"\bobra\s+nueva\b",
            r"\bpozo\b",
        ),
    ),
    (
        "renovated",
        (
            r"\brefaccionad",
            r"\breciclad",
            r"\bremodelad",
            r"\brenovad",
            r"\bimpecable\b",
            r"\bexcelente\s+estado\b",
            r"\bactualizad",
        ),
    ),
    (
        "used",
        (
            r"\busad[ao]\b",
            r"\bbuen\s+estado\b",
            r"\bmuy\s+buen\s+estado\b",
            r"\bantigu[ao]\b",
        ),
    ),
)


def fold_text(value):
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", value.lower()).strip()


def infer_condition(title, description, features):
    text = fold_text(" ".join([title or "", description or "", " ".join(features or [])]))
    if not text:
        return "unknown"
    for category, patterns in CONDITION_PATTERNS:
        if any(re.search(pattern, text, re.I) for pattern in patterns):
            return category
    return "unknown"


def backfill_condition_category(apps, schema_editor):
    Property = apps.get_model("properties", "Property")
    for property_obj in Property.objects.filter(condition_category="unknown").iterator():
        manual_overrides = property_obj.manual_overrides or {}
        if "condition_category" in manual_overrides:
            continue
        condition = infer_condition(
            property_obj.title,
            property_obj.description,
            property_obj.features,
        )
        if condition != "unknown":
            property_obj.condition_category = condition
            property_obj.save(update_fields=["condition_category"])


class Migration(migrations.Migration):

    dependencies = [
        ("properties", "0019_property_condition_category"),
    ]

    operations = [
        migrations.RunPython(backfill_condition_category, migrations.RunPython.noop),
    ]
