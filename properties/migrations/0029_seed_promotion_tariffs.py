from decimal import Decimal

from django.db import migrations

# code, назва, опис, ціна, днів (для відображення/обмеження БД), годин (точний термін) або None, ранг
PROMOTION_TARIFFS = [
    ("top1_7d", "ТОП №1", "Найвища позиція в каталозі на 7 днів.", "200.00", 7, None, 1),
    ("top1_1d", "ТОП №1 на добу", "Найвища позиція в каталозі на 24 години.", "100.00", 1, 24, 1),
    ("top3_5d", "ТОП №3", "Позиція в блоці ТОП одразу після ТОП №1 на 5 днів.", "120.00", 5, None, 3),
    ("active_12h", "Активна публікація", "Оголошення піднімається над звичайними на 12 годин.", "80.00", 1, 12, 5),
]


def seed(apps, schema_editor):
    Tariff = apps.get_model("properties", "Tariff")
    for code, name, description, price, days, hours, rank in PROMOTION_TARIFFS:
        # Наявний тариф (напр. змінений адміністратором) не перезаписуємо.
        Tariff.objects.get_or_create(code=code, defaults={
            "name": name, "description": description, "price": Decimal(price), "currency": "UAH",
            "duration_days": days, "duration_hours": hours, "promotion_rank": rank, "is_active": True,
        })


def unseed(apps, schema_editor):
    Tariff = apps.get_model("properties", "Tariff")
    Payment = apps.get_model("properties", "Payment")
    # Відкат не видаляє тариф, на який уже є платежі.
    Tariff.objects.filter(code__in=[t[0] for t in PROMOTION_TARIFFS]).exclude(
        pk__in=Payment.objects.values("tariff_id")).delete()


class Migration(migrations.Migration):
    dependencies = [("properties", "0028_promotion_tariffs")]
    operations = [migrations.RunPython(seed, unseed)]
