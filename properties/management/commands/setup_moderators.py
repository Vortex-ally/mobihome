"""
Створює/оновлює групу «Модератори» з мінімальними правами (ідемпотентно; повторний запуск безпечний):

    python manage.py setup_moderators
    python manage.py setup_moderators --add olena --add petro          # наявні акаунти -> модератори (is_staff)
    python manage.py setup_moderators --telegram olena=123456789         # прив'язати Telegram user ID (число)

Користувачів і паролів команда не створює. Модератор НЕ отримує доступу до користувачів, груп, платежів,
тарифів, налаштувань і видалення оголошень; у формі оголошення змінює лише поля модерації (див. admin.py).
"""

from django.contrib.auth.models import Group, Permission, User
from django.core.management.base import BaseCommand, CommandError

from properties.moderation import MODERATORS_GROUP
from properties.models import UserProfile

MODERATOR_PERMISSIONS = [
    "view_property", "change_property", "moderate_property",
    "view_moderationqueue", "change_moderationqueue",
    "view_propertyphoto", "view_feature",
    "view_accessibilityaudit", "add_accessibilityaudit", "change_accessibilityaudit",
    "view_listingverification", "add_listingverification", "change_listingverification",
    "view_moderationevent",
]


class Command(BaseCommand):
    help = "Створює групу «Модератори» з мінімальними правами; додає наявних користувачів."

    def add_arguments(self, parser):
        parser.add_argument("--add", action="append", default=[], metavar="USERNAME",
                            help="Додати наявного користувача до модераторів (стане is_staff).")
        parser.add_argument("--telegram", action="append", default=[], metavar="USERNAME=TELEGRAM_ID",
                            help="Прив'язати числовий Telegram user ID модератора.")

    def handle(self, *args, **options):
        group, created = Group.objects.get_or_create(name=MODERATORS_GROUP)
        perms = list(Permission.objects.filter(content_type__app_label="properties", codename__in=MODERATOR_PERMISSIONS))
        missing = set(MODERATOR_PERMISSIONS) - {p.codename for p in perms}
        if missing:
            raise CommandError(f"Не знайдено дозволи (застосуйте міграції): {', '.join(sorted(missing))}")
        group.permissions.set(perms)  # рівно цей набір — зайві права прибираються
        self.stdout.write(f"Група «{MODERATORS_GROUP}» {'створена' if created else 'оновлена'}: {len(perms)} дозволів.")

        for username in options["add"]:
            user = User.objects.filter(username=username).first()
            if user is None:
                raise CommandError(f"Користувача «{username}» не знайдено (створіть його через адмінку).")
            user.is_staff = True
            user.save(update_fields=["is_staff"])
            user.groups.add(group)
            self.stdout.write(f"  {username}: модератор (superuser: {'так' if user.is_superuser else 'ні'}).")

        for pair in options["telegram"]:
            username, _, tg_id = pair.partition("=")
            if not tg_id.strip().isdigit():
                raise CommandError(f"Некоректний Telegram ID у «{pair}» — потрібне число (user ID, не @username).")
            user = User.objects.filter(username=username.strip()).first()
            if user is None:
                raise CommandError(f"Користувача «{username}» не знайдено.")
            UserProfile.objects.update_or_create(user=user, defaults={"telegram_id": tg_id.strip()})
            self.stdout.write(f"  {username}: Telegram ID прив'язано.")
