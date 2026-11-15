"""
Соціальна авторизація: Google OAuth 2.0 та Telegram Login Widget.

Принципи безпеки:
- Особа підтверджується лише провайдером (Google: id_token з token endpoint; Telegram: HMAC-підпис).
- Жодних демо/mock-режимів: якщо ключі не налаштовані, вхід вимкнено.
- Соціальний вхід у staff/superuser-акаунти заборонено — адміністратори входять лише паролем.
- Дія тимчасово вимкнена; OTP-відновлення прибрано (використовується стандартний
  Django password reset через email).
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from .models import AuthProvider, UserProfile

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
TELEGRAM_FIELDS = ("id", "first_name", "last_name", "username", "photo_url", "auth_date")
TELEGRAM_MAX_AGE_SECONDS = 24 * 60 * 60
SOCIAL_BACKEND = "django.contrib.auth.backends.ModelBackend"


def safe_next_url(request, candidate, default=None):
    """Повертає candidate лише якщо це URL нашого сайту; інакше default."""
    default = default or reverse("properties:add_listing")
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return default


def _get_safe_next_url(request):
    candidate = (
        request.GET.get("next")
        or request.POST.get("next")
        or request.session.get("auth_next_url")
        or ""
    )
    return safe_next_url(request, candidate)


def _refuse_privileged(request, user):
    """Адміністратори не можуть входити через соцмережі."""
    if user.is_staff or user.is_superuser:
        logger.warning("Blocked social login attempt into privileged account id=%s", user.pk)
        messages.error(
            request,
            "Для цього облікового запису вхід через соцмережі заборонено. Увійдіть за паролем.",
        )
        return True
    if not user.is_active:
        messages.error(request, "Обліковий запис деактивовано.")
        return True
    return False


def _unique_username(base):
    base = (base or "user")[:140] or "user"
    username, counter = base, 1
    while User.objects.filter(username=username).exists():
        username = f"{base}_{counter}"
        counter += 1
    return username


# --- Google OAuth 2.0 ---


def google_enabled():
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def google_login(request):
    """Перенаправлення на Google з state (CSRF) та nonce (replay)."""
    if not google_enabled():
        messages.error(request, "Вхід через Google наразі не налаштовано.")
        return redirect("properties:auth")

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    request.session["google_oauth_state"] = state
    request.session["google_oauth_nonce"] = nonce
    request.session["auth_next_url"] = _get_safe_next_url(request)

    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": request.build_absolute_uri(reverse("properties:google_callback")),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    }
    return redirect(f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}")


def _exchange_google_code(request, code):
    """Обмін code на токени (server-to-server, з client_secret). Повертає dict або None."""
    body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": request.build_absolute_uri(reverse("properties:google_callback")),
            "grant_type": "authorization_code",
        }
    ).encode()
    req = urllib.request.Request(
        GOOGLE_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 - фіксований https URL
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, ValueError) as exc:
        logger.warning("Google token exchange failed: %s", exc)
        return None


def _decode_jwt_payload(token):
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except (IndexError, ValueError):
        return None


def validate_google_claims(claims, nonce):
    """
    Перевірка claims id_token. id_token отримано напряму з token endpoint Google по TLS
    з client_secret, тому (за OpenID Connect Core 3.1.3.7) підпис можна не перевіряти,
    але iss/aud/exp/nonce/email_verified перевіряються обов'язково.
    """
    if not claims:
        return False
    if claims.get("iss") not in GOOGLE_ISSUERS:
        return False
    if claims.get("aud") != settings.GOOGLE_CLIENT_ID:
        return False
    try:
        if int(claims.get("exp", 0)) < time.time():
            return False
    except (TypeError, ValueError):
        return False
    if not nonce or not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        return False
    if not claims.get("sub") or not claims.get("email"):
        return False
    if claims.get("email_verified") not in (True, "true"):
        return False
    return True


def google_callback(request):
    if not google_enabled():
        raise Http404
    expected_state = request.session.pop("google_oauth_state", None)
    nonce = request.session.pop("google_oauth_nonce", None)
    next_url = safe_next_url(request, request.session.pop("auth_next_url", ""))

    state = request.GET.get("state", "")
    code = request.GET.get("code")
    if not expected_state or not hmac.compare_digest(state, expected_state) or not code:
        messages.error(request, "Не вдалося підтвердити вхід через Google. Спробуйте ще раз.")
        return redirect("properties:auth")

    tokens = _exchange_google_code(request, code)
    claims = _decode_jwt_payload((tokens or {}).get("id_token", ""))
    if not validate_google_claims(claims, nonce):
        messages.error(request, "Google не підтвердив вашу особу. Спробуйте ще раз.")
        return redirect("properties:auth")

    sub = str(claims["sub"])
    email = claims["email"].lower()

    profile = UserProfile.objects.select_related("user").filter(google_sub=sub).first()
    if profile:
        user = profile.user
    else:
        # Email підтверджено Google, тому можна прив'язати існуючий акаунт з тим самим email.
        user = User.objects.filter(email__iexact=email).first()
        if user is not None and user.has_usable_password():
            # Email при реєстрації не підтверджується, тож акаунт з паролем міг створити
            # хтось інший (pre-account-takeover). Автоприв'язку не робимо.
            messages.error(
                request,
                "Акаунт з цим email уже існує. Увійдіть за паролем "
                "(або відновіть пароль через «Забули пароль?»).",
            )
            return redirect("properties:auth")
        if user is None:
            user = User.objects.create_user(
                username=_unique_username(email.split("@")[0]),
                email=email,
                first_name=(claims.get("given_name") or "")[:150],
                last_name=(claims.get("family_name") or "")[:150],
            )
            user.set_unusable_password()
            user.save(update_fields=["password"])

    if _refuse_privileged(request, user):
        return redirect("properties:auth")

    profile, _ = UserProfile.objects.get_or_create(user=user)
    if profile.google_sub and profile.google_sub != sub:
        messages.error(request, "Цей обліковий запис уже прив'язано до іншого Google-акаунта.")
        return redirect("properties:auth")
    profile.google_sub = sub
    if profile.auth_provider == AuthProvider.LOCAL and not user.has_usable_password():
        profile.auth_provider = AuthProvider.GOOGLE
    if claims.get("picture"):
        profile.avatar_url = claims["picture"][:500]
    profile.save()

    login(request, user, backend=SOCIAL_BACKEND)
    messages.success(request, f"Вітаємо, {user.first_name or user.username}! Вхід через Google.")
    return redirect(next_url)


# --- Telegram Login Widget ---


def telegram_enabled():
    return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_BOT_NAME)


def verify_telegram_hash(data_dict, bot_token):
    """Перевірка HMAC-SHA256 підпису даних Telegram Login Widget."""
    check_hash = data_dict.get("hash")
    if not check_hash or not bot_token:
        return False
    data_check_string = "\n".join(
        f"{k}={data_dict[k]}" for k in sorted(data_dict) if k != "hash"
    )
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    calculated = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(calculated, str(check_hash))


def telegram_callback(request):
    """Обробка redirect від Telegram Login Widget. Підпис перевіряється завжди."""
    if not telegram_enabled():
        messages.error(request, "Вхід через Telegram наразі не налаштовано.")
        return redirect("properties:auth")

    next_url = safe_next_url(request, request.session.pop("auth_next_url", ""))
    data = {k: request.GET[k] for k in TELEGRAM_FIELDS if k in request.GET}
    data["hash"] = request.GET.get("hash", "")

    if not data.get("id") or not verify_telegram_hash(data, settings.TELEGRAM_BOT_TOKEN):
        messages.error(request, "Не вдалося підтвердити дані Telegram.")
        return redirect("properties:auth")
    try:
        auth_date = int(data.get("auth_date", 0))
    except ValueError:
        auth_date = 0
    if time.time() - auth_date > TELEGRAM_MAX_AGE_SECONDS:
        messages.error(request, "Сесія авторизації Telegram застаріла. Спробуйте ще раз.")
        return redirect("properties:auth")

    telegram_id = str(data["id"])
    profile = UserProfile.objects.select_related("user").filter(telegram_id=telegram_id).first()
    if profile:
        user = profile.user
    else:
        user = User.objects.create_user(
            username=_unique_username(data.get("username") or f"tg_{telegram_id}"),
            email="",
            first_name=data.get("first_name", "")[:150],
            last_name=data.get("last_name", "")[:150],
        )
        user.set_unusable_password()
        user.save(update_fields=["password"])

    if _refuse_privileged(request, user):
        return redirect("properties:auth")

    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.telegram_id = telegram_id
    if not user.has_usable_password():
        profile.auth_provider = AuthProvider.TELEGRAM
    if data.get("photo_url"):
        profile.avatar_url = data["photo_url"][:500]
    profile.save()

    login(request, user, backend=SOCIAL_BACKEND)
    messages.success(request, f"Вітаємо, {user.first_name or user.username}! Вхід через Telegram.")
    return redirect(next_url)


# --- Дія (вимкнено) ---


def diia_disabled(request):
    messages.info(request, "Вхід через Дію тимчасово недоступний.")
    return redirect("properties:auth")
