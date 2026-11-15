from django.apps import AppConfig


class PropertiesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "properties"
    verbose_name = "Доступний Дім — Об'єкти та аудит"

    # Міграції застосовуються явно: `python manage.py migrate`.
    # Раніше тут виконувались makemigrations/migrate та масове схвалення оголошень
    # при кожному старті — це обходило модерацію і ламало розгортання.
