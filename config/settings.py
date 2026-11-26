"""
Django settings for «Доступний Дім» project.

Усі секрети та параметри середовища читаються зі змінних оточення
(або з файлу `.env` у корені проєкту). Приклад: `.env.example`.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path):
    """Мінімальний завантажувач .env (KEY=VALUE). Не перезаписує вже задані змінні."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_env_file(BASE_DIR / ".env")


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        # Лише для локальної розробки. У production ключ обов'язково задається через env.
        SECRET_KEY = "dev-only-insecure-key-do-not-use-in-production"
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY не задано. Створіть .env на основі .env.example "
            "або встановіть DJANGO_DEBUG=True для локальної розробки."
        )

ALLOWED_HOSTS = env_list(
    "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]" if DEBUG else ""
)
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

# База даних: SQLite. Шлях можна перевизначити через DJANGO_SQLITE_PATH.
DATABASE_PATH = Path(os.environ.get("DJANGO_SQLITE_PATH", BASE_DIR / "data" / "db.sqlite3"))
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "properties",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Статика (зокрема CSS/JS адмінки) роздається й при DEBUG=False — runserver/gunicorn без nginx.
    # За nginx /static/ обслуговує nginx, запит сюди не доходить.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "properties.context_processors.current_url_name",
                "properties.context_processors.site_contacts",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATABASE_PATH,
    }
}
# PostgreSQL (production): DJANGO_DB_ENGINE=postgresql + параметри підключення. Потрібен пакет
# psycopg (pip install "psycopg[binary]"). Без змінної — SQLite, як і раніше.
if os.environ.get("DJANGO_DB_ENGINE", "").lower() in ("postgresql", "postgres"):
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DJANGO_DB_NAME", "mobihome"),
        "USER": os.environ.get("DJANGO_DB_USER", "mobihome"),
        "PASSWORD": os.environ.get("DJANGO_DB_PASSWORD", ""),
        "HOST": os.environ.get("DJANGO_DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("DJANGO_DB_PORT", "5432"),
        "CONN_MAX_AGE": int(os.environ.get("DJANGO_DB_CONN_MAX_AGE", "60")),
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "uk"
TIME_ZONE = "Europe/Kyiv"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
STATIC_ROOT = Path(os.environ.get("DJANGO_STATIC_ROOT", BASE_DIR / "staticfiles"))
# WhiteNoise шукає файли й у застосунках (без обов'язкового collectstatic локально); у production
# після collectstatic файли беруться зі STATIC_ROOT. Стиснення/кешування — без зміни імен файлів.
WHITENOISE_USE_FINDERS = True
WHITENOISE_MAX_AGE = 3600
# Медіа (фото оголошень) через Django — для локального запуску з DEBUG=False. У production
# /media/ віддає nginx (deploy/nginx-mobihome.conf), тож цей маршрут туди не доходить.
SERVE_MEDIA = env_bool("DJANGO_SERVE_MEDIA", True)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MEDIA_URL = "media/"
MEDIA_ROOT = Path(os.environ.get("DJANGO_MEDIA_ROOT", BASE_DIR / "media"))
# Приватні файли (звіти перевірок житла): поза MEDIA_ROOT, не роздаються вебсервером.
PRIVATE_MEDIA_ROOT = Path(os.environ.get("DJANGO_PRIVATE_MEDIA_ROOT", BASE_DIR / "data" / "private"))

# Пошта (для скидання пароля). У розробці — у консоль; у production — SMTP через env.
EMAIL_BACKEND = os.environ.get(
    "DJANGO_EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend"
    if DEBUG
    else "django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = os.environ.get("DJANGO_EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("DJANGO_EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("DJANGO_EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("DJANGO_EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("DJANGO_EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = os.environ.get("DJANGO_DEFAULT_FROM_EMAIL", "mobi Home <noreply@localhost>")

LOGIN_URL = "/auth/"

# --- Безпека (HTTPS, cookies) ---
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", not DEBUG)
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", not DEBUG)
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
if env_bool("DJANGO_BEHIND_PROXY", False):
    # Лише якщо сайт стоїть за reverse-proxy (nginx), який виставляє цей заголовок.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Обмеження розміру завантажень (10 МБ на запит у пам'яті)
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

# Контакти сайту (підвал, віджет підтримки, сторінка «Контакти», листи). За замовчуванням — контакти,
# затверджені замовником; змінюються в .env без змін коду. Телефон — у міжнародному форматі (+380…).
SITE_PHONE = os.environ.get("SITE_PHONE", "+380991053309")
SITE_OFFICE_PHONE = os.environ.get("SITE_OFFICE_PHONE", "+380991053309")
SITE_EMAIL = os.environ.get("SITE_EMAIL", "dominikyarem@gmail.com")
SITE_SUPPORT_EMAIL = os.environ.get("SITE_SUPPORT_EMAIL", "dominikyarem@gmail.com")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "{asctime} {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

# --- Соціальна авторизація ---
# Google OAuth 2.0 (справжній). Порожні значення = вхід через Google вимкнено.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

# Telegram Login Widget. Порожній токен = вхід через Telegram вимкнено.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_NAME = os.environ.get("TELEGRAM_BOT_NAME", "")
# Telegram-бот модерації (той самий токен бота). Без увімкнення/чату — нічого не надсилається.
TELEGRAM_BOT_ENABLED = env_bool("TELEGRAM_BOT_ENABLED", False)
TELEGRAM_MODERATION_CHAT_ID = os.environ.get("TELEGRAM_MODERATION_CHAT_ID", "").strip()

# Дія: тимчасово вимкнено до появи справжньої інтеграції.
DIIA_ENABLED = False

# --- Оплата розміщення (LiqPay) ---
# Два незалежні перемикачі з різним призначенням (див. .env.example):
#   PAYMENTS_ENABLED           — користувачі МОЖУТЬ оплатити розміщення (працює checkout).
#                                Фактично активний лише якщо LiqPay коректно налаштовано.
#   PLACEMENT_PAYMENT_REQUIRED — каталог ПОКАЗУЄ лише оплачені оголошення.
#                                Не можна вмикати без PAYMENTS_ENABLED (перевіряється system check).
PAYMENTS_ENABLED = env_bool("PAYMENTS_ENABLED", False)
PLACEMENT_PAYMENT_REQUIRED = env_bool("PLACEMENT_PAYMENT_REQUIRED", False)

LIQPAY_PUBLIC_KEY = os.environ.get("LIQPAY_PUBLIC_KEY", "")
LIQPAY_PRIVATE_KEY = os.environ.get("LIQPAY_PRIVATE_KEY", "")
# True — тестовий режим (ключі sandbox_*, статус «sandbox» приймається). False — бойовий.
LIQPAY_SANDBOX = env_bool("LIQPAY_SANDBOX", True)
# Актуальна документація LiqPay: version=7, підпис base64(sha3-256(private+data+private)).
# Старі приклади/SDK: version=3 + sha1. Перемикається лише за результатами sandbox-перевірки.
LIQPAY_API_VERSION = int(os.environ.get("LIQPAY_API_VERSION", "7"))
LIQPAY_SIGNATURE_ALGORITHM = os.environ.get("LIQPAY_SIGNATURE_ALGORITHM", "sha3_256")
LIQPAY_CHECKOUT_URL = "https://www.liqpay.ua/api/3/checkout"
LIQPAY_API_URL = "https://www.liqpay.ua/api/request"
# Скільки хвилин дається на завершення оплати (expired_date для LiqPay).
LIQPAY_PAYMENT_TTL_MINUTES = int(os.environ.get("LIQPAY_PAYMENT_TTL_MINUTES", "60"))
# Публічна адреса сайту (https://домен) для server_url/result_url.
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")

# Fail-fast: неправильна production-конфігурація оплати зупиняє запуск (gunicorn/manage.py).
# У DEBUG (локальна розробка) ті самі проблеми показує `manage.py check`, але запуск не блокується.
if not DEBUG:
    from properties.payment_config import startup_problems as _payment_startup_problems

    _problems = _payment_startup_problems(
        payments_enabled=PAYMENTS_ENABLED,
        placement_required=PLACEMENT_PAYMENT_REQUIRED,
        liqpay_kwargs=dict(
            public_key=LIQPAY_PUBLIC_KEY, private_key=LIQPAY_PRIVATE_KEY, sandbox=LIQPAY_SANDBOX,
            version=LIQPAY_API_VERSION, algorithm=LIQPAY_SIGNATURE_ALGORITHM,
            site_url=SITE_URL, debug=DEBUG,
        ),
    )
    if _problems:
        raise ImproperlyConfigured("Некоректна конфігурація оплати: " + " | ".join(_problems))

# Обмеження спроб входу
LOGIN_THROTTLE_MAX_ATTEMPTS = int(os.environ.get("LOGIN_THROTTLE_MAX_ATTEMPTS", "5"))
LOGIN_THROTTLE_WINDOW_SECONDS = int(os.environ.get("LOGIN_THROTTLE_WINDOW_SECONDS", "900"))
