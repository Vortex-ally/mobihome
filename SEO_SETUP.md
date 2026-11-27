# SEO та індексація — mobi Home

Production: https://mobihome.org

## Що реалізовано

| Що | Де |
|---|---|
| Логіка SEO (метадані, robots.txt, sitemap, JSON-LD, X-Robots-Tag) | `properties/seo.py` |
| Шаблон meta/OG/Twitter/canonical/JSON-LD | `templates/includes/seo_meta.html` (підключено в `base.html`) |
| Метадані оголошення | `properties/templatetags/seo_tags.py`, `property_detail.html` |
| Маршрути `/robots.txt`, `/sitemap.xml` | `config/urls.py` |
| Налаштування | `config/settings.py` (`SEO_BASE_URL`, `SEO_ALLOW_INDEXING`, `SEO_INDEXABLE_HOSTS`) |
| Тести | `properties/tests/test_seo.py` |

**Аудит до змін:** були лише `<title>` та одна спільна `meta description`; не було canonical,
OG/Twitter, JSON-LD, robots.txt, sitemap.xml, meta robots чи X-Robots-Tag (ні в Django, ні в nginx).
`/faq/` — редірект на головну, тому в sitemap не включений.

- **Meta tags:** унікальні title/description для публічних сторінок (головна, каталог, рівні доступності,
  послуги, про нас, контакти, політика конфіденційності, правила). Для оголошень — з реальних полів
  (назва, тип, кімнати, площа, місто, ціна, рівень доступності, опис). `lang="uk"`, `og:locale=uk_UA`.
- **Canonical:** завжди `https://mobihome.org<шлях>`; у каталозі фільтри відкидаються, `?page=N` (N>1) зберігається.
- **Sitemap:** Django Sitemap Framework; статичні сторінки + `Property.objects.public()` (опубліковано,
  схвалено, фото перевірено, не в архіві, оплачене розміщення — якщо `PLACEMENT_PAYMENT_REQUIRED`).
  URL завжди HTTPS + production-домен.
- **robots.txt:** на production — дозвіл публічних сторінок, заборона `/admin/`, `/admin-action/`,
  `/auth/`, `/login/`, `/register/`, `/logout/`, `/profile/`, `/add-listing/`, `/payment/`, `/payments/`,
  `/staff/`, `/compare/`, `/audit/`, `/consultation/`, `/healthz/` та службових дій оголошень
  (`edit`, `delete`, `promote`, …), посилання на sitemap. Доступ до цих сторінок і далі захищає авторизація.
- **Не-production:** якщо `SEO_ALLOW_INDEXING=False`, `DEBUG=True` або хост не з `SEO_INDEXABLE_HOSTS`
  (localhost, trycloudflare sandbox, demo) — `robots.txt: Disallow: /`, `X-Robots-Tag: noindex, nofollow`
  на всіх відповідях і `<meta name="robots" content="noindex, nofollow">`.
- **noindex на production** лише для службових сторінок; публічні сторінки та опубліковані оголошення —
  `index, follow`. Неопубліковане оголошення, яке бачить власник/адмін, — `noindex`, без JSON-LD.
- **JSON-LD:** `Organization` + `WebSite` (головна), `BreadcrumbList` (інформаційні сторінки, оголошення),
  `RealEstateListing` з `Accommodation` та `Offer` (лише якщо ціна вказана, валюта UAH).
  Рейтингів, відгуків і вигаданих даних немає. Серіалізація через `json.dumps` з екрануванням `< > &`.

## Змінні оточення (production `.env`)

```
SEO_BASE_URL=https://mobihome.org
SEO_ALLOW_INDEXING=True          # за замовчуванням = not DEBUG
SEO_INDEXABLE_HOSTS=mobihome.org,www.mobihome.org
```
Для sandbox/demo додатково радимо `SEO_ALLOW_INDEXING=False`.
Додано `django.contrib.sitemaps` (входить у Django, нових залежностей немає). Міграції не потрібні.

## Google Search Console — підтвердження домену через Cloudflare DNS

1. https://search.google.com/search-console → «Додати ресурс» → тип **«Домен»** → `mobihome.org`.
2. Google покаже TXT-запис виду `google-site-verification=…` — скопіюйте **саме його** (токен видає лише Google).
3. Cloudflare → домен `mobihome.org` → **DNS → Records → Add record**:
   - Type: `TXT`, Name: `@`, Content: скопійоване значення, TTL: Auto. Зберегти.
4. У Search Console натисніть **«Підтвердити»** (поширення DNS — від кількох хвилин до години).
   Не видаляйте TXT-запис після підтвердження.

### Додати sitemap
Search Console → ресурс `mobihome.org` → **Індексування → Файли Sitemap** → ввести `https://mobihome.org/sitemap.xml`
→ «Надіслати». Статус має стати «Успішно». Далі — «Перевірка URL» для головної та 1–2 оголошень →
«Надіслати запит на індексування».

## Перевірка після деплою (вручну)

```
curl -sI https://mobihome.org/robots.txt      # 200, text/plain, без X-Robots-Tag
curl -s  https://mobihome.org/robots.txt      # Allow: / + Sitemap: https://mobihome.org/sitemap.xml
curl -sI https://mobihome.org/sitemap.xml     # 200, application/xml
curl -sI https://mobihome.org/ | grep -i x-robots   # має бути порожньо
curl -sI https://<sandbox-host>/robots.txt    # X-Robots-Tag: noindex, nofollow; тіло Disallow: /
```
- JSON-LD: https://search.google.com/test/rich-results та https://validator.schema.org/ для сторінки оголошення.
- Cloudflare: переконайтеся, що немає Transform Rules / Workers, які додають `X-Robots-Tag`, і що
  «Bot Fight Mode» не блокує Googlebot. Опційно: «Crawler Hints».
- `deploy/nginx-mobihome.conf` ще містить заглушку `mobihome.example` — на сервері має бути `mobihome.org`;
  `/robots.txt` і `/sitemap.xml` проксуються в Django через `location /`.

## Результати перевірок (2026-11-28, локально)

- `manage.py check` — без помилок.
- `manage.py test` — **466 тестів OK** (8 skipped, як і до змін; 452 → 466, +14 SEO-тестів).
- SEO-тести покривають: статуси та вміст robots.txt/sitemap.xml; відсутність у sitemap неопублікованих,
  прихованих, відхилених, неперевірених, архівних і прострочених оголошень; унікальні title/description;
  canonical; OG/Twitter; JSON-LD (типи, ціна, відсутність рейтингів, екранування `</script>`);
  відсутність noindex на production; noindex/Disallow на sandbox-хості та при `SEO_ALLOW_INDEXING=False`.
