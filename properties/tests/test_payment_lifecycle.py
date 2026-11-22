"""
Повний життєвий цикл оплати: валідація повідомлень, ручна перевірка невідповідності суми,
стан модерації на сторінці результату, бізнес-сценарії розміщення, відновлення через liqpay_sync_pending.
Усі відповіді LiqPay — імітація (мережа не використовується).
"""

import json
import urllib.error
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import CommandError, call_command
import threading
import traceback

from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from properties import liqpay
from properties.models import ModerationStatus, Payment, PaymentEvent, PaymentStatus, Property
from properties.placement import AMOUNT_MISMATCH_NOTE, PAYMENT_REVERSED_NOTE, create_payment, is_live

from .helpers import make_admin, make_property, make_user
from .test_liqpay import LIQ, PRIV, PUB, callback_body

CALLBACK = "/payments/liqpay/callback/"


def status_response(payment, **over):
    """Відповідь Status API (JSON без підпису) для платежу."""
    fields = {"result": "ok", "action": "pay", "status": "success", "order_id": payment.order_id,
              "payment_id": 555000 + payment.pk, "amount": float(payment.amount), "currency": payment.currency,
              "public_key": PUB, "version": 7}
    fields.update(over)
    return fields


def reversed_alerts(payment):
    return list(PaymentEvent.objects.filter(payment=payment, note__startswith=PAYMENT_REVERSED_NOTE))


def mismatch_alerts(payment):
    return list(PaymentEvent.objects.filter(payment=payment, note__startswith=AMOUNT_MISMATCH_NOTE))


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = make_user("owner")

    def send(self, payment, **over):
        resp = self.client.post(CALLBACK, callback_body(payment, **over))
        payment.refresh_from_db()
        return resp

    def approve(self, prop):
        prop.refresh_from_db()
        prop.is_published = True
        prop.moderation_status = ModerationStatus.APPROVED
        prop.is_verified_by_admin = True
        prop.save()  # сигнал post_save -> activate_on_publication
        prop.refresh_from_db()
        return prop


@override_settings(**LIQ)
class CallbackFieldValidationTests(_Base):
    """Некоректні поля в підписаному повідомленні не ламають обробку і нічого не зараховують."""

    def setUp(self):
        super().setUp()
        self.prop = make_property(owner=self.owner)
        self.payment = create_payment(self.owner, self.prop, is_test=True)

    def test_non_finite_amount_rejected_without_crash(self):
        for amount in ("NaN", "sNaN", "Infinity", "-Infinity", "abc", None):
            resp = self.send(self.payment, amount=amount)
            self.assertEqual(resp.status_code, 200, amount)
            self.assertEqual(self.payment.status, PaymentStatus.CREATED, amount)
            self.assertIsNone(self.payment.applied_at, amount)
        self.assertEqual(PaymentEvent.objects.filter(note__contains="некоректна сума").count(), 6)
        self.prop.refresh_from_db()
        self.assertIsNone(self.prop.placement_until)

    def test_non_ascii_or_non_string_public_key_rejected(self):
        for public_key in ("sandbox_ї00000000test", 12345, None, ["x"]):
            resp = self.send(self.payment, public_key=public_key)
            self.assertEqual(resp.status_code, 200, public_key)
            self.assertEqual(self.payment.status, PaymentStatus.CREATED, public_key)
        # Те саме через відповідь Status API (без підпису, лише звірка полів).
        result = liqpay.process_payment_fields(status_response(self.payment, public_key="ключ"),
                                               signature_valid=True, source="status")
        self.assertEqual(result.note, "public_key_mismatch")
        self.payment.refresh_from_db()
        self.assertIsNone(self.payment.applied_at)

    def test_unknown_order_does_not_create_payment(self):
        before = Payment.objects.count()
        fake = Payment(order_id="mh-999-0123456789abcdef0123", amount=self.payment.amount, currency="UAH", pk=999)
        self.assertEqual(self.client.post(CALLBACK, callback_body(fake)).status_code, 200)
        self.assertEqual(Payment.objects.count(), before)
        self.assertTrue(PaymentEvent.objects.filter(payment=None, note__contains="невідомий order_id").exists())

    def test_order_id_substitution_credits_only_named_order(self):
        """Підписане повідомлення про чужий order_id не зараховує поточний платіж."""
        other_owner = make_user("other")
        other = create_payment(other_owner, make_property(owner=other_owner), is_test=True)
        self.send(other)
        self.payment.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((self.payment.status, other.status), (PaymentStatus.CREATED, PaymentStatus.SUCCESS))


@override_settings(**LIQ)
class AmountMismatchReviewTests(_Base):
    """LiqPay підтвердив іншу суму/валюту: не зараховано, needs_review, одна ALERT-подія."""

    def setUp(self):
        super().setUp()
        self.prop = make_property(owner=self.owner)
        self.payment = create_payment(self.owner, self.prop, is_test=True)

    def test_mismatch_flags_review_with_single_alert(self):
        for _ in range(3):
            self.assertEqual(self.send(self.payment, amount=1.0).status_code, 200)
        self.assertEqual(self.payment.status, PaymentStatus.PENDING)
        self.assertIsNone(self.payment.applied_at)
        self.assertTrue(self.payment.needs_review)
        alerts = mismatch_alerts(self.payment)
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertFalse(alert.signature_valid)  # внутрішня подія застосунку, не повідомлення LiqPay
        self.assertEqual(alert.provider_status, "")
        self.assertEqual((alert.payload["amount"], alert.payload["expected_amount"]), ("1.0", "99.00"))
        dumped = json.dumps(alert.payload, ensure_ascii=False)
        for secret in ("424242*42", "380000000000", "Тест", PRIV):
            self.assertNotIn(secret, dumped)
        self.prop.refresh_from_db()
        self.assertIsNone(self.prop.placement_until)
        self.assertFalse(liqpay.last_attempt_try_again(self.payment))

    def test_currency_mismatch_and_new_amount_add_separate_alerts(self):
        self.send(self.payment, currency="USD")
        self.send(self.payment, currency="USD")
        self.send(self.payment, amount=5.0)
        self.assertEqual(len(mismatch_alerts(self.payment)), 2)

    def test_status_api_repeat_does_not_duplicate_alert(self):
        self.send(self.payment, amount=1.0, payment_id=555000 + self.payment.pk)
        with mock.patch.object(liqpay, "_post_api", return_value=status_response(self.payment, amount=1.0)):
            self.assertEqual(liqpay.fetch_status(self.payment).note, "not_credited")
        self.assertEqual(len(mismatch_alerts(self.payment)), 1)

    def test_result_page_shows_manual_review_without_refresh_or_cancel(self):
        self.send(self.payment, amount=1.0)
        self.client.force_login(self.owner)
        with mock.patch.object(liqpay, "_post_api", side_effect=urllib.error.URLError("down")):
            resp = self.client.get(f"/payments/{self.payment.order_id}/result/")
        self.assertContains(resp, "Оплата потребує ручної перевірки")
        self.assertNotContains(resp, 'http-equiv="refresh"')
        self.assertNotContains(resp, "Скасувати оплату")
        self.assertNotContains(resp, "Очікуємо підтвердження")

    def test_owner_cannot_cancel_mismatched_payment(self):
        self.send(self.payment, amount=1.0)
        self.client.force_login(self.owner)
        self.client.post(f"/payments/{self.payment.order_id}/cancel/")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.PENDING)
        self.assertTrue(self.payment.needs_review)

    def test_admin_warning_mentions_mismatch(self):
        self.send(self.payment, amount=1.0)
        self.client.force_login(make_admin())
        resp = self.client.get("/admin/properties/payment/")
        self.assertContains(resp, "невідповідності суми/валюти")
        self.assertContains(resp, "можливого подвійного списання")

    def test_correct_success_after_mismatch_still_not_auto_credited_twice(self):
        """Після невідповідності правильний success зараховується один раз (позначка лишається)."""
        self.send(self.payment, amount=1.0)
        for _ in range(2):
            self.send(self.payment)
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)
        self.assertTrue(self.payment.needs_review)
        self.prop.refresh_from_db()
        self.assertEqual(Payment.objects.filter(property=self.prop, applied_at__isnull=False).count(), 1)


@override_settings(**LIQ)
class ResultPageStateTests(_Base):
    """Сторінка результату показує перевірений стан і що потрібно зробити власнику."""

    def result(self, payment, query=""):
        self.client.force_login(self.owner)
        with mock.patch.object(liqpay, "_post_api", side_effect=urllib.error.URLError("down")):
            return self.client.get(f"/payments/{payment.order_id}/result/{query}")

    def paid(self, prop):
        payment = create_payment(self.owner, prop, is_test=True)
        self.send(payment)
        self.assertEqual(payment.status, PaymentStatus.SUCCESS)
        return payment

    def test_paid_draft_asks_to_submit_for_moderation(self):
        payment = self.paid(make_property(owner=self.owner, public=False))
        self.assertContains(self.result(payment), "ще не надіслано на модерацію")

    def test_paid_listing_on_moderation(self):
        payment = self.paid(make_property(owner=self.owner, public=False, is_published=True))
        self.assertContains(self.result(payment), "Оголошення на модерації")

    def test_paid_rejected_listing_explains_resubmission(self):
        prop = make_property(owner=self.owner, public=False, is_published=True,
                             moderation_status=ModerationStatus.REJECTED, moderation_note="Нечіткі фото")
        resp = self.result(self.paid(prop))
        self.assertContains(resp, "відхилено модератором")
        self.assertContains(resp, "Нечіткі фото")
        self.assertContains(resp, "Оплату збережено")

    def test_paid_approved_listing_shows_period(self):
        resp = self.result(self.paid(make_property(owner=self.owner)))
        self.assertContains(resp, "Розміщення діє з")

    def test_forged_success_redirect_does_not_change_status(self):
        payment = create_payment(self.owner, make_property(owner=self.owner), is_test=True)
        forged = callback_body(payment)
        resp = self.result(payment, f"?status=success&data={forged['data']}&signature={forged['signature']}")
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.CREATED)
        self.assertContains(resp, "Очікуємо підтвердження")
        self.assertNotContains(resp, "Оплату підтверджено")

    def test_other_user_cannot_see_payment(self):
        payment = self.paid(make_property(owner=self.owner))
        stranger = make_user("stranger")
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(f"/payments/{payment.order_id}/result/").status_code, 404)
        self.assertEqual(self.client.post(f"/payments/{payment.order_id}/cancel/").status_code, 404)
        self.assertNotContains(self.client.get("/profile/"), payment.order_id)

    def test_anonymous_redirected_to_login(self):
        payment = self.paid(make_property(owner=self.owner))
        self.client.logout()
        resp = self.client.get(f"/payments/{payment.order_id}/result/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/auth/", resp["Location"])

    def test_profile_history_shows_order_reference_and_status(self):
        payment = self.paid(make_property(owner=self.owner))
        self.client.force_login(self.owner)
        resp = self.client.get("/profile/")
        self.assertContains(resp, f'title="Номер замовлення">{payment.order_id}<')
        self.assertContains(resp, payment.get_status_display())


@override_settings(**LIQ)
class PlacementScenarioTests(_Base):
    """Оплата і модерація — окремі процеси; активація централізована в placement.py."""

    def pay(self, prop, **over):
        payment = create_payment(self.owner, prop, is_test=True)
        self.send(payment, **over)
        return payment

    def test_paid_rejected_listing_activates_only_after_approval(self):
        prop = make_property(owner=self.owner, public=False, is_published=True,
                             moderation_status=ModerationStatus.REJECTED)
        payment = self.pay(prop)
        prop.refresh_from_db()
        self.assertEqual((payment.status, payment.activated_at, prop.placement_until),
                         (PaymentStatus.SUCCESS, None, None))
        self.assertEqual(prop.moderation_status, ModerationStatus.REJECTED)  # оплата не схвалює
        before = timezone.now()
        prop = self.approve(prop)
        payment.refresh_from_db()
        self.assertGreaterEqual(payment.period_start, before)
        self.assertEqual(payment.period_end - payment.period_start, timedelta(days=30))
        self.assertEqual(prop.placement_until, payment.period_end)
        self.assertTrue(is_live(prop))

    def test_rejecting_paid_live_listing_hides_it_and_reapproval_does_not_extend(self):
        prop = make_property(owner=self.owner)
        payment = self.pay(prop)
        prop.refresh_from_db()
        until = prop.placement_until
        self.assertTrue(is_live(prop))
        prop.moderation_status = ModerationStatus.REJECTED
        prop.save()
        prop.refresh_from_db()
        self.assertFalse(is_live(prop))
        self.assertEqual(prop.placement_until, until)  # період не скасовується й не продовжується
        prop = self.approve(prop)
        payment.refresh_from_db()
        self.assertEqual(prop.placement_until, until)
        self.assertEqual(Payment.objects.filter(property=prop, activated_at__isnull=False).count(), 1)

    def test_duplicate_and_delayed_callbacks_never_extend_twice(self):
        prop = make_property(owner=self.owner)
        payment = self.pay(prop)
        prop.refresh_from_db()
        until = prop.placement_until
        for status in ("success", "processing", "failure", "sandbox", "success"):
            self.send(payment, status=status)
        prop.refresh_from_db()
        self.assertEqual(prop.placement_until, until)
        self.assertEqual(payment.status, PaymentStatus.SUCCESS)

    def test_extension_of_active_placement_adds_to_end_date(self):
        until = timezone.now() + timedelta(days=5)
        prop = make_property(owner=self.owner, placement_until=until)
        payment = self.pay(prop)
        prop.refresh_from_db()
        self.assertEqual(prop.placement_until, until + timedelta(days=30))
        self.assertEqual(payment.period_start, until)
        self.assertEqual(prop.moderation_status, ModerationStatus.APPROVED)  # без повторної модерації

    def test_expired_placement_returns_to_moderation_and_restarts_on_publication(self):
        prop = make_property(owner=self.owner, placement_until=timezone.now() - timedelta(days=1))
        payment = self.pay(prop)
        prop.refresh_from_db()
        self.assertEqual(prop.moderation_status, ModerationStatus.PENDING)
        self.assertIsNone(payment.activated_at)
        before = timezone.now()
        prop = self.approve(prop)
        payment.refresh_from_db()
        self.assertGreaterEqual(payment.period_start, before)
        self.assertEqual(prop.placement_until, payment.period_end)

    def test_two_payments_before_publication_run_sequentially(self):
        prop = make_property(owner=self.owner, public=False, is_published=True)
        first = self.pay(prop)
        second = self.pay(prop)
        self.assertEqual((first.status, second.status), (PaymentStatus.SUCCESS, PaymentStatus.SUCCESS))
        self.assertFalse(first.needs_review or second.needs_review)  # послідовні, не накладаються
        prop = self.approve(prop)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(second.period_start, first.period_end)
        self.assertEqual(prop.placement_until - first.period_start, timedelta(days=60))

    def test_pending_failed_and_cancelled_payments_do_not_touch_listing(self):
        for status in ("processing", "failure", "error"):
            prop = make_property(owner=self.owner)
            payment = self.pay(prop, status=status)
            prop.refresh_from_db()
            self.assertIsNone(prop.placement_until, status)
            self.assertIsNone(payment.applied_at, status)
        prop = make_property(owner=self.owner)
        payment = create_payment(self.owner, prop, is_test=True)
        self.client.force_login(self.owner)
        self.client.post(f"/payments/{payment.order_id}/cancel/")
        payment.refresh_from_db()
        prop.refresh_from_db()
        self.assertEqual((payment.status, prop.placement_until), (PaymentStatus.CANCELLED, None))

    def test_payment_never_publishes_unsubmitted_listing(self):
        prop = make_property(owner=self.owner, public=False)
        self.pay(prop)
        prop.refresh_from_db()
        self.assertFalse(prop.is_published)
        self.assertEqual(prop.moderation_status, ModerationStatus.PENDING)
        self.assertFalse(is_live(prop))


@override_settings(**LIQ)
class SyncPendingRecoveryTests(_Base):
    """liqpay_sync_pending: відновлення загублених callback, захист від недоступності API."""

    def setUp(self):
        super().setUp()
        self.payment = create_payment(self.owner, make_property(owner=self.owner), is_test=True)

    def age(self, payment, minutes):
        Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(minutes=minutes))
        payment.refresh_from_db()

    def run_sync(self, *args, **patch):
        out = StringIO()
        with mock.patch("properties.management.commands.liqpay_sync_pending.time.sleep") as sleep, \
                mock.patch.object(liqpay, "_post_api", **patch) as api:
            call_command("liqpay_sync_pending", *args, stdout=out)
        return out.getvalue(), api, sleep

    def test_recovers_lost_callback_for_recently_cancelled_payment(self):
        self.client.force_login(self.owner)
        self.client.post(f"/payments/{self.payment.order_id}/cancel/")
        self.age(self.payment, 20)
        self.assertEqual(self.payment.status, PaymentStatus.CANCELLED)
        out, api, _ = self.run_sync(return_value=status_response(self.payment))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)
        self.assertIn("credited=1", out)

    def test_cancelled_payment_unchanged_by_not_found_or_processing(self):
        Payment.objects.filter(pk=self.payment.pk).update(status=PaymentStatus.CANCELLED)
        self.age(self.payment, 20)
        for response in ({"result": "error", "status": "error", "err_code": "payment_not_found"},
                         status_response(self.payment, status="processing")):
            self.run_sync(return_value=response)
            self.payment.refresh_from_db()
            self.assertEqual(self.payment.status, PaymentStatus.CANCELLED)

    def test_old_closed_and_credited_payments_not_polled(self):
        Payment.objects.filter(pk=self.payment.pk).update(status=PaymentStatus.CANCELLED)
        self.age(self.payment, 60 * 24)
        credited = create_payment(self.owner, make_property(owner=self.owner), is_test=True)
        self.send(credited)
        self.age(credited, 20)
        _, api, _ = self.run_sync(return_value=status_response(self.payment))
        api.assert_not_called()

    def test_aborts_after_consecutive_failures_without_changing_payments(self):
        others = [create_payment(self.owner, make_property(owner=self.owner), is_test=True) for _ in range(3)]
        Payment.objects.update(created_at=timezone.now() - timedelta(hours=5))
        with self.assertRaises(CommandError):
            self.run_sync("--max-failures", "2", side_effect=urllib.error.URLError("down"))
        self.assertEqual(set(Payment.objects.values_list("status", flat=True)), {PaymentStatus.CREATED})
        self.assertEqual(len(others) + 1, Payment.objects.count())

    def test_api_failures_stop_after_limit_calls(self):
        for _ in range(4):
            create_payment(self.owner, make_property(owner=self.owner), is_test=True)
        Payment.objects.update(created_at=timezone.now() - timedelta(minutes=20))
        calls = []

        def fail(params, config, timeout=15):
            calls.append(params["order_id"])
            return {"result": "error", "status": "error", "err_code": "limit"}

        with self.assertRaises(CommandError):
            self.run_sync("--max-failures", "3", side_effect=fail)
        self.assertEqual(len(calls), 3)
        self.assertEqual(set(Payment.objects.values_list("status", flat=True)), {PaymentStatus.CREATED})

    def test_delay_between_requests(self):
        create_payment(self.owner, make_property(owner=self.owner), is_test=True)
        Payment.objects.update(created_at=timezone.now() - timedelta(minutes=20))
        _, api, sleep = self.run_sync("--delay", "1.5", return_value={"result": "ok", "status": "processing"})
        self.assertEqual(api.call_count, 2)
        sleep.assert_called_once_with(1.5)

    def test_success_payment_with_timeout_is_retried_next_run(self):
        self.age(self.payment, 20)
        self.run_sync(side_effect=TimeoutError("slow"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CREATED)
        self.run_sync(return_value=status_response(self.payment))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)

    def test_output_contains_no_secrets(self):
        self.age(self.payment, 20)
        out, _, _ = self.run_sync(return_value=status_response(self.payment))
        self.assertNotIn(PRIV, out)
        self.assertNotIn("signature", out)


FINANCE_FIELDS = ("provider_transaction_id", "paid_amount", "paid_currency", "paid_at", "applied_at",
                  "period_start", "period_end", "activated_at", "amount", "currency", "order_id")
LISTING_FIELDS = ("is_published", "moderation_status", "is_verified_by_admin", "placement_until", "archived_at")


def snapshot(obj, fields):
    obj.refresh_from_db()
    return {f: getattr(obj, f) for f in fields}


@override_settings(**LIQ)
class ReversedPaymentReviewTests(_Base):
    """reversed за зарахованим платежем: needs_review і рівно одна подія [ALERT payment_reversed]."""

    def setUp(self):
        super().setUp()
        self.prop = make_property(owner=self.owner, placement_until=timezone.now() + timedelta(days=5))
        self.payment = create_payment(self.owner, self.prop, is_test=True)
        self.send(self.payment, payment_id=111)
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)
        self.finance = snapshot(self.payment, FINANCE_FIELDS)
        self.listing = snapshot(self.prop, LISTING_FIELDS)

    def assert_history_and_listing_intact(self):
        self.assertEqual(snapshot(self.payment, FINANCE_FIELDS), self.finance)
        self.assertEqual(snapshot(self.prop, LISTING_FIELDS), self.listing)

    def test_first_reversed_notification(self):
        self.assertEqual(self.send(self.payment, payment_id=111, status="reversed").status_code, 200)
        self.assertEqual(self.payment.status, PaymentStatus.REVERSED)
        self.assertTrue(self.payment.needs_review)
        alerts = reversed_alerts(self.payment)
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertFalse(alert.signature_valid)  # внутрішня подія застосунку
        self.assertEqual((alert.provider_status, alert.order_id), ("", self.payment.order_id))
        self.assertEqual(alert.payload, {
            "type": "payment_reversed", "property_id": self.prop.pk, "payment_record_id": self.payment.pk,
            "order_id": self.payment.order_id, "liqpay_payment_id": "111", "reversed_payment_id": "111",
            "amount": "99.00", "currency": "UAH"})
        dumped = json.dumps(alert.payload, ensure_ascii=False) + alert.note
        for secret in ("424242*42", "380000000000", "Тест", PRIV):
            self.assertNotIn(secret, dumped)
        self.assert_history_and_listing_intact()
        # Історія подій збережена: підписані success і reversed від LiqPay лишаються в журналі.
        self.assertEqual(list(PaymentEvent.objects.filter(payment=self.payment, signature_valid=True)
                              .order_by("pk").values_list("provider_status", flat=True)), ["success", "reversed"])

    def test_duplicate_reversed_notifications(self):
        for _ in range(3):
            self.send(self.payment, payment_id=111, status="reversed")
        self.send(self.payment, payment_id=None, status="reversed")  # без payment_id — той самий платіж
        self.assertEqual(len(reversed_alerts(self.payment)), 1)
        self.assertTrue(self.payment.needs_review)
        self.assert_history_and_listing_intact()

    def test_duplicate_after_closed_review_does_not_reopen(self):
        self.send(self.payment, payment_id=111, status="reversed")
        Payment.objects.filter(pk=self.payment.pk).update(needs_review=False)  # адміністратор закрив перевірку
        self.send(self.payment, payment_id=111, status="reversed")
        self.assertFalse(self.payment.needs_review)
        self.assertEqual(len(reversed_alerts(self.payment)), 1)

    def test_reversed_received_through_status_api(self):
        with mock.patch.object(liqpay, "_post_api", return_value=status_response(
                self.payment, payment_id=111, status="reversed")):
            self.assertEqual(liqpay.fetch_status(self.payment).note, "reversed")
            self.assertEqual(liqpay.fetch_status(self.payment).note, "reversed")
            Payment.objects.filter(pk=self.payment.pk).update(created_at=timezone.now() - timedelta(minutes=20))
            call_command("liqpay_sync_pending", stdout=StringIO())
        self.send(self.payment, payment_id=111, status="reversed")  # пізній callback після Status API
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.REVERSED)
        self.assertTrue(self.payment.needs_review)
        self.assertEqual(len(reversed_alerts(self.payment)), 1)
        self.assert_history_and_listing_intact()

    def test_reversed_of_paid_listing_on_moderation_keeps_moderation(self):
        prop = make_property(owner=self.owner, public=False, is_published=True)
        payment = create_payment(self.owner, prop, is_test=True)
        self.send(payment)
        before = snapshot(prop, LISTING_FIELDS)
        self.send(payment, status="reversed")
        self.assertEqual(snapshot(prop, LISTING_FIELDS), before)
        self.assertEqual(prop.moderation_status, ModerationStatus.PENDING)
        self.assertTrue(payment.needs_review)
        self.assertEqual(len(reversed_alerts(payment)), 1)

    def test_reversed_payment_that_was_never_credited(self):
        prop = make_property(owner=self.owner)
        payment = create_payment(self.owner, prop, is_test=True)
        before = snapshot(prop, LISTING_FIELDS)
        self.assertEqual(self.send(payment, status="reversed").status_code, 200)
        self.assertEqual(payment.status, PaymentStatus.REVERSED)
        self.assertFalse(payment.needs_review)
        self.assertEqual(reversed_alerts(payment), [])
        self.assertIsNone(payment.applied_at)
        self.assertEqual(snapshot(prop, LISTING_FIELDS), before)

    def test_admin_sees_reversed_alert_and_warning(self):
        self.send(self.payment, payment_id=111, status="reversed")
        self.client.force_login(make_admin())
        resp = self.client.get("/admin/properties/payment/?needs_review__exact=1")
        self.assertContains(resp, self.payment.order_id)
        self.assertContains(resp, "повернення коштів за зарахованим платежем")


@override_settings(**LIQ)
class ConcurrentReversedTests(TransactionTestCase):
    """Паралельні однакові reversed: один статус, одна подія, needs_review, дані зарахування цілі."""

    serialized_rollback = True  # flush видаляє тариф із міграції 0022; відновлюємо його

    def test_concurrent_reversed_notifications(self):
        from .test_liqpay_hardening import _locked

        owner = make_user("owner")
        prop = make_property(owner=owner, placement_until=timezone.now() + timedelta(days=5))
        payment = create_payment(owner, prop, is_test=True)
        first = callback_body(payment, payment_id=111)
        liqpay.handle_callback(first["data"], first["signature"])
        finance, listing = snapshot(payment, FINANCE_FIELDS), snapshot(prop, LISTING_FIELDS)
        bodies = [callback_body(payment, payment_id=111, status="reversed") for _ in range(4)]
        barrier = threading.Barrier(len(bodies))
        errors = []

        def worker(body):
            try:
                barrier.wait(timeout=30)
                liqpay.handle_callback(body["data"], body["signature"])
            except Exception as exc:  # noqa: BLE001 — зберігаємо тип і traceback
                errors.append({"locked": _locked(exc), "traceback": traceback.format_exc()})
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
        self.assertEqual(payment.status, PaymentStatus.REVERSED)
        self.assertTrue(payment.needs_review)
        self.assertEqual(len(reversed_alerts(payment)), 1)
        self.assertEqual(snapshot(payment, FINANCE_FIELDS), finance)
        self.assertEqual(snapshot(prop, LISTING_FIELDS), listing)
        self.assertTrue(Property.objects.filter(pk=prop.pk, placement_until=listing["placement_until"]).exists())


@override_settings(**LIQ)
class NeedsReviewDisplayTests(_Base):
    """needs_review видно власнику в історії та на сторінці результату; не маскується під звичайний стан."""

    BADGE = "на перевірці"

    def setUp(self):
        super().setUp()
        self.prop = make_property(owner=self.owner, placement_until=timezone.now() + timedelta(days=5))
        self.payment = create_payment(self.owner, self.prop, is_test=True)
        self.client.force_login(self.owner)

    def pages(self):
        with mock.patch.object(liqpay, "_post_api", side_effect=urllib.error.URLError("down")):
            return (self.client.get(f"/payments/{self.payment.order_id}/result/"), self.client.get("/profile/"),
                    self.client.get(f"/payment/{self.prop.pk}/"))

    def test_no_badge_for_normal_success(self):
        self.send(self.payment, payment_id=111)
        result, profile, payment_page = self.pages()
        self.assertContains(result, "Оплату підтверджено")
        self.assertNotContains(result, "Платіж перевіряє адміністратор")
        self.assertNotContains(profile, self.BADGE)
        self.assertNotContains(payment_page, self.BADGE)

    def test_duplicate_charge_success_shows_review_notice(self):
        self.send(self.payment, payment_id=111)
        self.send(self.payment, payment_id=222)  # друге списання за тим самим order_id
        self.assertTrue(self.payment.needs_review)
        result, profile, payment_page = self.pages()
        self.assertContains(result, "Оплату підтверджено")
        self.assertContains(result, "Платіж перевіряє адміністратор")
        self.assertNotContains(result, "повторне списання")  # причина не вгадується користувачу
        self.assertContains(profile, self.BADGE)
        self.assertContains(payment_page, self.BADGE)

    def test_amount_mismatch_shows_badge_not_success(self):
        self.send(self.payment, amount=1.0)
        result, profile, payment_page = self.pages()
        self.assertNotContains(result, "Оплату підтверджено")
        self.assertContains(result, "Оплата потребує ручної перевірки")
        self.assertContains(profile, self.BADGE)
        self.assertContains(payment_page, self.BADGE)

    def test_reversed_shows_badge_and_refund_text(self):
        self.send(self.payment, payment_id=111)
        self.send(self.payment, payment_id=111, status="reversed")
        result, profile, _ = self.pages()
        self.assertContains(result, "Кошти за цим платежем повернено")
        self.assertNotContains(result, "Оплату підтверджено")
        self.assertContains(profile, self.BADGE)
        self.assertContains(profile, self.payment.get_status_display())
