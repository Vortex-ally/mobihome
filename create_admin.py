import os
import sys
import django

# Налаштування Django середовища
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model

def main():
    User = get_user_model()
    username = sys.argv[1] if len(sys.argv) > 1 else "admin"
    password = sys.argv[2] if len(sys.argv) > 2 else "admin12345"
    email = sys.argv[3] if len(sys.argv) > 3 else "admin@mobihome.local"

    user = User.objects.filter(username=username).first()
    if user:
        user.set_password(password)
        user.email = email
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True
        user.save()
        print(f"[УСПІХ] Пароль та права суперюзера '{username}' оновлено!")
    else:
        user = User.objects.create_superuser(username=username, email=email, password=password)
        print(f"[УСПІХ] Нового суперюзера '{username}' створено!")

    print(f"Логін:  {username}")
    print(f"Пароль: {password}")
    print(f"Email:  {email}")

if __name__ == "__main__":
    main()
