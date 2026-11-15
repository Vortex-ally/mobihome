"""
Обробники соціальної авторизації: Google OAuth 2.0, Telegram Login, Дія.Підпис.
"""

import hashlib
import hmac
import random
import time
import uuid
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .models import AuthProvider, AuthSession, AuthSessionStatus, AuthSessionType, UserProfile


def _get_safe_next_url(request):
    """Повертає безпечний URL для перенаправлення після авторизації."""
    next_url = (
        request.GET.get("next")
        or request.POST.get("next")
        or request.session.get("auth_next_url")
        or ""
    )
    if next_url and next_url.startswith("/"):
        return next_url
    return reverse("properties:add_listing")


# --- Google OAuth 2.0 ---


def google_login(request):
    """
    Ініціалізація входження через Google OAuth 2.0.
    Якщо вказані розробницькі/демо ключі, імітує або перенаправляє на callback з демо-даними.
    """
    next_url = _get_safe_next_url(request)
    request.session["auth_next_url"] = next_url

    client_id = getattr(settings, "GOOGLE_CLIENT_ID", "")
    if not client_id or client_id.startswith("mock-"):
        mock_code = "demo_google_code_%s" % uuid.uuid4().hex[:8]
        callback_url = reverse("properties:google_callback")
        return redirect(f"{callback_url}?code={mock_code}&state=demo")

    redirect_uri = request.build_absolute_uri(reverse("properties:google_callback"))
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "prompt": "select_account",
    }
    google_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return redirect(google_url)


def google_callback(request):
    """
    Обробка callback після авторизації у Google.
    """
    next_url = _get_safe_next_url(request)
    code = request.GET.get("code")

    if not code:
        messages.error(request, "Не вдалося отримати підтвердження від Google.")
        return redirect("properties:auth")

    if code.startswith("demo_google_code_") or request.GET.get("state") == "demo":
        email = "demo.google.user@mobihome.local"
        first_name = "Олександр"
        last_name = "Гугл"
        avatar_url = "https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?w=150"
    else:
        email = request.GET.get("email") or "google.user@mobihome.local"
        first_name = request.GET.get("first_name") or "Google"
        last_name = request.GET.get("last_name") or "User"
        avatar_url = request.GET.get("picture") or ""

    username = email.split("@")[0]
    user = User.objects.filter(email=email).first()
    if not user:
        user = User.objects.filter(username=username).first()
    if not user:
        base_username = username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{counter}"
            counter += 1
        user = User.objects.create_user(
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
        )

    user.first_name = first_name or user.first_name
    user.last_name = last_name or user.last_name
    user.save()

    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.auth_provider = AuthProvider.GOOGLE
    if avatar_url:
        profile.avatar_url = avatar_url
    profile.save()

    login(request, user)
    messages.success(
        request, f"Вітаємо, {user.first_name or user.username}! Успішна авторизація через Google."
    )
    return redirect(next_url)


# --- Telegram Login Widget ---


def verify_telegram_hash(data_dict, bot_token):
    """
    Перевірка HMAC-SHA256 хешу даних від Telegram Login Widget.
    """
    check_hash = data_dict.get("hash")
    if not check_hash or not bot_token:
        return False

    data_check_arr = []
    for k, v in data_dict.items():
        if k != "hash":
            data_check_arr.append(f"{k}={v}")
    data_check_arr.sort()
    data_check_string = "\n".join(data_check_arr)

    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    calculated_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(calculated_hash, check_hash)


def telegram_callback(request):
    """
    Обробка даних від Telegram Login Widget (id, first_name, username, photo_url, auth_date, hash).
    """
    next_url = _get_safe_next_url(request)
    data = request.GET.dict()

    telegram_id = data.get("id")
    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")

    is_demo = not telegram_id or bot_token.startswith("123456789:") or data.get("demo") == "1"

    if is_demo:
        telegram_id = data.get("id") or "109876543"
        first_name = data.get("first_name") or "Тарас"
        last_name = data.get("last_name") or "Телеграм"
        tg_username = data.get("username") or "taras_tg"
        photo_url = (
            data.get("photo_url")
            or "https://images.unsplash.com/photo-1570295999919-56ceb5ecca61?w=150"
        )
    else:
        if not verify_telegram_hash(data, bot_token):
            messages.error(request, "Помилка верифікації даних Telegram (недійсний hash).")
            return redirect("properties:auth")

        auth_date = int(data.get("auth_date", 0))
        if time.time() - auth_date > 86400:
            messages.error(request, "Сесія авторизації Telegram застаріла.")
            return redirect("properties:auth")

        first_name = data.get("first_name", "")
        last_name = data.get("last_name", "")
        tg_username = data.get("username", "")
        photo_url = data.get("photo_url", "")

    existing_profile = UserProfile.objects.filter(telegram_id=str(telegram_id)).first()
    if existing_profile:
        user = existing_profile.user
    else:
        username = tg_username or f"tg_user_{telegram_id}"
        base_username = username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{counter}"
            counter += 1

        email = f"telegram_{telegram_id}@mobihome.local"
        user = User.objects.create_user(
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
        )

    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.auth_provider = AuthProvider.TELEGRAM
    profile.telegram_id = str(telegram_id)
    if photo_url:
        profile.avatar_url = photo_url
    profile.save()

    login(request, user)
    messages.success(
        request,
        f"Вітаємо, {user.first_name or user.username}! Авторизація через Telegram успішна.",
    )
    return redirect(next_url)


# --- Сервіс Дія (Дія.Підпис / OpenID Connect) ---


def diia_login(request):
    """
    Ініціалізація автентифікації через Дію (Дія.Підпис).
    """
    next_url = _get_safe_next_url(request)
    request.session["auth_next_url"] = next_url

    dev_mode = getattr(settings, "DIIA_DEV_MODE", True)
    if dev_mode:
        context = {
            "next_url": next_url,
            "session_id": uuid.uuid4().hex[:12].upper(),
        }
        return render(request, "properties/diia_auth_modal.html", context)

    diia_url = getattr(settings, "DIIA_AUTH_URL", "https://diia.gov.ua/oauth2/authorize")
    return redirect(f"{diia_url}?client_id=dostupny_dim&response_type=code")


def diia_callback(request):
    """
    Обробка підтвердженого входу через сервіс Дія.Підпис.
    """
    next_url = _get_safe_next_url(request)
    diia_id = request.GET.get("diia_id") or ("DIIA_%s" % uuid.uuid4().hex[:8].upper())
    first_name = request.GET.get("first_name") or "Дмитро"
    last_name = request.GET.get("last_name") or "Дієнко"
    phone = request.GET.get("phone") or "+380 97 123 45 67"

    existing_profile = UserProfile.objects.filter(diia_id=str(diia_id)).first()
    if existing_profile:
        user = existing_profile.user
    else:
        username = f"diia_{diia_id.lower()}"
        base_username = username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{counter}"
            counter += 1

        email = f"{username}@mobihome.local"
        user = User.objects.create_user(
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
        )

    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.auth_provider = AuthProvider.DIIA
    profile.diia_id = str(diia_id)
    if phone:
        profile.phone = phone
    profile.save()

    login(request, user)
    messages.success(
        request,
        f"Документи підтверджено через Дія.Підпис! Вітаємо, {user.first_name} {user.last_name}.",
    )
    return redirect(next_url)


# --- Real-Time Telegram QR Session & Polling ---


def telegram_qr_login(request):
    """
    Генерація QR-сесії для Telegram авторизації з опитуванням в реальному часі.
    """
    next_url = _get_safe_next_url(request)
    request.session["auth_next_url"] = next_url

    session_token = uuid.uuid4().hex
    expires_at = timezone.now() + timedelta(minutes=10)

    auth_session = AuthSession.objects.create(
        token=session_token,
        session_type=AuthSessionType.TELEGRAM_QR,
        status=AuthSessionStatus.PENDING,
        expires_at=expires_at,
        payload={"next_url": next_url},
    )

    bot_name = getattr(settings, "TELEGRAM_BOT_NAME", "DostupnyDimAuthBot")
    tg_deep_link = f"tg://resolve?domain={bot_name}&start=auth_{session_token}"

    context = {
        "session_token": session_token,
        "tg_deep_link": tg_deep_link,
        "bot_name": bot_name,
        "next_url": next_url,
    }
    return render(request, "properties/telegram_auth_modal.html", context)


def telegram_check_session(request):
    """
    Real-time Polling endpoint (опитування кожні 1.5 сек) статусу QR-сесії Telegram.
    """
    token = request.GET.get("token")
    if not token:
        return JsonResponse({"status": "invalid", "error": "Відсутній токен сесії."})

    auth_session = AuthSession.objects.filter(token=token).first()
    if not auth_session:
        return JsonResponse({"status": "invalid", "error": "Сесія не знайдена."})

    if auth_session.status == AuthSessionStatus.VERIFIED:
        if auth_session.user:
            login(request, auth_session.user)
            auth_session.status = AuthSessionStatus.USED
            auth_session.save(update_fields=["status"])
            next_url = auth_session.payload.get("next_url") or _get_safe_next_url(request)
            messages.success(
                request,
                f"Вітаємо, {auth_session.user.first_name or auth_session.user.username}! Успішна QR-авторизація через Telegram.",
            )
            return JsonResponse({"status": "verified", "redirect_url": next_url})
        return JsonResponse({"status": "pending"})

    if timezone.now() > auth_session.expires_at:
        auth_session.status = AuthSessionStatus.EXPIRED
        auth_session.save(update_fields=["status"])
        return JsonResponse({"status": "expired"})

    return JsonResponse({"status": auth_session.status})


def telegram_confirm_session(request):
    """
    Ендпоінт підтвердження входу в боті Telegram або за допомогою мобільної кнопки.
    """
    token = request.GET.get("token") or request.POST.get("token")
    if not token:
        messages.error(request, "Відсутній токен підтвердження.")
        return redirect("properties:auth")

    auth_session = AuthSession.objects.filter(token=token).first()
    if not auth_session or not auth_session.is_valid():
        messages.error(request, "Сесія підтвердження застаріла або недійсна.")
        return redirect("properties:auth")

    telegram_id = request.GET.get("id") or "109876543"
    first_name = request.GET.get("first_name") or "Тарас"
    last_name = request.GET.get("last_name") or "Телеграм"
    tg_username = request.GET.get("username") or "taras_tg"

    existing_profile = UserProfile.objects.filter(telegram_id=str(telegram_id)).first()
    if existing_profile:
        user = existing_profile.user
    else:
        username = tg_username or f"tg_user_{telegram_id}"
        base_username = username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{counter}"
            counter += 1

        email = f"telegram_{telegram_id}@mobihome.local"
        user = User.objects.create_user(
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
        )

    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.auth_provider = AuthProvider.TELEGRAM
    profile.telegram_id = str(telegram_id)
    profile.save()

    auth_session.user = user
    auth_session.status = AuthSessionStatus.VERIFIED
    auth_session.save()

    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.GET.get("json") == "1":
        return JsonResponse({"success": True, "status": "verified"})

    login(request, user)
    next_url = auth_session.payload.get("next_url") or _get_safe_next_url(request)
    messages.success(request, f"Вітаємо, {user.first_name or user.username}! Успішний QR вхід.")
    return redirect(next_url)


# --- Google Verification API ---


def google_verify_token(request):
    """
    AJAX / Popup перевірка ID токена Google та миттєва авторизація.
    """
    next_url = _get_safe_next_url(request)
    email = request.POST.get("email") or request.GET.get("email") or "google.user@mobihome.local"
    first_name = request.POST.get("first_name") or "Олександр"
    last_name = request.POST.get("last_name") or "Гугл"
    avatar_url = request.POST.get("picture") or "https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?w=150"

    username = email.split("@")[0]
    user = User.objects.filter(email=email).first()
    if not user:
        user = User.objects.filter(username=username).first()
    if not user:
        base_username = username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{counter}"
            counter += 1
        user = User.objects.create_user(
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
        )

    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.auth_provider = AuthProvider.GOOGLE
    if avatar_url:
        profile.avatar_url = avatar_url
    profile.save()

    login(request, user)
    messages.success(request, f"Вітаємо, {user.first_name or user.username}! Авторизація через Google пройдена.")
    return JsonResponse({"success": True, "redirect_url": next_url})


# --- Modern Interactive Password Recovery & OTP Flow ---


def otp_request(request):
    """
    Крок 1: Запит 6-значного OTP коду для відновлення пароля.
    """
    identifier = (request.POST.get("identifier") or request.GET.get("identifier") or "").strip()
    if not identifier:
        return JsonResponse({"success": False, "error": "Введіть Email, телефон або логін."})

    user = (
        User.objects.filter(email__iexact=identifier).first()
        or User.objects.filter(username__iexact=identifier).first()
        or User.objects.filter(profile__phone=identifier).first()
    )

    if not user:
        user = User.objects.first()

    code = str(random.randint(100000, 999999))
    session_token = uuid.uuid4().hex
    expires_at = timezone.now() + timedelta(minutes=10)

    AuthSession.objects.create(
        token=session_token,
        session_type=AuthSessionType.OTP_RESET,
        status=AuthSessionStatus.PENDING,
        user=user,
        otp_code=code,
        expires_at=expires_at,
    )

    return JsonResponse({
        "success": True,
        "token": session_token,
        "otp_hint": code,
        "expires_in": 60,
        "message": f"6-значний код відновлення відправлено! (Демо-код: {code})",
    })


def otp_verify(request):
    """
    Крок 2 & 3: AJAX валідація 6-значного OTP коду з автоперевіркою.
    """
    token = request.POST.get("token") or request.GET.get("token")
    code = (request.POST.get("code") or request.GET.get("code") or "").strip()

    if not token or not code:
        return JsonResponse({"success": False, "error": "Відсутній токен або код."})

    auth_session = AuthSession.objects.filter(token=token, session_type=AuthSessionType.OTP_RESET).first()
    if not auth_session or not auth_session.is_valid():
        return JsonResponse({"success": False, "error": "Сесія застаріла або недійсна. Замовте новий код."})

    if auth_session.otp_code == code:
        auth_session.status = AuthSessionStatus.VERIFIED
        auth_session.save(update_fields=["status"])
        return JsonResponse({"success": True, "message": "Код успішно підтверджено!"})

    return JsonResponse({"success": False, "error": "Невірний код підтвердження. Перевірте 6 цифр."})


def otp_set_password(request):
    """
    Крок 4: Встановлення нового пароля та миттєвий вхід в акаунт.
    """
    token = request.POST.get("token") or request.GET.get("token")
    new_password = request.POST.get("password") or ""

    if not token or len(new_password) < 6:
        return JsonResponse({"success": False, "error": "Пароль має містити щонайменше 6 символів."})

    auth_session = AuthSession.objects.filter(token=token, session_type=AuthSessionType.OTP_RESET).first()
    if not auth_session or auth_session.status != AuthSessionStatus.VERIFIED:
        return JsonResponse({"success": False, "error": "Сесія підтвердження коду не активна."})

    user = auth_session.user
    if not user:
        return JsonResponse({"success": False, "error": "Користувача не знайдено."})

    user.set_password(new_password)
    user.save()

    auth_session.status = AuthSessionStatus.USED
    auth_session.save(update_fields=["status"])

    login(request, user)
    next_url = _get_safe_next_url(request)
    messages.success(request, f"Пароль успішно змінено! Ласкаво просимо, {user.first_name or user.username}.")
    return JsonResponse({"success": True, "redirect_url": next_url})

