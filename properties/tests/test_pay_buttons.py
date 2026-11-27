"""
Правки замовника: кнопка оплати в кожній картці тарифу просування (сума — з бази, оплата — лише POST через
наявний start_payment і LiqPay) і Telegram підтримки https://t.me/Yaremchuk06 (окремо від бота модерації).
"""

import base64
import json
import re
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from properties import telegram_bot
from properties.context_processors import _telegram_support
from properties.models import ModerationStatus, Payment, PaymentStatus, Property, Tariff

from .helpers import make_image, make_property, make_user
from .test_liqpay import LIQ, callback_body
from .test_moderation import TG, FakeTelegram, make_moderator
from .test_release import listing_data

CALLBACK = "/payments/liqpay/callback/"
CODES = ("top1_7d", "top1_1d", "top3_5d", "active_12h")
FORM_RE = re.compile(r'<input type="hidden" name="tariff" value="([a-z0-9_]+)">\s*'
                     r'<button type="submit"[^>]*promotion-pay-btn[^>]*>Оплатити (\d+) грн</button>')
DISABLED_RE = re.compile(r'<button type="button"[^>]*promotion-pay-btn[^>]*disabled[^>]*>Оплатити (\d+) грн</button>')


def tariff_page(client, prop):
    return client.get(reverse("properties:payment", args=[prop.pk])).content.decode()


def checkout_data(resp):
    html = resp.content.decode()
    data = re.search(r'name="data" value="([^"]+)"', html).group(1)
    return json.loads(base64.b64decode(data)), html


@override_settings(**LIQ)
class PayButtonsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = make_user("owner")
        self.client.force_login(self.owner)
        self.prop = make_property(owner=self.owner, public=False, is_published=True)  # на модерації

    def test_four_active_buttons_with_prices_from_db(self):
        html = tariff_page(self.client, self.prop)
        buttons = dict(FORM_RE.findall(html))                                     # 1., 3.
        self.assertEqual(set(buttons), set(CODES))
        for code, amount in buttons.items():                                      # 2.
            self.assertEqual(Decimal(amount), Tariff.objects.get(code=code).price)
        self.assertEqual(sorted(buttons.values()), ["100", "120", "200", "80"])
        forms = re.findall(r'<form method="post" action="/payment/%d/start/"[^>]*>(.*?)</form>' % self.prop.pk, html, re.S)
        self.assertEqual(len(forms), 4)
        self.assertTrue(all("csrfmiddlewaretoken" in f for f in forms))       # кожна форма — POST з CSRF
        self.assertNotIn("best_block", html)
        self.assertFalse(Payment.objects.exists())                                # 6. GET не створює платіж

    def test_price_follows_database(self):
        Tariff.objects.filter(code="top3_5d").update(price=Decimal("135.00"))
        self.assertEqual(dict(FORM_RE.findall(tariff_page(self.client, self.prop)))["top3_5d"], "135")

    def test_button_creates_payment_and_redirects_to_liqpay_sandbox(self):
        for code in CODES:
            Payment.objects.all().delete()
            resp = self.client.post(reverse("properties:start_payment", args=[self.prop.pk]), {"tariff": code})
            self.assertEqual(resp.status_code, 200)
            payment = Payment.objects.get()                                       # 4.
            tariff = Tariff.objects.get(code=code)
            data, html = checkout_data(resp)                                      # 5.
            self.assertIn('action="https://www.liqpay.ua/api/3/checkout"', html)
            self.assertEqual((payment.tariff, payment.amount, payment.promotion_rank, payment.is_test),
                             (tariff, tariff.price, tariff.promotion_rank, True))
            self.assertEqual((Decimal(str(data["amount"])), data["order_id"], data["public_key"]),
                             (tariff.price, payment.order_id, LIQ["LIQPAY_PUBLIC_KEY"]))  # тестовий (sandbox) ключ

    def test_get_on_start_payment_creates_nothing(self):
        resp = self.client.get(reverse("properties:start_payment", args=[self.prop.pk]))
        self.assertEqual(resp.status_code, 405)
        self.assertFalse(Payment.objects.exists())

    def test_csrf_required(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.owner)
        resp = client.post(reverse("properties:start_payment", args=[self.prop.pk]), {"tariff": "top1_1d"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Payment.objects.exists())

    def test_foreign_listing(self):                                               # 7.
        foreign = make_property(owner=make_user("stranger"))
        page = self.client.get(reverse("properties:payment", args=[foreign.pk]))
        self.assertEqual(page.status_code, 302)                                   # сторінку тарифів чужого не показуємо
        self.assertNotIn("promotion-pay-btn", self.client.get(page["Location"]).content.decode())
        resp = self.client.post(reverse("properties:start_payment", args=[foreign.pk]), {"tariff": "top1_7d"})
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(Payment.objects.exists())

    def test_inactive_tariff(self):                                               # 8.
        for code in ("best_block", "nope"):
            resp = self.client.post(reverse("properties:start_payment", args=[self.prop.pk]), {"tariff": code},
                                    follow=True)
            self.assertContains(resp, "Цей тариф недоступний")
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(Tariff.objects.get(code="best_block").is_active)

    def test_amount_tampering_ignored(self):                                      # 9.
        resp = self.client.post(reverse("properties:start_payment", args=[self.prop.pk]),
                                {"tariff": "top1_7d", "amount": "1", "price": "1", "currency": "USD"})
        data, _ = checkout_data(resp)
        payment = Payment.objects.get()
        self.assertEqual((payment.amount, payment.currency), (Decimal("200.00"), "UAH"))
        self.assertEqual((Decimal(str(data["amount"])), data["currency"]), (Decimal("200.00"), "UAH"))

    def test_paid_promotion_does_not_bypass_moderation(self):                     # 10.
        self.client.post(reverse("properties:start_payment", args=[self.prop.pk]), {"tariff": "top1_1d"})
        payment = Payment.objects.get()
        self.client.post(CALLBACK, callback_body(payment))
        payment.refresh_from_db()
        self.prop.refresh_from_db()
        self.assertEqual((payment.status, payment.activated_at), (PaymentStatus.SUCCESS, None))
        self.assertEqual(self.prop.moderation_status, ModerationStatus.PENDING)
        self.assertFalse(self.prop.is_verified)
        self.assertFalse(Property.objects.public().filter(pk=self.prop.pk).exists())

    def test_open_payment_disables_other_buttons(self):
        self.client.post(reverse("properties:start_payment", args=[self.prop.pk]), {"tariff": "top3_5d"})
        html = tariff_page(self.client, self.prop)
        self.assertEqual(len(DISABLED_RE.findall(html)), 4)
        self.assertIn("Спершу завершіть або скасуйте незавершену оплату", html)

    @override_settings(PAYMENTS_ENABLED=False)
    def test_payments_disabled(self):                                             # 11.
        html = tariff_page(self.client, self.prop)
        self.assertEqual(FORM_RE.findall(html), [])
        self.assertEqual(sorted(DISABLED_RE.findall(html)), ["100", "120", "200", "80"])
        self.assertEqual(html.count("Онлайн-оплата тимчасово недоступна."), 4)
        resp = self.client.post(reverse("properties:start_payment", args=[self.prop.pk]), {"tariff": "top1_7d"},
                                follow=True)
        self.assertContains(resp, "Онлайн-оплата тимчасово недоступна")
        self.assertFalse(Payment.objects.exists())

    @override_settings(LIQPAY_PUBLIC_KEY="", LIQPAY_PRIVATE_KEY="")
    def test_missing_liqpay_keys(self):                                           # 12.
        html = tariff_page(self.client, self.prop)
        self.assertEqual(FORM_RE.findall(html), [])
        self.assertEqual(len(DISABLED_RE.findall(html)), 4)
        self.client.post(reverse("properties:start_payment", args=[self.prop.pk]), {"tariff": "top1_7d"})
        self.assertFalse(Payment.objects.exists())

    def test_flow_after_save_and_from_profile(self):                              # 16.
        resp = self.client.post(reverse("properties:add_listing"),
                                {**listing_data(name="Нове"), "photo": make_image("m.png")})
        prop = Property.objects.get(name="Нове")
        self.assertRedirects(resp, reverse("properties:listing_saved", args=[prop.pk]), fetch_redirect_response=False)
        promote = reverse("properties:payment", args=[prop.pk]) + "#promotion"
        self.assertContains(self.client.get(resp["Location"]), f'href="{promote}"')
        self.assertContains(self.client.get(reverse("properties:profile")), f'href="{promote}"')
        self.assertEqual(len(FORM_RE.findall(tariff_page(self.client, prop))), 4)
        self.assertFalse(Payment.objects.filter(property=prop).exists())


class TelegramSupportLinkTests(TestCase):
    def test_new_link_on_pages(self):                                             # 13., 14.
        for url in ("/", "/contact/", "/catalog/", "/about/", "/rivni-dostupnosti/"):
            html = self.client.get(url).content.decode()
            self.assertIn('href="https://t.me/Yaremchuk06"', html, url)
            self.assertNotIn("mobi_home_support", html, url)
            self.assertIn('rel="noopener noreferrer"', html, url)

    def test_normalisation_and_invalid_values(self):
        for value in ("Yaremchuk06", "@Yaremchuk06", "https://t.me/Yaremchuk06", "https://t.me/Yaremchuk06/"):
            self.assertEqual(_telegram_support(value), ("https://t.me/Yaremchuk06", "@Yaremchuk06"), value)
        for bad in ("", "ab", "javascript:alert(1)", "https://evil.example/x", "Yarem chuk", "1abcde"):
            self.assertEqual(_telegram_support(bad), (None, None), bad)

    @override_settings(SITE_TELEGRAM_SUPPORT="")
    def test_button_hidden_without_value(self):
        self.assertNotIn("t.me/", self.client.get("/").content.decode())


@override_settings(**TG)
class TelegramModerationUnaffectedTests(TestCase):                                # 15.
    def test_moderation_bot_still_sends_and_approves(self):
        api = FakeTelegram()
        with mock.patch.object(telegram_bot, "_api", api), \
                mock.patch.object(telegram_bot, "_api_upload", mock.Mock(return_value={"message_id": 1})):
            make_moderator(telegram_id=4242)
            owner = make_user("owner")
            self.client.force_login(owner)
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse("properties:add_listing"),
                                 {**listing_data(name="Для бота"), "photo": make_image("m.png")})
            prop = Property.objects.get(name="Для бота")
            record = prop.telegram_messages.get()
            self.assertEqual(record.chat_id, "-100777")
            result = telegram_bot.handle_update({"update_id": 1, "callback_query": {
                "id": "cb", "from": {"id": 4242}, "message": {"message_id": record.message_id, "chat": {"id": -100777}},
                "data": f"mod:a:{prop.pk}:{prop.moderation_version}"}})
        self.assertEqual(result, "approved")
        self.assertNotIn("Yaremchuk06", json.dumps([p for _, p in api.calls], ensure_ascii=False))
