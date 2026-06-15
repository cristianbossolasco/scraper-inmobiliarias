from collections import Counter

from properties.models import Property, PropertyLocationIntelligence
from properties.services.zone_names import (
    UNIFIED_HURLINGHAM_CENTRO_ZONE,
    is_unified_hurlingham_centro_alias,
)


PROPERTY_ZONE_FIELDS = ("neighborhood", "detected_neighborhood", "inferred_neighborhood")


def canonicalize_hurlingham_centro_value(value):
    if isinstance(value, str) and is_unified_hurlingham_centro_alias(value):
        return UNIFIED_HURLINGHAM_CENTRO_ZONE
    return value


def _replace_aliases(value):
    if isinstance(value, str):
        return canonicalize_hurlingham_centro_value(value)
    if isinstance(value, dict):
        return {key: _replace_aliases(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_aliases(item) for item in value]
    return value


def backfill_hurlingham_centro_zone(*, dry_run=False, property_ids=None):
    counters = Counter()
    changed_properties = []
    queryset = Property.objects.all().order_by("pk")
    if property_ids:
        queryset = queryset.filter(pk__in=property_ids)

    for property_obj in queryset:
        update_fields = []
        for field in PROPERTY_ZONE_FIELDS:
            current = getattr(property_obj, field) or ""
            replacement = canonicalize_hurlingham_centro_value(current)
            if replacement != current:
                setattr(property_obj, field, replacement)
                update_fields.append(field)
                counters[f"property_{field}"] += 1

        for json_field in ("manual_overrides", "zone_inference_evidence"):
            current = getattr(property_obj, json_field)
            replacement = _replace_aliases(current)
            if replacement != current:
                setattr(property_obj, json_field, replacement)
                update_fields.append(json_field)
                counters[f"property_{json_field}"] += 1

        if update_fields:
            counters["properties"] += 1
            changed_properties.append(property_obj.pk)
            if not dry_run:
                property_obj.save(update_fields=sorted(set(update_fields)))

    intelligence_queryset = PropertyLocationIntelligence.objects.all().order_by("pk")
    if property_ids:
        intelligence_queryset = intelligence_queryset.filter(property_id__in=property_ids)

    for record in intelligence_queryset:
        update_fields = []
        replacement = canonicalize_hurlingham_centro_value(record.zone_name or "")
        if replacement != (record.zone_name or ""):
            record.zone_name = replacement
            update_fields.append("zone_name")
            counters["location_intelligence_zone_name"] += 1
        evidence = _replace_aliases(record.evidence)
        if evidence != record.evidence:
            record.evidence = evidence
            update_fields.append("evidence")
            counters["location_intelligence_evidence"] += 1
        if update_fields:
            counters["location_intelligence"] += 1
            if not dry_run:
                record.save(update_fields=sorted(set(update_fields)))

    return {
        "canonical_name": UNIFIED_HURLINGHAM_CENTRO_ZONE,
        "dry_run": dry_run,
        "changed_property_ids": changed_properties,
        "counts": dict(counters),
    }
