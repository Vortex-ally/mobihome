"""Налаштування для тестів: production-подібні (DEBUG=False), але без HTTPS-редиректу."""

import os
import tempfile

os.environ.setdefault("DJANGO_DEBUG", "False")
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key-not-for-production-0123456789abcdef")
os.environ.setdefault("DJANGO_SECURE_SSL_REDIRECT", "False")
os.environ.setdefault("DJANGO_SECURE_COOKIES", "False")
os.environ.setdefault("DJANGO_SQLITE_PATH", os.path.join(tempfile.gettempdir(), "mobihome-test.sqlite3"))

from .settings import *  # noqa: E402,F401,F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
MEDIA_ROOT = tempfile.mkdtemp(prefix="mobihome-test-media-")
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
GOOGLE_CLIENT_ID = ""
GOOGLE_CLIENT_SECRET = ""
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_BOT_NAME = ""
LOGGING = {"version": 1, "disable_existing_loggers": False, "root": {"level": "CRITICAL"}}
PAYMENTS_ENABLED = False
PLACEMENT_PAYMENT_REQUIRED = False
LIQPAY_PUBLIC_KEY = ""
LIQPAY_PRIVATE_KEY = ""
LIQPAY_SANDBOX = True
SITE_URL = "https://testserver.example"
