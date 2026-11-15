import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('properties', '0014_assign_feature_categories'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='property',
            name='moderation_status',
            field=models.CharField(
                choices=[('pending', 'Очікує модерації'), ('approved', 'Схвалено'), ('rejected', 'Відхилено')],
                default='approved',
                help_text='Перевірка оголошення та фотографій житла на доступність/мобільність адміністратором.',
                max_length=20,
                verbose_name='Статус модерації',
            ),
        ),
        migrations.AddField(
            model_name='property',
            name='moderation_note',
            field=models.TextField(
                blank=True,
                help_text='Зауваження або причина відхилення оголошення',
                verbose_name='Коментар модератора',
            ),
        ),
        migrations.CreateModel(
            name='AuditRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=150, verbose_name="ПІБ або ім'я замовника")),
                ('phone', models.CharField(max_length=30, verbose_name='Контактний телефон')),
                ('email', models.EmailField(blank=True, max_length=254, verbose_name='Email')),
                ('property_address', models.CharField(max_length=500, verbose_name="Адреса об'єкта нерухомості")),
                ('comments', models.TextField(blank=True, verbose_name='Побажання / Коментар')),
                ('status', models.CharField(choices=[('new', 'Нова'), ('in_progress', 'В обробці'), ('completed', 'Завершено'), ('cancelled', 'Скасовано')], default='new', max_length=20, verbose_name='Статус обробки')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Дата створення')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Дата оновлення')),
                ('applicant', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='audit_requests', to=settings.AUTH_USER_MODEL, verbose_name='Користувач')),
                ('property', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='audit_requests', to='properties.property', verbose_name="Об'єкт нерухомості (якщо обрано)")),
            ],
            options={
                'verbose_name': 'Заявка на аудит',
                'verbose_name_plural': 'Заявки на аудит',
                'ordering': ['-created_at'],
            },
        ),
    ]
