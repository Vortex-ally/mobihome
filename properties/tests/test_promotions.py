"""
Тарифи публікації та просування: ТОП №1, ТОП №3, активна публікація, ТОП №1 на добу.
Просування діє лише після підтвердженої LiqPay оплати і лише для оголошення в каталозі;
після period_end зникає саме. LiqPay імітовано (підписані callback з фіктивним ключем).
"""

import base64
import json
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from properties.models import ModerationStatus, Payment, PaymentStatus, Property, Tariff
from properties.placement import active_promotions, create_payment, promotion_ordering

from .helpers import make_admin, make_property, make_user
from .test_liqpay import LIQ, callback_body

CALLBACK = "/payments/liqpay/callback/"


def catalog_order():
    qs = active_promotions(Property.objects.public())
    return list(qs.order_by(*promotion_ordering("-created_at")).values_list("pk", flat=True))


@override_settings(**LIQ)
class PromotionTariffTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = make_user("owner")
        self.client.force_login(self.owner)

    def pay(self, prop, code, **callback):
        payment = create_payment(self.owner, prop, tariff=Tariff.objects.get(code=code), is_test=True)
        self.assertEqual(self.client.post(CALLBACK, callback_body(payment, **callback)).status_code, 200)
        payment.refresh_from_db()
        return payment

    # --- Тарифи ---

    def test_seeded_tariffs(self):
        rows = {t.code: (t.name, t.price, t.duration(), t.promotion_rank) for t in Tariff.promotions()}
        self.assertEqual(rows, {
            "top1_7d": ("ТОП №1", Decimal("200.00"), timedelta(days=7), 1),
            "top1_1d": ("ТОП №1 на добу", Decimal("100.00"), timedelta(hours=24), 1),
            "top3_5d": ("ТОП №3", Decimal("120.00"), timedelta(days=5), 3),
            "active_12h": ("Активна публікація", Decimal("80.00"), timedelta(hours=12), 5),
        })
        self.assertEqual(Tariff.objects.get(code="active_12h").duration_label(), "12 годин")
        self.assertEqual(Tariff.objects.get(code="top1_1d").duration_label(), "24 години")

    def test_publication_page_lists_free_and_all_tariffs(self):
        prop = make_property(owner=self.owner, public=False)
        html = self.client.get(f"/payment/{prop.pk}/").content.decode()
        for text in ("Безкоштовна публікація", "0 грн", "Надіслати на модерацію",
                     "ТОП №1", "200 грн", "7 днів", "ТОП №1 на добу", "100 грн", "24 години",
                     "ТОП №3", "120 грн", "5 днів", "Активна публікація", "80 грн", "12 годин"):
            self.assertIn(text, html, text)
        self.assertIn('name="tariff" value="top1_7d"', html)
        # Тариф «Найкращі оголошення» (99 грн, 7 днів) не продається до підтвердження замовника.
        self.assertNotIn('value="best_block"', html)
        self.assertNotIn("Розміщення квартири", html)

    # --- Створення платежу ---

    def test_start_payment_uses_server_price_for_chosen_tariff(self):
        prop = make_property(owner=self.owner)
        resp = self.client.post(f"/payment/{prop.pk}/start/", {"tariff": "top3_5d", "amount": "1"})
        self.assertEqual(resp.status_code, 200)
        fields = json.loads(base64.b64decode(resp.context["checkout"]["data"]))
        payment = Payment.objects.get()
        self.assertEqual((fields["amount"], payment.amount, payment.promotion_rank, payment.tariff_name),
                         ("120.00", Decimal("120.00"), 3, "ТОП №3"))
        self.assertIn("ТОП №3", fields["description"])

    def test_unknown_or_inactive_tariff_creates_nothing(self):
        prop = make_property(owner=self.owner)
        Tariff.objects.filter(code="top1_1d").update(is_active=False)
        for code in ("nope", "top1_1d"):
            resp = self.client.post(f"/payment/{prop.pk}/start/", {"tariff": code}, follow=True)
            self.assertContains(resp, "Цей тариф недоступний")
        self.assertFalse(Payment.objects.exists())

    def test_open_payment_of_other_tariff_blocks_new_choice(self):
        prop = make_property(owner=self.owner)
        self.client.post(f"/payment/{prop.pk}/start/", {"tariff": "top1_7d"})
        resp = self.client.post(f"/payment/{prop.pk}/start/", {"tariff": "active_12h"}, follow=True)
        self.assertContains(resp, "Є незавершена оплата «ТОП №1»")
        self.assertEqual(Payment.objects.count(), 1)
        # «Продовжити оплату» передає тариф незавершеної оплати — той самий order_id.
        self.assertContains(self.client.get(f"/payment/{prop.pk}/"), 'name="tariff" value="top1_7d"')
        self.assertEqual(self.client.post(f"/payment/{prop.pk}/start/", {"tariff": "top1_7d"}).status_code, 200)
        self.assertEqual(Payment.objects.count(), 1)

    def test_foreign_listing_cannot_be_promoted(self):
        other = make_user("other")
        prop = make_property(owner=other)
        self.assertEqual(self.client.post(f"/payment/{prop.pk}/start/", {"tariff": "top1_7d"}).status_code, 404)

    # --- Активація ---

    def test_success_activates_for_listing_in_catalog(self):
        prop = make_property(owner=self.owner)
        before = timezone.now()
        payment = self.pay(prop, "top1_7d")
        self.assertEqual(payment.status, PaymentStatus.SUCCESS)
        self.assertGreaterEqual(payment.period_start, before)
        self.assertEqual(payment.period_end - payment.period_start, timedelta(days=7))
        prop.refresh_from_db()
        self.assertIsNone(prop.placement_until)  # просування не змінює розміщення/модерацію
        self.assertEqual(prop.moderation_status, ModerationStatus.APPROVED)

    def test_hours_based_tariffs(self):
        for code, hours in (("active_12h", 12), ("top1_1d", 24)):
            payment = self.pay(make_property(owner=self.owner), code)
            self.assertEqual(payment.period_end - payment.period_start, timedelta(hours=hours), code)

    def test_failed_or_cancelled_payment_gives_no_promotion(self):
        top = make_property(owner=self.owner)
        normal = make_property(owner=self.owner)
        failed = self.pay(top, "top1_7d", status="failure")
        self.assertIsNone(failed.period_start)
        cancelled = create_payment(self.owner, top, tariff=Tariff.objects.get(code="top1_7d"), is_test=True)
        self.client.post(f"/payments/{cancelled.order_id}/cancel/")
        self.assertEqual(catalog_order(), [normal.pk, top.pk])  # звичайний порядок: новіші вище

    def test_repeated_callbacks_do_not_extend(self):
        prop = make_property(owner=self.owner)
        payment = self.pay(prop, "top1_7d")
        period = (payment.period_start, payment.period_end)
        for _ in range(3):
            self.client.post(CALLBACK, callback_body(payment))
        payment.refresh_from_db()
        self.assertEqual((payment.period_start, payment.period_end), period)
        self.assertEqual(Payment.objects.filter(property=prop, activated_at__isnull=False).count(), 1)

    def test_same_rank_purchases_queue_without_losing_days(self):
        prop = make_property(owner=self.owner)
        first = self.pay(prop, "top1_7d")
        second = self.pay(prop, "top1_1d")
        self.assertEqual(second.period_start, first.period_end)
        self.assertEqual(second.period_end - first.period_start, timedelta(days=8))
        self.assertFalse(first.needs_review or second.needs_review)  # різні тарифи — не дублікат

    def test_promotion_waits_for_moderation(self):
        prop = make_property(owner=self.owner, public=False, is_published=True)  # на модерації
        payment = self.pay(prop, "top3_5d")
        self.assertEqual((payment.status, payment.activated_at), (PaymentStatus.SUCCESS, None))
        prop.refresh_from_db()
        self.assertEqual(prop.moderation_status, ModerationStatus.PENDING)  # оплата не схвалює
        resp = self.client.get(f"/payments/{payment.order_id}/result/")
        self.assertContains(resp, "почнеться, щойно оголошення буде опубліковано")
        before = timezone.now()
        prop.moderation_status, prop.is_verified_by_admin = ModerationStatus.APPROVED, True
        prop.save()
        payment.refresh_from_db()
        self.assertGreaterEqual(payment.period_start, before)
        self.assertEqual(payment.period_end - payment.period_start, timedelta(days=5))

    def test_best_block_prepared_but_not_sold(self):
        t = Tariff.objects.get(code="best_block")
        self.assertEqual((t.name, t.price, t.duration(), t.promotion_rank, t.is_active),
                         ("Найкращі оголошення", Decimal("99.00"), timedelta(days=7), 4, False))
        prop = make_property(owner=self.owner, public=False, is_published=True)
        resp = self.client.post(f"/payment/{prop.pk}/start/", {"tariff": "best_block"}, follow=True)
        self.assertContains(resp, "Цей тариф недоступний")
        self.assertFalse(Payment.objects.exists())  # вимкнений — оплата не створюється

    def test_best_block_payment_never_approves_or_verifies(self):
        Tariff.objects.filter(code="best_block").update(is_active=True)  # як після підтвердження замовника
        prop = make_property(owner=self.owner, public=False, is_published=True)  # на модерації
        payment = self.pay(prop, "best_block")
        self.assertEqual((payment.status, payment.activated_at), (PaymentStatus.SUCCESS, None))
        prop.refresh_from_db()
        self.assertEqual(prop.moderation_status, ModerationStatus.PENDING)
        self.assertFalse(prop.is_verified_by_admin or prop.is_verified)

    def test_placement_and_promotion_together_on_publication(self):
        prop = make_property(owner=self.owner, public=False, is_published=True)
        placement = create_payment(self.owner, prop, is_test=True)
        self.client.post(CALLBACK, callback_body(placement))
        promo = self.pay(prop, "active_12h")
        prop.moderation_status, prop.is_verified_by_admin = ModerationStatus.APPROVED, True
        prop.save()
        placement.refresh_from_db()
        promo.refresh_from_db()
        prop.refresh_from_db()
        self.assertEqual(placement.period_end - placement.period_start, timedelta(days=30))
        self.assertEqual(promo.period_end - promo.period_start, timedelta(hours=12))
        self.assertEqual(prop.placement_until, placement.period_end)
        self.assertFalse(placement.needs_review or promo.needs_review)

    # --- Каталог ---

    def test_catalog_order_top1_top3_active_then_regular(self):
        regular = make_property(owner=self.owner)
        active = make_property(owner=self.owner)
        top3 = make_property(owner=self.owner)
        top1_old = make_property(owner=self.owner)
        top1_new = make_property(owner=self.owner)
        self.pay(active, "active_12h")
        self.pay(top3, "top3_5d")
        self.pay(top1_old, "top1_7d")
        self.pay(top1_new, "top1_1d")
        self.assertEqual(catalog_order(), [top1_new.pk, top1_old.pk, top3.pk, active.pk, regular.pk])
        html = self.client.get("/catalog/").content.decode()
        self.assertEqual(html.count("listing-badge--top\">ТОП<"), 3)
        self.assertEqual(html.count("listing-badge--active\">Активне<"), 1)
        positions = [html.index(f'/property/{p.pk}/"') for p in (top1_new, top1_old, top3, active, regular)]
        self.assertEqual(positions, sorted(positions))

    def test_expired_and_reversed_promotions_return_to_regular(self):
        older = make_property(owner=self.owner)
        newer = make_property(owner=self.owner)
        expired = self.pay(older, "top1_7d")
        self.assertEqual(catalog_order(), [older.pk, newer.pk])
        Payment.objects.filter(pk=expired.pk).update(period_start=timezone.now() - timedelta(days=8),
                                                     period_end=timezone.now() - timedelta(seconds=1))
        self.assertEqual(catalog_order(), [newer.pk, older.pk])
        reversed_payment = self.pay(older, "top1_1d")
        self.assertEqual(catalog_order()[0], older.pk)
        self.client.post(CALLBACK, callback_body(reversed_payment, status="reversed"))
        self.assertEqual(catalog_order(), [newer.pk, older.pk])
        self.assertNotIn("listing-badge--top\">ТОП<", self.client.get("/catalog/").content.decode())

    def test_explicit_sort_overrides_promotion(self):
        cheap = make_property(owner=self.owner, price=100)
        top = make_property(owner=self.owner, price=999)
        self.pay(top, "top1_7d")
        html = self.client.get("/catalog/?sort=price_asc").content.decode()
        self.assertLess(html.index(f'/property/{cheap.pk}/"'), html.index(f'/property/{top.pk}/"'))

    # --- Відображення статусу ---

    def test_owner_sees_promotion_status_and_history(self):
        prop = make_property(owner=self.owner)
        self.pay(prop, "top1_7d")
        page = self.client.get(f"/payment/{prop.pk}/").content.decode()
        self.assertIn("Діє просування «ТОП №1»", page)
        self.assertContains(self.client.get("/profile/"), "ТОП №1")
        self.assertContains(self.client.get(f"/property/{prop.pk}/"), "ТОП №1</span>")

    def test_admin_sees_promotion_and_tariff(self):
        prop = make_property(owner=self.owner)
        self.pay(prop, "top3_5d")
        self.client.force_login(make_admin())
        self.assertContains(self.client.get("/admin/properties/property/"), "ТОП №3 до")
        self.assertContains(self.client.get("/admin/properties/payment/"), "ТОП №3")
