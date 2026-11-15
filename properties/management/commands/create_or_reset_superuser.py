"""
Management-команда для створення або скидання пароля суперюзера у Django.
Використання:
    python manage.py create_or_reset_superuser [--username admin] [--password admin] [--email admin@example.com]
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = "Створює нового суперюзера або скидає пароль існуючого адміністратора."

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            type=str,
            default="admin",
            help="Логін суперюзера (за замовчуванням: admin)",
        )
        parser.add_argument(
            "--password",
            type=str,
            default="admin12345",
            help="Пароль суперюзера (за замовчуванням: admin12345)",
        )
        parser.add_argument(
            "--email",
            type=str,
            default="admin@mobihome.local",
            help="Email суперюзера (за замовчуванням: admin@mobihome.local)",
        )

    def handle(self, *args, **options):
        username = options["username"].strip()
        password = options["password"].strip()
        email = options["email"].strip()

        user = User.objects.filter(username=username).first()
        if user:
            user.set_password(password)
            user.email = email
            user.is_staff = True
            user.is_superuser = True
            user.is_active = True
            user.save()
            self.stdout.write(
                self.style.SUCCESS(
                    f"УСПІХ: Пароль та права адміністратора для користувача '{username}' успішно ОНОВЛЕНО!\n"
                    f"Логін: {username}\nПароль: {password}\nEmail: {email}"
                )
            )
        else:
            user = User.objects.create_superuser(
                username=username,
                email=email,
                password=password,
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"УСПІХ: Нового суперюзера '{username}' СТВОРЕНО!\n"
                    f"Логін: {username}\nПароль: {password}\nEmail: {email}"
                )
            )
