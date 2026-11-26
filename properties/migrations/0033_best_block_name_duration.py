from django.db import migrations


def apply(apps, schema_editor):
    # Назва й рекомендована тривалість (7 днів) тарифу 99 грн. Продаж (is_active) НЕ вмикається:
    # його вмикає адміністратор лише після підтвердження замовника.
    Tariff = apps.get_model("properties", "Tariff")
    Tariff.objects.filter(code="best_block").update(
        name="Найкращі оголошення", duration_days=7, duration_hours=None, promotion_rank=4,
    )


def revert(apps, schema_editor):
    Tariff = apps.get_model("properties", "Tariff")
    Tariff.objects.filter(code="best_block").update(name="Блок найкращих оголошень")


class Migration(migrations.Migration):
    dependencies = [("properties", "0032_seed_best_block_tariff")]
    operations = [migrations.RunPython(apply, revert)]
