"""Модерація, права доступу до оголошень, IDOR, фіктивна оплата, адмін-дії."""

import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.test import TestCase

from properties.models import AccessibilityAudit, AuditRequest, Feature, ModerationStatus, Property

from .helpers import make_admin, make_image, make_property, make_user


class ModerationFlowTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.admin = make_admin()

    def test_new_listing_requires_admin_approval(self):
        self.client.force_login(self.owner)
        resp = self.client.post(
            "/add-listing/",
            {"name": "Нова", "listing_type": "sale", "address": "вул. 1", "city": "Львів",
             "price": "100", "photo": make_image()},
        )
        prop = Property.objects.get(name="Нова")
        self.assertRedirects(resp, f"/payment/{prop.pk}/", fetch_redirect_response=False)
        self.assertEqual(prop.owner, self.owner)
        self.assertEqual(prop.moderation_status, ModerationStatus.PENDING)

        # «Безкоштовна публікація» більше не схвалює автоматично
        self.client.post(f"/payment/{prop.pk}/", {"action": "free"})
        prop.refresh_from_db()
        self.assertEqual(prop.moderation_status, ModerationStatus.PENDING)
        self.assertFalse(prop.is_verified_by_admin)
        self.assertFalse(Property.objects.public().filter(pk=prop.pk).exists())
        self.client.logout()
        self.assertEqual(self.client.get(f"/property/{prop.pk}/").status_code, 404)

        # Схвалення адміністратором
        self.client.force_login(self.admin)
        self.client.post(f"/admin-action/property/{prop.pk}/approve/")
        self.assertTrue(Property.objects.public().filter(pk=prop.pk).exists())

    def test_non_owner_cannot_publish(self):
        prop = make_property(owner=self.owner, public=False)
        self.client.force_login(make_user("other"))
        self.client.post(f"/payment/{prop.pk}/", {"action": "free"})
        prop.refresh_from_db()
        self.assertFalse(prop.is_published)

    def test_non_staff_cannot_use_admin_actions(self):
        prop = make_property(owner=self.owner, public=False)
        self.client.force_login(self.owner)
        self.client.post(f"/admin-action/property/{prop.pk}/approve/")
        self.client.post(f"/admin-action/property/{prop.pk}/verify-photos/")
        prop.refresh_from_db()
        self.assertEqual(prop.moderation_status, ModerationStatus.PENDING)
        self.assertFalse(prop.is_verified_by_admin)

    def test_anonymous_cannot_access_django_admin(self):
        resp = self.client.get("/admin/properties/property/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp["Location"])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get("/admin/properties/property/").status_code, 302)


class FakePaymentRemovedTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.prop = make_property(owner=self.owner)

    def test_other_user_cannot_promote(self):
        self.client.force_login(make_user("other"))
        resp = self.client.post(
            f"/property/{self.prop.pk}/promote/",
            {"card_number": "4111111111111111", "expiry_month": 12, "expiry_year": 2030,
             "cvc": "123", "card_holder": "X Y"},
        )
        self.assertEqual(resp.status_code, 302)
        self.prop.refresh_from_db()
        self.assertFalse(self.prop.is_featured)

    def test_owner_cannot_get_free_promotion(self):
        self.client.force_login(self.owner)
        self.client.post(f"/property/{self.prop.pk}/promote/", {"card_number": "4111111111111111"})
        self.prop.refresh_from_db()
        self.assertFalse(self.prop.is_featured)

    def test_no_card_fields_in_payment_page(self):
        prop = make_property(owner=self.owner, public=False)
        self.client.force_login(self.owner)
        html = self.client.get(f"/payment/{prop.pk}/").content.decode().lower()
        self.assertNotIn("cvc", html)
        self.assertNotIn("card_number", html)
        self.assertNotIn("paypal", html)


class UnpublishedVisibilityTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.hidden = make_property(owner=self.owner, public=False)
        self.public = make_property(owner=self.owner, name="Публічна")

    def test_hidden_listing_not_exposed_to_strangers(self):
        self.client.force_login(make_user("stranger"))
        pk = self.hidden.pk
        self.assertEqual(self.client.get(f"/property/{pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/property/{pk}/quick-view/").status_code, 404)
        self.assertEqual(self.client.get(f"/property/{pk}/download-passport/").status_code, 404)
        self.assertEqual(self.client.get(f"/audit/request/{pk}/").status_code, 404)
        self.assertEqual(self.client.post(f"/property/{pk}/favorite/").status_code, 404)
        compare = self.client.get(f"/compare/?ids={pk}")
        self.assertEqual(compare.status_code, 200)
        self.assertEqual(list(compare.context["properties"]), [])

    def test_owner_sees_own_hidden_listing(self):
        self.client.force_login(self.owner)
        pk = self.hidden.pk
        self.assertEqual(self.client.get(f"/property/{pk}/").status_code, 200)
        self.assertEqual(self.client.get(f"/property/{pk}/quick-view/").status_code, 200)
        self.assertEqual(self.client.get(f"/property/{pk}/download-passport/").status_code, 200)

    def test_quick_view_public_no_500(self):
        resp = self.client.get(f"/property/{self.public.pk}/quick-view/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Публічна")

    def test_favorite_requires_post(self):
        self.client.force_login(make_user("u"))
        self.assertEqual(self.client.get(f"/property/{self.public.pk}/favorite/").status_code, 405)
        resp = self.client.post(f"/property/{self.public.pk}/favorite/")
        self.assertEqual(resp.json(), {"is_favorite": True})

    def test_non_owner_cannot_delete(self):
        self.client.force_login(make_user("stranger"))
        self.client.post(f"/property/{self.public.pk}/delete/")
        self.assertTrue(Property.objects.filter(pk=self.public.pk).exists())


class AdminPanelTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.client.force_login(self.admin)

    def test_bulk_actions_work(self):
        prop = make_property(public=False)
        for action in ("make_approved", "unverify_by_admin", "make_verified_by_admin",
                       "make_rejected", "make_verified"):
            extra = {"post": "yes", "reason": "Нечіткі фото"} if action == "make_rejected" else {}
            resp = self.client.post(
                "/admin/properties/property/", {"action": action, "_selected_action": [prop.pk], **extra}
            )
            self.assertEqual(resp.status_code, 302, action)
        prop.refresh_from_db()
        self.assertEqual(prop.moderation_status, ModerationStatus.REJECTED)

    def test_admin_changelists_render(self):
        for model in ("property", "propertyphoto", "feature", "accessibilityaudit", "auditrequest",
                      "consultationrequest", "userprofile", "authsession", "profilereview"):
            self.assertEqual(self.client.get(f"/admin/properties/{model}/").status_code, 200, model)

    def test_update_audit_request_validates_input(self):
        prop = make_property()
        req = AuditRequest.objects.create(name="A", phone="123", property_address="x", property=prop)
        resp = self.client.post(f"/admin-action/audit-request/{req.pk}/update/",
                                {"status": "completed", "mobility_level": "abc", "total_score": "zzz"})
        self.assertEqual(resp.status_code, 302)
        self.client.post(f"/admin-action/audit-request/{req.pk}/update/",
                         {"status": "completed", "mobility_level": "4", "total_score": "8.5"})
        prop.refresh_from_db()
        self.assertEqual(prop.mobility_level, 4)
        self.assertTrue(prop.is_verified)
        self.assertEqual(AccessibilityAudit.objects.get(property=prop).total_score, 8.5)

    def test_auditor_form_invalid_property_no_500(self):
        resp = self.client.post("/audit/new/", {"property": "999999", "entrance_access": "5"})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post("/audit/new/", {"property": "abc"})
        self.assertEqual(resp.status_code, 200)


class SmokePagesTests(TestCase):
    def test_public_pages_render(self):
        prop = make_property()
        for url in ("/", "/catalog/", "/about/", "/services/", "/contact/", "/auth/",
                    "/rivni-dostupnosti/", f"/property/{prop.pk}/", "/privacy-policy/",
                    "/terms-of-service/", "/auth/password-reset/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_catalog_escapes_listing_name(self):
        make_property(name='<img src=x onerror=alert(1)>')
        html = self.client.get("/catalog/").content.decode()
        self.assertNotIn("<img src=x onerror", html)
        self.assertIn("qvEscape", html)


class MigrationAndSettingsTests(TestCase):
    def test_feature_slugs_unique_and_non_empty(self):
        slugs = list(Feature.objects.values_list("slug", flat=True))
        self.assertTrue(slugs)
        self.assertNotIn("", slugs)
        self.assertEqual(len(slugs), len(set(slugs)))

    def test_production_refuses_to_start_without_secret_key(self):
        if (Path(settings.BASE_DIR) / ".env").exists():
            self.skipTest("Локальний .env задає ключ — перевірка не застосовна")
        env = {k: v for k, v in os.environ.items() if not k.startswith("DJANGO_")}
        env.update(DJANGO_DEBUG="False", DJANGO_SETTINGS_MODULE="config.settings",
                   DJANGO_SQLITE_PATH=str(Path(settings.MEDIA_ROOT) / "x.sqlite3"))
        proc = subprocess.run(
            [sys.executable, "-c", "import django; django.setup()"],
            cwd=settings.BASE_DIR, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", proc.stderr)
