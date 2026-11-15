from django.apps import AppConfig


class PropertiesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "properties"
    verbose_name = "Доступний Дім — Об'єкти та аудит"

    def ready(self):
        try:
            from django.core.management import call_command
            call_command("makemigrations", "properties", interactive=False)
            call_command("migrate", interactive=False)
        except Exception as e:
            print(f"[AutoMigrate] Migration info: {e}")

        try:
            from .models import Property, ModerationStatus
            Property.objects.filter(is_published=True).update(
                moderation_status=ModerationStatus.APPROVED,
                is_verified_by_admin=True,
            )
        except Exception as e:
            print(f"[AutoApprove] Sync info: {e}")


