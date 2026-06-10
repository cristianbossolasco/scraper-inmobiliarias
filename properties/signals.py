from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from .models import LocationHistory, Property, PropertyLocation
from .services.indexes import (
    remove_location_rtree,
    remove_property_fts,
    sync_location_rtree,
    sync_property_fts,
)


@receiver(post_save, sender=Property)
def update_fts(sender, instance, **kwargs):
    sync_property_fts(instance)


@receiver(post_delete, sender=Property)
def delete_fts(sender, instance, **kwargs):
    remove_property_fts(instance.pk)


@receiver(pre_save, sender=PropertyLocation)
def preserve_location_history(sender, instance, **kwargs):
    if not instance.pk:
        return
    previous = PropertyLocation.objects.filter(pk=instance.pk).first()
    if previous and (
        previous.latitude != instance.latitude
        or previous.longitude != instance.longitude
        or previous.precision != instance.precision
    ):
        LocationHistory.objects.create(
            property=instance.property,
            latitude=previous.latitude,
            longitude=previous.longitude,
            precision=previous.precision,
            provider=previous.provider,
        )


@receiver(post_save, sender=PropertyLocation)
def update_rtree(sender, instance, **kwargs):
    sync_location_rtree(instance)


@receiver(post_delete, sender=PropertyLocation)
def delete_rtree(sender, instance, **kwargs):
    remove_location_rtree(instance.property_id)
