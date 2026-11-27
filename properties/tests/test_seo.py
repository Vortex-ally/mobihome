"""SEO: robots.txt, sitemap.xml, meta-теги, canonical, JSON-LD, noindex для не-production."""

import json
import re
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from properties.models import ModerationStatus

from .helpers import make_property, make_user

PROD = dict(SEO_ALLOW_INDEXING=True, SEO_BASE_URL="https://mobihome.org",
            SEO_INDEXABLE_HOSTS=["mobihome.org", "www.mobihome.org"],
            ALLOWED_HOSTS=["mobihome.org", "www.mobihome.org", "sandbox.trycloudflare.com", "testserver"])
PROD_HOST = {"HTTP_HOST": "mobihome.org", "secure": True}
SANDBOX_HOST = {"HTTP_HOST": "sandbox.trycloudflare.com", "secure": True}

PUBLIC_URLS = ["/", "/catalog/", "/about/", "/services/", "/contact/",
               "/rivni-dostupnosti/", "/privacy-policy/", "/terms-of-service/"]


def jsonld(html):
    return [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


@override_settings(**PROD)
class ProductionSeoTests(TestCase):
    def get(self, url, **extra):
        return self.client.get(url, **PROD_HOST, **extra)

    def test_robots_txt(self):
        resp = self.get("/robots.txt")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/plain; charset=utf-8")
        body = resp.content.decode()
        self.assertIn("Allow: /\n", body)
        self.assertNotIn("Disallow: /\n", body)
        for path in ("/admin/", "/profile/", "/payment/", "/payments/", "/auth/", "/add-listing/"):
            self.assertIn("Disallow: %s\n" % path, body)
        self.assertIn("Sitemap: https://mobihome.org/sitemap.xml", body)
        self.assertFalse(resp.has_header("X-Robots-Tag"))

    def test_sitemap_contains_only_public_listings(self):
        owner = make_user("owner")
        public = make_property(owner=owner, name="Публічна")
        hidden = [
            make_property(owner=owner, public=False),  # приватне/немодероване
            make_property(owner=owner, is_published=False),  # приховане
            make_property(owner=owner, moderation_status=ModerationStatus.REJECTED),
            make_property(owner=owner, is_verified_by_admin=False),
            make_property(owner=owner, archived_at=timezone.now()),  # видалене (архів)
        ]
        resp = self.get("/sitemap.xml")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("xml", resp["Content-Type"])
        body = resp.content.decode()
        self.assertIn("<loc>https://mobihome.org/property/%d/</loc>" % public.pk, body)
        for prop in hidden:
            self.assertNotIn("/property/%d/" % prop.pk, body)
        for url in PUBLIC_URLS:
            self.assertIn("<loc>https://mobihome.org%s</loc>" % url, body)
        self.assertNotIn("http://", body.replace("http://www.sitemaps.org", "").replace("http://www.w3.org", ""))
        self.assertNotIn("/faq/", body)  # редірект на головну
        for private in ("/admin/", "/profile/", "/payment", "/auth/"):
            self.assertNotIn(private, body)

    @override_settings(PLACEMENT_PAYMENT_REQUIRED=True)
    def test_sitemap_respects_paid_placement(self):
        expired = make_property(placement_until=timezone.now() - timedelta(days=1))
        active = make_property(placement_until=timezone.now() + timedelta(days=1))
        body = self.get("/sitemap.xml").content.decode()
        self.assertNotIn("/property/%d/" % expired.pk, body)
        self.assertIn("/property/%d/" % active.pk, body)

    def test_public_pages_have_unique_meta_and_are_indexable(self):
        titles, descriptions = set(), set()
        for url in PUBLIC_URLS:
            resp = self.get(url)
            self.assertEqual(resp.status_code, 200, url)
            html = resp.content.decode()
            self.assertIn('<meta name="robots" content="index, follow">', html, url)
            self.assertNotIn("noindex", html, url)
            self.assertFalse(resp.has_header("X-Robots-Tag"), url)
            self.assertIn('<link rel="canonical" href="https://mobihome.org%s">' % url, html, url)
            self.assertIn('<html lang="uk"', html)
            self.assertIn('property="og:title"', html)
            self.assertIn('name="twitter:card"', html)
            titles.add(re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip())
            descriptions.add(re.search(r'<meta name="description" content="(.*?)">', html).group(1))
            for data in jsonld(html):
                self.assertEqual(data["@context"], "https://schema.org")
        self.assertEqual(len(titles), len(PUBLIC_URLS))
        self.assertEqual(len(descriptions), len(PUBLIC_URLS))

    def test_homepage_jsonld(self):
        types = {d["@type"] for d in jsonld(self.get("/").content.decode())}
        self.assertEqual(types, {"Organization", "WebSite"})
        crumbs = jsonld(self.get("/about/").content.decode())
        self.assertEqual(crumbs[0]["@type"], "BreadcrumbList")

    def test_catalog_canonical_drops_filters_keeps_page(self):
        html = self.get("/catalog/?city=Львів&type=sale").content.decode()
        self.assertIn('<link rel="canonical" href="https://mobihome.org/catalog/">', html)

    def test_listing_meta_and_jsonld(self):
        prop = make_property(name="Квартира біля парку", rooms=2, area_sqm=54, city="Львів",
                             description="Світла квартира з пандусом і широкими дверима.", mobility_level=3)
        html = self.get("/property/%d/" % prop.pk).content.decode()
        self.assertIn("<title>Квартира біля парку — Продаж, 2-кімн., Львів | mobi Home</title>", html)
        self.assertIn('content="Світла квартира з пандусом і широкими дверима."', html)
        self.assertIn('<link rel="canonical" href="https://mobihome.org/property/%d/">' % prop.pk, html)
        self.assertIn('<meta name="robots" content="index, follow">', html)
        data = {d["@type"]: d for d in jsonld(html)}
        listing = data["RealEstateListing"]
        self.assertEqual(listing["offers"]["price"], "1000000.00")
        self.assertEqual(listing["offers"]["priceCurrency"], "UAH")
        self.assertEqual(listing["about"]["address"]["addressLocality"], "Львів")
        self.assertEqual(listing["about"]["numberOfRooms"], 2)
        self.assertNotIn("aggregateRating", html)
        self.assertNotIn("review", json.dumps(listing))
        self.assertEqual(len(data["BreadcrumbList"]["itemListElement"]), 3)

    def test_listing_without_price_has_no_offer(self):
        prop = make_property(price=None)
        listing = {d["@type"]: d for d in jsonld(self.get("/property/%d/" % prop.pk).content.decode())}
        self.assertNotIn("offers", listing["RealEstateListing"])

    def test_jsonld_escapes_user_data(self):
        prop = make_property(name='</script><script>alert(1)</script>', description="a & b <b>")
        html = self.get("/property/%d/" % prop.pk).content.decode()
        self.assertNotIn("<script>alert(1)", html)
        data = {d["@type"]: d for d in jsonld(html)}
        self.assertEqual(data["RealEstateListing"]["name"], '</script><script>alert(1)</script>')

    def test_unpublished_listing_visible_to_owner_is_noindex(self):
        owner = make_user("owner")
        prop = make_property(owner=owner, public=False)
        self.client.force_login(owner)
        resp = self.get("/property/%d/" % prop.pk)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('<meta name="robots" content="noindex, nofollow">', html)
        self.assertNotIn("RealEstateListing", html)

    def test_private_pages_are_noindex(self):
        for url in ("/auth/", "/login/", "/register/", "/admin/login/"):
            resp = self.get(url)
            self.assertEqual(resp["X-Robots-Tag"], "noindex, nofollow", url)
        self.assertIn('content="noindex, nofollow"', self.get("/auth/").content.decode())


@override_settings(**PROD)
class SandboxSeoTests(TestCase):
    def test_sandbox_host_disallows_everything(self):
        resp = self.client.get("/robots.txt", **SANDBOX_HOST)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content.decode(), "User-agent: *\nDisallow: /\n")
        self.assertEqual(resp["X-Robots-Tag"], "noindex, nofollow")

    def test_sandbox_pages_noindex(self):
        resp = self.client.get("/", **SANDBOX_HOST)
        self.assertEqual(resp["X-Robots-Tag"], "noindex, nofollow")
        self.assertIn('<meta name="robots" content="noindex, nofollow">', resp.content.decode())
        # canonical завжди вказує на production
        self.assertIn('<link rel="canonical" href="https://mobihome.org/">', resp.content.decode())

    @override_settings(SEO_ALLOW_INDEXING=False)
    def test_indexing_flag_off_blocks_production_host(self):
        resp = self.client.get("/robots.txt", **PROD_HOST)
        self.assertIn("Disallow: /\n", resp.content.decode())
        self.assertEqual(self.client.get("/", **PROD_HOST)["X-Robots-Tag"], "noindex, nofollow")
