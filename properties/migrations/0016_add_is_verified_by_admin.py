import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


def set_existing_properties_verified(apps, schema_editor):
    Property = apps.get_model('properties', 'Property')
    now = timezone.now()
    Property.objects.filter(is_published=True).update(
        is_verified_by_admin=True,
        verified_at=now,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('properties', '0015_add_moderation_and_auditrequest'),
    ]

    operations = [
        migrations.AddField(
            model_name='property',
            name='is_verified_by_admin',
            field=models.BooleanField(
                default=False,
                help_text='Оголошення та фотографії підтверджені адміністратором.',
                verbose_name='Фото перевірено адміном',
            ),
        ),
        migrations.AddField(
            model_name='property',
            name='verified_at',
            field=models.DateTimeField(
                blank=True,
                help_text='Дата та час підтвердження фотомодерації адміністратором.',
                null=True,
                verbose_name='Дата перевірки фото',
            ),
        ),
        migrations.RunPython(set_existing_properties_verified, reverse_code=migrations.RunPython.noop),
    ]
