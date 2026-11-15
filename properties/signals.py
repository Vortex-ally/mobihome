"""Сигнали: запуск оплачених періодів у момент публікації оголошення."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Property

# Збереження, після яких перевіряти активацію не потрібно (лічильник переглядів, сама активація).
_SKIP_FIELDS = ({"view_count"}, {"placement_until", "updated_at"})


@receiver(post_save, sender=Property)
def activate_paid_placement_on_publication(sender, instance, update_fields=None, **kwargs):
    if update_fields is not None and set(update_fields) in [set(f) for f in _SKIP_FIELDS]:
        return
    from .placement import activate_on_publication, is_publishable

    if is_publishable(instance):
        activate_on_publication(instance.pk)
