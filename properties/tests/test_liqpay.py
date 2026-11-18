"""LiqPay: підпис, checkout, callback, запит статусу, безпека, бізнес-логіка розміщення."""

import base64
import hashlib
import json
import threading
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.checks import run_checks
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from properties import liqpay
from properties.models import ModerationStatus, Payment, PaymentEvent, PaymentStatus, Property
from properties.placement import create_payment

from .helpers import make_admin, make_property, make_user

# Фіктивні тестові ключі (не справжні): лише для обчислення підписів у тестах.
PUB = "sandbox_i00000000test"
PRIV = "sandbox_FAKE-private-key-for-tests-only"
LIQ = dict(PAYMENTS_ENABLED=True, LIQPAY_PUBLIC_KEY=PUB, LIQPAY_PRIVATE_KEY=PRIV, LIQPAY_SANDBOX=True,
           LIQPAY_API_VERSION=7, LIQPAY_SIGNATURE_ALGORITHM="sha3_256", SITE_URL="https://example.ua")
PROD_PUB = "i00000000live"
PROD = dict(LIQ, LIQPAY_PUBLIC_KEY=PROD_PUB, LIQPAY_PRIVATE_KEY="FAKE-live-private", LIQPAY_SANDBOX=False)


def sign(data, priv=PRIV, algo=hashlib.sha3_256):
    return base64.b64encode(algo((priv + data + priv).encode()).digest()).decode()


def callback_body(payment, priv=PRIV, **over):
    fields = {"action": "pay", "status": "success", "order_id": payment.order_id, "payment_id": 777000 + payment.pk,
              "amount": float(payment.amount), "currency": payment.currency, "public_key": PUB, "version": 7,
              "end_date": int(timezone.now().timestamp() * 1000),
              "sender_card_mask2": "424242*42", "sender_phone": "380000000000", "sender_first_name": "Тест"}
    fields.update(over)
    data = base64.b64encode(json.dumps(fields).encode()).decode()
    return {"data": data, "signature": sign(data, priv)}


@override_settings(**LIQ)
class SignatureTests(TestCase):
    def test_sha3_256_matches_spec_formula(self):
        data = liqpay.encode_data({"a": 1})
        self.assertEqual(liqpay.make_signature(data), sign(data))
        self.assertEqual(len(base64.b64decode(liqpay.make_signature(data))), 32)  # sha3-256
        self.assertTrue(liqpay.verify_signature(data, sign(data)))

    @override_settings(LIQPAY_SIGNATURE_ALGORITHM="sha1", LIQPAY_API_VERSION=3)
    def test_sha1_only_when_configured(self):
        data = liqpay.encode_data({"a": 1})
        self.assertTrue(liqpay.verify_signature(data, sign(data, algo=hashlib.sha1)))
        self.assertFalse(liqpay.verify_signature(data, sign(data)))  # sha3 не приймається в режимі sha1

    def test_wrong_or_malformed_signatures_rejected(self):
        data = liqpay.encode_data({"a": 1})
        for bad in ("", "x", sign(data, priv="other"), sign(data, algo=hashlib.sha1), sign(data) + "A", None, "ї" * 10):
            self.assertFalse(liqpay.verify_signature(data, bad), bad)
        self.assertFalse(liqpay.verify_signature("", sign("")))

    def test_uses_constant_time_compare(self):
        data = liqpay.encode_data({"a": 1})
        with mock.patch.object(liqpay.hmac, "compare_digest", wraps=liqpay.hmac.compare_digest) as cd:
            liqpay.verify_signature(data, sign(data))
        self.assertTrue(cd.called)

    def test_private_key_never_in_repr(self):
        self.assertNotIn(PRIV, repr(liqpay.LiqPayConfig.from_settings()))


@override_settings(**LIQ)
class CheckoutTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.prop = make_property(owner=self.owner)
        self.client.force_login(self.owner)

    def test_start_payment_builds_signed_checkout_with_server_amount(self):
        resp = self.client.post(f"/payment/{self.prop.pk}/start/", {"amount": "1", "currency": "USD"})
        self.assertEqual(resp.status_code, 200)
        payment = Payment.objects.get()
        self.assertEqual((payment.amount, payment.currency, payment.is_test), (Decimal("99.00"), "UAH", True))
        checkout = resp.context["checkout"]
        self.assertEqual(checkout["url"], "https://www.liqpay.ua/api/3/checkout")
        self.assertTrue(liqpay.verify_signature(checkout["data"], checkout["signature"]))
        params = liqpay.decode_data(checkout["data"])
        self.assertEqual((params["amount"], params["currency"], params["action"], params["version"]), ("99.00", "UAH", "pay", 7))
        self.assertEqual(params["order_id"], payment.order_id)
        self.assertEqual(params["public_key"], PUB)
        self.assertEqual(params["server_url"], "https://example.ua/payments/liqpay/callback/")
        self.assertEqual(params["result_url"], f"https://example.ua/payments/{payment.order_id}/result/")
        html = resp.content.decode()
        self.assertIn('action="https://www.liqpay.ua/api/3/checkout"', html)
        self.assertNotIn(PRIV, html)
        self.assertNotIn(PRIV, base64.b64decode(checkout["data"]).decode())
        for card_field in ("card_number", "cvv", "cvc", "expiry"):
            self.assertNotIn(card_field, html.lower())

    def test_unique_order_ids(self):
        for _ in range(3):
            self.client.post(f"/payment/{self.prop.pk}/start/")
        ids = list(Payment.objects.values_list("order_id", flat=True))
        self.assertEqual(len(ids), len(set(ids)))

    def test_only_owner_can_pay(self):
        self.client.force_login(make_user("stranger"))
        self.assertEqual(self.client.post(f"/payment/{self.prop.pk}/start/").status_code, 404)
        self.client.logout()
        self.client.post(f"/payment/{self.prop.pk}/start/")
        self.assertFalse(Payment.objects.exists())

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(f"/payment/{self.prop.pk}/start/").status_code, 405)

    @override_settings(PAYMENTS_ENABLED=False)
    def test_disabled_creates_nothing(self):
        self.client.post(f"/payment/{self.prop.pk}/start/")
        self.assertFalse(Payment.objects.exists())
        self.assertNotContains(self.client.get(f"/payment/{self.prop.pk}/"), "/start/")

    @override_settings(LIQPAY_PRIVATE_KEY="")
    def test_not_active_without_keys(self):
        self.assertFalse(liqpay.payments_active())
        self.client.post(f"/payment/{self.prop.pk}/start/")
        self.assertFalse(Payment.objects.exists())

    def test_payment_page_shows_pay_button(self):
        self.assertContains(self.client.get(f"/payment/{self.prop.pk}/"), f"/payment/{self.prop.pk}/start/")

    def test_rate_limit(self):
        # Не більше 10 нових оплат за годину на користувача (на одне оголошення — лише одна відкрита).
        for _ in range(12):
            self.client.post(f"/payment/{make_property(owner=self.owner).pk}/start/")
        self.assertEqual(Payment.objects.count(), 10)


@override_settings(**LIQ)
class CallbackTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.live = make_property(owner=self.owner, placement_until=timezone.now() + timedelta(days=5))
        self.payment = create_payment(self.owner, self.live, is_test=True)

    def post(self, body):
        return self.client.post("/payments/liqpay/callback/", body)

    def test_valid_success_credits_and_extends(self):
        before = self.live.placement_until
        resp = self.post(callback_body(self.payment))
        self.assertEqual((resp.status_code, resp.content), (200, b"OK"))
        self.payment.refresh_from_db()
        self.live.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)
        self.assertEqual(self.payment.paid_amount, Decimal("99.00"))
        self.assertEqual(self.payment.provider_transaction_id, str(777000 + self.payment.pk))
        self.assertEqual(self.live.placement_until, before + timedelta(days=30))
        self.assertTrue(PaymentEvent.objects.filter(payment=self.payment, signature_valid=True).exists())

    def test_callback_is_csrf_exempt(self):
        from django.test import Client
        resp = Client(enforce_csrf_checks=True).post("/payments/liqpay/callback/", callback_body(self.payment))
        self.assertEqual(resp.status_code, 200)

    def test_forged_signature_rejected(self):
        body = callback_body(self.payment, priv="attacker-guess")
        resp = self.post(body)
        self.assertEqual(resp.status_code, 400)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CREATED)
        self.assertTrue(PaymentEvent.objects.filter(signature_valid=False).exists())

    def test_tampered_data_with_old_signature_rejected(self):
        body = callback_body(self.payment, status="failure")
        forged = callback_body(self.payment)
        resp = self.post({"data": forged["data"], "signature": body["signature"]})
        self.assertEqual(resp.status_code, 400)
        self.payment.refresh_from_db()
        self.assertNotEqual(self.payment.status, PaymentStatus.SUCCESS)

    def test_missing_fields_and_garbage(self):
        for body in ({}, {"data": "!!!", "signature": "x"}, {"data": "e30=", "signature": ""}):
            self.assertEqual(self.post(body).status_code, 400)
        self.assertEqual(self.client.get("/payments/liqpay/callback/").status_code, 405)

    def test_signed_but_not_json_data(self):
        data = base64.b64encode(b"not json").decode()
        self.assertEqual(self.post({"data": data, "signature": sign(data)}).status_code, 400)

    def test_wrong_amount_and_currency_not_credited(self):
        for over in ({"amount": 1.0}, {"amount": 98.99}, {"currency": "USD"}, {"amount": "abc"}):
            self.post(callback_body(self.payment, **over))
            self.payment.refresh_from_db()
            self.assertNotEqual(self.payment.status, PaymentStatus.SUCCESS, over)
            self.assertIsNone(self.payment.applied_at)

    def test_unknown_order_id(self):
        body = callback_body(self.payment, order_id="mh-999-unknown")
        self.assertEqual(self.post(body).status_code, 200)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CREATED)
        self.assertTrue(PaymentEvent.objects.filter(order_id="mh-999-unknown", payment=None).exists())

    def test_foreign_public_key_not_credited(self):
        self.post(callback_body(self.payment, public_key="sandbox_other_shop"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CREATED)

    def test_unexpected_action_not_credited(self):
        self.post(callback_body(self.payment, action="hold"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CREATED)

    def test_repeated_callback_is_idempotent(self):
        body = callback_body(self.payment)
        for _ in range(3):
            self.assertEqual(self.post(body).status_code, 200)
        self.live.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(self.live.placement_until, self.payment.period_end)
        self.assertEqual(self.payment.period_end - self.payment.period_start, timedelta(days=30))
        self.assertEqual(PaymentEvent.objects.filter(note__contains="Оплату зараховано").count(), 1)

    def test_sandbox_status_accepted_in_test_mode(self):
        self.post(callback_body(self.payment, status="sandbox"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)

    def test_failure_error_pending_statuses(self):
        # Окреме оголошення для кожного випадку: на одне оголошення — лише одна незавершена оплата.
        for status, expected in (("failure", PaymentStatus.FAILURE), ("error", PaymentStatus.FAILURE)):
            p = create_payment(self.owner, make_property(owner=self.owner), is_test=True)
            self.post(callback_body(p, status=status, err_code="limit"))
            p.refresh_from_db()
            self.assertEqual(p.status, expected)
            self.assertIn("limit", p.error_message)
        for status in ("3ds_verify", "processing", "wait_secure", "otp_verify"):
            p = create_payment(self.owner, make_property(owner=self.owner), is_test=True)
            self.post(callback_body(p, status=status))
            p.refresh_from_db()
            self.assertEqual(p.status, PaymentStatus.PENDING, status)
        self.live.refresh_from_db()
        self.assertEqual(Payment.objects.filter(status=PaymentStatus.SUCCESS).count(), 0)

    def test_pending_after_failure_does_not_reopen(self):
        self.post(callback_body(self.payment, status="failure"))
        self.post(callback_body(self.payment, status="processing"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.FAILURE)

    def test_late_failure_after_success_ignored(self):
        self.post(callback_body(self.payment))
        self.post(callback_body(self.payment, status="failure"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)

    def test_reversed(self):
        self.post(callback_body(self.payment))
        self.post(callback_body(self.payment, status="reversed"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.REVERSED)

    def test_event_payload_has_no_payer_details(self):
        self.post(callback_body(self.payment))
        for event in PaymentEvent.objects.all():
            dumped = json.dumps(event.payload, ensure_ascii=False)
            for secret in ("424242*42", "380000000000", "Тест", PRIV):
                self.assertNotIn(secret, dumped)

    def test_payment_cannot_credit_another_listing(self):
        other = make_property(owner=self.owner, placement_until=timezone.now() + timedelta(days=2))
        other_before = other.placement_until
        self.post(callback_body(self.payment, description=f"оголошення #{other.pk}", property_id=other.pk))
        other.refresh_from_db()
        self.assertEqual(other.placement_until, other_before)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.property_id, self.live.pk)


@override_settings(**PROD)
class ProductionModeTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.prop = make_property(owner=self.owner)

    def body(self, payment, **over):
        return callback_body(payment, priv="FAKE-live-private", public_key=PROD_PUB, **over)

    def test_sandbox_status_rejected_in_production(self):
        payment = create_payment(self.owner, self.prop, is_test=False)
        self.client.post("/payments/liqpay/callback/", self.body(payment, status="sandbox"))
        payment.refresh_from_db()
        self.assertNotEqual(payment.status, PaymentStatus.SUCCESS)
        self.assertTrue(PaymentEvent.objects.filter(note__contains="sandbox").exists())

    def test_test_payment_not_credited_in_production(self):
        payment = create_payment(self.owner, self.prop, is_test=True)
        self.client.post("/payments/liqpay/callback/", self.body(payment))
        payment.refresh_from_db()
        self.assertNotEqual(payment.status, PaymentStatus.SUCCESS)

    def test_live_success_credited(self):
        payment = create_payment(self.owner, self.prop, is_test=False)
        self.client.post("/payments/liqpay/callback/", self.body(payment))
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.SUCCESS)

    def test_sandbox_key_in_production_is_config_error(self):
        with self.settings(LIQPAY_PUBLIC_KEY="sandbox_x"):
            self.assertFalse(liqpay.payments_active())
            self.assertTrue(any(e.id == "properties.E002" for e in run_checks()))


@override_settings(**LIQ)
class ResultUrlAndStatusTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.prop = make_property(owner=self.owner, public=False)
        self.payment = create_payment(self.owner, self.prop, is_test=True)
        self.client.force_login(self.owner)
        self.url = f"/payments/{self.payment.order_id}/result/"
        from django.core.cache import cache
        cache.clear()

    def _status_response(self, **over):
        resp = {"result": "ok", "action": "pay", "status": "success", "order_id": self.payment.order_id,
                "payment_id": 555, "amount": 99.0, "currency": "UAH", "public_key": PUB, "version": 7}
        resp.update(over)
        return resp

    def test_return_without_callback_does_not_confirm(self):
        with mock.patch.object(liqpay, "_post_api", return_value={"result": "error", "status": "error",
                                                                  "err_code": "payment_not_found"}):
            for resp in (self.client.get(self.url + "?status=success&amount=99"),
                         self.client.post(self.url, callback_body(self.payment))):
                self.assertEqual(resp.status_code, 200)
                self.assertContains(resp, "Очікуємо підтвердження")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CREATED)

    def test_status_check_pending(self):
        with mock.patch.object(liqpay, "_post_api", return_value=self._status_response(status="3ds_verify")):
            self.client.get(self.url)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.PENDING)

    def test_status_check_success_credits_and_waits_moderation(self):
        with mock.patch.object(liqpay, "_post_api", return_value=self._status_response()) as api:
            resp = self.client.get(self.url)
        sent = api.call_args.args[0]
        self.assertEqual((sent["action"], sent["order_id"], sent["public_key"]), ("status", self.payment.order_id, PUB))
        self.payment.refresh_from_db()
        self.prop.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)
        self.assertIsNone(self.payment.period_start)
        self.assertFalse(self.prop.is_published)
        self.assertContains(resp, "почнуться з моменту публікації")

    def test_status_check_rejects_mismatches(self):
        for over in ({"order_id": "mh-other"}, {"public_key": "sandbox_other"}, {"amount": 1.0}, {"currency": "EUR"}):
            with mock.patch.object(liqpay, "_post_api", return_value=self._status_response(**over)):
                liqpay.fetch_status(self.payment)
            self.payment.refresh_from_db()
            self.assertNotEqual(self.payment.status, PaymentStatus.SUCCESS, over)

    def test_status_signed_response_verified(self):
        data = liqpay.encode_data(self._status_response())
        with mock.patch.object(liqpay, "_post_api", return_value={"data": data, "signature": sign(data, "wrong")}):
            liqpay.fetch_status(self.payment)
        self.payment.refresh_from_db()
        self.assertNotEqual(self.payment.status, PaymentStatus.SUCCESS)
        with mock.patch.object(liqpay, "_post_api", return_value={"data": data, "signature": sign(data)}):
            liqpay.fetch_status(self.payment)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)

    def test_network_error_changes_nothing(self):
        import urllib.error
        with mock.patch.object(liqpay, "_post_api", side_effect=urllib.error.URLError("down")):
            result = liqpay.fetch_status(self.payment)
        self.assertEqual(result.note, "network_error")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CREATED)

    def test_status_check_throttled(self):
        with mock.patch.object(liqpay, "_post_api", return_value=self._status_response(status="processing")) as api:
            self.client.get(self.url)
            self.client.get(self.url)
        self.assertEqual(api.call_count, 1)

    def test_other_user_cannot_view_result(self):
        self.client.force_login(make_user("stranger"))
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_cancel_then_late_success_still_credited(self):
        self.client.post(f"/payments/{self.payment.order_id}/cancel/")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CANCELLED)
        self.client.post("/payments/liqpay/callback/", callback_body(self.payment))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)

    def test_expire_stale_and_sync_command(self):
        Payment.objects.filter(pk=self.payment.pk).update(created_at=timezone.now() - timedelta(hours=3))
        with mock.patch.object(liqpay, "_post_api", return_value={"result": "error", "status": "error"}):
            out = StringIO()
            call_command("liqpay_sync_pending", stdout=out)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.EXPIRED)
        self.assertNotIn(PRIV, out.getvalue())

    def test_profile_shows_history(self):
        resp = self.client.get("/profile/")
        self.assertContains(resp, "Історія платежів")
        self.assertContains(resp, self.payment.order_id)


@override_settings(**LIQ)
class PlacementBusinessFlowTests(TestCase):
    """Етап оплати + модерація + публікація, через реальний callback."""

    def setUp(self):
        self.owner = make_user("owner")
        self.admin = make_admin()

    def pay(self, prop):
        payment = create_payment(self.owner, prop, is_test=True)
        self.assertEqual(self.client.post("/payments/liqpay/callback/", callback_body(payment)).status_code, 200)
        payment.refresh_from_db()
        return payment

    def test_new_listing_waits_moderation_then_30_days_from_publication(self):
        prop = make_property(owner=self.owner, public=False)
        payment = self.pay(prop)
        prop.refresh_from_db()
        self.assertEqual(prop.moderation_status, ModerationStatus.PENDING)
        self.assertIsNone(prop.placement_until)
        self.client.force_login(self.admin)
        before = timezone.now()
        self.client.post(f"/admin-action/property/{prop.pk}/approve/")
        payment.refresh_from_db()
        prop.refresh_from_db()
        self.assertGreaterEqual(payment.period_start, before)
        self.assertEqual(prop.placement_until - payment.period_start, timedelta(days=30))

    def test_expired_listing_returns_to_moderation(self):
        prop = make_property(owner=self.owner, placement_until=timezone.now() - timedelta(days=1))
        payment = self.pay(prop)
        prop.refresh_from_db()
        self.assertEqual(prop.moderation_status, ModerationStatus.PENDING)
        self.assertIsNone(payment.period_start)

    @override_settings(PLACEMENT_PAYMENT_REQUIRED=True)
    def test_catalog_visibility_follows_payment(self):
        prop = make_property(owner=self.owner, public=False, name="Платне")
        self.pay(prop)
        self.assertNotContains(self.client.get("/catalog/"), "Платне")
        self.client.force_login(self.admin)
        self.client.post(f"/admin-action/property/{prop.pk}/approve/")
        self.client.logout()
        self.assertContains(self.client.get("/catalog/"), "Платне")


@override_settings(**LIQ)
class ConfigChecksTests(TestCase):
    def ids(self):
        return {e.id for e in run_checks()}

    def test_placement_required_without_payments_is_error(self):
        with self.settings(PAYMENTS_ENABLED=False, PLACEMENT_PAYMENT_REQUIRED=True):
            self.assertIn("properties.E001", self.ids())

    def test_enabled_without_keys_is_error(self):
        with self.settings(LIQPAY_PRIVATE_KEY=""):
            self.assertIn("properties.E002", self.ids())

    def test_valid_sandbox_config_has_no_errors(self):
        self.assertFalse({"properties.E001", "properties.E002"} & self.ids())

    def test_sandbox_mode_with_live_key_is_error(self):
        with self.settings(LIQPAY_PUBLIC_KEY="i0000live"):
            self.assertIn("properties.E002", self.ids())


@override_settings(**LIQ)
class ParallelCallbackTests(TransactionTestCase):
    """Кілька одночасних callback для одного платежу — зарахування рівно один раз."""

    serialized_rollback = True  # flush видаляє тариф із міграції 0022; відновлюємо його

    def test_parallel_callbacks_credit_once(self):
        import traceback

        owner = make_user("owner")
        start_end = timezone.now() + timedelta(days=5)
        prop = make_property(owner=owner, placement_until=start_end)
        payment = create_payment(owner, prop, is_test=True)
        body = callback_body(payment)
        n = 6
        barrier = threading.Barrier(n)
        results, errors = [], []

        def worker():
            try:
                barrier.wait(timeout=30)
                results.append(liqpay.handle_callback(body["data"], body["signature"]).credited)
            except Exception as exc:  # noqa: BLE001 — зберігаємо тип і traceback будь-якого винятку
                errors.append({"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()})
            finally:
                close_old_connections()
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Допустима лише конкуренція SQLite «database (table) is locked» — LiqPay повторить callback.
        unexpected = [e for e in errors if not (e["type"] == "OperationalError" and "locked" in e["message"])]
        self.assertEqual(unexpected, [], "\n\n".join(e["traceback"] for e in unexpected))
        self.assertEqual(len(results) + len(errors), n)
        retry = liqpay.handle_callback(body["data"], body["signature"])
        prop.refresh_from_db()
        payment.refresh_from_db()
        total_credited = results.count(True) + (1 if retry.credited else 0)
        self.assertEqual(total_credited, 1, (results, [e["message"] for e in errors], retry.note))
        self.assertEqual(payment.status, PaymentStatus.SUCCESS)
        self.assertEqual(prop.placement_until, start_end + timedelta(days=30))
        self.assertEqual(Property.objects.get(pk=prop.pk).placement_until, payment.period_end)
