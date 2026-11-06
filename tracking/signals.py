import logging

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import ShipmentPhoto

log = logging.getLogger(__name__)


@receiver(post_delete, sender=ShipmentPhoto)
def delete_photo_file(sender, instance, **kwargs):
    """Remove the image from disk once a photo (or its shipment) deletion is committed."""
    if not instance.image:
        return
    storage, name = instance.image.storage, instance.image.name

    def _remove():
        try:
            storage.delete(name)
        except OSError as exc:  # e.g. file locked by an in-flight download on Windows
            log.warning("Could not delete %s: %s", name, exc)

    transaction.on_commit(_remove)
