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

    def test_expired_payment_late_success_still_credited(self):
        before = self.live.placement_until
        old_order_id = self.payment.order_id
        Payment.objects.filter(pk=self.payment.pk).update(created_at=timezone.now() - timedelta(hours=3))
        self.payment.refresh_from_db()
        self.payment = liqpay.expire_stale(self.payment)
        self.assertEqual(self.payment.status, PaymentStatus.EXPIRED)
        self.assertIsNone(self.payment.applied_at)

        # Після TTL старий order_id більше не потрапляє в Checkout: створюється нова оплата.
        self.client.force_login(self.owner)
        resp = self.client.post(f"/payment/{self.live.pk}/start/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotEqual(liqpay.decode_data(resp.context["checkout"]["data"])["order_id"], old_order_id)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.EXPIRED)

        body = callback_body(self.payment, payment_id=424242)
        self.assertEqual(self.post(body).status_code, 200)
        self.payment.refresh_from_db()
        self.live.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)
        self.assertIsNotNone(self.payment.applied_at)
        self.assertEqual(self.payment.provider_transaction_id, "424242")
        self.assertEqual(self.payment.paid_amount, Decimal("99.00"))
        self.assertEqual(self.live.placement_until, before + timedelta(days=30))
        applied_at = self.payment.applied_at

        self.assertEqual(self.post(body).status_code, 200)  # повторний callback
        self.payment.refresh_from_db()
        self.live.refresh_from_db()
        self.assertEqual(self.live.placement_until, before + timedelta(days=30))
        self.assertEqual(self.payment.applied_at, applied_at)
        self.assertEqual(PaymentEvent.objects.filter(payment=self.payment, note__contains="Оплату зараховано").count(), 1)
        # Новіший відкритий платіж уже існує — пізній успіх старого позначається для перевірки.
        self.assertTrue(self.payment.needs_review)
        self.assertTrue(Payment.objects.exclude(pk=self.payment.pk).get().needs_review)

    # --- Два успішні платежі одного оголошення з РІЗНИМИ order_id (A прострочено, B створено) ---

    NOT_FOUND = {"result": "error", "status": "error", "err_code": "payment_not_found"}

    def _expire_a_and_start_b(self):
        """A «старіє» на 3 год; start_payment уточнює статус (LiqPay: не знайдено), закриває A і створює B."""
        self.client.force_login(self.owner)
        Payment.objects.filter(pk=self.payment.pk).update(created_at=timezone.now() - timedelta(hours=3))
        with mock.patch.object(liqpay, "_post_api", return_value=self.NOT_FOUND) as api:
            resp = self.client.post(f"/payment/{self.live.pk}/start/")
        self.assertEqual(api.call_args.args[0]["order_id"], self.payment.order_id)
        self.assertEqual(resp.status_code, 200)
        a = Payment.objects.get(pk=self.payment.pk)
        b = Payment.objects.exclude(pk=a.pk).get()
        self.assertEqual(a.status, PaymentStatus.EXPIRED)
        self.assertNotEqual(a.order_id, b.order_id)
        self.assertEqual(liqpay.decode_data(resp.context["checkout"]["data"])["order_id"], b.order_id)
        return a, b

    def _pay(self, payment, payment_id):
        self.assertEqual(self.post(callback_body(payment, payment_id=payment_id)).status_code, 200)

    def _alerts(self, payment):
        return list(PaymentEvent.objects.filter(payment=payment, note__startswith="[ALERT duplicate_property_charge]")
                    .order_by("pk"))

    def _assert_both_credited_and_flagged(self, a, b, before):
        a.refresh_from_db()
        b.refresh_from_db()
        self.live.refresh_from_db()
        for payment, txn in ((a, "1001"), (b, "2002")):
            self.assertEqual(payment.status, PaymentStatus.SUCCESS)
            self.assertIsNotNone(payment.applied_at)
            self.assertEqual(payment.provider_transaction_id, txn)  # кожна транзакція — у своєму Payment
            self.assertEqual(payment.paid_amount, Decimal("99.00"))
            self.assertEqual(PaymentEvent.objects.filter(payment=payment, note__contains="Оплату зараховано").count(), 1)
            self.assertTrue(payment.needs_review)
        # Строки — за чинними правилами (кожна оплата +30 днів), але ситуація позначена для адміністратора.
        self.assertEqual(self.live.placement_until, before + timedelta(days=60))
        # Для кожного платежу є ALERT з повними даними пари: обидва order_id і обидва payment_id.
        for this, that, this_txn, that_txn in ((a, b, "1001", "2002"), (b, a, "2002", "1001")):
            # Рівно одна ALERT-подія на платіж пари; ID запису Payment і ID транзакції LiqPay — окремо.
            self.assertEqual([e.payload for e in self._alerts(this)], [{
                "type": "duplicate_property_charge", "property_id": self.live.pk,
                "payment_record_id": this.pk, "order_id": this.order_id, "liqpay_payment_id": this_txn,
                "other_payment_record_id": that.pk, "other_order_id": that.order_id,
                "other_liqpay_payment_id": that_txn}])
            note = self._alerts(this)[0].note
            for text in (this.order_id, that.order_id, this_txn, that_txn):
                self.assertIn(text, note)
        for event in PaymentEvent.objects.filter(note__startswith="[ALERT"):
            self.assertNotIn("424242*42", json.dumps(event.payload) + event.note)
            self.assertNotIn("380000000000", json.dumps(event.payload) + event.note)
            self.assertIn(str(self.live.pk), event.note)

    def test_two_order_ids_late_a_flags_immediately(self):
        a, b = self._expire_a_and_start_b()
        self._pay(a, 1001)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.status, a.needs_review), (PaymentStatus.SUCCESS, True))
        self.assertEqual((b.status, b.needs_review), (PaymentStatus.CREATED, True))  # B ще відкритий
        self.assertEqual(len(self._alerts(a)), 1)
        self.assertEqual(len(self._alerts(b)), 1)

    def test_two_order_ids_late_a_then_b_both_flagged(self):
        before = self.live.placement_until
        a, b = self._expire_a_and_start_b()
        self._pay(a, 1001)
        self._pay(b, 2002)
        self._assert_both_credited_and_flagged(a, b, before)

    def test_two_order_ids_b_then_late_a_both_flagged(self):
        before = self.live.placement_until
        a, b = self._expire_a_and_start_b()
        self._pay(b, 2002)
        self._pay(a, 1001)
        self._assert_both_credited_and_flagged(a, b, before)
        self.assertEqual(len(self._alerts(a)), 1)  # B уже мав payment_id — одна подія на платіж
        self.assertEqual(len(self._alerts(b)), 1)

    def test_two_order_ids_repeated_callbacks_add_nothing(self):
        before = self.live.placement_until
        a, b = self._expire_a_and_start_b()
        self._pay(a, 1001)
        self._pay(b, 2002)
        events = PaymentEvent.objects.filter(note__startswith="[ALERT").count()
        for _ in range(3):
            self._pay(a, 1001)
            self._pay(b, 2002)
        self._assert_both_credited_and_flagged(a, b, before)
        self.assertEqual(PaymentEvent.objects.filter(note__startswith="[ALERT").count(), events)
        self.assertEqual(Payment.objects.count(), 2)

    def test_two_order_ids_transaction_ids_and_events_kept(self):
        a, b = self._expire_a_and_start_b()
        self._pay(a, 1001)
        self._pay(b, 2002)
        self.assertEqual(sorted(Payment.objects.filter(property=self.live)
                                .values_list("order_id", "provider_transaction_id")),
                         sorted([(a.order_id, "1001"), (b.order_id, "2002")]))
        for payment, txn in ((a, "1001"), (b, "2002")):
            events = PaymentEvent.objects.filter(payment=payment, signature_valid=True, provider_status="success",
                                                 note__startswith="[callback]")
            self.assertTrue(events.exists())
            self.assertTrue(all(str(e.payload.get("payment_id")) == txn for e in events))

    def test_normal_extension_after_success_not_flagged(self):
        before = self.live.placement_until
        self._pay(self.payment, 1001)  # A зараховано вчасно
        Payment.objects.filter(pk=self.payment.pk).update(created_at=timezone.now() - timedelta(hours=3),
                                                          applied_at=timezone.now() - timedelta(hours=2))
        self.client.force_login(self.owner)
        with mock.patch.object(liqpay, "_post_api") as api:
            resp = self.client.post(f"/payment/{self.live.pk}/start/")
        api.assert_not_called()  # відкритих платежів немає — Status API не потрібен
        b = Payment.objects.exclude(pk=self.payment.pk).get()
        self.assertEqual(liqpay.decode_data(resp.context["checkout"]["data"])["order_id"], b.order_id)
        self._pay(b, 2002)
        self._pay(b, 2002)
        self.live.refresh_from_db()
        self.assertEqual(self.live.placement_until, before + timedelta(days=60))
        self.assertFalse(Payment.objects.filter(needs_review=True).exists())
        self.assertFalse(PaymentEvent.objects.filter(note__startswith="[ALERT").exists())

    # --- Status API перед закриттям простроченої оплати ---

    def _stale_start(self, **mock_kwargs):
        self.client.force_login(self.owner)
        Payment.objects.filter(pk=self.payment.pk).update(created_at=timezone.now() - timedelta(hours=3))
        with mock.patch.object(liqpay, "_post_api", **mock_kwargs) as api:
            resp = self.client.post(f"/payment/{self.live.pk}/start/")
        self.assertEqual(api.call_count, 1)
        self.payment.refresh_from_db()
        return resp

    def _api(self, **over):
        resp = {"result": "ok", "action": "pay", "status": "success", "order_id": self.payment.order_id,
                "payment_id": 1001, "amount": 99.0, "currency": "UAH", "public_key": PUB, "version": 7}
        resp.update(over)
        return resp

    def test_status_api_errors_do_not_expire_or_create_new_payment(self):
        import urllib.error
        before = self.live.placement_until
        for kwargs in ({"side_effect": urllib.error.URLError("down")}, {"side_effect": TimeoutError()},
                       {"return_value": "not a dict"},
                       {"return_value": {"data": liqpay.encode_data(self._api()), "signature": "forged"}},
                       {"return_value": self._api(order_id="mh-1-00000000000000000000")},
                       {"return_value": self._api(public_key="sandbox_other")},
                       {"return_value": self._api(status="something_new")},
                       {"return_value": {"result": "error", "status": "error"}},
                       {"return_value": {"result": "error"}},
                       {"return_value": {"result": "error", "status": "error", "err_code": "err_access"}},
                       {"return_value": {"result": "error", "status": "error", "err_code": "limit"}},
                       {"return_value": {"result": "error", "status": "error", "err_code": "PAYMENT_NOT_FOUND "}}):
            resp = self._stale_start(**kwargs)
            self.assertRedirects(resp, f"/payment/{self.live.pk}/", fetch_redirect_response=False)
            self.assertIn(self.payment.status, (PaymentStatus.CREATED, PaymentStatus.PENDING), kwargs)
            self.assertNotEqual(self.payment.status, PaymentStatus.FAILURE)
            self.assertEqual(Payment.objects.count(), 1, kwargs)
        self.live.refresh_from_db()
        self.assertEqual(self.live.placement_until, before)

    def test_status_api_pending_keeps_old_payment_and_no_new_one(self):
        resp = self._stale_start(return_value=self._api(status="wait_accept"))
        self.assertEqual(self.payment.status, PaymentStatus.PENDING)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("checkout", resp.context or {})

    def test_status_api_success_credits_old_payment_and_no_new_one(self):
        before = self.live.placement_until
        resp = self._stale_start(return_value=self._api())
        self.assertRedirects(resp, f"/payments/{self.payment.order_id}/result/", fetch_redirect_response=False)
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)
        self.assertEqual(self.payment.provider_transaction_id, "1001")
        self.assertFalse(self.payment.needs_review)
        self.assertEqual(Payment.objects.count(), 1)
        self.live.refresh_from_db()
        self.assertEqual(self.live.placement_until, before + timedelta(days=30))
        self._pay(self.payment, 1001)  # запізнілий callback того самого списання — без змін
        self.live.refresh_from_db()
        self.assertEqual(self.live.placement_until, before + timedelta(days=30))

    def test_status_api_failure_closes_and_allows_new_payment(self):
        resp = self._stale_start(return_value=self._api(status="failure"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.payment.status, PaymentStatus.FAILURE)
        self.assertEqual(Payment.objects.count(), 2)

    def test_status_api_try_again_after_hold_expires_and_allows_new_payment(self):
        resp = self._stale_start(return_value=self._api(status="try_again"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.payment.status, PaymentStatus.EXPIRED)
        self.assertEqual(Payment.objects.count(), 2)

    def test_status_api_payment_not_found_expires_and_allows_new_payment(self):
        resp = self._stale_start(return_value={"result": "error", "status": "error",
                                               "err_code": "payment_not_found"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.payment.status, PaymentStatus.EXPIRED)
        new = Payment.objects.exclude(pk=self.payment.pk).get()
        self.assertEqual(liqpay.decode_data(resp.context["checkout"]["data"])["order_id"], new.order_id)
        self.assertTrue(PaymentEvent.objects.filter(payment=self.payment, note__contains="не знайдено").exists())

    def test_status_api_error_without_not_found_code_keeps_payment(self):
        for response in ({"result": "error", "status": "error"},
                         {"result": "error", "status": "error", "err_code": "err_access",
                          "err_description": "Access denied"},
                         {"result": "error", "status": "error", "err_code": "<script>x</script>"}):
            with mock.patch.object(liqpay, "_post_api", return_value=response):
                result = liqpay.fetch_status(self.payment)
            self.assertEqual((result.ok, result.note), (False, "api_error"), response)
            self.payment.refresh_from_db()
            self.assertEqual(self.payment.status, PaymentStatus.CREATED)
        notes = list(PaymentEvent.objects.filter(payment=self.payment, note__contains="Помилка API")
                     .order_by("pk").values_list("note", flat=True))
        self.assertEqual(len(notes), 3)
        self.assertIn("без коду", notes[0])
        self.assertIn("err_access", notes[1])
        self.assertNotIn("<", notes[2])  # код очищено перед записом у журнал

    def test_sync_command_generic_api_error_does_not_expire(self):
        Payment.objects.filter(pk=self.payment.pk).update(created_at=timezone.now() - timedelta(hours=3))
        with mock.patch.object(liqpay, "_post_api", return_value={"result": "error", "status": "error"}):
            call_command("liqpay_sync_pending", stdout=StringIO())
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CREATED)

    # --- ALERT duplicate_property_charge: одна подія на платіж пари, оновлюється ---

    def test_alert_updated_in_place_when_second_transaction_becomes_known(self):
        a, b = self._expire_a_and_start_b()
        self._pay(a, 1001)  # B ще відкритий, ID його транзакції невідомий
        a_event, b_event = self._alerts(a)[0], self._alerts(b)[0]
        self.assertEqual((a_event.payload["liqpay_payment_id"], a_event.payload["other_liqpay_payment_id"]),
                         ("1001", ""))
        self.assertEqual((b_event.payload["liqpay_payment_id"], b_event.payload["other_liqpay_payment_id"]),
                         ("", "1001"))
        self.assertIn("LiqPay —", a_event.note)
        self._pay(b, 2002)
        self._pay(b, 2002)
        self.assertEqual([e.pk for e in self._alerts(a)], [a_event.pk])  # та сама подія, не друга
        self.assertEqual([e.pk for e in self._alerts(b)], [b_event.pk])
        a_event.refresh_from_db()
        b_event.refresh_from_db()
        self.assertEqual((a_event.payload["payment_record_id"], a_event.payload["other_payment_record_id"]),
                         (a.pk, b.pk))
        self.assertEqual((a_event.payload["liqpay_payment_id"], a_event.payload["other_liqpay_payment_id"]),
                         ("1001", "2002"))
        self.assertEqual((b_event.payload["liqpay_payment_id"], b_event.payload["other_liqpay_payment_id"]),
                         ("2002", "1001"))
        self.assertIn("2002", a_event.note)
        self.assertNotIn("LiqPay —", a_event.note)
        for event in (a_event, b_event):
            self.assertNotIn("424242*42", json.dumps(event.payload) + event.note)
            self.assertNotIn("380000000000", json.dumps(event.payload) + event.note)

    def test_alert_update_changes_admin_review_version(self):
        from properties.admin import _latest_alert_id
        a, b = self._expire_a_and_start_b()
        self._pay(a, 1001)
        seen = _latest_alert_id(a)
        self._pay(b, 2002)  # подія A оновилась на місці (id той самий)
        self.assertNotEqual(_latest_alert_id(a), seen)

    def test_admin_cannot_close_review_with_stale_alert_version(self):
        from .test_liqpay_hardening import CHANGELIST, _staff
        a, b = self._expire_a_and_start_b()
        self._pay(a, 1001)
        from properties.admin import _latest_alert_id
        seen = _latest_alert_id(a)  # адміністратор відкрив сторінку підтвердження, поки B не оплачено
        self._pay(b, 2002)
        self.client.force_login(_staff("reviewer", "view_payment", "review_payment"))
        self.client.post(CHANGELIST, {"action": "mark_reviewed", "_selected_action": [a.pk], "post": "yes",
                                      f"alerts_{a.pk}": seen})
        a.refresh_from_db()
        self.assertTrue(a.needs_review)
        self.client.post(CHANGELIST, {"action": "mark_reviewed", "_selected_action": [a.pk], "post": "yes",
                                      f"alerts_{a.pk}": _latest_alert_id(a)})
        a.refresh_from_db()
        self.assertFalse(a.needs_review)

    # --- Однаковий created_at: порядок (created_at, pk) ---

    def _same_created_pair(self):
        stamp = timezone.now() - timedelta(hours=3)
        a = self.payment
        Payment.objects.filter(pk=a.pk).update(status=PaymentStatus.EXPIRED, created_at=stamp)
        b = create_payment(self.owner, self.live, is_test=True)
        Payment.objects.filter(pk=b.pk).update(created_at=stamp)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.created_at, b.created_at)
        return a, b

    def test_same_created_at_late_a_then_b_flagged(self):
        before = self.live.placement_until
        a, b = self._same_created_pair()
        self._pay(a, 1001)
        self._pay(b, 2002)
        self._assert_both_credited_and_flagged(a, b, before)

    def test_same_created_at_b_then_a_flagged(self):
        before = self.live.placement_until
        a, b = self._same_created_pair()
        self._pay(b, 2002)
        self._pay(a, 1001)
        self._assert_both_credited_and_flagged(a, b, before)

    def test_not_found_with_matching_or_absent_fields_expires(self):
        for extra in ({}, {"order_id": self.payment.order_id}, {"public_key": PUB},
                      {"order_id": self.payment.order_id, "public_key": PUB}, {"order_id": "", "public_key": None}):
            Payment.objects.filter(pk=self.payment.pk).update(status=PaymentStatus.CREATED, error_message="")
            resp = {"result": "error", "status": "error", "err_code": "payment_not_found", **extra}
            with mock.patch.object(liqpay, "_post_api", return_value=resp):
                result = liqpay.fetch_status(self.payment)
            self.assertEqual((result.ok, result.note), (True, "not_found"), extra)

    def test_not_found_with_foreign_order_id_or_public_key_keeps_payment(self):
        before = self.live.placement_until
        for extra in ({"order_id": "mh-1-00000000000000000000"}, {"public_key": "sandbox_other"},
                      {"order_id": self.payment.order_id, "public_key": "sandbox_other"},
                      {"order_id": "mh-1-00000000000000000000", "public_key": PUB}):
            resp = self._stale_start(return_value={"result": "error", "status": "error",
                                                   "err_code": "payment_not_found", **extra})
            self.assertRedirects(resp, f"/payment/{self.live.pk}/", fetch_redirect_response=False)
            self.assertEqual(self.payment.status, PaymentStatus.CREATED, extra)
            self.assertEqual(Payment.objects.count(), 1, extra)
        self.live.refresh_from_db()
        self.assertEqual(self.live.placement_until, before)
        events = PaymentEvent.objects.filter(payment=self.payment, note__contains="з чужим")
        self.assertEqual(events.count(), 4)
        self.assertFalse(events.filter(signature_valid=True).exists())

    def test_not_found_with_non_ascii_or_wrong_type_fields_keeps_payment(self):
        cases = ({"order_id": "mh-1-ааааааааааааааааааа"}, {"order_id": self.payment.order_id + "é"},
                 {"public_key": "sandbox_ключ"}, {"public_key": PUB + "\u00a0"},
                 {"order_id": 12345}, {"order_id": [self.payment.order_id]}, {"public_key": {"k": PUB}},
                 {"public_key": True}, {"order_id": 0.5})
        for extra in cases:
            resp = {"result": "error", "status": "error", "err_code": "payment_not_found", **extra}
            with mock.patch.object(liqpay, "_post_api", return_value=resp):
                result = liqpay.fetch_status(self.payment)  # без TypeError
            self.assertEqual((result.ok, result.note), (False, "error_response_mismatch"), extra)
            self.payment.refresh_from_db()
            self.assertEqual(self.payment.status, PaymentStatus.CREATED, extra)
        # Через start_payment після TTL: не закриває й не створює нову оплату.
        resp = self._stale_start(return_value={"result": "error", "status": "error",
                                               "err_code": "payment_not_found", "public_key": "sandbox_ключ"})
        self.assertRedirects(resp, f"/payment/{self.live.pk}/", fetch_redirect_response=False)
        self.assertEqual(self.payment.status, PaymentStatus.CREATED)
        self.assertEqual(Payment.objects.count(), 1)
        for event in PaymentEvent.objects.filter(note__contains="з чужим"):
            self.assertFalse(event.signature_valid)
            self.assertNotIn(PRIV, json.dumps(event.payload, ensure_ascii=False) + event.note)

    def test_property_alert_is_internal_event_not_provider_message(self):
        a, b = self._expire_a_and_start_b()
        self._pay(a, 1001)
        self._pay(b, 2002)
        for payment in (a, b):
            event = self._alerts(payment)[0]
            self.assertFalse(event.signature_valid)
            self.assertEqual(event.provider_status, "")
            # Перевірені повідомлення LiqPay лишаються signature_valid=True.
            self.assertTrue(PaymentEvent.objects.filter(payment=payment, signature_valid=True,
                                                        provider_status="success",
                                                        note__startswith="[callback]").exists())

    def test_property_alert_does_not_hide_try_again_of_open_payment(self):
        a, b = self._expire_a_and_start_b()
        self.assertEqual(self.post(callback_body(b, status="try_again")).status_code, 200)
        self._pay(a, 1001)  # пізній успіх A позначає відкритий B
        b.refresh_from_db()
        self.assertEqual((b.status, b.needs_review), (PaymentStatus.CREATED, True))
        self.assertTrue(liqpay.last_attempt_try_again(b))
        with mock.patch.object(liqpay, "_post_api", side_effect=OSError("offline")):
            resp = self.client.get(f"/payments/{b.order_id}/result/")
        self.assertContains(resp, "Платіж не пройшов. Ви можете спробувати оплатити ще раз")

    BAD_NOT_FOUND_CODES = ("payment_not_found!", " payment_not_found", "payment_not_found ", "payment_not_found\n",
                           "Payment_Not_Found", "payment-not-found", "payment_not_found<x>", None, 123, [], {},
                           ["payment_not_found"], {"code": "payment_not_found"})

    def test_err_code_exact_payment_not_found_confirms(self):
        with mock.patch.object(liqpay, "_post_api", return_value={"result": "error", "status": "error",
                                                                  "err_code": "payment_not_found"}):
            result = liqpay.fetch_status(self.payment)
        self.assertEqual((result.ok, result.note), (True, "not_found"))

    def test_err_code_not_exact_does_not_confirm(self):
        for code in self.BAD_NOT_FOUND_CODES:
            with mock.patch.object(liqpay, "_post_api", return_value={"result": "error", "status": "error",
                                                                      "err_code": code}):
                result = liqpay.fetch_status(self.payment)
            self.assertEqual((result.ok, result.note), (False, "api_error"), repr(code))
            self.payment.refresh_from_db()
            self.assertEqual(self.payment.status, PaymentStatus.CREATED, repr(code))
        events = PaymentEvent.objects.filter(payment=self.payment, note__contains="Помилка API").order_by("pk")
        self.assertEqual(events.count(), len(self.BAD_NOT_FOUND_CODES))
        self.assertEqual(events[0].payload["err_code"], "payment_not_found!")  # повне значення — у payload
        self.assertFalse(PaymentEvent.objects.filter(payment=self.payment, note__contains="не знайдено").exists())

    def test_start_payment_with_bad_err_code_keeps_old_payment(self):
        before = self.live.placement_until
        for code in self.BAD_NOT_FOUND_CODES:
            Payment.objects.filter(pk=self.payment.pk).update(status=PaymentStatus.CREATED)
            resp = self._stale_start(return_value={"result": "error", "status": "error", "err_code": code})
            self.assertRedirects(resp, f"/payment/{self.live.pk}/", fetch_redirect_response=False)
            self.assertEqual(self.payment.status, PaymentStatus.CREATED, repr(code))
            self.assertEqual(Payment.objects.count(), 1, repr(code))
        self.live.refresh_from_db()
        self.assertEqual(self.live.placement_until, before)
        resp = self._stale_start(return_value={"result": "error", "status": "error",
                                               "err_code": "payment_not_found"})
        self.assertEqual(resp.status_code, 200)  # точний код — закриває й дозволяє нову оплату
        self.assertEqual(self.payment.status, PaymentStatus.EXPIRED)
        self.assertEqual(Payment.objects.count(), 2)

    def test_sync_command_network_error_does_not_expire(self):
        import urllib.error
        Payment.objects.filter(pk=self.payment.pk).update(created_at=timezone.now() - timedelta(hours=3))
        with mock.patch.object(liqpay, "_post_api", side_effect=urllib.error.URLError("down")):
            call_command("liqpay_sync_pending", stdout=StringIO())
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CREATED)

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
        with mock.patch.object(liqpay, "_post_api", return_value={"result": "error", "status": "error",
                                                                  "err_code": "payment_not_found"}):
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
class TryAgainTests(TestCase):
    """try_again (напр. AntiFraud): спроба не пройшла, але замовлення лишається відкритим."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.owner = make_user("owner")
        self.prop = make_property(owner=self.owner, placement_until=timezone.now() + timedelta(days=5))
        self.client.force_login(self.owner)
        self.client.post(f"/payment/{self.prop.pk}/start/")
        self.payment = Payment.objects.get()
        self.result_url = f"/payments/{self.payment.order_id}/result/"

    def callback(self, **over):
        resp = self.client.post("/payments/liqpay/callback/", callback_body(self.payment, **over))
        self.assertEqual(resp.status_code, 200)
        self.payment.refresh_from_db()
        self.prop.refresh_from_db()

    def result_page(self, status_api="try_again"):
        api = {"result": "ok", "action": "pay", "status": status_api, "order_id": self.payment.order_id,
               "payment_id": 555, "amount": 99.0, "currency": "UAH", "public_key": PUB, "version": 7}
        with mock.patch.object(liqpay, "_post_api", return_value=api):
            return self.client.get(self.result_url)

    def test_try_again_keeps_payment_open_and_not_credited(self):
        before = self.prop.placement_until
        self.callback(status="try_again")
        self.assertEqual(self.payment.status, PaymentStatus.CREATED)
        self.assertIsNone(self.payment.applied_at)
        self.assertEqual(self.payment.error_message, "")
        self.assertEqual(self.prop.placement_until, before)
        self.assertTrue(PaymentEvent.objects.filter(payment=self.payment, provider_status="try_again",
                                                    signature_valid=True).exists())

    def test_try_again_after_pending_reopens_for_retry(self):
        self.callback(status="3ds_verify")
        self.assertEqual(self.payment.status, PaymentStatus.PENDING)
        self.callback(status="try_again")
        self.assertEqual(self.payment.status, PaymentStatus.CREATED)

    def test_result_page_shows_retry_not_endless_waiting(self):
        self.callback(status="try_again")
        resp = self.result_page()
        self.assertContains(resp, "Платіж не пройшов. Ви можете спробувати оплатити ще раз")
        self.assertContains(resp, f'action="/payment/{self.prop.pk}/start/"')
        self.assertNotContains(resp, "Очікуємо підтвердження")
        self.assertNotContains(resp, 'http-equiv="refresh"')

    def test_retry_reuses_same_order_id_within_ttl(self):
        self.callback(status="try_again")
        resp = self.client.post(f"/payment/{self.prop.pk}/start/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(liqpay.decode_data(resp.context["checkout"]["data"])["order_id"], self.payment.order_id)

    def test_retry_hidden_after_ttl(self):
        self.callback(status="try_again")
        Payment.objects.filter(pk=self.payment.pk).update(created_at=timezone.now() - timedelta(minutes=70))
        resp = self.result_page()
        self.assertContains(resp, "Платіж не пройшов")
        self.assertNotContains(resp, "Спробувати ще раз</button>")
        resp = self.client.post(f"/payment/{self.prop.pk}/start/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Payment.objects.count(), 1)

    def test_late_success_credited_once(self):
        before = self.prop.placement_until
        self.callback(status="try_again")
        self.callback()
        self.callback()
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)
        self.assertEqual(self.prop.placement_until, before + timedelta(days=30))
        self.callback(status="try_again")  # запізнілий try_again після успіху нічого не змінює
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)
        self.assertEqual(self.prop.placement_until, before + timedelta(days=30))
        resp = self.client.get(self.result_url)
        self.assertContains(resp, "Оплату підтверджено")
        self.assertNotContains(resp, "Спробувати ще раз")

    def test_late_success_via_status_api(self):
        self.callback(status="try_again")
        resp = self.result_page(status_api="success")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)
        self.assertContains(resp, "Оплату підтверджено")

    def test_no_retry_for_final_statuses(self):
        self.callback(status="try_again")
        for status in (PaymentStatus.CANCELLED, PaymentStatus.EXPIRED, PaymentStatus.FAILURE,
                       PaymentStatus.REVERSED):
            Payment.objects.filter(pk=self.payment.pk).update(status=status)
            resp = self.client.get(self.result_url)
            self.assertNotContains(resp, "Спробувати ще раз", msg_prefix=status)
            self.assertNotContains(resp, "Платіж не пройшов", msg_prefix=status)

    def test_failure_still_final(self):
        self.callback(status="failure")
        self.assertEqual(self.payment.status, PaymentStatus.FAILURE)
        self.assertNotContains(self.client.get(self.result_url), "Спробувати ще раз")


@override_settings(**LIQ)
class CrossOrderBoundaryTests(TestCase):
    """_cross_order_duplicates: межі applied_at старішого платежу відносно created_at новішого."""

    def setUp(self):
        self.owner = make_user("owner")
        self.prop = make_property(owner=self.owner, placement_until=timezone.now() + timedelta(days=5))
        self.t0 = timezone.now() - timedelta(hours=5)
        self.a = create_payment(self.owner, self.prop, is_test=True)
        self.a_success(applied_at=self.t0 + timedelta(hours=1))
        Payment.objects.filter(pk=self.a.pk).update(created_at=self.t0)
        self.b = create_payment(self.owner, self.prop, is_test=True)

    def a_success(self, applied_at):
        Payment.objects.filter(pk=self.a.pk).update(status=PaymentStatus.SUCCESS, applied_at=applied_at,
                                                    provider_transaction_id="1001")

    def dups(self, b_created_at):
        from properties.placement import _cross_order_duplicates
        Payment.objects.filter(pk=self.b.pk).update(created_at=b_created_at)
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        return [p.pk for p in _cross_order_duplicates(self.b)]

    def test_older_credited_before_newer_created_is_normal_extension(self):
        self.assertEqual(self.dups(self.t0 + timedelta(hours=1, microseconds=1)), [])
        self.assertEqual(self.dups(self.t0 + timedelta(hours=2)), [])

    def test_older_applied_exactly_at_newer_created_is_flagged(self):
        # Порядок за timestamp не встановлюється — обрано безпечну поведінку: позначити для перевірки.
        self.assertEqual(self.dups(self.t0 + timedelta(hours=1)), [self.a.pk])

    def test_older_credited_after_newer_created_is_flagged(self):
        self.assertEqual(self.dups(self.t0 + timedelta(minutes=59, seconds=59, microseconds=999999)), [self.a.pk])
        self.assertEqual(self.dups(self.t0 + timedelta(minutes=30)), [self.a.pk])

    def test_same_created_at_ordered_by_pk(self):
        # Однаковий created_at: старіший — менший pk. A (менший pk) зарахований після «створення» B.
        self.assertLess(self.a.pk, self.b.pk)
        self.assertEqual(self.dups(self.t0), [self.a.pk])
        # Із боку A: B новіший за pk; відкритий B при зарахуванні A — теж пара.
        from properties.placement import _cross_order_duplicates
        Payment.objects.filter(pk=self.a.pk).update(status=PaymentStatus.EXPIRED, applied_at=None)
        self.a.refresh_from_db()
        self.assertEqual([p.pk for p in _cross_order_duplicates(self.a)], [self.b.pk])

    def test_normal_extension_through_callbacks_not_flagged(self):
        Payment.objects.filter(pk=self.b.pk).update(created_at=self.t0 + timedelta(hours=2))
        body = callback_body(self.b, payment_id=2002)
        self.assertEqual(self.client.post("/payments/liqpay/callback/", body).status_code, 200)
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, PaymentStatus.SUCCESS)
        self.assertFalse(Payment.objects.filter(needs_review=True).exists())
        self.assertFalse(PaymentEvent.objects.filter(note__startswith="[ALERT").exists())


@override_settings(**LIQ)
class CancelPaymentTests(TestCase):
    """cancel_payment: лише власник, лише POST, ідемпотентно, фінальні платежі не чіпає."""

    FIELDS = ("status", "applied_at", "provider_transaction_id", "paid_amount", "period_start", "period_end",
              "error_message", "updated_at")

    def setUp(self):
        self.owner = make_user("owner")
        self.prop = make_property(owner=self.owner, placement_until=timezone.now() + timedelta(days=5))
        self.payment = create_payment(self.owner, self.prop, is_test=True)
        self.url = f"/payments/{self.payment.order_id}/cancel/"
        self.client.force_login(self.owner)

    def snapshot(self):
        self.payment.refresh_from_db()
        self.prop.refresh_from_db()
        return ({f: getattr(self.payment, f) for f in self.FIELDS}, self.prop.placement_until,
                PaymentEvent.objects.count(), Payment.objects.count())

    def test_other_user_gets_404_and_payment_unchanged(self):
        before = self.snapshot()
        self.client.force_login(make_user("stranger"))
        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.payment.status, PaymentStatus.CREATED)

    def test_get_not_allowed(self):
        before = self.snapshot()
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(self.snapshot(), before)

    def test_repeat_cancel_is_noop(self):
        self.assertContains(self.client.post(self.url, follow=True), "Оплату скасовано.")
        first = self.snapshot()
        self.assertEqual(self.payment.status, PaymentStatus.CANCELLED)
        resp = self.client.post(self.url, follow=True)
        self.assertRedirects(resp, f"/payment/{self.prop.pk}/")
        self.assertNotContains(resp, "Оплату скасовано.")  # повідомлення лише при першому скасуванні
        self.assertEqual(self.snapshot(), first)  # статус, updated_at, події, кількість платежів

    def test_cannot_cancel_successful_payment(self):
        self.assertEqual(self.client.post("/payments/liqpay/callback/", callback_body(self.payment)).status_code, 200)
        before = self.snapshot()
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)
        self.assertIsNotNone(self.payment.applied_at)
        self.assertEqual(self.client.post(self.url).status_code, 302)
        self.assertEqual(self.snapshot(), before)


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

    def test_parallel_callbacks_different_order_ids_flag_both(self):
        """Пізній успіх A і успіх B одночасно: блокування оголошення не дає їм «не побачити» одне одного."""
        import traceback

        owner = make_user("owner")
        start_end = timezone.now() + timedelta(days=5)
        prop = make_property(owner=owner, placement_until=start_end)
        a = create_payment(owner, prop, is_test=True)
        Payment.objects.filter(pk=a.pk).update(status=PaymentStatus.EXPIRED,
                                               created_at=timezone.now() - timedelta(hours=3))
        b = create_payment(owner, prop, is_test=True)
        bodies = [callback_body(a, payment_id=1001), callback_body(b, payment_id=2002)] * 3
        barrier = threading.Barrier(len(bodies))
        errors = []

        def worker(body):
            try:
                barrier.wait(timeout=30)
                liqpay.handle_callback(body["data"], body["signature"])
            except Exception as exc:  # noqa: BLE001 — зберігаємо тип і traceback будь-якого винятку
                errors.append({"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()})
            finally:
                close_old_connections()
                connection.close()

        threads = [threading.Thread(target=worker, args=(body,)) for body in bodies]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        unexpected = [e for e in errors if not (e["type"] == "OperationalError" and "locked" in e["message"])]
        self.assertEqual(unexpected, [], "\n\n".join(e["traceback"] for e in unexpected))
        for body in bodies[:2]:  # LiqPay повторює callback, якщо SQLite відповів «database is locked»
            liqpay.handle_callback(body["data"], body["signature"])
        a.refresh_from_db()
        b.refresh_from_db()
        prop.refresh_from_db()
        self.assertEqual((a.status, a.provider_transaction_id, a.needs_review), (PaymentStatus.SUCCESS, "1001", True))
        self.assertEqual((b.status, b.provider_transaction_id, b.needs_review), (PaymentStatus.SUCCESS, "2002", True))
        self.assertEqual(prop.placement_until, start_end + timedelta(days=60))
        for payment in (a, b):
            self.assertEqual(PaymentEvent.objects.filter(payment=payment, note__contains="Оплату зараховано").count(), 1)
            full = [e for e in PaymentEvent.objects.filter(payment=payment,
                                                           note__startswith="[ALERT duplicate_property_charge]")
                    if e.payload["liqpay_payment_id"] and e.payload["other_liqpay_payment_id"]]
            self.assertEqual(len(full), 1)
            self.assertEqual(PaymentEvent.objects.filter(payment=payment,
                                                         note__startswith="[ALERT duplicate_property_charge]").count(), 1)

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
