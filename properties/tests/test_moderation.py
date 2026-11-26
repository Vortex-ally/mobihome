"""
Модерація: роль «Модератори» (дозволи й заборони, field-level обмеження, підміна POST), спільний сервіс
(причина відхилення, версія заявки, одночасні рішення, історія), перевірки житла (онлайн/виїзна, приватні звіти),
Telegram-бот (імітація Bot API), контакти, статика адмінки при DEBUG=False.
"""

from io import StringIO
from unittest import mock

from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings

from properties import moderation, telegram_bot
from properties.models import (
    ListingVerification,
    ModerationEvent,
    ModerationStatus,
    Property,
    TelegramModerationMessage,
    UserProfile,
)

from .helpers import make_admin, make_property, make_user

CHANGE = "/admin/properties/property/{}/change/"


def make_moderator(username="moder", telegram_id=None):
    call_command("setup_moderators", stdout=StringIO())
    user = make_user(username, is_staff=True)
    user.groups.add(Group.objects.get(name=moderation.MODERATORS_GROUP))
    if telegram_id:
        UserProfile.objects.update_or_create(user=user, defaults={"telegram_id": str(telegram_id)})
    return user


def pending_property(**extra):
    owner = make_user(extra.pop("owner_name", "owner"))
    prop = make_property(owner=owner, public=False, **extra)
    return moderation.submit(prop.pk, actor=owner)


class ModeratorRoleTests(TestCase):
    def setUp(self):
        self.mod = make_moderator()
        self.prop = pending_property(price=1000000)
        self.client.force_login(self.mod)

    def test_setup_command_is_idempotent_and_creates_no_users(self):
        from django.contrib.auth.models import User

        users = User.objects.count()
        call_command("setup_moderators", stdout=StringIO())
        call_command("setup_moderators", stdout=StringIO())
        group = Group.objects.get(name="Модератори")
        codes = set(group.permissions.values_list("codename", flat=True))
        self.assertIn("moderate_property", codes)
        self.assertFalse(codes & {"view_user", "change_user", "view_payment", "change_tariff", "delete_property",
                                  "view_group", "change_group"})
        self.assertEqual(User.objects.count(), users)

    def test_moderator_can_open_admin_and_queue(self):
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        queue = self.client.get("/admin/properties/moderationqueue/")
        self.assertContains(queue, self.prop.name)
        self.assertEqual(self.client.get(CHANGE.format(self.prop.pk)).status_code, 200)

    def test_moderator_denied_users_payments_tariffs_groups_delete(self):
        for url in ("/admin/auth/user/", "/admin/auth/group/", "/admin/properties/payment/",
                    "/admin/properties/tariff/", "/admin/properties/paymentevent/",
                    f"/admin/properties/property/{self.prop.pk}/delete/", "/admin/auth/user/add/"):
            self.assertIn(self.client.get(url).status_code, (403, 302), url)
        self.assertEqual(self.client.post(f"/admin/properties/property/{self.prop.pk}/delete/",
                                          {"post": "yes"}).status_code, 403)
        self.assertTrue(Property.objects.filter(pk=self.prop.pk).exists())
        index = self.client.get("/admin/").content.decode()
        for hidden in ("/admin/auth/user/", "/admin/properties/payment/", "/admin/properties/tariff/"):
            self.assertNotIn(hidden, index)

    def test_payment_inline_hidden_from_moderator(self):
        html = self.client.get(CHANGE.format(self.prop.pk)).content.decode()
        self.assertNotIn("payments-TOTAL_FORMS", html)

    def form_data(self, **over):
        p = Property.objects.get(pk=self.prop.pk)
        data = {"moderation_status": p.moderation_status, "moderation_note": p.moderation_note,
                "moderation_internal_note": "", "mobility_level": "", "expected_version": p.moderation_version,
                "_save": "Зберегти"}
        for prefix in ("extra_photos", "audit", "verifications", "moderation_events"):
            data.update({f"{prefix}-TOTAL_FORMS": "0", f"{prefix}-INITIAL_FORMS": "0",
                         f"{prefix}-MIN_NUM_FORMS": "0", f"{prefix}-MAX_NUM_FORMS": "1000"})
        data.update(over)
        return data

    def post_change(self, **over):
        return self.client.post(CHANGE.format(self.prop.pk), self.form_data(**over))

    def test_post_tampering_of_forbidden_fields_is_ignored(self):
        other = make_user("thief")
        resp = self.post_change(price="1", name="Злам", owner=other.pk, is_published="", archived_at="2026-01-01",
                                mobility_level="3", moderation_internal_note="Дзвонив власнику")
        self.assertEqual(resp.status_code, 302)
        p = Property.objects.get(pk=self.prop.pk)
        self.assertEqual((p.price, p.name, p.owner_id, p.is_published, p.archived_at),
                         (self.prop.price, self.prop.name, self.prop.owner_id, True, None))
        self.assertEqual((p.mobility_level, p.moderation_internal_note), (3, "Дзвонив власнику"))

    def test_reject_requires_reason_and_logs_history(self):
        resp = self.post_change(moderation_status="rejected", moderation_note="")
        self.assertEqual(resp.status_code, 200)  # помилка форми
        self.assertEqual(Property.objects.get(pk=self.prop.pk).moderation_status, ModerationStatus.PENDING)
        self.post_change(moderation_status="rejected", moderation_note="Фото не відповідають опису")
        p = Property.objects.get(pk=self.prop.pk)
        self.assertEqual((p.moderation_status, p.moderation_note), (ModerationStatus.REJECTED, "Фото не відповідають опису"))
        event = ModerationEvent.objects.filter(property=p, action="rejected").get()
        self.assertEqual((event.actor, event.source, event.from_status, event.reason),
                         (self.mod, "admin", "pending", "Фото не відповідають опису"))

    def test_approve_via_form_publishes(self):
        self.post_change(moderation_status="approved")
        p = Property.objects.get(pk=self.prop.pk)
        self.assertTrue(Property.objects.public().filter(pk=p.pk).exists())
        self.assertTrue(ModerationEvent.objects.filter(property=p, action="approved", source="admin").exists())

    def test_stale_form_version_rejected(self):
        moderation.approve(self.prop.pk, actor=make_admin(), source="admin")  # інший модератор встиг
        resp = self.post_change(moderation_status="rejected", moderation_note="Пізно",
                                expected_version=self.prop.moderation_version)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Property.objects.get(pk=self.prop.pk).moderation_status, ModerationStatus.APPROVED)

    def test_verified_flag_requires_passed_verification(self):
        resp = self.post_change(is_verified="on")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Property.objects.get(pk=self.prop.pk).is_verified)
        ListingVerification.objects.create(property=self.prop, kind="online", result="passed", photos_checked=True)
        self.post_change(is_verified="on")
        self.assertTrue(Property.objects.get(pk=self.prop.pk).is_verified)

    def test_moderator_only_sees_moderation_actions(self):
        html = self.client.get("/admin/properties/property/").content.decode()
        self.assertIn('value="make_approved"', html)
        self.assertIn('value="make_rejected"', html)
        for forbidden in ("archive_listings", "deactivate_listings", "make_verified\"", "delete_selected"):
            self.assertNotIn(f'value="{forbidden}', html)
        resp = self.client.post("/admin/properties/property/", {"action": "archive_listings",
                                                                 "_selected_action": [self.prop.pk]})
        self.assertIsNone(Property.objects.get(pk=self.prop.pk).archived_at)
        self.assertIn(resp.status_code, (200, 302))

    def test_reject_action_asks_reason(self):
        resp = self.client.post("/admin/properties/property/", {"action": "make_rejected",
                                                                 "_selected_action": [self.prop.pk]})
        self.assertContains(resp, "Причина відхилення")
        self.client.post("/admin/properties/property/", {"action": "make_rejected", "_selected_action": [self.prop.pk],
                                                          "post": "yes", "reason": "Немає фото санвузла"})
        self.assertEqual(Property.objects.get(pk=self.prop.pk).moderation_note, "Немає фото санвузла")

    def test_regular_staff_without_permission_cannot_moderate(self):
        staff = make_user("staffer", is_staff=True)
        self.client.force_login(staff)
        self.client.post(f"/admin-action/property/{self.prop.pk}/approve/")
        self.assertEqual(Property.objects.get(pk=self.prop.pk).moderation_status, ModerationStatus.PENDING)
        self.client.force_login(make_user("plain"))
        self.client.post(f"/admin-action/property/{self.prop.pk}/reject/", {"moderation_note": "x"})
        self.assertEqual(Property.objects.get(pk=self.prop.pk).moderation_status, ModerationStatus.PENDING)


class ModerationLifecycleTests(TestCase):
    def setUp(self):
        self.mod = make_moderator()
        self.owner = make_user("owner")
        self.client.force_login(self.owner)

    def test_full_cycle_create_reject_fix_resubmit_approve(self):
        from .test_release import listing_data
        from .helpers import make_image

        self.client.post("/add-listing/", {**listing_data(name="Цикл"), "photo": make_image()})
        prop = Property.objects.get(name="Цикл")
        self.assertEqual(prop.moderation_status, ModerationStatus.PENDING)
        self.assertFalse(Property.objects.public().filter(pk=prop.pk).exists())
        moderation.reject(prop.pk, actor=self.mod, reason="Додайте фото входу", source="site")
        self.assertContains(self.client.get("/profile/"), "Додайте фото входу")
        self.client.post(f"/property/{prop.pk}/edit/", listing_data(name="Цикл виправлено"))
        prop.refresh_from_db()
        self.assertEqual(prop.moderation_status, ModerationStatus.PENDING)
        moderation.approve(prop.pk, actor=self.mod, source="telegram")
        self.assertTrue(Property.objects.public().filter(pk=prop.pk).exists())
        actions = list(prop.moderation_events.order_by("pk").values_list("action", "source"))
        self.assertEqual(actions, [("submitted", "owner"), ("rejected", "site"), ("submitted", "owner"),
                                   ("approved", "telegram")])

    def test_concurrent_decisions_only_first_wins(self):
        prop = pending_property(owner_name="o2")
        version = prop.moderation_version
        moderation.approve(prop.pk, actor=self.mod, source="admin", expected_version=version)
        with self.assertRaises(moderation.ModerationError):
            moderation.reject(prop.pk, actor=make_moderator("m2"), reason="x", source="telegram",
                              expected_version=version)
        with self.assertRaises(moderation.ModerationError):
            moderation.approve(prop.pk, actor=self.mod, source="admin")  # уже не pending
        self.assertEqual(Property.objects.get(pk=prop.pk).moderation_status, ModerationStatus.APPROVED)
        self.assertEqual(prop.moderation_events.filter(action="approved").count(), 1)

    def test_payment_never_approves(self):
        from properties.placement import apply_successful_payment, create_payment

        prop = pending_property(owner_name="payer")
        payment = create_payment(prop.owner, prop, is_test=True)
        apply_successful_payment(payment.pk, paid_amount=payment.amount, paid_currency="UAH", transaction_id="t1")
        self.assertEqual(Property.objects.get(pk=prop.pk).moderation_status, ModerationStatus.PENDING)
        self.assertFalse(Property.objects.get(pk=prop.pk).is_verified)


class VerificationTests(TestCase):
    def setUp(self):
        self.mod = make_moderator()
        self.prop = pending_property()

    def test_online_and_onsite_records_and_private_report(self):
        self.client.force_login(self.mod)
        online = ListingVerification.objects.create(property=self.prop, kind="online", photos_checked=True,
                                                    video_checked=True, owner_contacted=True, result="passed",
                                                    internal_comment="Відео підтверджує пандус")
        onsite = ListingVerification.objects.create(
            property=self.prop, kind="onsite", inspector_name="Інспектор Іван", checklist="Двері 90 см",
            inspector_fee="500.00", payout_status="pending", result="passed",
            report=SimpleUploadedFile("report.pdf", b"%PDF-1.4 secret", content_type="application/pdf"))
        self.assertTrue(onsite.report.name.startswith("verification_reports/"))
        url = onsite.report.url
        self.assertTrue(url.startswith("/staff/verification-report/"))
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b"".join(resp.streaming_content), b"%PDF-1.4 secret")
        self.assertEqual(self.client.get("/media/" + onsite.report.name).status_code, 404)  # не публічно
        self.assertEqual(self.client.get("/staff/verification-report/../../.env").status_code, 404)
        self.assertEqual(self.client.get("/staff/verification-report/verification_reports/nope.pdf").status_code, 404)
        self.client.logout()
        self.assertNotEqual(self.client.get(url).status_code, 200)
        self.client.force_login(make_user("viewer"))
        self.assertNotEqual(self.client.get(url).status_code, 200)
        detail = self.client.get(f"/property/{self.prop.pk}/")
        for secret in ("Відео підтверджує пандус", "Інспектор Іван", "500.00"):
            self.assertNotContains(detail, secret, status_code=detail.status_code)
        self.assertEqual(online.get_kind_display(), "Онлайн-перевірка")


TG = dict(TELEGRAM_BOT_ENABLED=True, TELEGRAM_BOT_TOKEN="123:FAKE-TOKEN", TELEGRAM_MODERATION_CHAT_ID="-100777",
          SITE_URL="https://mobihome.example")


class FakeTelegram:
    """Імітація Bot API: записує виклики, повертає message_id."""

    def __init__(self):
        self.calls, self.next_id = [], 500

    def __call__(self, method, payload, timeout=15):
        self.calls.append((method, payload))
        if method in ("sendMessage", "sendPhoto"):
            self.next_id += 1
            return {"message_id": self.next_id}
        return True

    def methods(self):
        return [m for m, _ in self.calls]


@override_settings(**TG)
class TelegramBotTests(TestCase):
    def setUp(self):
        self.api = FakeTelegram()
        patcher = mock.patch.object(telegram_bot, "_api", self.api)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mod = make_moderator(telegram_id=4242)
        with self.captureOnCommitCallbacks(execute=True):
            self.prop = pending_property(name='Квартира <b>"тест"</b>', price=2500000, description="Опис " * 100)

    def callback(self, action, version=None, user_id=4242, chat_id=-100777, data=None):
        return telegram_bot.handle_update({"update_id": 1, "callback_query": {
            "id": "cb1", "from": {"id": user_id, "username": "moder"},
            "message": {"message_id": 501, "chat": {"id": chat_id}},
            "data": data or f"mod:{action}:{self.prop.pk}:{version or self.prop.moderation_version}"}})

    def test_submission_sends_one_escaped_message_with_buttons(self):
        self.assertEqual(self.api.methods(), ["sendMessage"])
        payload = self.api.calls[0][1]
        self.assertEqual(payload["chat_id"], "-100777")
        self.assertIn("&lt;b&gt;&quot;тест&quot;&lt;/b&gt;", payload["text"])
        self.assertIn(f"/admin/properties/property/{self.prop.pk}/change/",
                      payload["reply_markup"]["inline_keyboard"][1][0]["url"])  # публічний https SITE_URL
        self.assertNotIn("owner@example.com", payload["text"])  # без персональних даних власника
        buttons = payload["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["callback_data"] for b in buttons],
                         [f"mod:a:{self.prop.pk}:1", f"mod:r:{self.prop.pk}:1"])
        telegram_bot.notify_new_submission(self.prop.pk)  # повтор — без дубля
        self.assertEqual(self.api.methods(), ["sendMessage"])

    def test_approve_button(self):
        self.assertEqual(self.callback("a"), "approved")
        self.prop.refresh_from_db()
        self.assertEqual(self.prop.moderation_status, ModerationStatus.APPROVED)
        self.assertTrue(ModerationEvent.objects.filter(property=self.prop, action="approved", source="telegram",
                                                       actor=self.mod).exists())
        self.assertIn("editMessageReplyMarkup", self.api.methods())
        self.assertEqual(self.callback("a", version=1), "rejected_action")  # повторне натискання
        self.assertEqual(ModerationEvent.objects.filter(action="approved").count(), 1)

    def test_reject_flow_requires_reason_from_same_moderator(self):
        self.assertEqual(self.callback("r"), "reason_requested")
        prompt_id = self.api.next_id
        self.assertEqual(Property.objects.get(pk=self.prop.pk).moderation_status, ModerationStatus.PENDING)
        reply = {"update_id": 2, "message": {"message_id": 900, "chat": {"id": -100777}, "from": {"id": 9999},
                                             "text": "Чужа причина", "reply_to_message": {"message_id": prompt_id}}}
        self.assertEqual(telegram_bot.handle_update(reply), "other_user")
        reply["message"]["from"]["id"] = 4242
        reply["message"]["text"] = "Немає фото санвузла"
        self.assertEqual(telegram_bot.handle_update(reply), "rejected")
        self.prop.refresh_from_db()
        self.assertEqual((self.prop.moderation_status, self.prop.moderation_note),
                         (ModerationStatus.REJECTED, "Немає фото санвузла"))
        self.assertEqual(telegram_bot.handle_update(reply), "ignored")  # запит причини використано

    def test_authorization_checks(self):
        self.assertEqual(self.callback("a", chat_id=-1), "wrong_chat")
        self.assertEqual(self.callback("a", user_id=1111), "forbidden")  # не прив'язаний ID
        self.mod.is_active = False
        self.mod.save()
        self.assertEqual(self.callback("a"), "forbidden")
        self.mod.is_active = True
        self.mod.save()
        self.mod.groups.clear()  # прав модерації більше немає
        self.assertEqual(self.callback("a"), "forbidden")
        self.assertEqual(self.callback("a", data="mod:a:abc:1"), "bad_data")
        self.assertEqual(Property.objects.get(pk=self.prop.pk).moderation_status, ModerationStatus.PENDING)

    def test_stale_message_after_resubmission(self):
        moderation.reject(self.prop.pk, actor=self.mod, reason="x", source="admin")
        moderation.submit(self.prop.pk, actor=self.prop.owner)
        self.assertEqual(self.callback("a", version=1), "rejected_action")
        self.assertEqual(self.callback("r", version=1), "stale")

    def test_telegram_failure_does_not_break_submission_and_is_retried(self):
        from properties.telegram_bot import TelegramError

        with mock.patch.object(telegram_bot, "_api", side_effect=TelegramError("timeout")):
            with self.captureOnCommitCallbacks(execute=True):
                prop = pending_property(owner_name="o3")
        record = TelegramModerationMessage.objects.get(property=prop)
        self.assertEqual((record.status, record.attempts), ("failed", 1))
        self.assertEqual(Property.objects.get(pk=prop.pk).moderation_status, ModerationStatus.PENDING)
        telegram_bot.retry_failed()
        record.refresh_from_db()
        self.assertEqual(record.status, "sent")

    @override_settings(TELEGRAM_BOT_ENABLED=False)
    def test_disabled_bot_sends_nothing(self):
        calls = len(self.api.calls)
        with self.captureOnCommitCallbacks(execute=True):
            pending_property(owner_name="o4")
        self.assertEqual(len(self.api.calls), calls)


class ContactsAndStaticTests(TestCase):
    def test_client_contacts_everywhere(self):
        for url in ("/", "/contact/", "/catalog/"):
            html = self.client.get(url).content.decode()
            self.assertIn('href="tel:+380991053309"', html, url)
            self.assertIn("dominikyarem@gmail.com", html, url)
            for old in ("1234567", "123-45-67", "info@mobi-home.ua", "support@mobi-home.ua"):
                self.assertNotIn(old, html, url)

    @override_settings(DEBUG=False)
    def test_admin_static_served_without_debug(self):
        for path, ctype in (("/static/admin/css/base.css", "text/css"), ("/static/admin/js/theme.js", "javascript")):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            self.assertIn(ctype, resp["Content-Type"], path)
        self.client.force_login(make_admin())
        admin_html = self.client.get("/admin/").content.decode()
        self.assertIn("/static/admin/css/base.css", admin_html)
        self.assertIn(".theme-toggle { display: none", admin_html)  # лише темна тема
