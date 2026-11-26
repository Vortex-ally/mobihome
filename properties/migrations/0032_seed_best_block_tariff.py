from decimal import Decimal

from django.db import migrations


def seed(apps, schema_editor):
    Tariff = apps.get_model("properties", "Tariff")
    # Тривалість замовник не підтвердив: тариф створюється ВИМКНЕНИМ (не продається) — після підтвердження
    # адміністратор задає термін і вмикає «Доступний для оплати» в адмінці «Тарифи».
    Tariff.objects.get_or_create(code="best_block", defaults={
        "name": "Блок найкращих оголошень",
        "description": "Показ у блоці найкращих оголошень на головній і вище звичайних у каталозі.",
        "price": Decimal("99.00"), "currency": "UAH", "duration_days": 7, "duration_hours": None,
        "promotion_rank": 4, "is_active": False,
    })


def unseed(apps, schema_editor):
    Tariff = apps.get_model("properties", "Tariff")
    Payment = apps.get_model("properties", "Payment")
    Tariff.objects.filter(code="best_block").exclude(pk__in=Payment.objects.values("tariff_id")).delete()


class Migration(migrations.Migration):
    dependencies = [("properties", "0031_promotion_rank_best")]
    operations = [migrations.RunPython(seed, unseed)]
