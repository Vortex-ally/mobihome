"""
Telegram-бот: фото оголошення (імітація Bot API, реальні локальні файли через Storage API).
1 фото — sendPhoto з підписом і кнопками; кілька — альбоми sendMediaGroup по 10 (головне першим) + повідомлення
з кнопками у відповідь на альбом; пошкоджені/відсутні файли й відмови Telegram не блокують заявку; без дублів.
"""

import io
import shutil
import tempfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image

from properties import moderation, telegram_bot
from properties.models import ModerationStatus, Property, PropertyPhoto, TelegramModerationMessage
from properties.telegram_bot import TelegramError

from .helpers import make_property, make_user
from .test_moderation import TG, FakeTelegram, make_moderator

REAL_UPLOAD = telegram_bot._api_upload


def jpeg(color=(200, 10, 10), size=(64, 48)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


class FakeUpload:
    """Імітація multipart-методів: записує (метод, поля, файли)."""

    def __init__(self, api):
        self.api, self.calls, self.fail = api, [], {}

    def __call__(self, method, fields, files, timeout=60):
        self.calls.append((method, fields, files))
        error = self.fail.get(method)
        if error:
            raise error
        if method == "sendMediaGroup":
            ids = []
            for _ in files:
                self.api.next_id += 1
                ids.append({"message_id": self.api.next_id})
            return ids
        self.api.next_id += 1
        return {"message_id": self.api.next_id}

    def methods(self):
        return [m for m, _, _ in self.calls]


@override_settings(**TG)
class TelegramPhotoTests(TestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        settings_patch = override_settings(MEDIA_ROOT=media)
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        self.api = FakeTelegram()
        self.upload = FakeUpload(self.api)
        for name, fake in (("_api", self.api), ("_api_upload", self.upload)):
            patcher = mock.patch.object(telegram_bot, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.mod = make_moderator(telegram_id=4242)
        self.owner = make_user("owner")

    def submit(self, main=True, extra=0, extra_files=None):
        prop = make_property(owner=self.owner, public=False, name="Квартира з пандусом", city="Львів",
                             address="вул. Зелена, 5", rooms=2, area_sqm=54.5, mobility_level=3,
                             description="Широкі двері, без порогів.")
        if main:
            prop.photo = SimpleUploadedFile("main.jpg", jpeg((0, 0, 255)), content_type="image/jpeg")
            prop.save()
        files = list(extra_files or []) + [jpeg((i, 100, 100)) for i in range(extra)]
        for i, content in enumerate(files):
            PropertyPhoto.objects.create(property=prop, order=i,
                                         image=SimpleUploadedFile(f"g{i}.jpg", content, content_type="image/jpeg"))
        with self.captureOnCommitCallbacks(execute=True):
            moderation.submit(prop.pk, actor=self.owner)
        prop.refresh_from_db()
        return prop

    def record(self, prop):
        return TelegramModerationMessage.objects.get(property=prop)

    def text_call(self):
        sends = [p for m, p in self.api.calls if m == "sendMessage"]
        self.assertEqual(len(sends), 1)
        return sends[0]

    def assert_buttons(self, markup, prop):
        self.assertEqual([b["callback_data"] for b in markup["inline_keyboard"][0]],
                         [f"mod:a:{prop.pk}:1", f"mod:r:{prop.pk}:1"])

    # --- Варіанти фото ---

    def test_without_photos_sends_text_with_all_fields(self):
        prop = self.submit(main=False)
        self.assertEqual(self.upload.calls, [])
        payload = self.text_call()
        for part in (f"ID {prop.pk}", "Квартира з пандусом", "1 000 000 грн", "Львів, вул. Зелена, 5", "54.5 м²",
                     "Кімнат: 2", "рівень 3", "Широкі двері", "Статус: Очікує модерації"):
            self.assertIn(part, payload["text"], part)
        self.assert_buttons(payload["reply_markup"], prop)
        self.assertEqual(self.record(prop).status, "sent")

    def test_single_photo_uses_send_photo_with_caption_and_buttons(self):
        prop = self.submit()
        self.assertEqual(self.upload.methods(), ["sendPhoto"])
        _, fields, files = self.upload.calls[0]
        with open(prop.photo.path, "rb") as fh:
            self.assertEqual(files[0][2], fh.read())  # справжній файл власника
        self.assertIn(f"ID {prop.pk}", fields["caption"])
        self.assertEqual(fields["parse_mode"], "HTML")
        self.assert_buttons(fields["reply_markup"], prop)
        self.assertNotIn("sendMessage", self.api.methods())
        record = self.record(prop)
        self.assertEqual((record.status, record.message_id), ("sent", self.api.next_id))

    def test_several_photos_album_main_first_then_buttons_message(self):
        prop = self.submit(extra=3)
        self.assertEqual(self.upload.methods(), ["sendMediaGroup"])
        _, fields, files = self.upload.calls[0]
        self.assertEqual(len(files), 4)
        with open(prop.photo.path, "rb") as fh:
            self.assertEqual(files[0][2], fh.read())  # головне фото — перше
        self.assertEqual([m["media"] for m in fields["media"]], [f"attach://p{i}" for i in range(4)])
        self.assertNotIn("reply_markup", fields)  # альбом не підтримує кнопки
        payload = self.text_call()
        record = self.record(prop)
        self.assertEqual(payload["reply_parameters"]["message_id"], record.media_message_id)
        self.assert_buttons(payload["reply_markup"], prop)
        self.assertEqual((record.status, record.media_done), ("sent", True))

    def test_more_than_ten_photos_split_into_albums(self):
        prop = self.submit(extra=11)  # 12 фото: 10 + 2
        self.assertEqual(self.upload.methods(), ["sendMediaGroup", "sendMediaGroup"])
        self.assertEqual([len(f) for _, _, f in self.upload.calls], [10, 2])
        self.text_call()
        self.upload.calls.clear()
        self.api.calls.clear()
        self.submit(extra=20)  # 21 фото: 10 + 10 + 1 (одне — sendPhoto)
        self.assertEqual(self.upload.methods(), ["sendMediaGroup", "sendMediaGroup", "sendPhoto"])
        self.assertEqual([len(f) for _, _, f in self.upload.calls], [10, 10, 1])
        self.assertNotIn("reply_markup", self.upload.calls[2][1])
        self.text_call()
        self.assertTrue(self.record(prop).media_done)

    def test_broken_or_missing_files_are_skipped(self):
        prop = self.submit(extra_files=[b"not an image", jpeg()])
        prop.photo.storage.delete(prop.photo.name)  # головне фото зникло з диска
        TelegramModerationMessage.objects.all().delete()
        telegram_bot.notify_new_submission(prop.pk)
        self.assertEqual(self.upload.methods()[-1], "sendPhoto")  # лишилось одне справне фото
        self.assertEqual(len(self.upload.calls[-1][2]), 1)
        self.assertEqual(self.record(prop).status, "sent")

    def test_all_photos_broken_still_sends_text(self):
        prop = self.submit(main=False, extra_files=[b"broken", b"\xff\xd8garbage"])
        self.assertEqual(self.upload.calls, [])
        self.assert_buttons(self.text_call()["reply_markup"], prop)

    # --- Помилки Telegram ---

    def test_telegram_rejects_single_photo_text_still_sent(self):
        self.upload.fail["sendPhoto"] = TelegramError("Bad Request: IMAGE_PROCESS_FAILED")
        prop = self.submit()
        self.assert_buttons(self.text_call()["reply_markup"], prop)
        self.assertEqual(self.record(prop).status, "sent")

    def test_telegram_rejects_album_falls_back_to_single_photos(self):
        self.upload.fail["sendMediaGroup"] = TelegramError("Bad Request: wrong file")
        self.upload.fail["sendPhoto"] = TelegramError("Bad Request: wrong file")
        prop = self.submit(extra=2)
        self.assertEqual(self.upload.methods(), ["sendMediaGroup", "sendPhoto", "sendPhoto", "sendPhoto"])
        payload = self.text_call()
        self.assertNotIn("reply_parameters", payload)
        self.assertEqual(self.record(prop).status, "sent")

    def test_network_error_retries_whole_submission_once(self):
        self.upload.fail["sendMediaGroup"] = TelegramError("URLError", network=True)
        prop = self.submit(extra=1)
        record = self.record(prop)
        self.assertEqual((record.status, record.media_done), ("failed", False))
        self.assertNotIn("sendMessage", self.api.methods())
        self.upload.fail.clear()
        telegram_bot.retry_failed()
        record.refresh_from_db()
        self.assertEqual((record.status, record.media_done, record.attempts), ("sent", True, 2))
        self.assertEqual(self.upload.methods(), ["sendMediaGroup", "sendMediaGroup"])
        self.text_call()

    def test_error_messages_do_not_leak_token(self):
        from urllib.error import URLError

        with mock.patch("urllib.request.urlopen", side_effect=URLError("boom")):
            with self.assertRaises(TelegramError) as ctx:
                REAL_UPLOAD("sendPhoto", {"chat_id": "1"}, [("photo", "a.jpg", b"x")])
        self.assertNotIn("FAKE-TOKEN", str(ctx.exception))
        self.assertTrue(ctx.exception.network)

    def test_multipart_body_and_http_errors(self):
        import json
        from email.parser import BytesParser
        from email.policy import HTTP
        from urllib.error import HTTPError

        captured = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout):
            captured["req"] = req
            return Resp(json.dumps({"ok": True, "result": {"message_id": 7}}).encode())

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(REAL_UPLOAD("sendPhoto", {"chat_id": "-100777", "reply_markup": {"inline_keyboard": []}},
                                         [("photo", "a.jpg", b"\xff\xd8JPEGDATA")]), {"message_id": 7})
        req = captured["req"]
        ctype = req.get_header("Content-type")
        msg = BytesParser(policy=HTTP).parsebytes(f"Content-Type: {ctype}\r\n\r\n".encode() + req.data)
        parts = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()}
        self.assertEqual(parts["chat_id"].get_content().strip(), "-100777")
        self.assertEqual(json.loads(parts["reply_markup"].get_content()), {"inline_keyboard": []})
        self.assertEqual(parts["photo"].get_content(), b"\xff\xd8JPEGDATA")
        self.assertEqual(parts["photo"].get_filename(), "a.jpg")

        for code, network in ((400, False), (429, True)):
            body = io.BytesIO(json.dumps({"ok": False, "description": f"Error {code}"}).encode())
            err = HTTPError("https://api.telegram.org/botX/sendPhoto", code, "x", {}, body)
            with mock.patch("urllib.request.urlopen", side_effect=err), self.assertRaises(TelegramError) as ctx:
                REAL_UPLOAD("sendPhoto", {"chat_id": "1"}, [("photo", "a.jpg", b"x")])
            self.assertEqual((str(ctx.exception), ctx.exception.network), (f"Error {code}", network))
            self.assertNotIn("FAKE-TOKEN", str(ctx.exception))

    # --- Повторне надсилання ---

    def test_repeat_does_not_duplicate_album_or_message(self):
        prop = self.submit(extra=2)
        telegram_bot.notify_new_submission(prop.pk)
        telegram_bot.retry_failed()
        self.assertEqual(self.upload.methods(), ["sendMediaGroup"])
        self.text_call()

    def test_buttons_message_failure_retries_without_resending_album(self):
        real_api = self.api

        def flaky(method, payload, timeout=15):
            if method == "sendMessage" and not getattr(flaky, "done", False):
                flaky.done = True
                raise TelegramError("Bad Request: message is too long")
            return real_api(method, payload, timeout)

        with mock.patch.object(telegram_bot, "_api", flaky):
            prop = self.submit(extra=2)
            record = self.record(prop)
            self.assertEqual((record.status, record.media_done), ("failed", True))
            telegram_bot.retry_failed()
        record.refresh_from_db()
        self.assertEqual(record.status, "sent")
        self.assertEqual(self.upload.methods(), ["sendMediaGroup"])  # альбом — лише один раз
        self.text_call()

    def test_concurrent_sending_claim_prevents_duplicates(self):
        prop = self.submit(main=False)
        TelegramModerationMessage.objects.filter(property=prop).update(status="sending")
        calls = len(self.api.calls)
        self.assertEqual(telegram_bot.retry_failed(), 0)  # «надсилається» іншим процесом
        self.assertEqual(len(self.api.calls), calls)

    # --- Посилання ---

    @override_settings(SITE_URL="http://127.0.0.1:8010")
    def test_localhost_site_url_gives_no_link(self):
        self.submit(main=False)
        payload = self.text_call()
        self.assertEqual(len(payload["reply_markup"]["inline_keyboard"]), 1)
        self.assertNotIn("127.0.0.1", payload["text"])

    def test_public_https_site_url_gives_view_button(self):
        prop = self.submit()
        row = self.upload.calls[0][1]["reply_markup"]["inline_keyboard"][1][0]
        self.assertEqual(row["url"], f"https://mobihome.example/admin/properties/property/{prop.pk}/change/")

    # --- Рішення модератора для заявки з фото ---

    def press(self, prop, action, message_id):
        return telegram_bot.handle_update({"update_id": 1, "callback_query": {
            "id": "cb", "from": {"id": 4242}, "message": {"message_id": message_id, "chat": {"id": -100777}},
            "data": f"mod:{action}:{prop.pk}:{prop.moderation_version}"}})

    def test_approve_album_submission(self):
        prop = self.submit(extra=3)
        record = self.record(prop)
        self.assertEqual(self.press(prop, "a", record.message_id), "approved")
        prop.refresh_from_db()
        self.assertEqual(prop.moderation_status, ModerationStatus.APPROVED)
        self.assertFalse(prop.is_verified)  # схвалення не дає «Перевірене житло»
        self.assertIn(("editMessageReplyMarkup", {"chat_id": "-100777", "message_id": record.message_id,
                                                  "reply_markup": {"inline_keyboard": []}}), self.api.calls)
        self.assertEqual(self.record(prop).status, "closed")

    def test_reject_single_photo_submission_with_reason(self):
        prop = self.submit()
        record = self.record(prop)
        self.assertEqual(self.press(prop, "r", record.message_id), "reason_requested")
        reply = {"update_id": 2, "message": {"message_id": 900, "chat": {"id": -100777}, "from": {"id": 4242},
                                             "text": "Фото санвузла нечітке", "reply_to_message": {"message_id": self.api.next_id}}}
        self.assertEqual(telegram_bot.handle_update(reply), "rejected")
        prop.refresh_from_db()
        self.assertEqual((prop.moderation_status, prop.moderation_note),
                         (ModerationStatus.REJECTED, "Фото санвузла нечітке"))
        self.assertIn("editMessageReplyMarkup", self.api.methods())
        self.assertEqual(self.press(prop, "a", record.message_id), "rejected_action")  # вже розглянуто
        self.assertEqual(Property.objects.get(pk=prop.pk).moderation_status, ModerationStatus.REJECTED)

    def test_wrong_chat_or_user_cannot_decide_photo_submission(self):
        prop = self.submit()
        record = self.record(prop)
        wrong_chat = {"update_id": 3, "callback_query": {"id": "c", "from": {"id": 4242},
                      "message": {"message_id": record.message_id, "chat": {"id": -1}},
                      "data": f"mod:a:{prop.pk}:1"}}
        self.assertEqual(telegram_bot.handle_update(wrong_chat), "wrong_chat")
        wrong_chat["callback_query"]["message"]["chat"]["id"] = -100777
        wrong_chat["callback_query"]["from"]["id"] = 777
        self.assertEqual(telegram_bot.handle_update(wrong_chat), "forbidden")
        self.assertEqual(Property.objects.get(pk=prop.pk).moderation_status, ModerationStatus.PENDING)
