"""
Релізні сценарії: редагування оголошення (права, повторна модерація, збереження даних і фото),
пошук і фільтри каталогу, приховування непублічних оголошень, XSS, завантаження фото,
health check, сторінки помилок.
"""

from django.test import TestCase, override_settings
from django.urls import reverse

from properties.models import ModerationStatus, Property, PropertyPhoto

from .helpers import make_admin, make_image, make_property, make_user


def listing_data(**over):
    data = {"name": "Оновлена квартира", "listing_type": "sale", "address": "вул. Нова, 5", "city": "Львів",
            "price": "2500000", "rooms": "3", "area_sqm": "70", "floor": "2", "total_floors": "9",
            "description": "Пандус, ліфт", "contact_phone": "+380500000001", "mobility_level": "2"}
    data.update(over)
    return data


class EditListingTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.prop = make_property(owner=self.owner, photo=make_image("main.png"))
        self.url = reverse("properties:edit_listing", args=[self.prop.pk])

    def test_owner_edits_and_listing_returns_to_moderation(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(self.url), "Редагувати оголошення")
        resp = self.client.post(self.url, listing_data())
        self.assertRedirects(resp, reverse("properties:listing_saved", args=[self.prop.pk]) + "?edited=1",
                             fetch_redirect_response=False)
        self.prop.refresh_from_db()
        self.assertEqual((self.prop.name, self.prop.city, self.prop.rooms, self.prop.area_sqm, self.prop.floor),
                         ("Оновлена квартира", "Львів", 3, 70.0, 2))
        self.assertTrue(self.prop.photo)  # головне фото збережено без повторного завантаження
        self.assertTrue(self.prop.is_published)
        self.assertEqual(self.prop.moderation_status, ModerationStatus.PENDING)
        self.assertFalse(self.prop.is_verified_by_admin)
        self.assertFalse(Property.objects.public().filter(pk=self.prop.pk).exists())  # до повторного схвалення

    def test_edit_appends_extra_photos(self):
        PropertyPhoto.objects.create(property=self.prop, image=make_image("a.png"), order=0)
        self.client.force_login(self.owner)
        self.client.post(self.url, {**listing_data(), "extra_photos": [make_image("b.png"), make_image("c.png")]})
        self.assertEqual(list(self.prop.extra_photos.order_by("order").values_list("order", flat=True)), [0, 1, 2])

    def test_invalid_data_keeps_listing_unchanged(self):
        self.client.force_login(self.owner)
        resp = self.client.post(self.url, listing_data(floor="10", total_floors="5"))
        self.assertEqual(resp.status_code, 200)
        self.prop.refresh_from_db()
        self.assertEqual((self.prop.name, self.prop.moderation_status), ("Квартира", ModerationStatus.APPROVED))

    def test_stranger_anonymous_and_archived(self):
        self.client.force_login(make_user("stranger"))
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url, listing_data(name="Злам")).status_code, 404)
        self.prop.refresh_from_db()
        self.assertEqual(self.prop.name, "Квартира")
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.owner)
        Property.objects.filter(pk=self.prop.pk).update(archived_at="2026-01-01T00:00:00Z")
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_owner_cannot_reassign_owner_via_form(self):
        other = make_user("other")
        self.client.force_login(self.owner)
        self.client.post(self.url, listing_data(owner=other.pk))
        self.prop.refresh_from_db()
        self.assertEqual(self.prop.owner_id, self.owner.pk)

    def test_admin_can_edit(self):
        self.client.force_login(make_admin())
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_detail_shows_edit_link_only_to_owner(self):
        detail = reverse("properties:property_detail", args=[self.prop.pk])
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(detail), self.url)
        self.client.force_login(make_user("viewer"))
        self.assertNotContains(self.client.get(detail), self.url)


class CreateListingTests(TestCase):
    def test_create_with_photos_goes_to_saved_confirmation(self):
        owner = make_user("owner")
        self.client.force_login(owner)
        resp = self.client.post(reverse("properties:add_listing"), {
            **listing_data(name="Нова"), "photo": make_image("m.png"), "extra_photos": [make_image("x.png")]})
        prop = Property.objects.get(name="Нова")
        self.assertRedirects(resp, reverse("properties:listing_saved", args=[prop.pk]), fetch_redirect_response=False)
        # Нове оголошення одразу в черзі модерації (не в каталозі до схвалення).
        self.assertEqual((prop.owner_id, prop.moderation_status, prop.extra_photos.count()),
                         (owner.pk, ModerationStatus.PENDING, 1))
        self.assertFalse(Property.objects.public().filter(pk=prop.pk).exists())
        self.assertTrue(prop.moderation_events.filter(action="submitted", source="owner").exists())
        self.assertContains(self.client.get(resp["Location"]), "Оголошення збережено")

    def test_photo_required_and_non_image_rejected(self):
        self.client.force_login(make_user("owner"))
        from django.core.files.uploadedfile import SimpleUploadedFile

        fake = SimpleUploadedFile("evil.png", b"<?php echo 1; ?>", content_type="image/png")
        for files in ({}, {"photo": fake}):
            resp = self.client.post(reverse("properties:add_listing"), {**listing_data(name="Без фото"), **files})
            self.assertEqual(resp.status_code, 200)
        self.assertFalse(Property.objects.filter(name="Без фото").exists())


class CatalogSearchFilterTests(TestCase):
    def setUp(self):
        owner = make_user("owner")
        self.kyiv = make_property(owner=owner, name="Світла двокімнатна", city="Київ", price=3000000, rooms=2,
                                  area_sqm=55, mobility_level=2, description="Поруч метро")
        self.lviv = make_property(owner=owner, name="Простора трикімнатна", city="Львів", price=2000000, rooms=3,
                                  area_sqm=80, mobility_level=4, listing_type="rent")
        self.hidden = [
            make_property(owner=owner, name="Чернетка-приховано", public=False),
            make_property(owner=owner, name="Відхилене-приховано", moderation_status=ModerationStatus.REJECTED),
            make_property(owner=owner, name="Без фотоперевірки-приховано", is_verified_by_admin=False),
            make_property(owner=owner, name="Архів-приховано", archived_at="2026-01-01T00:00:00Z"),
        ]

    def names(self, query=""):
        html = self.client.get("/catalog/" + query).content.decode()
        return {p.name for p in Property.objects.all() if f'/property/{p.pk}/"' in html}

    def test_only_public_listings_shown(self):
        self.assertEqual(self.names(), {self.kyiv.name, self.lviv.name})
        for prop in self.hidden:
            self.assertEqual(self.client.get(f"/property/{prop.pk}/").status_code, 404, prop.name)

    def test_search_and_filters(self):
        cases = {"?q=метро": {self.kyiv.name}, "?q=трикімнатна": {self.lviv.name}, "?city=Львів": {self.lviv.name},
                 "?price_max=2500000": {self.lviv.name}, "?price_min=2500000": {self.kyiv.name},
                 "?rooms=3": {self.lviv.name}, "?area_min=60": {self.lviv.name}, "?type=rent": {self.lviv.name},
                 "?accessibility_level=2": {self.kyiv.name}, "?q=приховано": set()}
        for query, expected in cases.items():
            self.assertEqual(self.names(query), expected, query)

    def test_bad_filter_values_do_not_crash(self):
        for query in ("?price_min=abc", "?rooms=-1", "?area_max=x", "?page=999", "?accessibility_level=9",
                      "?feature=abc", "?sort=unknown"):
            self.assertEqual(self.client.get("/catalog/" + query).status_code, 200, query)

    def test_user_content_is_escaped(self):
        self.kyiv.name = '<script>alert("x")</script>'
        self.kyiv.save()
        for url in ("/catalog/", f"/property/{self.kyiv.pk}/"):
            html = self.client.get(url).content.decode()
            self.assertNotIn('<script>alert("x")</script>', html, url)
            self.assertIn("&lt;script&gt;", html, url)


class OpsPagesTests(TestCase):
    def test_healthz(self):
        resp = self.client.get("/healthz/")
        self.assertEqual((resp.status_code, resp.json()), (200, {"status": "ok"}))

    def test_healthz_reports_db_failure_without_details(self):
        from unittest import mock

        with mock.patch("django.db.backends.utils.CursorWrapper.execute", side_effect=RuntimeError("secret detail")):
            resp = self.client.get("/healthz/")
        self.assertEqual((resp.status_code, resp.json()), (503, {"status": "error"}))

    @override_settings(DEBUG=False)
    def test_custom_404_page(self):
        resp = self.client.get("/no-such-page/")
        self.assertEqual(resp.status_code, 404)
        self.assertContains(resp, "Сторінку не знайдено", status_code=404)
        self.assertContains(resp, 'data-theme="dark"', status_code=404)


class FormRateLimitTests(TestCase):
    """Спам-захист форм, які анонім може надсилати масово (ліміт на IP, лише прийняті запити)."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def test_password_reset_sends_at_most_limit_emails(self):
        from django.core import mail

        make_user("bob")
        for _ in range(8):
            resp = self.client.post("/auth/password-reset/", {"email": "bob@example.com"})
            self.assertEqual(resp.status_code, 302)  # та сама відповідь — не розкриває ліміт чи існування email
        self.assertEqual(len(mail.outbox), 5)

    def test_registration_limit(self):
        from django.contrib.auth.models import User

        for i in range(12):
            self.client.post("/auth/", {"form_type": "register", "username": f"user{i}",
                                        "email": f"user{i}@example.com", "password1": "Str0ng!Passw0rd-xyz",
                                        "password2": "Str0ng!Passw0rd-xyz"})
            self.client.logout()
        self.assertEqual(User.objects.filter(username__startswith="user").count(), 10)

    def test_consultation_limit(self):
        import json

        from properties.models import ConsultationRequest

        codes = [self.client.post("/consultation/submit/", json.dumps({
            "name": "Тест", "phone": "+380501234567", "service_type": "realtor_certification", "message": "x"}),
            content_type="application/json").status_code for _ in range(7)]
        self.assertEqual(codes.count(429), 7 - ConsultationRequest.objects.count())
        self.assertLessEqual(ConsultationRequest.objects.count(), 5)


class DemoFindingsRegressionTests(TestCase):
    """Вади, знайдені під час браузерної перевірки 25.11: фейкова статистика, «за 1 днів», згорнута форма."""

    def setUp(self):
        self.owner = make_user("owner")
        self.client.force_login(self.owner)

    def test_profile_stats_are_real(self):
        from properties.models import PropertyFavorite

        live = make_property(owner=self.owner, view_count=7)
        make_property(owner=self.owner, public=False, view_count=3)
        PropertyFavorite.objects.create(user=make_user("fan"), property=live)
        html = self.client.get("/profile/").content.decode()
        for fake in ("1,420", "+18%", "+12 дзвонів", "5.9%", "Висока активність"):
            self.assertNotIn(fake, html)
        stats = self.client.get("/profile/").context["owner_stats"]
        self.assertEqual(stats, {"views": 10, "favorites": 1, "published": 1, "pending": 1})

    @override_settings(PAYMENTS_ENABLED=True, LIQPAY_PUBLIC_KEY="sandbox_t", LIQPAY_PRIVATE_KEY="sandbox_p",
                       LIQPAY_SANDBOX=True, SITE_URL="https://example.ua")
    def test_checkout_page_shows_exact_duration(self):
        prop = make_property(owner=self.owner)
        html = self.client.post(f"/payment/{prop.pk}/start/", {"tariff": "active_12h"}).content.decode()
        self.assertIn("на 12 годин", html)
        self.assertNotIn("днів.", html)

    def test_add_listing_form_is_expanded(self):
        html = self.client.get("/add-listing/").content.decode()
        self.assertNotIn('class="collapse', html.split('id="addListingCollapse"')[0][-60:] + html.split('id="addListingCollapse"')[1][:40])
        self.assertNotIn('data-bs-target="#addListingCollapse"', html)
