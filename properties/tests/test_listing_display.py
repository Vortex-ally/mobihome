"""Етап 5: відображення оголошень (картки, сторінка, галерея, форма, без фальшивих даних)."""

import json

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from properties.models import Feature, Property, PropertyPhoto

from .helpers import make_image, make_property, make_user


def _with_photos(owner, extra=2, **kw):
    prop = make_property(owner=owner, photo=make_image("main.png"), **kw)
    for i in range(extra):
        PropertyPhoto.objects.create(property=prop, image=make_image(f"g{i}.png"), order=i)
    return prop


class NoFakePhotosTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.bare = make_property(owner=self.owner, name="Без фото", city="Львів")

    def test_catalog_and_detail_use_neutral_placeholder(self):
        for url in ("/catalog/", f"/property/{self.bare.pk}/", f"/compare/?ids={self.bare.pk}"):
            html = self.client.get(url).content.decode()
            self.assertNotIn("images.unsplash.com", html, url)
        catalog = self.client.get("/catalog/").content.decode()
        self.assertIn("img/no-photo.svg", catalog)
        self.assertIn("Фото відсутнє", catalog)
        detail = self.client.get(f"/property/{self.bare.pk}/").content.decode()
        self.assertIn("Фотографії відсутні", detail)

    def test_quick_view_returns_no_photos_for_bare_listing(self):
        data = self.client.get(f"/property/{self.bare.pk}/quick-view/").json()
        self.assertEqual(data["photos"], [])

    def test_homepage_card_gets_placeholder_not_stock_photo(self):
        resp = self.client.get("/")
        cards = resp.context["featured"]
        self.assertTrue(all(not p.listing_photo_url.startswith("https://images.unsplash") for p in cards))


class GalleryAndCardTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.prop = _with_photos(self.owner, extra=2, rooms=2, area_sqm=55.5, floor=5, total_floors=9,
                                 price=1500000, description="Світла квартира з ремонтом " * 10)

    def test_catalog_card_shows_specs_count_and_formatted_price(self):
        html = self.client.get("/catalog/").content.decode()
        self.assertIn("📷 3", html)
        self.assertIn("5/9 пов.", html)
        self.assertIn("55.5 м²", html.replace(",", "."))
        self.assertRegex(html, r"1[\s  ]500[\s  ]000 грн")
        self.assertIn("listing-card-desc", html)

    def test_detail_gallery_has_all_real_photos_in_order(self):
        resp = self.client.get(f"/property/{self.prop.pk}/")
        photos = resp.context["property_photos"]
        self.assertEqual(len(photos), 3)
        self.assertIn("main", photos[0])
        self.assertContains(resp, "5 з 9")

    def test_quick_view_floor_and_photos(self):
        data = self.client.get(f"/property/{self.prop.pk}/quick-view/").json()
        self.assertEqual((data["floor"], data["total_floors"], len(data["photos"])), (5, 9, 3))

    def test_catalog_has_no_n_plus_one(self):
        feature = Feature.objects.first()
        def count_queries():
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get("/catalog/").status_code, 200)
            return len(ctx.captured_queries)
        for p in Property.objects.all():
            p.features.add(feature)
        base = count_queries()
        for i in range(6):
            extra = _with_photos(self.owner, extra=1, name=f"Ще {i}")
            extra.features.add(feature)
        self.assertEqual(count_queries(), base)


class DetailHonestyAndSimilarTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.prop = make_property(owner=self.owner, city="Київ", rooms=2, listing_type="sale")

    def test_no_fabricated_points_of_interest(self):
        html = self.client.get(f"/property/{self.prop.pk}/").content.decode()
        for fake in ("Сільпо", "Бажаємо Здоров", "тролейбуса №18", "Інклюзивне паркомісце"):
            self.assertNotIn(fake, html)
        self.assertIn("Розташування", html)
        self.assertIn("приблизне розташування", html)
        self.assertIn("openstreetmap.org/search?query=", html)

    def test_map_only_for_known_city(self):
        unknown = make_property(owner=self.owner, city="Невідомськ")
        resp = self.client.get(f"/property/{unknown.pk}/")
        self.assertIsNone(resp.context["map_center"])
        self.assertNotContains(resp, 'id="poi-map"')

    def test_catalog_map_skips_unknown_cities(self):
        make_property(owner=self.owner, city="Невідомськ")
        cities = json.loads(self.client.get("/catalog/").context["map_cities_json"])
        self.assertNotIn("Невідомськ", [c["city"] for c in cities])

    def test_similar_listings(self):
        same = make_property(owner=self.owner, city="Київ", rooms=2, name="Схожа")
        make_property(owner=self.owner, city="Львів", name="Інше місто")
        make_property(owner=self.owner, city="Київ", listing_type="rent", name="Оренда")
        make_property(owner=self.owner, city="Київ", public=False, name="Прихована")
        similar = self.client.get(f"/property/{self.prop.pk}/").context["similar_listings"]
        self.assertEqual([p.name for p in similar], ["Схожа"])
        self.assertEqual(self.client.get(f"/property/{same.pk}/").context["similar_listings"][0].pk, self.prop.pk)

    def test_no_similar_section_when_empty(self):
        self.assertNotContains(self.client.get(f"/property/{self.prop.pk}/"), "Схожі оголошення")

    def test_favorite_button_is_wired(self):
        self.client.force_login(make_user("viewer"))
        html = self.client.get(f"/property/{self.prop.pk}/").content.decode()
        self.assertIn(f"/property/{self.prop.pk}/favorite/", html)
        self.assertIn("X-CSRFToken", html)


class AddListingFormTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.client.force_login(self.owner)

    def _post(self, **extra):
        data = {"name": "Нова", "listing_type": "sale", "address": "вул. 1", "city": "Київ",
                "price": "100", "photo": make_image()}
        data.update(extra)
        return self.client.post("/add-listing/", data)

    def test_floor_saved(self):
        self._post(floor="3", total_floors="9")
        prop = Property.objects.get(name="Нова")
        self.assertEqual((prop.floor, prop.total_floors), (3, 9))

    def test_floor_above_total_rejected(self):
        resp = self._post(floor="10", total_floors="9")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Поверх не може бути більшим")
        self.assertFalse(Property.objects.filter(name="Нова").exists())

    def test_floor_zero_rejected(self):
        self._post(floor="0")
        self.assertFalse(Property.objects.filter(name="Нова").exists())

    def test_extra_photos_validated_and_saved(self):
        self._post(extra_photos=[make_image("a.png"), make_image("b.png")])
        prop = Property.objects.get(name="Нова")
        self.assertEqual(prop.extra_photos.count(), 2)

    def test_non_image_extra_photo_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad = SimpleUploadedFile("evil.png", b"<script>alert(1)</script>", content_type="image/png")
        resp = self._post(extra_photos=[bad])
        self.assertContains(resp, "не є коректним зображенням")
        self.assertFalse(Property.objects.filter(name="Нова").exists())

    def test_too_many_extra_photos_rejected(self):
        resp = self._post(extra_photos=[make_image(f"{i}.png") for i in range(21)])
        self.assertContains(resp, "не більше 20")
        self.assertFalse(Property.objects.filter(name="Нова").exists())

    def test_form_renders_floor_inputs(self):
        html = self.client.get("/add-listing/").content.decode()
        self.assertIn('name="floor"', html)
        self.assertIn('name="total_floors"', html)
