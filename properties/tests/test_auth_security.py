"""Безпека авторизації: соцвхід, OTP, редиректи, обмеження спроб."""

import hashlib
import hmac
import time
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings

from properties import social_auth
from properties.models import UserProfile

from .helpers import PASSWORD, make_admin, make_user

GOOGLE = dict(GOOGLE_CLIENT_ID="cid.apps.googleusercontent.com", GOOGLE_CLIENT_SECRET="sec")
TG = dict(TELEGRAM_BOT_TOKEN="111:test-bot-token", TELEGRAM_BOT_NAME="TestBot")


def _session_user_id(client):
    return client.session.get("_auth_user_id")


class RemovedInsecureEndpointsTests(TestCase):
    def test_google_verify_token_removed(self):
        make_admin()
        resp = self.client.post("/auth/google/verify/", {"email": "boss@example.com"})
        self.assertEqual(resp.status_code, 404)
        self.assertIsNone(_session_user_id(self.client))

    def test_otp_endpoints_removed(self):
        make_admin()
        for url in ("/auth/otp/request/", "/auth/otp/verify/", "/auth/otp/set-password/"):
            resp = self.client.post(url, {"identifier": "nobody", "token": "x", "code": "1"})
            self.assertEqual(resp.status_code, 404, url)

    def test_telegram_qr_endpoints_removed(self):
        for url in ("/auth/telegram/qr/", "/auth/telegram/check-session/", "/auth/telegram/confirm-session/"):
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_diia_disabled_does_not_login_or_create_users(self):
        before = User.objects.count()
        resp = self.client.get("/auth/diia/callback/?diia_id=X1&first_name=A")
        self.assertRedirects(resp, "/auth/", fetch_redirect_response=False)
        self.assertIsNone(_session_user_id(self.client))
        self.assertEqual(User.objects.count(), before)


class GoogleOAuthTests(TestCase):
    def test_disabled_without_keys(self):
        resp = self.client.get("/auth/google/login/")
        self.assertRedirects(resp, "/auth/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/auth/google/callback/?code=x").status_code, 404)

    @override_settings(**GOOGLE)
    def test_forged_email_param_is_ignored(self):
        make_admin()
        resp = self.client.get("/auth/google/callback/?code=abc&state=demo&email=boss@example.com")
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(_session_user_id(self.client))

    def _start(self):
        resp = self.client.get("/auth/google/login/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(social_auth.GOOGLE_AUTH_URL))
        return self.client.session["google_oauth_state"], self.client.session["google_oauth_nonce"]

    def _claims(self, real_nonce, **over):
        claims = {
            "iss": "https://accounts.google.com",
            "aud": GOOGLE["GOOGLE_CLIENT_ID"],
            "exp": int(time.time()) + 300,
            "nonce": real_nonce,
            "sub": "google-sub-1",
            "email": "new.user@gmail.com",
            "email_verified": True,
        }
        claims.update(over)
        return claims

    def _callback(self, state, claims):
        with mock.patch.object(social_auth, "_exchange_google_code", return_value={"id_token": "x"}), \
                mock.patch.object(social_auth, "_decode_jwt_payload", return_value=claims):
            return self.client.get(f"/auth/google/callback/?code=c&state={state}")

    @override_settings(**GOOGLE)
    def test_valid_login_creates_user(self):
        state, nonce = self._start()
        self._callback(state, self._claims(nonce))
        user = User.objects.get(email="new.user@gmail.com")
        self.assertEqual(_session_user_id(self.client), str(user.pk))
        self.assertFalse(user.has_usable_password())
        self.assertEqual(UserProfile.objects.get(user=user).google_sub, "google-sub-1")

    @override_settings(**GOOGLE)
    def test_wrong_state_rejected(self):
        _, nonce = self._start()
        self._callback("wrong-state", self._claims(nonce))
        self.assertIsNone(_session_user_id(self.client))

    @override_settings(**GOOGLE)
    def test_bad_claims_rejected(self):
        for over in ({"aud": "other"}, {"iss": "https://evil"}, {"exp": 1},
                     {"email_verified": False}, {"nonce": "replayed"}):
            self.client.logout()
            state, nonce = self._start()
            self._callback(state, self._claims(nonce, **over))
            self.assertIsNone(_session_user_id(self.client), over)

    @override_settings(**GOOGLE)
    def test_admin_account_cannot_be_entered_via_google(self):
        admin = make_admin()
        admin.set_unusable_password()
        admin.save()
        state, nonce = self._start()
        self._callback(state, self._claims(nonce, email="boss@example.com"))
        self.assertIsNone(_session_user_id(self.client))

    @override_settings(**GOOGLE)
    def test_no_autolink_to_password_account(self):
        make_user("victim")  # має пароль, email victim@example.com
        state, nonce = self._start()
        self._callback(state, self._claims(nonce, email="victim@example.com"))
        self.assertIsNone(_session_user_id(self.client))


def _tg_sign(data, token=TG["TELEGRAM_BOT_TOKEN"]):
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hashlib.sha256(token.encode()).digest()
    return hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()


class TelegramTests(TestCase):
    def test_disabled_without_token(self):
        resp = self.client.get("/auth/telegram/callback/?id=1&demo=1")
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(_session_user_id(self.client))
        self.assertFalse(User.objects.exists())

    @override_settings(**TG)
    def test_valid_signature_logs_in(self):
        data = {"id": "555", "first_name": "Taras", "auth_date": str(int(time.time()))}
        data["hash"] = _tg_sign(data)
        self.client.get("/auth/telegram/callback/", data)
        self.assertIsNotNone(_session_user_id(self.client))
        self.assertEqual(UserProfile.objects.get(telegram_id="555").user.first_name, "Taras")

    @override_settings(**TG)
    def test_invalid_signature_rejected(self):
        data = {"id": "555", "auth_date": str(int(time.time())), "hash": "0" * 64}
        self.client.get("/auth/telegram/callback/", data)
        self.assertIsNone(_session_user_id(self.client))

    @override_settings(**TG)
    def test_stale_auth_date_rejected(self):
        data = {"id": "555", "auth_date": str(int(time.time()) - 3 * 86400)}
        data["hash"] = _tg_sign(data)
        self.client.get("/auth/telegram/callback/", data)
        self.assertIsNone(_session_user_id(self.client))

    @override_settings(**TG)
    def test_admin_linked_to_telegram_cannot_login_via_telegram(self):
        admin = make_admin()
        UserProfile.objects.filter(user=admin).update(telegram_id="777")
        data = {"id": "777", "auth_date": str(int(time.time()))}
        data["hash"] = _tg_sign(data)
        self.client.get("/auth/telegram/callback/", data)
        self.assertIsNone(_session_user_id(self.client))


class LoginRedirectAndThrottleTests(TestCase):
    def setUp(self):
        self.user = make_user("alice")

    def _login(self, password=PASSWORD, **extra):
        return self.client.post("/auth/", {"form_type": "login", "username": "alice", "password": password, **extra})

    def test_open_redirect_blocked(self):
        for bad in ("//evil.com", "https://evil.com/x", "/\\evil.com"):
            self.client.logout()
            resp = self._login(next=bad)
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(resp["Location"], "/add-listing/", bad)

    def test_local_next_allowed(self):
        resp = self._login(next="/profile/")
        self.assertEqual(resp["Location"], "/profile/")

    @override_settings(LOGIN_THROTTLE_MAX_ATTEMPTS=3)
    def test_lockout_after_failures(self):
        for _ in range(3):
            self._login(password="wrong")
        resp = self._login()
        self.assertEqual(resp.status_code, 429)
        self.assertIsNone(_session_user_id(self.client))

    @override_settings(LOGIN_THROTTLE_MAX_ATTEMPTS=3)
    def test_admin_login_throttled(self):
        make_admin()
        for _ in range(3):
            self.client.post("/admin/login/", {"username": "boss", "password": "wrong"})
        resp = self.client.post("/admin/login/", {"username": "boss", "password": PASSWORD})
        self.assertEqual(resp.status_code, 429)
        self.assertIsNone(_session_user_id(self.client))

    def test_admin_login_works(self):
        make_admin()
        resp = self.client.post("/admin/login/?next=/admin/", {"username": "boss", "password": PASSWORD, "next": "/admin/"})
        self.assertEqual(resp.status_code, 302)
        self.assertIsNotNone(_session_user_id(self.client))


class PasswordResetTests(TestCase):
    def test_reset_email_sent_and_no_code_leak(self):
        make_user("bob")
        resp = self.client.post("/auth/password-reset/", {"email": "bob@example.com"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/auth/reset/", mail.outbox[0].body)

    def test_unknown_email_sends_nothing(self):
        make_admin()
        self.client.post("/auth/password-reset/", {"email": "nobody@example.com"})
        self.assertEqual(len(mail.outbox), 0)

    def test_auth_page_has_no_otp_or_diia(self):
        html = self.client.get("/auth/").content.decode()
        self.assertNotIn("otp", html.lower())
        self.assertNotIn("diia", html.lower())
        self.assertIn("/auth/password-reset/", html)
