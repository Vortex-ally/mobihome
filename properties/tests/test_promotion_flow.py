"""
Правки замовника (27.11): просування — лише після збереження оголошення і за окремою кнопкою
«Просунути оголошення» (без примусового переходу до оплати); описи рівнів доступності з «Для …»;
місто компанії в контактах — Львів.
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from properties import telegram_bot
from properties.mobility import MOBILITY_LEVELS
from properties.models import ModerationStatus, Payment, PaymentStatus, Property, Tariff

from .helpers import make_image, make_property, make_user
from .test_liqpay import LIQ, callback_body
from .test_moderation import TG, FakeTelegram, make_moderator
from .test_release import listing_data

CALLBACK = "/payments/liqpay/callback/"
PROMOTE = "Просунути оголошення"


def create_listing(client, name="Нова квартира"):
    resp = client.post(reverse("properties:add_listing"), {**listing_data(name=name), "photo": make_image("m.png")})
    return resp, Property.objects.get(name=name)


@override_settings(**LIQ)
class PromoteAfterSaveTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = make_user("owner")
        self.client.force_login(self.owner)

    def test_saving_listing_creates_no_payment_and_does_not_redirect_to_checkout(self):
        resp, prop = create_listing(self.client)
        saved_url = reverse("properties:listing_saved", args=[prop.pk])
        self.assertRedirects(resp, saved_url, fetch_redirect_response=False)
        self.assertFalse(Payment.objects.exists())                      # 1. без оплати
        self.assertEqual(prop.moderation_status, ModerationStatus.PENDING)  # 2. збережено, у черзі модерації
        page = self.client.get(saved_url)
        self.assertContains(page, "Оголошення збережено")
        self.assertContains(page, "Публікація безкоштовна")
        self.assertNotContains(page, 'name="tariff"')                   # тарифи — не на цій сторінці
        self.assertNotContains(page, "liqpay.ua")
        self.assertFalse(Payment.objects.exists())

    def test_promote_button_opens_tariff_choice(self):
        _, prop = create_listing(self.client)
        page = self.client.get(reverse("properties:listing_saved", args=[prop.pk]))
        promote_url = reverse("properties:payment", args=[prop.pk]) + "#promotion"
        self.assertContains(page, f'href="{promote_url}"')              # 3. кнопка є
        self.assertContains(page, PROMOTE)
        tariffs = self.client.get(reverse("properties:payment", args=[prop.pk]))  # 4. вибір тарифів
        self.assertContains(tariffs, 'id="promotion"')
        for code in ("active_12h", "top1_1d", "top3_5d", "top1_7d"):
            self.assertContains(tariffs, f'name="tariff" value="{code}"')
        self.assertNotContains(tariffs, 'value="best_block"')           # вимкнений тариф не продається
        self.assertFalse(Payment.objects.exists())                      # перегляд тарифів — не оплата

    def test_skip_promotion(self):
        _, prop = create_listing(self.client)
        page = self.client.get(reverse("properties:listing_saved", args=[prop.pk]))
        self.assertContains(page, f'href="{reverse("properties:profile")}"')  # 5. «Пропустити»
        self.assertContains(page, "Пропустити")
        self.assertEqual(self.client.get(reverse("properties:profile")).status_code, 200)
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(Property.objects.get(pk=prop.pk).moderation_status, ModerationStatus.PENDING)

    def test_promote_later_from_profile(self):
        approved = make_property(owner=self.owner, name="Схвалене")
        pending = make_property(owner=self.owner, public=False, is_published=True, name="На модерації")
        rejected = make_property(owner=self.owner, public=False, name="Відхилене",
                                 moderation_status=ModerationStatus.REJECTED)
        html = self.client.get(reverse("properties:profile")).content.decode()
        for prop in (approved, pending):                                # 6. кнопка біля власних оголошень
            self.assertIn(f'href="{reverse("properties:payment", args=[prop.pk])}#promotion"', html)
        self.assertNotIn(f'href="{reverse("properties:payment", args=[rejected.pk])}#promotion"', html)
        self.assertEqual(html.count(PROMOTE), 2)

    def test_editing_shows_saved_page_with_promotion(self):
        prop = make_property(owner=self.owner, photo=make_image("main.png"))
        resp = self.client.post(reverse("properties:edit_listing", args=[prop.pk]), listing_data(name="Змінене"))
        self.assertRedirects(resp, reverse("properties:listing_saved", args=[prop.pk]) + "?edited=1",
                             fetch_redirect_response=False)
        page = self.client.get(resp["Location"])
        self.assertContains(page, "Зміни збережено")
        self.assertContains(page, PROMOTE)

    def test_foreign_listing_cannot_be_promoted(self):
        foreign = make_property(owner=make_user("stranger"), name="Чуже")
        self.assertEqual(self.client.get(reverse("properties:listing_saved", args=[foreign.pk])).status_code, 404)
        page = self.client.get(reverse("properties:payment", args=[foreign.pk]))
        self.assertNotContains(page, 'name="tariff"', status_code=302)
        resp = self.client.post(reverse("properties:start_payment", args=[foreign.pk]), {"tariff": "top1_1d"})
        self.assertNotEqual(resp.status_code, 200)                      # 7. оплата чужого — ні
        self.assertFalse(Payment.objects.exists())

    def test_payment_does_not_bypass_moderation(self):
        _, prop = create_listing(self.client)
        resp = self.client.post(reverse("properties:start_payment", args=[prop.pk]),
                                {"tariff": "top1_7d", "amount": "1", "price": "1"})  # ціна з frontend ігнорується
        self.assertEqual(resp.status_code, 200)
        payment = Payment.objects.get()
        self.assertEqual(payment.amount, Decimal("200.00"))
        self.client.post(CALLBACK, callback_body(payment))
        self.client.post(CALLBACK, callback_body(payment))              # повторний callback — без дубля
        payment.refresh_from_db()
        prop.refresh_from_db()
        self.assertEqual((payment.status, payment.activated_at), (PaymentStatus.SUCCESS, None))
        self.assertEqual(prop.moderation_status, ModerationStatus.PENDING)  # 8. оплата не схвалює
        self.assertFalse(prop.is_verified)
        self.assertFalse(Property.objects.public().filter(pk=prop.pk).exists())
        self.assertEqual(Payment.objects.count(), 1)

    def test_prices_and_durations_unchanged(self):
        rows = {t.code: (t.price, t.duration(), t.is_active) for t in Tariff.objects.filter(promotion_rank__isnull=False)}
        self.assertEqual(rows, {                                        # 9.
            "active_12h": (Decimal("80.00"), timedelta(hours=12), True),
            "top1_1d": (Decimal("100.00"), timedelta(hours=24), True),
            "top3_5d": (Decimal("120.00"), timedelta(days=5), True),
            "top1_7d": (Decimal("200.00"), timedelta(days=7), True),
            "best_block": (Decimal("99.00"), timedelta(days=7), False),
        })

    @override_settings(PAYMENTS_ENABLED=False)
    def test_payments_disabled(self):
        _, prop = create_listing(self.client)
        page = self.client.get(reverse("properties:listing_saved", args=[prop.pk]))
        self.assertEqual(page.status_code, 200)                         # 10.
        self.assertContains(page, "Онлайн-оплата просування зараз недоступна")
        self.assertNotContains(page, PROMOTE + "</a>")
        self.assertNotContains(self.client.get(reverse("properties:profile")), "#promotion")
        tariffs = self.client.get(reverse("properties:payment", args=[prop.pk]))
        self.assertEqual(tariffs.status_code, 200)
        self.assertNotContains(tariffs, 'name="tariff"')
        resp = self.client.post(reverse("properties:start_payment", args=[prop.pk]), {"tariff": "top1_1d"})
        self.assertNotEqual(resp.status_code, 500)
        self.assertFalse(Payment.objects.exists())

    def test_anonymous_cannot_open_saved_page(self):
        prop = make_property(owner=self.owner)
        self.client.logout()
        resp = self.client.get(reverse("properties:listing_saved", args=[prop.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/auth/", resp["Location"])


class AccessibilityTextsTests(TestCase):
    def test_every_level_description_starts_with_dlia(self):  # 11.
        for level in MOBILITY_LEVELS:
            text = level["best_for"]
            self.assertTrue(text.startswith("Для "), text)
            self.assertNotIn("Для для", text.capitalize())
            self.assertNotRegex(text.lower(), r"\bдля для\b")
        self.assertEqual([L["id"] for L in MOBILITY_LEVELS], [1, 2, 3, 4, 5])

    def test_levels_page_shows_descriptions(self):
        html = self.client.get(reverse("properties:accessibility_levels")).content.decode()
        self.assertIn("Для батьків з візочками та сімей з малими дітьми", html)
        self.assertIn("Для людей, які пересуваються на кріслі колісному", html)
        self.assertNotIn(">батьків з візочками", html)


class CompanyCityTests(TestCase):
    def test_lviv_in_footer_and_contacts_with_same_phone_and_email(self):  # 12., 13.
        for url in ("/", "/contact/", "/catalog/", "/about/"):
            html = self.client.get(url).content.decode()
            self.assertIn("Львів, Україна", html, url)
            self.assertNotIn("м. Київ, Україна", html, url)
            self.assertNotIn("Хрещатик", html, url)
            self.assertIn('href="tel:+380991053309"', html, url)
            self.assertIn("dominikyarem@gmail.com", html, url)

    def test_listing_city_kyiv_is_not_replaced(self):
        prop = make_property(owner=make_user("o"), city="Київ", name="Київська квартира")
        self.assertContains(self.client.get(reverse("properties:property_detail", args=[prop.pk])), "Київ")


@override_settings(**TG)
class TelegramModerationRegressionTests(TestCase):  # 14.
    def setUp(self):
        self.api = FakeTelegram()
        for name, fake in (("_api", self.api), ("_api_upload", mock.Mock(return_value={"message_id": 1}))):
            patcher = mock.patch.object(telegram_bot, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        make_moderator(telegram_id=4242)
        self.owner = make_user("owner")
        self.client.force_login(self.owner)

    def test_saved_listing_reaches_moderators_once_and_can_be_approved(self):
        with self.captureOnCommitCallbacks(execute=True):
            _, prop = create_listing(self.client)
        self.client.get(reverse("properties:listing_saved", args=[prop.pk]))  # сторінка підтвердження — без дубля
        self.assertEqual(prop.telegram_messages.count(), 1)
        record = prop.telegram_messages.get()
        result = telegram_bot.handle_update({"update_id": 1, "callback_query": {
            "id": "cb", "from": {"id": 4242}, "message": {"message_id": record.message_id, "chat": {"id": -100777}},
            "data": f"mod:a:{prop.pk}:{prop.moderation_version}"}})
        self.assertEqual(result, "approved")
        self.assertEqual(Property.objects.get(pk=prop.pk).moderation_status, ModerationStatus.APPROVED)
