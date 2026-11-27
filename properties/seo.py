"""SEO: метадані сторінок, robots.txt, sitemap.xml, JSON-LD, X-Robots-Tag.

Індексація дозволена лише на production: settings.SEO_ALLOW_INDEXING=True і хост запиту
входить у settings.SEO_INDEXABLE_HOSTS. Локальне, demo та sandbox середовища (інший хост
або DEBUG) отримують robots.txt «Disallow: /», meta robots noindex та X-Robots-Tag.
"""

import json
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.sitemaps import Sitemap
from django.http import HttpResponse
from django.templatetags.static import static
from django.urls import reverse
from django.utils.html import strip_tags
from django.utils.safestring import mark_safe
from django.utils.text import Truncator
from django.views.decorators.http import require_GET

from .models import ListingType, Property

SITE_NAME = "mobi Home"
DEFAULT_DESCRIPTION = (
    "mobi Home — каталог доступного житла без бар'єрів в Україні: квартири для продажу та "
    "оренди з перевіреним рівнем доступності для людей з інвалідністю, батьків з дітьми та літніх людей."
)

# Публічні сторінки, що індексуються: url_name -> (title, description, breadcrumb-назва).
PUBLIC_PAGES = {
    "index": (
        "mobi Home — житло без бар'єрів: доступні квартири для продажу та оренди",
        DEFAULT_DESCRIPTION,
        None,
    ),
    "home": (
        "Каталог доступного житла — квартири для продажу та оренди | mobi Home",
        "Каталог перевірених оголошень про продаж та оренду житла з оцінкою доступності: "
        "ліфт, пандус, ширина дверей, санвузол. Фільтри за містом, ціною та рівнем доступності.",
        "Каталог",
    ),
    "accessibility_levels": (
        "Рівні доступності житла — пояснення шкали 1–5 | mobi Home",
        "Що означають рівні доступності житла від 1 до 5 на mobi Home: для батьків з дитячим візком, "
        "людей на кріслі колісному, з порушеннями опорно-рухового апарату та універсальний рівень.",
        "Рівні доступності",
    ),
    "services": (
        "Послуги — аудит доступності житла | mobi Home",
        "Послуги mobi Home: аудит доступності квартири та будинку, паспорт доступності, "
        "консультації з підбору житла без бар'єрів.",
        "Послуги",
    ),
    "about": (
        "Про компанію mobi Home — місія та команда",
        "mobi Home допомагає людям з інвалідністю, батькам з дітьми та літнім людям знаходити "
        "житло без бар'єрів. Дізнайтеся про нашу місію та підхід до перевірки оголошень.",
        "Про нас",
    ),
    "contact": (
        "Контакти mobi Home — зв'язатися з нами",
        "Контакти mobi Home: телефон, електронна пошта та Telegram для питань щодо оголошень, "
        "аудиту доступності та співпраці.",
        "Контакти",
    ),
    "privacy_policy": (
        "Політика конфіденційності | mobi Home",
        "Політика конфіденційності mobi Home: які персональні дані ми збираємо, як їх використовуємо та захищаємо.",
        "Політика конфіденційності",
    ),
    "terms_of_service": (
        "Правила сервісу | mobi Home",
        "Правила користування сервісом mobi Home: розміщення оголошень, модерація та відповідальність сторін.",
        "Правила сервісу",
    ),
}

# Службові шляхи: заборонені в robots.txt і отримують X-Robots-Tag: noindex навіть на production.
PRIVATE_PREFIXES = (
    "/admin/", "/admin-action/", "/auth/", "/login/", "/register/", "/logout/",
    "/profile/", "/add-listing/", "/payment/", "/payments/", "/staff/", "/compare/",
    "/audit/", "/consultation/", "/healthz/",
)
PRIVATE_PROPERTY_SUFFIXES = (
    "edit/", "delete/", "saved/", "promote/", "favorite/", "track-click/",
    "quick-view/", "download-passport/",
)
NOINDEX = "noindex, nofollow"


def base_url():
    return settings.SEO_BASE_URL


def absolute(path):
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    return base_url() + (path if path.startswith("/") else "/" + path)


def indexing_allowed(request):
    """Чи можна індексувати це середовище (production-хост і прапорець увімкнено)."""
    if not settings.SEO_ALLOW_INDEXING:
        return False
    try:
        host = request.get_host().split(":")[0].lower()
    except Exception:
        return False
    return host in settings.SEO_INDEXABLE_HOSTS


def is_private_path(path):
    if path.startswith(PRIVATE_PREFIXES):
        return True
    return path.startswith("/property/") and path.endswith(PRIVATE_PROPERTY_SUFFIXES)


def ld_json(data):
    """Безпечний <script type="application/ld+json">: екрануються <, >, & (захист від </script>)."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return mark_safe('<script type="application/ld+json">%s</script>' % text)


def breadcrumbs(items):
    """items: [(назва, абсолютний URL), ...] -> BreadcrumbList."""
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": name, "item": url}
            for i, (name, url) in enumerate(items, start=1)
        ],
    }


def organization_ld():
    org = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "@id": base_url() + "/#organization",
        "name": SITE_NAME,
        "url": base_url() + "/",
        "logo": absolute(static("img/logo.png")),
    }
    contact = {k: v for k, v in (("email", settings.SITE_EMAIL), ("telephone", settings.SITE_PHONE)) if v}
    if contact:
        org["contactPoint"] = [{"@type": "ContactPoint", "contactType": "customer support",
                                "availableLanguage": "uk", **contact}]
    return org


def website_ld():
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "@id": base_url() + "/#website",
        "name": SITE_NAME,
        "url": base_url() + "/",
        "inLanguage": "uk",
        "publisher": {"@id": base_url() + "/#organization"},
    }


def _home_crumb():
    return ("Головна", base_url() + "/")


def page_seo(request):
    """SEO-контекст сторінки для base.html (контекст-процесор)."""
    match = getattr(request, "resolver_match", None)
    url_name = getattr(match, "url_name", "") or ""
    page = PUBLIC_PAGES.get(url_name)
    allowed = indexing_allowed(request)

    canonical = absolute(request.path)
    if url_name == "home":
        num = request.GET.get("page", "")
        if num.isdigit() and int(num) > 1:
            canonical += "?page=%d" % int(num)

    jsonld = []
    if url_name == "index":
        jsonld = [organization_ld(), website_ld()]
    elif page and page[2]:
        jsonld = [breadcrumbs([_home_crumb(), (page[2], absolute(request.path))])]

    return {
        "url_name": url_name,
        "title": page[0] if page else SITE_NAME,
        "description": page[1] if page else DEFAULT_DESCRIPTION,
        "canonical": canonical,
        # property_detail вирішує сам (лише публічні оголошення); інші — лише з білого списку.
        "robots": "index, follow" if allowed and (page or url_name == "property_detail") else NOINDEX,
        "indexing_allowed": allowed,
        "image": absolute(static("img/hero.png")),
        "site_name": SITE_NAME,
        "jsonld": mark_safe("".join(ld_json(d) for d in jsonld)),
    }


# --- Оголошення -------------------------------------------------------------------------

def _listing_photos(prop):
    urls = []
    for f in [prop.photo] + [p.image for p in prop.extra_photos.all()]:
        try:
            url = f.url if f else ""
        except ValueError:
            url = ""
        if url and absolute(url) not in urls:
            urls.append(absolute(url))
    return urls


def _fmt_number(value):
    return "{:,.0f}".format(value).replace(",", " ")


def listing_title(prop):
    parts = [ListingType(prop.listing_type).label if prop.listing_type in ListingType.values else ""]
    if prop.rooms:
        parts.append("%d-кімн." % prop.rooms)
    if prop.city:
        parts.append(prop.city)
    suffix = ", ".join(p for p in parts if p)
    return "%s — %s | %s" % (prop.name, suffix, SITE_NAME) if suffix else "%s | %s" % (prop.name, SITE_NAME)


def listing_description(prop):
    text = " ".join(strip_tags(prop.description or "").split())
    if text:
        return Truncator(text).chars(160)
    facts = []
    if prop.listing_type in ListingType.values:
        facts.append(ListingType(prop.listing_type).label)
    if prop.rooms:
        facts.append("%d кімн." % prop.rooms)
    if prop.area_sqm:
        facts.append("%s м²" % ("%g" % prop.area_sqm))
    if prop.city:
        facts.append(prop.city)
    out = ", ".join(facts)
    if prop.price:
        out += ". Ціна: %s грн" % _fmt_number(prop.price)
    if prop.mobility_level:
        out += ". Рівень доступності: %d з 5" % prop.mobility_level
    return (out + ". Оголошення на mobi Home.").lstrip(". ")


def listing_jsonld(prop, url, photos):
    accommodation = {"@type": "Accommodation", "name": prop.name}
    address = {k: v for k, v in (("streetAddress", prop.address), ("addressLocality", prop.city)) if v}
    if address:
        accommodation["address"] = {"@type": "PostalAddress", **address}
    if prop.rooms:
        accommodation["numberOfRooms"] = prop.rooms
    if prop.area_sqm:
        accommodation["floorSize"] = {"@type": "QuantitativeValue", "value": prop.area_sqm, "unitCode": "MTK"}
    if prop.floor is not None:
        accommodation["floorLevel"] = str(prop.floor)

    listing = {
        "@context": "https://schema.org",
        "@type": "RealEstateListing",
        "name": prop.name,
        "url": url,
        "inLanguage": "uk",
        "datePosted": prop.created_at.date().isoformat() if prop.created_at else None,
        "dateModified": prop.updated_at.isoformat() if prop.updated_at else None,
        "description": listing_description(prop),
        "image": photos or None,
        "about": accommodation,
    }
    if prop.price:
        listing["offers"] = {
            "@type": "Offer",
            "price": str(prop.price),
            "priceCurrency": "UAH",
            "businessFunction": "http://purl.org/goodrelations/v1#"
            + ("LeaseOut" if prop.listing_type == ListingType.RENT else "Sell"),
        }
    return {k: v for k, v in listing.items() if v is not None}


def listing_seo(request, prop):
    """Метадані сторінки оголошення: лише реальні поля; noindex, якщо оголошення не публічне."""
    url = absolute(reverse("properties:property_detail", args=[prop.pk]))
    photos = _listing_photos(prop)
    public = Property.objects.public().filter(pk=prop.pk).exists()
    data = [
        breadcrumbs([_home_crumb(), ("Каталог", absolute(reverse("properties:home"))), (prop.name, url)]),
    ]
    if public:
        data.insert(0, listing_jsonld(prop, url, photos))
    return {
        "title": listing_title(prop),
        "description": listing_description(prop),
        "canonical": url,
        "robots": "index, follow" if public and indexing_allowed(request) else NOINDEX,
        "image": photos[0] if photos else absolute(static("img/hero.png")),
        "og_type": "article",
        "jsonld": mark_safe("".join(ld_json(d) for d in data)) if public else "",
    }


# --- robots.txt, sitemap.xml, middleware ------------------------------------------------

@require_GET
def robots_txt(request):
    if not indexing_allowed(request):
        lines = ["User-agent: *", "Disallow: /"]
    else:
        lines = ["User-agent: *"]
        lines += ["Disallow: %s" % p for p in PRIVATE_PREFIXES]
        lines += ["Disallow: /property/*/%s" % s for s in PRIVATE_PROPERTY_SUFFIXES]
        lines += ["Allow: /", "", "Sitemap: %s/sitemap.xml" % base_url()]
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain; charset=utf-8")


class _ProductionSitemap(Sitemap):
    """Абсолютні URL завжди з SEO_BASE_URL (HTTPS, production-домен), а не з хоста запиту."""

    def get_domain(self, site=None):
        return urlsplit(base_url()).netloc

    def get_protocol(self, protocol=None):
        return urlsplit(base_url()).scheme or "https"


class StaticViewSitemap(_ProductionSitemap):
    changefreq = "weekly"

    def items(self):
        return list(PUBLIC_PAGES)

    def location(self, item):
        return reverse("properties:%s" % item)

    def priority(self, item):
        return {"index": 1.0, "home": 0.9}.get(item, 0.5)


class PropertySitemap(_ProductionSitemap):
    changefreq = "daily"
    priority = 0.8
    limit = 5000

    def items(self):
        # Ті самі правила, що й публічний каталог: опубліковано, схвалено, перевірено, не в архіві.
        return Property.objects.public().only("pk", "updated_at").order_by("-updated_at", "pk")

    def location(self, obj):
        return reverse("properties:property_detail", args=[obj.pk])

    def lastmod(self, obj):
        return obj.updated_at


SITEMAPS = {"static": StaticViewSitemap, "properties": PropertySitemap}


class RobotsHeaderMiddleware:
    """X-Robots-Tag: noindex для всього не-production середовища та службових шляхів."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not response.has_header("X-Robots-Tag") and (
            not indexing_allowed(request) or is_private_path(request.path)
        ):
            response["X-Robots-Tag"] = NOINDEX
        return response
