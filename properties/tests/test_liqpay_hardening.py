"""Виправлення аудиту LiqPay: А1–А7 і конкурентне створення платежів."""

import http.client
import os
import subprocess
import sys
import threading
import traceback
from datetime import timedelta
from datetime import timezone as dt_timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.checks import run_checks
from django.core.management import call_command
from django.db import IntegrityError, OperationalError, connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from properties import liqpay
from properties.models import Payment, PaymentEvent, PaymentStatus, Property
from properties.payment_config import liqpay_problems
from properties.placement import create_payment, get_or_create_open_payment

from .helpers import make_admin, make_property, make_user
from .test_liqpay import LIQ, PUB, callback_body, sign


def _locked(exc):
    return isinstance(exc, OperationalError) and "locked" in str(exc)


@override_settings(**LIQ)
class A1StatusNetworkErrorsTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.payment = create_payment(self.owner, make_property(owner=self.owner), is_test=True)

    def test_http_level_errors_are_handled(self):
        for exc in (http.client.RemoteDisconnected("x"), http.client.IncompleteRead(b""),
                    ConnectionResetError(), TimeoutError(), OSError("tls"), ValueError("json")):
            with mock.patch.object(liqpay, "_post_api", side_effect=exc):
                self.assertEqual(liqpay.fetch_status(self.payment).note, "network_error", exc)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CREATED)

    def test_result_page_survives_network_error(self):
        cache.clear()
        self.client.force_login(self.owner)
        with mock.patch.object(liqpay, "_post_api", side_effect=http.client.RemoteDisconnected("x")):
            self.assertEqual(self.client.get(f"/payments/{self.payment.order_id}/result/").status_code, 200)

    def test_non_dict_response_ignored(self):
        for response in ([1, 2], "text", None):
            with mock.patch.object(liqpay, "_post_api", return_value=response):
                self.assertEqual(liqpay.fetch_status(self.payment).note, "invalid_response")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CREATED)

    def test_sync_command_continues_after_unexpected_error(self):
        other = create_payment(self.owner, make_property(owner=self.owner), is_test=True)
        Payment.objects.update(created_at=timezone.now() - timedelta(minutes=20))
        ok = {"result": "ok", "action": "pay", "status": "processing", "public_key": PUB,
              "amount": 99, "currency": "UAH"}

        def fake(payment, config=None):
            if payment.pk == self.payment.pk:
                raise RuntimeError("boom")
            with mock.patch.object(liqpay, "_post_api", return_value=dict(ok, order_id=payment.order_id)):
                return liqpay.process_payment_fields(dict(ok, order_id=payment.order_id),
                                                     signature_valid=True, source="status")

        out = StringIO()
        with mock.patch.object(liqpay, "fetch_status", side_effect=fake):
            call_command("liqpay_sync_pending", stdout=out)
        self.assertIn("error:RuntimeError=1", out.getvalue())
        other.refresh_from_db()
        self.assertEqual(other.status, PaymentStatus.PENDING)


@override_settings(**LIQ)
class A2CallbackAbuseTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = make_user("owner")
        self.payment = create_payment(self.owner, make_property(owner=self.owner), is_test=True)

    def forged(self, **over):
        return callback_body(self.payment, priv="attacker", **over)

    def test_oversized_callback_rejected_without_db_write(self):
        resp = self.client.post("/payments/liqpay/callback/", {"data": "A" * 40000, "signature": "x"})
        self.assertEqual(resp.status_code, 413)
        self.assertFalse(PaymentEvent.objects.exists())

    def test_invalid_signature_stored_minimally(self):
        resp = self.client.post("/payments/liqpay/callback/", self.forged(err_description="Z" * 5000))
        self.assertEqual(resp.status_code, 400)
        event = PaymentEvent.objects.get()
        self.assertEqual(event.payload, {})
        self.assertIsNone(event.payment)
        self.assertFalse(event.signature_valid)
        self.assertEqual(event.order_id, self.payment.order_id)
        self.assertNotIn("Z" * 10, event.note)

    def test_garbage_order_id_not_stored(self):
        self.client.post("/payments/liqpay/callback/", self.forged(order_id="<script>" + "x" * 300))
        self.assertEqual(PaymentEvent.objects.get().order_id, "")

    def test_rate_limit_per_ip_and_global_db_cap(self):
        codes = [self.client.post("/payments/liqpay/callback/", self.forged()).status_code for _ in range(30)]
        self.assertEqual(codes[:20], [400] * 20)
        self.assertEqual(set(codes[20:]), {429})
        self.assertEqual(PaymentEvent.objects.count(), 20)
        other_ip = Client(REMOTE_ADDR="10.0.0.2")
        for _ in range(40):
            other_ip.post("/payments/liqpay/callback/", self.forged())
        self.assertLessEqual(PaymentEvent.objects.count(), 50)  # глобальна межа записів

    def test_valid_callback_processed_even_when_ip_is_limited(self):
        for _ in range(25):
            self.client.post("/payments/liqpay/callback/", self.forged())
        resp = self.client.post("/payments/liqpay/callback/", callback_body(self.payment))
        self.assertEqual(resp.status_code, 200)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)

    def test_valid_event_values_truncated(self):
        self.client.post("/payments/liqpay/callback/",
                         callback_body(self.payment, status="failure", err_description="E" * 5000))
        event = PaymentEvent.objects.get(signature_valid=True)
        self.assertLessEqual(len(event.payload["err_description"]), 255)


class A3FailFastTests(TestCase):
    """Неправильна production-конфігурація зупиняє запуск; у DEBUG — ні."""

    def _start(self, **env_over):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("DJANGO_", "LIQPAY_", "PAYMENTS_", "PLACEMENT_", "SITE_URL"))}
        env.update(DJANGO_SETTINGS_MODULE="config.settings", DJANGO_DEBUG="False",
                   DJANGO_SECRET_KEY="x" * 60, DJANGO_SQLITE_PATH=str(Path(settings.MEDIA_ROOT) / "ff.sqlite3"))
        env.update(env_over)
        return subprocess.run([sys.executable, "-c", "from config.wsgi import application; print('STARTED')"],
                              cwd=settings.BASE_DIR, env=env, capture_output=True, text=True, timeout=60)

    def setUp(self):
        if (Path(settings.BASE_DIR) / ".env").exists():
            self.skipTest("Локальний .env впливає на конфігурацію")

    def test_bad_production_configs_refuse_to_start(self):
        live = dict(PAYMENTS_ENABLED="True", LIQPAY_PUBLIC_KEY="i000live", LIQPAY_PRIVATE_KEY="FAKE",
                    LIQPAY_SANDBOX="False", SITE_URL="https://example.ua")
        cases = {
            "placement без оплати": dict(PLACEMENT_PAYMENT_REQUIRED="True"),
            "оплата без ключів": dict(PAYMENTS_ENABLED="True", SITE_URL="https://example.ua"),
            "v7 + sha1": dict(live, LIQPAY_SIGNATURE_ALGORITHM="sha1"),
            "sandbox-ключ у бойовому": dict(live, LIQPAY_PUBLIC_KEY="sandbox_x"),
            "SITE_URL без https": dict(live, SITE_URL="http://example.ua"),
        }
        for name, env in cases.items():
            proc = self._start(**env)
            self.assertNotEqual(proc.returncode, 0, name)
            self.assertIn("ImproperlyConfigured", proc.stderr, name)
            self.assertNotIn("FAKE", proc.stderr, name)  # значення ключів не виводяться

    def test_valid_production_config_starts(self):
        proc = self._start(PAYMENTS_ENABLED="True", LIQPAY_PUBLIC_KEY="i000live", LIQPAY_PRIVATE_KEY="FAKE",
                           LIQPAY_SANDBOX="False", SITE_URL="https://example.ua")
        self.assertIn("STARTED", proc.stdout, proc.stderr[-500:])
        self.assertIn("STARTED", self._start().stdout)  # оплата вимкнена — теж стартує

    def test_debug_does_not_block(self):
        proc = self._start(DJANGO_DEBUG="True", PLACEMENT_PAYMENT_REQUIRED="True")
        self.assertIn("STARTED", proc.stdout, proc.stderr[-500:])


@override_settings(**LIQ)
class A4StrictSignatureTests(TestCase):
    def test_non_ascii_in_signature_rejected(self):
        data = liqpay.encode_data({"a": 1})
        good = sign(data)
        self.assertTrue(liqpay.verify_signature(data, good))
        for bad in (good[:5] + "ї" + good[5:], good + " ", "​" + good):
            self.assertFalse(liqpay.verify_signature(data, bad))


@override_settings(**LIQ)
class A7VersionAlgorithmTests(TestCase):
    base = dict(public_key="sandbox_x", private_key="p", sandbox=True, site_url="https://e.ua", debug=False)

    def test_only_allowed_pairs(self):
        self.assertEqual(liqpay_problems(version=7, algorithm="sha3_256", **self.base), [])
        self.assertEqual(liqpay_problems(version=3, algorithm="sha1", **self.base), [])
        for version, algorithm in ((7, "sha1"), (3, "sha3_256"), (5, "sha3_256"), (7, "md5")):
            self.assertTrue(liqpay_problems(version=version, algorithm=algorithm, **self.base), (version, algorithm))

    def test_mismatched_pair_never_signs_or_verifies(self):
        with self.settings(LIQPAY_API_VERSION=7, LIQPAY_SIGNATURE_ALGORITHM="sha1"):
            data = liqpay.encode_data({"a": 1})
            with self.assertRaises(liqpay.LiqPayConfigError):
                liqpay.make_signature(data)
            self.assertFalse(liqpay.verify_signature(data, sign(data)))
            self.assertFalse(liqpay.payments_active())
            self.assertIn("properties.E003", {e.id for e in run_checks()})


@override_settings(**LIQ)
class A5A6OpenPaymentAndOwnerTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = make_user("owner")
        self.prop = make_property(owner=self.owner)
        self.client.force_login(self.owner)
        self.start_url = f"/payment/{self.prop.pk}/start/"

    def test_repeated_click_reuses_open_payment(self):
        first = self.client.post(self.start_url).context["payment"]
        second = self.client.post(self.start_url).context["payment"]
        self.assertEqual(first.order_id, second.order_id)
        self.assertEqual(Payment.objects.count(), 1)

    def test_db_constraint_blocks_second_open_payment(self):
        create_payment(self.owner, self.prop, is_test=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_payment(self.owner, self.prop, is_test=True)

    def test_get_or_create_handles_race_via_integrity_error(self):
        existing = create_payment(self.owner, self.prop, is_test=True)
        with mock.patch("properties.placement.open_payment_for", side_effect=[None, existing]):
            payment, created = get_or_create_open_payment(self.owner, self.prop, is_test=True)
        self.assertEqual((payment.pk, created), (existing.pk, False))
        self.assertEqual(Payment.objects.count(), 1)

    def test_payment_page_offers_continue_and_cancel(self):
        payment = self.client.post(self.start_url).context["payment"]
        html = self.client.get(f"/payment/{self.prop.pk}/").content.decode()
        self.assertIn("Є незавершена оплата", html)
        self.assertIn("Продовжити оплату", html)
        self.assertIn(f"/payments/{payment.order_id}/cancel/", html)

    def test_pending_payment_is_not_reopened(self):
        payment = self.client.post(self.start_url).context["payment"]
        Payment.objects.filter(pk=payment.pk).update(status=PaymentStatus.PENDING)
        resp = self.client.post(self.start_url)
        self.assertRedirects(resp, f"/payments/{payment.order_id}/result/", fetch_redirect_response=False)
        self.assertEqual(Payment.objects.count(), 1)

    def _age(self, payment, **delta):
        Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(**delta))

    def test_cancel_blocks_new_payment_while_old_form_is_payable(self):
        # Друга вкладка з формою LiqPay ще жива — нова оплата створила б ризик подвійного списання.
        first = self.client.post(self.start_url).context["payment"]
        self.client.post(f"/payments/{first.order_id}/cancel/")
        resp = self.client.post(self.start_url, follow=True)
        self.assertContains(resp, "подвійного")
        self.assertEqual(Payment.objects.count(), 1)
        # Після закінчення терміну форми + запасу — нова оплата з новим order_id.
        self._age(first, minutes=settings.LIQPAY_PAYMENT_TTL_MINUTES + 31)
        second = self.client.post(self.start_url).context["payment"]
        self.assertNotEqual(first.order_id, second.order_id)

    def test_failed_payment_blocks_new_one_until_window_ends(self):
        first = self.client.post(self.start_url).context["payment"]
        self.client.post("/payments/liqpay/callback/", callback_body(first, status="failure"))
        self.client.post(self.start_url)
        self.assertEqual(Payment.objects.count(), 1)
        self._age(first, minutes=settings.LIQPAY_PAYMENT_TTL_MINUTES + 31)
        self.assertEqual(self.client.post(self.start_url).status_code, 200)
        self.assertEqual(Payment.objects.count(), 2)

    def test_resume_does_not_extend_checkout_expiry(self):
        first = self.client.post(self.start_url).context["payment"]
        self._age(first, minutes=50)
        checkout = self.client.post(self.start_url).context["checkout"]
        first.refresh_from_db()
        expected = first.created_at + timedelta(minutes=settings.LIQPAY_PAYMENT_TTL_MINUTES)
        self.assertEqual(liqpay.decode_data(checkout["data"])["expired_date"],
                         expected.astimezone(dt_timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(liqpay.decode_data(checkout["data"])["order_id"], first.order_id)

    def test_resume_after_form_expired_gives_no_checkout(self):
        first = self.client.post(self.start_url).context["payment"]
        self._age(first, minutes=settings.LIQPAY_PAYMENT_TTL_MINUTES + 5)
        resp = self.client.post(self.start_url)
        self.assertRedirects(resp, f"/payment/{self.prop.pk}/", fetch_redirect_response=False)
        self.assertEqual(Payment.objects.count(), 1)

    def test_paid_order_id_is_credited_once(self):
        # Повторні/запізнілі повідомлення для того самого order_id не дають другого зарахування.
        payment = self.client.post(self.start_url).context["payment"]
        body = callback_body(payment)
        for _ in range(3):
            self.client.post("/payments/liqpay/callback/", body)
        payment.refresh_from_db()
        self.assertIsNotNone(payment.applied_at)
        self.assertEqual(PaymentEvent.objects.filter(payment=payment, note__contains="Оплату зараховано").count(), 1)

    def test_stale_open_payment_expires_and_is_replaced(self):
        first = self.client.post(self.start_url).context["payment"]
        Payment.objects.filter(pk=first.pk).update(created_at=timezone.now() - timedelta(hours=3))
        second = self.client.post(self.start_url).context["payment"]
        first.refresh_from_db()
        self.assertEqual(first.status, PaymentStatus.EXPIRED)
        self.assertNotEqual(first.order_id, second.order_id)

    def test_test_payment_closed_when_mode_changes(self):
        first = self.client.post(self.start_url).context["payment"]
        with self.settings(LIQPAY_SANDBOX=False, LIQPAY_PUBLIC_KEY="i000live", LIQPAY_PRIVATE_KEY="FAKE-live"):
            second = self.client.post(self.start_url).context["payment"]
        first.refresh_from_db()
        self.assertEqual(first.status, PaymentStatus.CANCELLED)
        self.assertFalse(second.is_test)

    def test_mismatch_payment_does_not_block_new_one(self):
        payment = create_payment(self.owner, self.prop, is_test=True)
        self.client.post("/payments/liqpay/callback/", callback_body(payment, amount=1.0))
        self.assertEqual(self.client.post(self.start_url).status_code, 200)
        self.assertEqual(Payment.objects.count(), 2)

    def test_admin_cannot_pay_foreign_listing(self):
        self.client.force_login(make_admin())
        self.assertEqual(self.client.post(self.start_url, {"user": self.owner.pk}).status_code, 404)
        self.assertFalse(Payment.objects.exists())
        self.assertContains(self.client.get(f"/payment/{self.prop.pk}/"), "лише власник")

    def test_owner_id_from_form_is_ignored(self):
        other = make_user("other")
        self.client.force_login(other)
        self.client.post(self.start_url, {"user": other.pk, "owner": other.pk, "property": self.prop.pk})
        self.assertFalse(Payment.objects.exists())

    def test_admin_can_pay_own_listing(self):
        admin = make_admin()
        own = make_property(owner=admin)
        self.client.force_login(admin)
        self.assertEqual(self.client.post(f"/payment/{own.pk}/start/").status_code, 200)

    def test_listing_without_owner_cannot_be_paid(self):
        orphan = make_property(owner=None)
        self.client.force_login(make_admin())
        self.assertEqual(self.client.post(f"/payment/{orphan.pk}/start/").status_code, 404)


@override_settings(**LIQ)
class ConcurrentPaymentCreationTests(TransactionTestCase):
    """Паралельні натискання «Оплатити» (дві вкладки) — у БД рівно одна незавершена оплата."""

    serialized_rollback = True  # flush видаляє тариф із міграції 0022; відновлюємо його

    def test_parallel_start_creates_single_open_payment(self):
        owner = make_user("owner")
        prop = make_property(owner=owner)
        n = 6
        barrier = threading.Barrier(n)
        order_ids, failures = [], []

        # Логін — послідовно до старту потоків: паралельний force_login сам блокує SQLite.
        clients = []
        for _ in range(n):
            client = Client()
            client.force_login(owner)
            clients.append(client)

        def worker(client):
            try:
                barrier.wait(timeout=30)  # без тайм-ауту падіння одного потоку вішає тест назавжди
                resp = client.post(f"/payment/{prop.pk}/start/")
                if resp.status_code == 200:
                    order_ids.append(resp.context["payment"].order_id)
                else:
                    failures.append(f"HTTP {resp.status_code}")
            except Exception as exc:  # noqa: BLE001 — фіксуємо будь-який виняток із traceback
                failures.append({"type": type(exc).__name__, "message": str(exc),
                                 "locked": _locked(exc), "traceback": traceback.format_exc()})
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(c,)) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Допустима лише конкуренція SQLite «database is locked»; решта — з типом і traceback у звіті.
        unexpected = [f for f in failures if not (isinstance(f, dict) and f["locked"])]
        self.assertEqual(unexpected, [], unexpected)
        self.assertEqual(Payment.objects.count(), 1, (order_ids, failures))
        self.assertLessEqual(len(set(order_ids)), 1)
        self.assertEqual(len(order_ids) + len(failures), n)


ALERT = "[ALERT duplicate_charge]"


def _alerts(payment):
    return PaymentEvent.objects.filter(payment=payment, note__startswith=ALERT)


def _alert_ids(payment):
    # Значення з payload у Python: SQLite при вибірці JSON-ключа через ORM перетворює "222" на 222.
    return sorted(e.payload["payment_id"] for e in _alerts(payment))


class _CreditedPaymentBase(TestCase):
    """Платіж активного оголошення; send() надсилає підписаний callback LiqPay."""

    def setUp(self):
        cache.clear()
        self.owner = make_user("owner")
        self.until = timezone.now() + timedelta(days=5)
        self.prop = make_property(owner=self.owner, placement_until=self.until)
        self.payment = create_payment(self.owner, self.prop, is_test=True)
        self.cb = "/payments/liqpay/callback/"

    def send(self, payment_id, **extra):
        return self.client.post(self.cb, callback_body(self.payment, payment_id=payment_id, **extra))

    def state(self):
        self.payment.refresh_from_db()
        return self.payment.provider_transaction_id, Property.objects.get(pk=self.prop.pk).placement_until

    def finance(self):
        """Фінансовий стан платежу (крім needs_review) — не має змінюватись від чужих payment_id."""
        self.payment.refresh_from_db()
        fields = ("status", "provider_transaction_id", "paid_amount", "paid_currency", "paid_at", "applied_at",
                  "period_start", "period_end", "activated_at", "error_message")
        return {f: getattr(self.payment, f) for f in fields}, Property.objects.get(pk=self.prop.pk).placement_until


@override_settings(**LIQ)
class DuplicateChargeTests(_CreditedPaymentBase):
    """Другий успішний платіж за тим самим order_id з ІНШИМ payment_id — не зараховується, але помітний."""

    def test_same_payment_id_repeat_is_idempotent(self):
        self.send(111)
        first = self.state()
        for _ in range(3):
            self.assertEqual(self.send(111).status_code, 200)
        self.assertEqual(self.state(), first)
        self.assertEqual(first, ("111", self.until + timedelta(days=30)))
        self.assertFalse(self.payment.needs_review)
        self.assertFalse(_alerts(self.payment).exists())

    def test_other_payment_id_after_success_is_flagged_not_credited(self):
        self.send(111)
        first = self.state()
        resp = self.send(222, sender_card_mask2="424242*42", sender_phone="380000000000")
        self.assertEqual(resp.status_code, 200)  # прийнято, щоб LiqPay не повторював без кінця
        self.assertEqual(self.state(), first)  # перший payment_id і термін не змінились
        self.assertTrue(self.payment.needs_review)
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)
        event = _alerts(self.payment).get()
        self.assertEqual(event.order_id, self.payment.order_id)
        self.assertEqual(event.payload["payment_id"], "222")
        self.assertIn("222", event.note)
        self.assertIn("111", event.note)
        dumped = str(event.payload) + event.note
        for secret in ("424242", "380000000000", "sender"):
            self.assertNotIn(secret, dumped)  # без карткових і персональних даних

    def test_repeated_second_payment_id_creates_single_alert(self):
        self.send(111)
        first = self.state()
        for _ in range(4):
            self.send(222)
        self.send(333)
        self.assertEqual(self.state(), first)
        self.assertEqual(
            _alert_ids(self.payment), ["222", "333"])
        self.assertEqual(Payment.objects.filter(needs_review=True).count(), 1)

    def test_second_charge_via_status_api_is_flagged_too(self):
        self.send(111)
        fields = liqpay.decode_data(callback_body(self.payment, payment_id=444)["data"])
        result = liqpay.process_payment_fields(fields, signature_valid=True, source="status")
        self.assertEqual(result.note, "duplicate_charge")
        self.assertEqual(self.state()[0], "111")
        self.assertTrue(_alerts(self.payment).filter(payload__payment_id="444").exists())

    def test_admin_sees_warning(self):
        self.send(111)
        self.send(222)
        self.client.force_login(make_admin())
        for url in ("/admin/properties/payment/", "/admin/properties/property/",
                    f"/admin/properties/payment/{self.payment.pk}/change/"):
            self.assertContains(self.client.get(url), "ДРУГИМ успішним списанням", msg_prefix=url)
        filtered = self.client.get("/admin/properties/payment/?needs_review__exact=1")
        self.assertContains(filtered, self.payment.order_id)

    def test_no_warning_without_duplicates(self):
        self.send(111)
        self.client.force_login(make_admin())
        self.assertNotContains(self.client.get("/admin/properties/payment/"), "ДРУГИМ успішним списанням")


@override_settings(**LIQ)
class ParallelDuplicateChargeTests(TransactionTestCase):
    """Паралельні callback із РІЗНИМИ payment_id: одне зарахування, решта — позначені списання."""

    serialized_rollback = True  # flush видаляє тариф із міграції 0022; відновлюємо його

    def test_parallel_different_payment_ids(self):
        owner = make_user("owner")
        until = timezone.now() + timedelta(days=5)
        prop = make_property(owner=owner, placement_until=until)
        payment = create_payment(owner, prop, is_test=True)
        n = 5
        bodies = [callback_body(payment, payment_id=900 + i) for i in range(n)]
        barrier = threading.Barrier(n)
        results, errors = [], []

        def worker(body):
            try:
                barrier.wait(timeout=30)
                results.append(liqpay.handle_callback(body["data"], body["signature"]).note)
            except Exception as exc:  # noqa: BLE001 — зберігаємо тип, повідомлення і traceback
                errors.append({"type": type(exc).__name__, "message": str(exc),
                               "locked": _locked(exc), "traceback": traceback.format_exc()})
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(b,)) for b in bodies]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        unexpected = [e for e in errors if not e["locked"]]
        self.assertEqual(unexpected, [], "\n\n".join(e["traceback"] for e in unexpected))
        self.assertLessEqual(results.count("credited"), 1)
        # LiqPay повторює неприйняті callback (SQLite «database is locked») — повторюємо всі послідовно.
        for body in bodies:
            liqpay.handle_callback(body["data"], body["signature"])
        payment.refresh_from_db()
        credited_id = payment.provider_transaction_id
        self.assertIn(credited_id, {str(900 + i) for i in range(n)})
        self.assertEqual(Property.objects.get(pk=prop.pk).placement_until, until + timedelta(days=30))
        self.assertTrue(payment.needs_review)
        alert_ids = _alert_ids(payment)
        self.assertEqual(alert_ids, sorted(str(900 + i) for i in range(n) if str(900 + i) != credited_id))
        self.assertEqual(PaymentEvent.objects.filter(payment=payment, note__contains="Оплату зараховано").count(), 1)


REFUND_ALERT = "[ALERT duplicate_refund]"


def _refund_alert_ids(payment):
    return sorted(e.payload["payment_id"] for e in PaymentEvent.objects.filter(
        payment=payment, note__startswith=REFUND_ALERT))


@override_settings(**LIQ)
class DuplicateReversedTests(_CreditedPaymentBase):
    """reversed прив'язується до зарахованого payment_id; повернення другого списання не чіпає перший платіж."""

    def test_reversed_of_first_payment_id_follows_refund_rules(self):
        self.send(111)
        until = self.state()[1]
        self.assertEqual(self.send(111, status="reversed").status_code, 200)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.REVERSED)
        self.assertIn("Кошти повернено", self.payment.error_message)
        self.assertEqual(self.state(), ("111", until))  # чинна політика: період не скасовується автоматично
        self.assertFalse(self.payment.needs_review)
        self.assertEqual(_refund_alert_ids(self.payment), [])

    def test_reversed_of_second_payment_id_keeps_first_payment_intact(self):
        self.send(111)
        before = self.finance()
        self.send(222)  # друге списання -> DuplicateChargeError
        self.assertEqual(self.send(222, status="reversed").status_code, 200)
        self.assertEqual(self.finance(), before)
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)
        self.assertTrue(self.payment.needs_review)
        self.assertEqual(_refund_alert_ids(self.payment), ["222"])
        self.assertEqual(_alert_ids(self.payment), ["222"])  # подія про списання збережена
        event = PaymentEvent.objects.get(payment=self.payment, note__startswith=REFUND_ALERT)
        self.assertEqual((event.order_id, event.provider_status), (self.payment.order_id, "reversed"))

    def test_repeated_reversed_of_second_payment_id_single_event(self):
        self.send(111)
        self.send(222)
        before = self.finance()
        for _ in range(3):
            self.send(222, status="reversed")
        self.assertEqual(self.finance(), before)
        self.assertEqual(_refund_alert_ids(self.payment), ["222"])

    def test_reversed_via_status_api(self):
        self.send(111)
        self.send(222)
        before = self.finance()

        def status_api(payment_id):
            fields = liqpay.decode_data(callback_body(self.payment, payment_id=payment_id, status="reversed")["data"])
            return liqpay.process_payment_fields(fields, signature_valid=True, source="status").note

        self.assertEqual(status_api(222), "duplicate_reversed")
        self.assertEqual(self.finance(), before)
        self.assertEqual(status_api(111), "reversed")  # законне повернення — за чинними правилами
        self.payment.refresh_from_db()
        self.assertEqual((self.payment.status, self.payment.provider_transaction_id), (PaymentStatus.REVERSED, "111"))
        self.assertTrue(self.payment.needs_review)

    def test_needs_review_survives_callbacks_status_api_and_sync(self):
        self.send(111)
        self.send(222)
        for extra in ({}, {"status": "failure"}, {"status": "processing"}, {"status": "sandbox"}):
            self.send(111, **extra)
        fields = liqpay.decode_data(callback_body(self.payment, payment_id=111)["data"])
        liqpay.process_payment_fields(fields, signature_valid=True, source="status")
        call_command("liqpay_sync_pending", stdout=StringIO())
        liqpay.expire_stale(self.payment, now=timezone.now() + timedelta(days=1))
        self.send(111, status="reversed")  # навіть законне повернення не знімає позначку
        self.payment.refresh_from_db()
        self.assertTrue(self.payment.needs_review)


@override_settings(**LIQ)
class ParallelSuccessReversedTests(TransactionTestCase):
    """Паралельні success/reversed для різних payment_id після зарахування першого."""

    serialized_rollback = True  # flush видаляє тариф із міграції 0022; відновлюємо його

    def test_parallel_success_and_reversed(self):
        owner = make_user("owner")
        until = timezone.now() + timedelta(days=5)
        prop = make_property(owner=owner, placement_until=until)
        payment = create_payment(owner, prop, is_test=True)
        first = callback_body(payment, payment_id=111)
        liqpay.handle_callback(first["data"], first["signature"])
        payment.refresh_from_db()
        credited = (payment.provider_transaction_id, payment.paid_amount, payment.paid_at, payment.applied_at,
                    payment.period_start, payment.period_end)
        bodies = [callback_body(payment, payment_id=222), callback_body(payment, payment_id=222, status="reversed"),
                  callback_body(payment, payment_id=111, status="reversed"), callback_body(payment, payment_id=222)]
        barrier = threading.Barrier(len(bodies))
        errors = []

        def worker(body):
            try:
                barrier.wait(timeout=30)
                liqpay.handle_callback(body["data"], body["signature"])
            except Exception as exc:  # noqa: BLE001 — зберігаємо тип, повідомлення і traceback
                errors.append({"type": type(exc).__name__, "message": str(exc),
                               "locked": _locked(exc), "traceback": traceback.format_exc()})
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(b,)) for b in bodies]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        unexpected = [e for e in errors if not e["locked"]]
        self.assertEqual(unexpected, [], "\n\n".join(e["traceback"] for e in unexpected))
        for body in bodies:  # повтори LiqPay після «database is locked» (SQLite)
            liqpay.handle_callback(body["data"], body["signature"])
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.REVERSED)  # законне повернення 111
        self.assertEqual((payment.provider_transaction_id, payment.paid_amount, payment.paid_at, payment.applied_at,
                          payment.period_start, payment.period_end), credited)
        self.assertEqual(Property.objects.get(pk=prop.pk).placement_until, until + timedelta(days=30))
        self.assertTrue(payment.needs_review)
        self.assertEqual(_alert_ids(payment), ["222"])
        self.assertEqual(_refund_alert_ids(payment), ["222"])


REVIEW = "[REVIEW closed]"
CHANGELIST = "/admin/properties/payment/"


def _staff(username, *codenames):
    from django.contrib.auth.models import Permission

    user = make_user(username, is_staff=True)
    user.user_permissions.set(Permission.objects.filter(content_type__app_label="properties",
                                                        codename__in=codenames))
    return user


def _review_logs(payment):
    from django.contrib.admin.models import CHANGE, LogEntry
    from django.contrib.contenttypes.models import ContentType

    return LogEntry.objects.filter(content_type=ContentType.objects.get_for_model(Payment),
                                   object_id=str(payment.pk), action_flag=CHANGE)


@override_settings(**LIQ)
class AdminReviewActionTests(_CreditedPaymentBase):
    """Дія «Позначити перевіреним»: дозвіл review_payment, підтвердження, аудит, лише needs_review."""

    def setUp(self):
        super().setUp()
        self.send(111)
        self.send(222)
        self.send(222, status="reversed")
        self.payment.refresh_from_db()
        self.assertTrue(self.payment.needs_review)
        self.reviewer = _staff("reviewer", "view_payment", "review_payment")
        self.viewer = _staff("viewer", "view_payment")

    def stage(self, **extra):
        return self.client.post(CHANGELIST, {"action": "mark_reviewed", "_selected_action": [self.payment.pk],
                                             **extra})

    def confirm(self, seen=None):
        seen = _latest_alert_id_for(self.payment) if seen is None else seen
        return self.stage(post="yes", **{f"alerts_{self.payment.pk}": seen})

    def alerts(self):
        return list(PaymentEvent.objects.filter(payment=self.payment, note__startswith="[ALERT")
                    .order_by("id").values_list("id", "note", "payload", "created_at"))

    def test_action_visible_only_with_permission(self):
        self.client.force_login(self.reviewer)
        self.assertContains(self.client.get(CHANGELIST), 'value="mark_reviewed"')
        self.client.force_login(self.viewer)
        resp = self.client.get(CHANGELIST)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'value="mark_reviewed"')

    def test_direct_post_without_permission_is_403(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.stage().status_code, 403)
        self.assertEqual(self.confirm().status_code, 403)
        self.payment.refresh_from_db()
        self.assertTrue(self.payment.needs_review)
        self.assertFalse(PaymentEvent.objects.filter(note__startswith=REVIEW).exists())

    def test_plain_staff_without_any_payment_permission_is_denied(self):
        self.client.force_login(_staff("intern"))
        self.assertIn(self.confirm().status_code, (302, 403))  # без view_payment адмінка не пускає взагалі
        self.payment.refresh_from_db()
        self.assertTrue(self.payment.needs_review)

    def test_confirmation_page_shows_order_status_and_alerts(self):
        self.client.force_login(self.reviewer)
        resp = self.stage()
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "admin/properties/payment/confirm_review.html")
        for text in (self.payment.order_id, "потребує перевірки", "[ALERT duplicate_charge]",
                     "[ALERT duplicate_refund]", "Так, перевірено"):
            self.assertContains(resp, text)
        self.payment.refresh_from_db()
        self.assertTrue(self.payment.needs_review)  # сторінка підтвердження нічого не змінює

    def test_close_review_keeps_finance_and_alerts_and_writes_audit(self):
        before_finance, before_alerts = self.finance(), self.alerts()
        self.client.force_login(self.reviewer)
        self.assertEqual(self.confirm().status_code, 302)
        self.payment.refresh_from_db()
        self.assertFalse(self.payment.needs_review)
        self.assertEqual(self.finance(), before_finance)
        self.assertEqual(self.alerts(), before_alerts)
        event = PaymentEvent.objects.get(payment=self.payment, note__startswith=REVIEW)
        self.assertIn("reviewer", event.note)
        self.assertEqual(event.payload["reviewed_by"], "reviewer")
        self.assertTrue(event.payload["reviewed_at"])
        self.assertEqual(event.payload["alert_event_ids"], [a[0] for a in before_alerts])
        log = _review_logs(self.payment).get()
        self.assertEqual(log.user, self.reviewer)
        self.assertIn("needs_review", log.get_change_message())

    def test_repeated_close_creates_no_duplicates(self):
        self.client.force_login(self.reviewer)
        for _ in range(3):
            self.confirm()
        self.assertEqual(PaymentEvent.objects.filter(payment=self.payment, note__startswith=REVIEW).count(), 1)
        self.assertEqual(_review_logs(self.payment).count(), 1)

    def test_not_flagged_payment_cannot_be_closed(self):
        Payment.objects.filter(pk=self.payment.pk).update(needs_review=False)
        self.client.force_login(self.reviewer)
        resp = self.client.post(CHANGELIST, {"action": "mark_reviewed", "_selected_action": [self.payment.pk]},
                                follow=True)
        self.assertContains(resp, "немає платежів, що потребують перевірки")
        self.assertFalse(PaymentEvent.objects.filter(note__startswith=REVIEW).exists())

    def test_new_alert_after_confirmation_page_blocks_close(self):
        self.client.force_login(self.reviewer)
        seen = _latest_alert_id_for(self.payment)
        self.send(333)  # нове друге списання, поки адміністратор читав сторінку підтвердження
        self.confirm(seen=seen)
        self.payment.refresh_from_db()
        self.assertTrue(self.payment.needs_review)
        self.assertFalse(PaymentEvent.objects.filter(note__startswith=REVIEW).exists())


def _latest_alert_id_for(payment):
    return PaymentEvent.objects.filter(payment=payment, note__startswith="[ALERT").order_by("id").last().id


@override_settings(**LIQ)
class ParallelReviewCloseTests(TransactionTestCase):
    serialized_rollback = True  # flush видаляє тариф із міграції 0022; відновлюємо його

    def test_parallel_close_attempts_create_single_event(self):
        owner = make_user("owner")
        prop = make_property(owner=owner, placement_until=timezone.now() + timedelta(days=5))
        payment = create_payment(owner, prop, is_test=True)
        for pid in (111, 222):
            body = callback_body(payment, payment_id=pid)
            liqpay.handle_callback(body["data"], body["signature"])
        seen = _latest_alert_id_for(payment)
        reviewer = _staff("reviewer", "view_payment", "review_payment")
        n = 4
        clients = []
        for _ in range(n):
            client = Client()
            client.force_login(reviewer)
            clients.append(client)
        data = {"action": "mark_reviewed", "_selected_action": [payment.pk], "post": "yes",
                f"alerts_{payment.pk}": seen}
        barrier = threading.Barrier(n)
        errors = []

        def worker(client):
            try:
                barrier.wait(timeout=30)
                client.post(CHANGELIST, data)
            except Exception as exc:  # noqa: BLE001 — зберігаємо тип, повідомлення і traceback
                errors.append({"type": type(exc).__name__, "message": str(exc),
                               "locked": _locked(exc), "traceback": traceback.format_exc()})
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(c,)) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        unexpected = [e for e in errors if not e["locked"]]
        self.assertEqual(unexpected, [], "\n\n".join(e["traceback"] for e in unexpected))
        if len(errors) == n:  # усі потоки впали на SQLite-блокуванні — повторюємо, як зробив би адміністратор
            clients[0].post(CHANGELIST, data)
        payment.refresh_from_db()
        self.assertFalse(payment.needs_review)
        self.assertEqual(PaymentEvent.objects.filter(payment=payment, note__startswith=REVIEW).count(), 1)
        self.assertEqual(_review_logs(payment).count(), 1)
