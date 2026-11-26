"""
Production-захист: адреса клієнта за nginx (X-Real-IP лише від довіреного proxy), спільний кеш Redis для лімітів
(атомарність, недоступність Redis, перевірка --deploy), відновлення заявки в Telegram після обриву запиту.

Тести зі справжнім Redis запускаються, якщо задано MOBIHOME_TEST_REDIS_URL (напр. redis://127.0.0.1:6390/15);
інакше вони пропускаються з поясненням. Тести недоступного Redis справжнього сервера не потребують.
"""

import os
import subprocess
import sys
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.cache import cache, caches
from django.core.checks import run_checks
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from properties import moderation, security, telegram_bot
from properties.models import ModerationStatus, Property, TelegramModerationMessage

from .helpers import make_property, make_user
from .test_moderation import TG, FakeTelegram

REDIS_URL = os.environ.get("MOBIHOME_TEST_REDIS_URL", "")
REDIS_CACHES = {"default": {"BACKEND": "properties.cache.ResilientRedisCache", "LOCATION": REDIS_URL,
                            "KEY_PREFIX": "mobihome-test", "OPTIONS": {"socket_connect_timeout": 1, "socket_timeout": 1}}}
DOWN_CACHES = {"default": {"BACKEND": "properties.cache.ResilientRedisCache", "LOCATION": "redis://127.0.0.1:1/0",
                           "OPTIONS": {"socket_connect_timeout": 1, "socket_timeout": 1}}}
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def ip_of(remote="", real=None, forwarded=None):
    meta = {"REMOTE_ADDR": remote}
    if real is not None:
        meta["HTTP_X_REAL_IP"] = real
    if forwarded is not None:
        meta["HTTP_X_FORWARDED_FOR"] = forwarded
    return security._client_ip(RequestFactory().get("/", **meta))


# --- Адреса клієнта ---


@override_settings(BEHIND_PROXY=False, TRUSTED_PROXY_IPS=["127.0.0.1", "::1"])
class ClientIpWithoutProxyTests(SimpleTestCase):
    def test_remote_addr_used_and_headers_ignored(self):
        self.assertEqual(ip_of("198.51.100.4", real="203.0.113.7", forwarded="203.0.113.8"), "198.51.100.4")

    def test_missing_or_invalid_remote_addr(self):
        self.assertEqual(ip_of(""), "unknown")
        self.assertEqual(ip_of("not-an-ip"), "unknown")


@override_settings(BEHIND_PROXY=True, TRUSTED_PROXY_IPS=["127.0.0.1", "::1"])
class ClientIpBehindProxyTests(SimpleTestCase):
    def test_unix_socket_and_trusted_proxy_use_x_real_ip(self):
        self.assertEqual(ip_of("", real="203.0.113.7"), "203.0.113.7")         # nginx → unix-сокет
        self.assertEqual(ip_of("127.0.0.1", real="203.0.113.8"), "203.0.113.8")  # nginx → 127.0.0.1
        self.assertEqual(ip_of("::1", real="2001:db8::5"), "2001:db8::5")

    def test_spoofed_header_from_untrusted_source_ignored(self):
        # Gunicorn напряму (не через nginx): клієнт підставляє X-Real-IP — не довіряємо.
        for spoof in ("203.0.113.1", "203.0.113.2", "127.0.0.1"):
            self.assertEqual(ip_of("198.51.100.9", real=spoof), "198.51.100.9")

    def test_x_forwarded_for_never_trusted(self):
        self.assertEqual(ip_of("", forwarded="203.0.113.7"), "unknown")
        self.assertEqual(ip_of("127.0.0.1", forwarded="203.0.113.7, 10.0.0.1"), "127.0.0.1")

    def test_missing_or_invalid_x_real_ip_falls_back(self):
        for bad in ("", "   ", "garbage", "203.0.113.7, 10.0.0.1", "203.0.113.7:443", "999.1.1.1",
                    "[2001:db8::1]", "1.2.3", "203.0.113.7\r\nX-Evil: 1"):
            self.assertEqual(ip_of("127.0.0.1", real=bad), "127.0.0.1", repr(bad))
            self.assertEqual(ip_of("", real=bad), "unknown", repr(bad))

    def test_ipv4_and_ipv6_normalised(self):
        self.assertEqual(ip_of("", real=" 203.0.113.7 "), "203.0.113.7")
        self.assertEqual(ip_of("", real="2001:DB8:0:0::0001"), "2001:db8::1")
        self.assertEqual(ip_of("", real="::ffff:203.0.113.7"), "203.0.113.7")
        self.assertEqual(ip_of("::ffff:127.0.0.1", real="203.0.113.9"), "203.0.113.9")  # довірений у IPv6-формі

    @override_settings(TRUSTED_PROXY_IPS=["10.0.0.2"])
    def test_custom_trusted_proxy_list(self):
        self.assertEqual(ip_of("10.0.0.2", real="203.0.113.7"), "203.0.113.7")
        self.assertEqual(ip_of("127.0.0.1", real="203.0.113.7"), "127.0.0.1")


@override_settings(BEHIND_PROXY=True, TRUSTED_PROXY_IPS=["127.0.0.1", "::1"], LOGIN_THROTTLE_MAX_ATTEMPTS=2,
                   CACHES=LOCMEM)
class RateLimitPerVisitorTests(TestCase):
    """Різні відвідувачі за nginx (порожній REMOTE_ADDR) мають окремі лічильники."""

    def setUp(self):
        cache.clear()

    def login(self, real_ip, n, remote=""):
        return self.client.post("/admin/login/", {"username": f"u{n}", "password": "wrong"},
                                REMOTE_ADDR=remote, HTTP_X_REAL_IP=real_ip)

    def test_visitors_do_not_share_limit(self):
        for n in range(8):  # ліміт на IP = 2 × 4
            self.assertEqual(self.login("203.0.113.10", n).status_code, 200)
        self.assertEqual(self.login("203.0.113.10", 99).status_code, 429)
        self.assertEqual(self.login("203.0.113.11", 100).status_code, 200)  # інший відвідувач не заблокований
        self.assertEqual(self.login("2001:db8::11", 101).status_code, 200)

    def test_rotating_spoofed_header_does_not_bypass_limit_when_untrusted(self):
        for n in range(8):
            self.login(f"203.0.113.{n}", n, remote="198.51.100.9")
        self.assertEqual(self.login("203.0.113.200", 50, remote="198.51.100.9").status_code, 429)

    def test_form_counters_are_per_visitor(self):
        factory = RequestFactory()
        a = factory.post("/", REMOTE_ADDR="", HTTP_X_REAL_IP="203.0.113.20")
        b = factory.post("/", REMOTE_ADDR="", HTTP_X_REAL_IP="203.0.113.21")
        for _ in range(5):
            security.count_form_submission(a, "password_reset")
        self.assertTrue(security.form_rate_limited(a, "password_reset"))
        self.assertFalse(security.form_rate_limited(b, "password_reset"))


# --- Спільний кеш ---


@override_settings(CACHES=DOWN_CACHES)
class RedisUnavailableTests(TestCase):
    def test_operations_fail_open_quickly_and_log(self):
        started = time.monotonic()
        with self.assertLogs("properties.cache", "ERROR") as logs:
            self.assertEqual(cache.get("k", 0), 0)
            self.assertTrue(cache.add("k", 1, 60))
            self.assertEqual(cache.incr("k"), 1)
            cache.set("k", 1, 60)
            cache.delete_many(["k"])
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(all("недоступний" in line for line in logs.output))
        self.assertFalse(any("redis://" in line for line in logs.output))  # без адреси/пароля в логах

    @override_settings(BEHIND_PROXY=True)
    def test_login_and_forms_keep_working(self):
        with self.assertLogs("properties.cache", "ERROR"):
            resp = self.client.post("/admin/login/", {"username": "x", "password": "y"},
                                    REMOTE_ADDR="", HTTP_X_REAL_IP="203.0.113.30")
            self.assertEqual(resp.status_code, 200)  # сторінка входу з помилкою, а не 500
            self.assertEqual(self.client.get("/auth/password-reset/").status_code, 200)


class SharedCacheCheckTests(SimpleTestCase):
    def errors(self):
        return [m.id for m in run_checks(include_deployment_checks=True) if m.id == "properties.E010"]

    @override_settings(BEHIND_PROXY=True, ALLOW_LOCAL_CACHE=False, CACHES=LOCMEM)
    def test_production_without_redis_is_an_error(self):
        self.assertEqual(self.errors(), ["properties.E010"])

    @override_settings(BEHIND_PROXY=True, ALLOW_LOCAL_CACHE=False, CACHES=DOWN_CACHES)
    def test_redis_backend_passes(self):
        self.assertEqual(self.errors(), [])

    @override_settings(BEHIND_PROXY=True, ALLOW_LOCAL_CACHE=True, CACHES=LOCMEM)
    def test_explicit_local_cache_exception(self):
        self.assertEqual(self.errors(), [])

    @override_settings(BEHIND_PROXY=False, ALLOW_LOCAL_CACHE=False, CACHES=LOCMEM)
    def test_local_without_proxy_needs_no_redis(self):
        self.assertEqual(self.errors(), [])

    def test_settings_select_redis_from_env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("DJANGO_")}
        env.update(DJANGO_SETTINGS_MODULE="config.settings", DJANGO_DEBUG="True",
                   DJANGO_REDIS_URL="redis://127.0.0.1:6379/1", DJANGO_BEHIND_PROXY="True")
        code = "from django.conf import settings as s; print(s.CACHES['default']['BACKEND'], s.BEHIND_PROXY)"
        proc = subprocess.run([sys.executable, "-c", "import django; django.setup(); " + code],
                              cwd=settings.BASE_DIR, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout.strip(), "properties.cache.ResilientRedisCache True", proc.stderr[-500:])


def needs_redis(cls):
    cls = unittest.skipUnless(REDIS_URL, "MOBIHOME_TEST_REDIS_URL не задано — тест зі справжнім Redis")(cls)
    return override_settings(CACHES=REDIS_CACHES)(cls)


@needs_redis
class RedisCounterTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_concurrent_increments_are_atomic(self):
        cache.add("hits", 0, 60)
        workers = [caches.create_connection("default") for _ in range(4)]  # як окремі воркери Gunicorn

        def hammer(conn):
            for _ in range(250):
                conn.incr("hits")

        threads = [threading.Thread(target=hammer, args=(workers[i % 4],)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(cache.get("hits"), 2000)

    def test_incr_keeps_ttl_and_never_creates_key(self):
        cache.add("c", 1, 30)
        cache.incr("c")
        client = cache._cache.get_client("x", write=True)
        ttl = client.ttl(cache.make_and_validate_key("c"))
        self.assertTrue(0 < ttl <= 30, ttl)
        with self.assertRaises(ValueError):
            cache.incr("missing")
        self.assertIsNone(cache.get("missing"))
        self.assertEqual(client.exists(cache.make_and_validate_key("missing")), 0)

    def test_expired_counter_is_not_recreated_without_ttl(self):
        cache.add("short", 1, 1)
        time.sleep(1.2)
        with self.assertRaises(ValueError):
            cache.incr("short")
        client = cache._cache.get_client("x", write=True)
        self.assertEqual(client.exists(cache.make_and_validate_key("short")), 0)

    @override_settings(BEHIND_PROXY=True, LOGIN_THROTTLE_MAX_ATTEMPTS=2)
    def test_login_limit_shared_between_workers(self):
        request = RequestFactory().post("/", REMOTE_ADDR="", HTTP_X_REAL_IP="203.0.113.40")
        other = caches.create_connection("default")
        for i in range(8):
            with mock.patch.object(security, "cache", other if i % 2 else cache):  # чергуємо «воркери»
                security.register_login_failure(request, f"user{i}")
        self.assertTrue(security.login_is_locked(request, "fresh"))
        ip_key = "login-fail:ip:203.0.113.40"
        ttl = cache._cache.get_client("x").ttl(cache.make_and_validate_key(ip_key))
        self.assertTrue(0 < ttl <= settings.LOGIN_THROTTLE_WINDOW_SECONDS)


# --- Telegram: обрив запиту (timeout Gunicorn) ---


@override_settings(**TG)
class TelegramInterruptedRequestTests(TestCase):
    def setUp(self):
        self.api = FakeTelegram()
        for name in ("_api", "_api_upload"):
            patcher = mock.patch.object(telegram_bot, name, self.api if name == "_api" else mock.Mock())
            patcher.start()
            self.addCleanup(patcher.stop)
        self.owner = make_user("owner")

    def test_listing_saved_even_if_sending_crashes(self):
        prop = make_property(owner=self.owner, public=False)
        with mock.patch.object(telegram_bot, "_send", side_effect=RuntimeError("worker killed")):
            with self.captureOnCommitCallbacks(execute=True):
                moderation.submit(prop.pk, actor=self.owner)
        prop.refresh_from_db()
        self.assertEqual(prop.moderation_status, ModerationStatus.PENDING)
        self.assertTrue(Property.objects.filter(pk=prop.pk).exists())

    def test_stale_sending_record_is_resent_by_bot(self):
        prop = make_property(owner=self.owner, public=False)
        with mock.patch.object(telegram_bot, "notify_new_submission"):
            with self.captureOnCommitCallbacks(execute=True):
                moderation.submit(prop.pk, actor=self.owner)
        prop.refresh_from_db()
        # Воркер Gunicorn убито посеред надсилання: запис лишився «sending».
        TelegramModerationMessage.objects.create(property=prop, version=prop.moderation_version, chat_id="-100777",
                                                 status="sending", attempts=1)
        self.assertEqual(telegram_bot.retry_failed(), 0)  # ще може надсилатись іншим процесом
        TelegramModerationMessage.objects.update(updated_at=timezone.now() - timedelta(minutes=11))
        self.assertEqual(telegram_bot.retry_failed(), 1)
        record = TelegramModerationMessage.objects.get(property=prop)
        self.assertEqual((record.status, record.attempts), ("sent", 2))
        self.assertEqual(self.api.methods(), ["sendMessage"])
