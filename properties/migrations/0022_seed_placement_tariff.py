from decimal import Decimal

from django.db import migrations


def seed_tariff(apps, schema_editor):
    Tariff = apps.get_model("properties", "Tariff")
    # Якщо тариф уже існує (напр. змінений адміністратором) — не перезаписуємо.
    Tariff.objects.get_or_create(
        code="placement",
        defaults={
            "name": "Розміщення квартири",
            "price": Decimal("99.00"),
            "currency": "UAH",
            "duration_days": 30,
            "is_active": True,
        },
    )


def unseed_tariff(apps, schema_editor):
    Tariff = apps.get_model("properties", "Tariff")
    Payment = apps.get_model("properties", "Payment")
    # Відкат не видаляє тариф, якщо на нього вже є платежі.
    Tariff.objects.filter(code="placement").exclude(
        pk__in=Payment.objects.values("tariff_id")
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("properties", "0021_placement_payments_and_integrity")]
    operations = [migrations.RunPython(seed_tariff, unseed_tariff)]
