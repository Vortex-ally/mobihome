"""
Створення / оновлення суперкористувача без збереження пароля у коді чи виводі.

    python create_admin.py <username> <email>

Пароль запитується інтерактивно (не відображається). Рекомендовано натомість:
    python manage.py createsuperuser
"""

import getpass
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.auth.password_validation import validate_password  # noqa: E402
from django.core.exceptions import ValidationError  # noqa: E402


def main():
    if len(sys.argv) < 3:
        print("Використання: python create_admin.py <username> <email>")
        sys.exit(1)
    username, email = sys.argv[1], sys.argv[2]
    password = getpass.getpass("Пароль: ")
    if password != getpass.getpass("Повторіть пароль: "):
        print("Паролі не збігаються.")
        sys.exit(1)

    User = get_user_model()
    user = User.objects.filter(username=username).first() or User(username=username)
    try:
        validate_password(password, user)
    except ValidationError as exc:
        print("Пароль не відповідає вимогам:", "; ".join(exc.messages))
        sys.exit(1)

    user.email = email
    user.is_staff = user.is_superuser = user.is_active = True
    user.set_password(password)
    user.save()
    print(f"[УСПІХ] Суперкористувача '{username}' збережено.")


if __name__ == "__main__":
    main()
