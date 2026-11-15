import os
import django
from django.conf import settings

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.contrib.auth.models import User
users = User.objects.all()
for u in users:
    print(f"Username: {u.username}, Staff: {u.is_staff}, Superuser: {u.is_superuser}")
