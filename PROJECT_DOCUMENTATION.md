# MobiHome («Доступний Дім») — документація проєкту

> Стан на 19.11.2026. Документ описує **актуальний код робочої копії** гілки `stage/liqpay`,
> включно з незакоміченими змінами LiqPay (див. [розділ 12](#12-поточний-стан-і-майбутні-завдання)).
> Усе, що не вдалося перевірити, позначено як **невідоме** або **не перевірено**.

## Зміст

1. [Що таке MobiHome](#1-що-таке-mobihome)
2. [Технології](#2-технології)
3. [Архітектура](#3-архітектура)
4. [Функціональність](#4-функціональність)
5. [Оплата розміщення через LiqPay](#5-оплата-розміщення-через-liqpay)
6. [Безпека](#6-безпека)
7. [Встановлення та локальний запуск (Arch Linux)](#7-встановлення-та-локальний-запуск-arch-linux)
8. [Змінні середовища](#8-змінні-середовища)
9. [Management-команди](#9-management-команди)
10. [Тестування](#10-тестування)
11. [Структура проєкту](#11-структура-проєкту)
12. [Поточний стан і майбутні завдання](#12-поточний-стан-і-майбутні-завдання)
13. [Історія розробки](#13-історія-розробки)

---

## 1. Що таке MobiHome

**MobiHome** (у коді й шаблонах також «Доступний Дім», «mobi home») — вебплатформа оголошень про продаж і оренду житла з акцентом на **безбар'єрність**: для людей на кріслах колісних, сімей з дитячими візками, людей з обмеженою рухливістю.

Основні можливості:

- каталог квартир із фільтрами за ціною, кімнатами, площею, містом і **параметрами доступності** (ширина ліфта, душ-трап, пороги, простір для розвороту, рівень доступності 1–5);
- **аудит доступності** об'єкта адміністратором з автоматичним розрахунком балу та рівня;
- «паспорт доступності» об'єкта для друку;
- оголошення від власників з **обов'язковою модерацією** та фотоперевіркою;
- **платне розміщення** оголошення через LiqPay (тариф за замовчуванням 99 UAH / 30 днів);
- обране, порівняння об'єктів, відгуки про користувачів, заявки на аудит і консультації;
- вхід за паролем, через Google OAuth або Telegram Login Widget.

---

## 2. Технології

| Компонент | Що використовується | Звідки відомо |
|---|---|---|
| Мова | Python 3.14 (перевірено у `venv/`) | `venv/bin/python --version` |
| Фреймворк | Django 5.2.19 (`Django>=5.0,<6`) | [requirements.txt](requirements.txt), `pip list` |
| Зображення | Pillow (у venv — 13.0.0) | [requirements.txt](requirements.txt) |
| WSGI-сервер | gunicorn 25.1.0 | [requirements.txt](requirements.txt) |
| База даних | SQLite (`data/db.sqlite3`) | [config/settings.py](config/settings.py) |
| Кеш | стандартний Django `LocMemCache` (налаштування `CACHES` відсутнє в `settings.py`) | [config/settings.py](config/settings.py) |
| Frontend | Django templates, Bootstrap 5.3.2 і шрифт Outfit (CDN), Leaflet 1.9.4 (карта на сторінці об'єкта), vanilla JS | [templates/base.html](templates/base.html), [templates/properties/property_detail.html](templates/properties/property_detail.html) |
| PWA | service worker і manifest | [static/sw.js](static/sw.js), [static/manifest.json](static/manifest.json) |
| Платежі | LiqPay Hosted Checkout + callback + Status API (без SDK, власна реалізація) | [properties/liqpay.py](properties/liqpay.py) |
| Інструменти якості | flake8, black, isort (конфіги є; у `requirements.txt` не входять) | [.flake8](.flake8), [pyproject.toml](pyproject.toml), [.isort.cfg](.isort.cfg) |

Docker, docker-compose, конфігурації nginx і systemd у репозиторії **відсутні**.

---

## 3. Архітектура

Монолітний Django-проєкт з одним застосунком `properties`.

```
Браузер ──HTTP──► config/urls.py ──► properties/urls.py
                                         │
             ┌───────────────────────────┼───────────────────────────┐
             ▼                           ▼                           ▼
     properties/views.py        properties/payment_views.py   properties/social_auth.py
     (каталог, оголошення,      (старт оплати, callback,      (Google OAuth, Telegram)
      профіль, модерація)        результат, скасування)
             │                           │
             ▼                           ▼
     properties/forms.py        properties/liqpay.py  ──HTTPS──► LiqPay (checkout, api/request)
             │                           │
             └──────────► properties/placement.py ◄┘   (бізнес-правила оплати й строків)
                                         │
                                         ▼
                              properties/models.py  ──► SQLite
                                         ▲
                     properties/signals.py (активація оплаченого періоду при публікації)
```

| Шар | Файли | Призначення |
|---|---|---|
| Налаштування | [config/settings.py](config/settings.py), [config/test_settings.py](config/test_settings.py) | Усі параметри з env/`.env`; fail-fast перевірка конфігурації оплати в production |
| Маршрути | [config/urls.py](config/urls.py), [properties/urls.py](properties/urls.py) | `admin/` + усі маршрути сайту |
| Представлення | [properties/views.py](properties/views.py), [properties/payment_views.py](properties/payment_views.py), [properties/social_auth.py](properties/social_auth.py) | Обробка запитів |
| Бізнес-логіка | [properties/placement.py](properties/placement.py), [properties/liqpay.py](properties/liqpay.py), [properties/payment_config.py](properties/payment_config.py), [properties/mobility.py](properties/mobility.py) | Строки розміщення, оплати, перевірки конфігурації, рівні доступності |
| Безпека | [properties/security.py](properties/security.py), [properties/checks.py](properties/checks.py) | Обмеження спроб входу, system checks |
| Дані | [properties/models.py](properties/models.py), [properties/migrations/](properties/migrations/) | Моделі й міграції 0001–0027 |
| Адмінка | [properties/admin.py](properties/admin.py), [templates/admin/properties/payment/confirm_review.html](templates/admin/properties/payment/confirm_review.html) | Django admin з кастомними діями |
| Сигнали | [properties/signals.py](properties/signals.py), [properties/apps.py](properties/apps.py) | `post_save` для `Property`, реєстрація checks |

### Моделі

| Модель | Призначення |
|---|---|
| `Feature` | Зручності («Ліфт», «Пандус»…) з категоріями `apartment` / `building` / `rules` |
| `Property` | Оголошення: тип (продаж/оренда), адреса, ціна, кімнати, площа, поверх, фото, рівень доступності, статус модерації, `placement_until`, `archived_at`, власник |
| `PropertyPhoto` | Додаткові фото галереї |
| `PropertyFavorite` | Обране (унікальна пара користувач–оголошення) |
| `AccessibilityAudit` | Аудит доступності (вхід, ліфт, санвузол, пороги, розворот) з `total_score` |
| `ProfileReview` | Відгук і оцінка 1–5 про користувача (один відгук від автора) |
| `AuditRequest` | Заявка на професійний аудит |
| `ConsultationRequest` | Заявка на консультацію (зі сторінки послуг) |
| `UserProfile` | Провайдер входу, `telegram_id`, `google_sub`, аватар, телефон |
| `AuthSession` | Застаріла модель QR/OTP-сесій; самі QR/OTP-маршрути видалені, модель лишилась (в адмінці лише перегляд) |
| `Tariff` | Тариф розміщення (`code="placement"`) |
| `Payment` | Платіж LiqPay зі знімком умов тарифу, статусом, періодом, `needs_review` |
| `PaymentEvent` | Журнал повідомлень LiqPay і внутрішніх ALERT/REVIEW-подій |
| `PlacementTransition` | Журнал одноразового перехідного періоду |

Обмеження БД (з [properties/models.py](properties/models.py)): невід'ємна ціна, кімнати ≥ 1, площа > 0, рівень 1–5, поверх ≤ поверховості, сума і термін платежу > 0, коректний період, унікальна пара в обраному, **не більше однієї незавершеної оплати на оголошення** (`one_open_payment_per_property`).

---

## 4. Функціональність

### 4.1 Реєстрація та авторизація

- **Єдина сторінка входу/реєстрації** `/auth/` (вкладки). Функція `auth_page` у [properties/views.py](properties/views.py). Старі `/login/` і `/register/` перенаправляють сюди.
- **Реєстрація:** `RegisterForm` ([properties/forms.py](properties/forms.py)): логін, обов'язковий email, пароль зі стандартними валідаторами Django. Email **не підтверджується**.
- **Вхід за паролем:** `AuthenticationForm`, «запам'ятати мене» (сесія на 14 днів, інакше до закриття браузера), обмеження спроб (див. [6.2](#62-авторизація)).
- **Google OAuth 2.0:** `google_login` / `google_callback` у [properties/social_auth.py](properties/social_auth.py). Вмикається, якщо задано `GOOGLE_CLIENT_ID` і `GOOGLE_CLIENT_SECRET`.
- **Telegram Login Widget:** `telegram_callback`. Вмикається, якщо задано `TELEGRAM_BOT_TOKEN` (і `TELEGRAM_BOT_NAME` для віджета).
- **Дія:** вимкнена (`DIIA_ENABLED = False`), маршрути показують повідомлення про недоступність.
- **Відновлення пароля:** стандартний Django password reset через email (`/auth/password-reset/`). Шаблони — у [templates/registration/](templates/registration/). У `DEBUG` листи виводяться в консоль.
- **Вихід:** лише POST із CSRF (`logout_view`). GET показує сторінку підтвердження.

### 4.2 Профіль користувача

- `/profile/` (`profile` у [properties/views.py](properties/views.py)): редагування імені, прізвища, email (`UserProfileForm`), власні оголошення (без архівних), обране, **історія платежів** (останні 50), отримані відгуки та середня оцінка.
- Для staff на цій же сторінці є черга модерації та список заявок на аудит.
- Публічний профіль `/profile/<username>/` (`public_profile`): публічні оголошення користувача, відгуки, форма відгуку (не можна оцінювати себе; повторний відгук оновлює попередній).

### 4.3 Створення та видалення оголошень

- `/add-listing/` (`add_listing`, `AddListingForm`): назва, тип, адреса, місто, ціна, кімнати, площа, поверх / поверховість, опис, головне фото (обов'язкове), рівень доступності, телефон, зручності, додаткові фото.
- Нове оголошення **завжди** створюється неопублікованим зі статусом модерації `pending` (`AddListingForm.save`). Після збереження користувач потрапляє на сторінку публікації та оплати `/payment/<pk>/`.
- **Окремої форми редагування оголошення власником у коді немає.** Змінювати оголошення можна через Django admin (staff). Аудит редагує лише staff (`auditor_form`).
- **Видалення** `/property/<pk>/delete/` (`delete_property`): лише власник або staff, через сторінку підтвердження (POST). Оголошення з платежами **не видаляється фізично**, а архівується (`archived_at`), щоб зберегти фінансову історію.

### 4.4 Завантаження фотографій і валідація

Реалізовано в `AddListingForm` ([properties/forms.py](properties/forms.py)):

- головне фото обов'язкове, до 10 МБ;
- до 20 додаткових фото (`extra_photos`), кожне до 10 МБ, кожне перевіряється як справжнє зображення через `forms.ImageField` (Pillow);
- загальний ліміт тіла запиту в пам'яті — 10 МБ (`DATA_UPLOAD_MAX_MEMORY_SIZE`, `FILE_UPLOAD_MAX_MEMORY_SIZE`);
- файли зберігаються в `media/properties/` і `media/properties/gallery/%Y/%m/`;
- якщо фото немає, показується нейтральна заглушка [static/img/no-photo.svg](static/img/no-photo.svg) (жодних стокових фото квартир).

### 4.5 Каталог, пошук і фільтри

`/catalog/` (`home` у [properties/views.py](properties/views.py), шаблон [templates/properties/home.html](templates/properties/home.html)). Показує лише **публічні** оголошення (`Property.objects.public()`): опубліковані, схвалені модерацією, з перевіреними фото, не в архіві. Якщо `PLACEMENT_PAYMENT_REQUIRED=True`, додатково показуються лише ті, в яких `placement_until` ще не минув.

Фільтри (GET-параметри):

| Параметр | Фільтр |
|---|---|
| `q` | текст у назві, адресі, місті, описі |
| `type` | `sale` / `rent` |
| `city` | точне місто |
| `price_min`, `price_max` | ціна |
| `rooms`, `rooms_max` | кімнати |
| `area_min`, `area_max` | площа |
| `lift_80`, `shower_drain`, `no_thresholds` | ліфт > 80 см, душ-трап, пороги ≤ 2 см (за аудитом) |
| `turning_radius`, `bathroom_type`, `has_audit`, `audit_score_min` | інші параметри аудиту |
| `verified` | лише «перевірене житло» (`is_verified`) |
| `accessibility_level` / `level` / `mobility` | рівень доступності 1–5 |
| `feature` (кілька) | зручності |
| `sort` | `newest` (за замовчуванням: спершу чинний старий ТОП), `price_asc`, `price_desc`, `score` |

Пагінація — 9 оголошень на сторінку. Є карта міст (координати лише для 10 міст зі словника `CITY_COORDINATES`), швидкий перегляд `/property/<pk>/quick-view/` (JSON) і порівняння 2–4 об'єктів `/compare/?ids=1,2,3`.

Головна сторінка `/` (`index`, [templates/properties/index.html](templates/properties/index.html)) — лендинг зі статистикою і до 6 обраних оголошень.

### 4.6 Сторінка оголошення

`/property/<pk>/` (`property_detail`):

- неопубліковані, неперевірені чи архівні оголошення бачать лише власник і staff (архівні — лише staff), іншим повертається 404;
- галерея всіх реальних фото, характеристики, ціна за м², орієнтовна ціна в USD (фіксований курс 41.5 у `Property.price_usd`);
- таблиця аудиту доступності з кольоровими статусами;
- карта лише на рівні міста (Leaflet) та посилання на пошук адреси, без вигаданих «об'єктів поруч»;
- схожі оголошення (те саме місто і тип);
- лічильник переглядів (один раз за сесію).

Додатково: «Паспорт доступності» `/property/<pk>/download-passport/` — HTML-сторінка для друку ([templates/properties/passport_pdf.html](templates/properties/passport_pdf.html)). Справжньої генерації PDF у коді немає.

### 4.7 Аудит доступності та рівні

- Форма аудитора `/audit/new/`, `/audit/<pk>/` (`auditor_form`, `AccessibilityAuditForm`), лише для staff.
- `AccessibilityAudit.save()` перераховує `total_score` (середнє балів входу, ліфта, порогів і 10/0 за розворот) і автоматично визначає `Property.mobility_level` (`compute_mobility_level`).
- Опис п'яти рівнів — [properties/mobility.py](properties/mobility.py), сторінка `/rivni-dostupnosti/`.
- Заявка на аудит `/audit/request/` (`request_audit`, модель `AuditRequest`). Staff обробляє її в профілі (`admin_update_audit_request`): статус, бал, рівень. Статус `completed` позначає оголошення як «перевірене».
- Заявка на консультацію: `POST /consultation/submit/` (JSON або форма) зберігається в `ConsultationRequest`.

### 4.8 Обране

`POST /property/<pk>/favorite/` (`toggle_favorite`) додає або прибирає оголошення з обраного і повертає JSON `{"is_favorite": …}`. Лише POST; можна додати тільки видиме користувачу оголошення. Анонімний користувач отримує 401 (AJAX) або редірект на вхід.

### 4.9 Модерація оголошень

Шлях оголошення:

1. Створено → `is_published=False`, `moderation_status=pending`.
2. Власник на `/payment/<pk>/` натискає «Надіслати на модерацію» (`action=free`) → `is_published=True`, але `pending` і `is_verified_by_admin=False`, тож у каталозі ще не видно.
3. Staff схвалює (`/admin-action/property/<pk>/approve/` з профілю або дія «Схвалити та опублікувати» в адмінці) → `approved`, `is_verified_by_admin=True`, оголошення з'являється в каталозі.
4. Відхилення з коментарем (`admin_reject_property`) → `rejected`.

Окремо є фотоперевірка (`admin_verify_photos`, дії «Підтвердити / скасувати фотомодерацію»). Оплата **ніколи** сама не публікує й не схвалює оголошення.

### 4.10 Адміністративна панель

`/admin/` ([properties/admin.py](properties/admin.py)):

- **Оголошення:** фільтри за станом розміщення та архівом, інлайни фото, аудиту й платежів. Масові дії: схвалити, відхилити, підтвердити або скасувати фотомодерацію, «перевірене житло», активувати, деактивувати, архівувати, повернути з архіву. Дії схвалення й активації запускають оплачені періоди (`activate_on_publication`).
- **Тарифи:** редагування ціни і терміну. Код незмінний; тариф із платежами не видаляється. Зміна тарифу не впливає на вже створені платежі (вони зберігають знімок умов).
- **Платежі:** **лише перегляд** (без додавання, зміни, видалення), пошук за `order_id`, ID транзакції, користувачем, оголошенням; фільтр `needs_review`. Дія **«Позначити перевіреним»** доступна лише з дозволом `properties.review_payment` і працює через сторінку підтвердження. Вона знімає лише `needs_review`, пише подію `[REVIEW closed]` і `LogEntry`. Якщо після відкриття сторінки підтвердження ALERT-події змінились, перевірку не буде закрито.
- **Події платежів:** лише перегляд.
- **Попередження** про платежі з `needs_review` показується на списках оголошень і платежів.
- **Користувачі:** кількість оголошень і успішних платежів, деактивація та активація (staff не може деактивувати superuser чи себе).
- **Перехідний період:** журнал лише для читання.
- Вхід в адмінку обмежений за кількістю спроб.

### 4.11 Платежі та історія платежів

Детально — у [розділі 5](#5-оплата-розміщення-через-liqpay). Історія доступна власнику в профілі та на сторінці оплати оголошення (останні 20), адміністратору — в адмінці.

### 4.12 Інші функції

| Функція | Стан |
|---|---|
| Сторінки «Про нас», «Послуги», «Контакти», політика конфіденційності, правила | Статичні шаблони |
| `/faq/` | Редірект на головну |
| Форма «Контакти» | **Лише показує повідомлення «Дякуємо»: дані не зберігаються і не надсилаються** (`contact` у views) |
| `/property/<pk>/track-click/` | Повертає JSON, **нічого не зберігає** |
| Стара система «ТОП» (`promote_listing`, `payment_paid`) | Продаж припинено; маршрути лише перенаправляють. Чинний ТОП (`is_featured` / `featured_until`) показується до завершення |
| PWA (service worker) | Кешує `/` і `manifest.json` (див. [розділ 12](#12-поточний-стан-і-майбутні-завдання)) |

---

## 5. Оплата розміщення через LiqPay

### 5.1 Бізнес-логіка розміщення

Правила — в [properties/placement.py](properties/placement.py) (`_apply_period_rules`, `_activate`, `activate_on_publication`).

- **Тариф:** міграція [0022_seed_placement_tariff.py](properties/migrations/0022_seed_placement_tariff.py) створює тариф `placement` «Розміщення квартири» — **99.00 UAH за 30 днів**. Адміністратор може змінити ціну чи термін в адмінці, тож актуальне значення в конкретній БД треба дивитись у «Тарифах». Платіж зберігає знімок суми, валюти й терміну на момент створення.
- **Оплата не публікує оголошення.** Після успішної оплати платіж отримує `applied_at` («зараховано»), але оголошення лишається в тому стані модерації, в якому було.
- **Нове оголошення (ще не опубліковане):** 30 днів **не починаються** після оплати. Період стартує в момент фактичної публікації після схвалення: сигнал `post_save` у [properties/signals.py](properties/signals.py) і дії адмінки викликають `activate_on_publication`.
- **Прострочене оголошення** (`placement_until` минув): після оплати повертається на модерацію (`approved` → `pending`), 30 днів почнуться з повторної публікації.
- **Активне опубліковане оголошення:** +30 днів додаються **до поточної дати завершення** без повторної модерації.
- **Схвалене, але ще не оплачене:** період починається з моменту оплати.
- **Кілька оплат до публікації** активуються послідовно (ланцюжком) при публікації.
- **Повторна активація не нараховує період вдруге:** `activate_on_publication` бере лише платежі з `activated_at IS NULL` під `select_for_update`.
- **Повторні повідомлення про той самий платіж** нічого не додають (див. [5.6](#56-ідемпотентність-і-захист-від-подвійного-зарахування)).

**Два незалежні перемикачі** (обидва за замовчуванням `False`):

| Змінна | Що робить |
|---|---|
| `PAYMENTS_ENABLED` | Користувачі **можуть** платити (checkout працює лише з коректною конфігурацією LiqPay — `payments_active()`) |
| `PLACEMENT_PAYMENT_REQUIRED` | Каталог **показує** лише оплачені оголошення. Без `PAYMENTS_ENABLED` — помилка конфігурації |

Для запуску обов'язкової оплати є одноразовий перехідний період: команда `apply_placement_transition` (див. [розділ 9](#9-management-команди)).

### 5.2 Hosted Checkout: `data` і `signature`

Сайт **не збирає й не зберігає** дані картки: користувач вводить їх лише на сторінці LiqPay.

1. Власник натискає «Оплатити» → `POST /payment/<pk>/start/` (`start_payment` у [properties/payment_views.py](properties/payment_views.py)).
2. Створюється або повторно використовується `Payment` з `order_id = mh-<id оголошення>-<20 hex>` (`create_payment`).
3. `build_checkout()` у [properties/liqpay.py](properties/liqpay.py) формує параметри **лише з серверного запису**: `version`, `public_key`, `action=pay`, `amount`, `currency`, `description`, `order_id`, `language=uk`, `result_url`, `server_url`, `expired_date` (час створення платежу + `LIQPAY_PAYMENT_TTL_MINUTES`).
4. `data = base64(json)`, `signature = base64(HASH(private_key + data + private_key))`.
   - Поточна специфікація: `LIQPAY_API_VERSION=7` + `sha3_256`.
   - Застаріла пара `3` + `sha1` підтримується лише як явне налаштування.
   - Інші комбінації заборонені: `ALLOWED_VERSION_ALGORITHMS` у [properties/payment_config.py](properties/payment_config.py).
5. Сторінка [templates/properties/liqpay_redirect.html](templates/properties/liqpay_redirect.html) автоматично відправляє форму на `https://www.liqpay.ua/api/3/checkout`.

URL-адреси будуються з `SITE_URL`:

- `server_url` = `SITE_URL` + `/payments/liqpay/callback/`
- `result_url` = `SITE_URL` + `/payments/<order_id>/result/`

### 5.3 Перевірка callback

`POST /payments/liqpay/callback/` (`liqpay_callback`, CSRF-exempt, бо це запит server-to-server):

1. Тіло понад 32 КБ — відповідь 413.
2. `verify_signature()`: підпис порівнюється в постійному часі (`hmac.compare_digest`), не-ASCII підпис відхиляється.
3. Недійсний підпис — 400. Вміст такого запиту в журнал не пишеться, лише мінімальний запис із розміром. Діє ліміт (див. [6.6](#66-rate-limiting)).
4. Для дійсного підпису `process_payment_fields()` додатково перевіряє: відомий `order_id`, збіг `public_key`, `action=pay`. Для успіху також звіряються сума і валюта з нашим записом.
5. Статус `sandbox` приймається лише в тестовому режимі й лише для тестового платежу. Тестовий платіж не зараховується в бойовому режимі.
6. Кожне повідомлення записується в `PaymentEvent` **без персональних і карткових даних** (лише поля зі списку `SAFE_EVENT_FIELDS`).

**Повернення користувача на `result_url` нічого не підтверджує**: параметри адреси й тіла ігноруються, стан береться з БД та Status API.

### 5.4 Status API

`fetch_status()` у [properties/liqpay.py](properties/liqpay.py) надсилає підписаний `action=status` на `https://www.liqpay.ua/api/request`:

- мережеві помилки, некоректний JSON, недійсний підпис відповіді (якщо він є) чи чужий `order_id` нічого не змінюють;
- `payment_not_found` визнається **лише за точним рядком** `err_code == "payment_not_found"`. Інші коди, відсутній код чи код з додатковими символами повертають `api_error`, і платіж не закривається;
- якщо відповідь про помилку містить `order_id` чи `public_key`, вони мають збігатися з нашими (не-ASCII чи значення не того типу вважаються розбіжністю).

Де викликається Status API:

- сторінка результату `/payments/<order_id>/result/` — для незавершеного платежу, не частіше одного разу на 10 с;
- `start_payment` — перед закриттям простроченої оплати;
- команда `liqpay_sync_pending`.

### 5.5 Стани платежу

| Статус | Значення |
|---|---|
| `created` | Створено, форма LiqPay ще може бути оплачена |
| `pending` | LiqPay обробляє (3-D Secure, `wait_*`, `*_verify` тощо) |
| `success` | Підтверджено й зараховано |
| `failure` | Відхилено (`failure`, `error`) |
| `cancelled` | Скасовано користувачем або при зміні режиму LiqPay |
| `expired` | Не завершено вчасно (лише після достовірної відповіді Status API) |
| `reversed` | Кошти повернено (період автоматично не скасовується — рішення адміністратора) |

Фінальні статуси: `success`, `failure`, `cancelled`, `expired`, `reversed`.

Особливі випадки:

- **`try_again`** (наприклад, відхилення AntiFraud): платіж **лишається відкритим** (`created`, із `pending` повертається в `created`), нічого не зараховується. Сторінка результату показує «Платіж не пройшов. Ви можете спробувати оплатити ще раз» і кнопку повтору з **тим самим `order_id`** у межах TTL. Пізніший `success` зараховується звичайним шляхом.
- **Невідповідність суми або валюти:** платіж не зараховується, лишається `pending` з `error_message` для ручної перевірки.
- **Пізній `success` після `cancelled` чи `expired`** зараховується (гроші реально списано).

### 5.6 Ідемпотентність і захист від подвійного зарахування

- `apply_successful_payment()` ([properties/placement.py](properties/placement.py)) блокує рядки **оголошення, а потім платежу** (`select_for_update`; порядок той самий, що в `activate_on_publication`) і зараховує платіж лише якщо `applied_at IS NULL` (умовний UPDATE).
- Повторний callback або відповідь Status API з тим самим `payment_id` нічого не змінюють.
- Один `provider_transaction_id` не може бути прив'язаний до двох платежів.
- Одночасно може існувати лише **одна незавершена оплата** на оголошення: частковий унікальний індекс `one_open_payment_per_property`, паралельні запити отримують наявну оплату. «Продовжити оплату» використовує той самий `order_id`; `expired_date` не зсувається.
- Після скасованої, відхиленої чи простроченої незарахованої оплати нова оплата блокується на `LIQPAY_PAYMENT_TTL_MINUTES + 30` хвилин (`new_payment_blocked_until`), бо стару форму ще можна оплатити в іншій вкладці.
- Оплачувати може **лише власник** оголошення (`can_pay_for`), а не адміністратор.

### 5.7 Повторні списання та `needs_review`

| Ситуація | Поведінка |
|---|---|
| Другий `success` з **іншим `payment_id`** для того самого `order_id` | Не зараховується. `needs_review=True`, подія `[ALERT duplicate_charge]` |
| `reversed` для **другого** `payment_id` | Зарахований платіж не змінюється. `needs_review=True`, подія `[ALERT duplicate_refund]` |
| Два успішні платежі **з різними `order_id`** одного оголошення, де старіший отримав успіх уже після створення новішого | Обидва зараховуються за звичайними правилами, але **обидва** отримують `needs_review=True` і по одній події `[ALERT duplicate_property_charge]`. Payload події містить ID записів Payment, обидва `order_id`, обидва ID транзакцій LiqPay і `property_id`. Коли стає відомим ID другої транзакції, подія оновлюється на місці |
| Звичайне продовження (A зараховано, потім створено й оплачено B) | Не позначається |

Межовий випадок: якщо `applied_at` старішого платежу **точно дорівнює** `created_at` новішого, ситуація позначається для перевірки (`>=`). Цей вибір пояснено в докстрінгу `_cross_order_duplicates`.

Автоматичних повернень коштів немає. Рішення ухвалює адміністратор, після чого закриває перевірку дією «Позначити перевіреним».

### 5.8 Відновлення, якщо callback не надійшов

- **Сторінка результату:** при поверненні з LiqPay запитує Status API (не частіше ніж раз на 10 с) і застосовує результат.
- **`start_payment`:** перед тим як закрити прострочену оплату (після TTL + 30 хв) запитує її статус. Якщо оплату підтверджено, вона зараховується, а нова не створюється. Якщо LiqPay недоступний, повертає помилку або «ще обробляється», стара оплата лишається відкритою, нова не створюється.
- **Команда `liqpay_sync_pending`** — періодична синхронізація (див. [розділ 9](#9-management-команди)).
- Користувач може сам скасувати незавершену оплату: `POST /payments/<order_id>/cancel/` (лише власник, лише незавершену).

### 5.9 Sandbox і Production

| | Sandbox | Production |
|---|---|---|
| `LIQPAY_SANDBOX` | `True` (за замовчуванням) | `False` |
| Ключі | Обов'язково з префіксом `sandbox_` | Без префікса `sandbox_` |
| `Payment.is_test` | `True` | `False` |
| Статус `sandbox` | Приймається для тестових платежів | Відхиляється |

Fail-fast: при `DJANGO_DEBUG=False` і `PAYMENTS_ENABLED=True` неправильна конфігурація зупиняє запуск з `ImproperlyConfigured`:

- немає ключів;
- недозволена пара версії й алгоритму;
- тестовий ключ у бойовому режимі або навпаки;
- немає `SITE_URL` або він не `https://`;
- `PLACEMENT_PAYMENT_REQUIRED` без `PAYMENTS_ENABLED`.

У `DEBUG` ті самі проблеми показує `python manage.py check` (`properties.E001`–`E003`, `W001`). Значення ключів у повідомленнях не виводяться.

Якщо змінився режим (тест ↔ бойовий), стару незавершену оплату автоматично скасовують при наступному старті оплати.

### 5.10 Як налаштувати Sandbox для тестування

LiqPay має надсилати callback на публічну HTTPS-адресу, тому локальний сервер потрібно відкрити через тунель. Нижче — підхід, який використовувався під час розробки: окремий файл `.env.sandbox` (Git його ігнорує за шаблоном `.env.*`) і окрема база.

1. Встановіть тунель (у системі вже встановлено `cloudflared`; інакше):

   ```bash
   sudo pacman -S cloudflared
   ```

2. Запустіть тунель у **першому терміналі** й скопіюйте адресу `https://….trycloudflare.com` (вона змінюється при кожному запуску):

   ```bash
   cloudflared tunnel --url http://127.0.0.1:8000
   ```

3. Створіть `.env.sandbox` у корені проєкту з правами лише для себе (`chmod 600 .env.sandbox`). Ключі вписуйте в редакторі, **не надсилайте їх у чати**:

   ```dotenv
   DJANGO_DEBUG=False
   DJANGO_SECRET_KEY=<згенерований-ключ>
   DJANGO_BEHIND_PROXY=True
   DJANGO_SQLITE_PATH=data/sandbox.sqlite3
   SITE_URL=https://<хост-тунелю>.trycloudflare.com
   DJANGO_ALLOWED_HOSTS=<хост-тунелю>.trycloudflare.com
   DJANGO_CSRF_TRUSTED_ORIGINS=https://<хост-тунелю>.trycloudflare.com
   LIQPAY_PUBLIC_KEY=<sandbox_...>
   LIQPAY_PRIVATE_KEY=<sandbox_...>
   LIQPAY_SANDBOX=True
   LIQPAY_API_VERSION=7
   LIQPAY_SIGNATURE_ALGORITHM=sha3_256
   PAYMENTS_ENABLED=True
   PLACEMENT_PAYMENT_REQUIRED=False
   ```

   Секретний ключ можна згенерувати командою:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(50))"
   ```

4. У **другому терміналі** підключіть ці змінні лише для поточної оболонки й запустіть сервер. Змінні оболонки мають пріоритет над `.env`, бо завантажувач `.env` використовує `os.environ.setdefault`.

   ```bash
   cd ~/Desktop/mobihome
   source venv/bin/activate
   set -a; . ./.env.sandbox; set +a           # змінні лише для цієї оболонки
   python manage.py migrate                     # ⚠ змінює лише data/sandbox.sqlite3
   python manage.py check
   python manage.py createsuperuser             # адмін для Sandbox-бази
   python manage.py runserver 127.0.0.1:8000 --insecure
   ```

   `--insecure` потрібен, щоб `runserver` віддавав статичні файли при `DEBUG=False`. **Завантажені фото (media) при `DEBUG=False` не віддаються**, бо маршрут media підключається лише в `DEBUG`.

5. Відкривайте сайт **лише за https-адресою тунелю**. При `DEBUG=False` прямий `http://127.0.0.1:8000` перенаправляє на HTTPS.

**Тестові картки:** беріть з офіційної сторінки LiqPay <https://www.liqpay.ua/uk/doc/api/testing>. Які саме картки використовувались у Sandbox-тестах, **у репозиторії не зафіксовано (невідомо)**.

**Результати реальних Sandbox-тестів:** у репозиторії не зберігаються. Відомо лише зі слів розробника, що Sandbox повернув `try_again` після відхилення картки AntiFraud. Після цього додано окрему обробку `try_again` ([5.5](#55-стани-платежу)). Повний сценарій Sandbox (успіх, повтор, дві вкладки, відмова, повернення коштів) **не задокументовано як виконаний**.

---

## 6. Безпека

### 6.1 Секрети та конфігурація

- Усі секрети читаються зі змінних оточення або з `.env` у корені ([config/settings.py](config/settings.py), `_load_env_file`). `.env`, `.env.*` (крім `.env.example`), `data/`, `*.sqlite3`, `media/`, `*.log` ігноруються Git ([.gitignore](.gitignore)).
- `DJANGO_SECRET_KEY` обов'язковий при `DEBUG=False`, інакше запуск зупиняється. У `DEBUG` використовується явно небезпечний dev-ключ.
- Приватний ключ LiqPay ніколи не логується і не потрапляє в HTML, відповіді чи події (перевіряється тестами). `LiqPayConfig.__repr__` його приховує.
- У репозиторії немає стандартних паролів адміністратора: `create_or_reset_superuser` вимагає явний `--password`, [create_admin.py](create_admin.py) запитує пароль інтерактивно.

### 6.2 Авторизація

- Обмеження спроб входу ([properties/security.py](properties/security.py)): за замовчуванням 5 невдалих спроб на логін (хеш) або 20 на IP за 15 хвилин. Діє для сайту (відповідь 429) і для `/admin/` (обгортка `_throttled_admin_login`). IP береться лише з `REMOTE_ADDR`, `X-Forwarded-For` не враховується.
- **Google:** `state` (CSRF) і `nonce` (replay), обмін `code` на сервері з `client_secret`, перевірка `iss`, `aud`, `exp`, `nonce`, `email_verified`. Автоприв'язка до акаунта з паролем заборонена (захист від pre-account-takeover).
- **Telegram:** обов'язкова перевірка HMAC-SHA256 підпису та «свіжості» `auth_date` (24 год).
- Вхід через соцмережі **заборонено для staff і superuser**: вони входять лише паролем.
- Безпечні редіректи після входу: `safe_next_url`, захист від open redirect.
- Небезпечні старі механізми видалено: OTP з кодом у відповіді, «демо»-режими Telegram і Google, фальшивий вхід через Дію.

### 6.3 Перевірка прав доступу

- Приховані оголошення, швидкий перегляд, паспорт, порівняння, обране й заявки на аудит доступні лише для видимих користувачу оголошень (`_visible_properties`).
- Видалення — власник або staff. Аудит, модерація, адмін-дії — лише staff.
- Оплата — лише власник (`can_pay_for`, перевірка на сервері, чужий запит отримує 404). Результат і скасування оплати — власник або staff (`_own_payment_or_404`).
- Платежі й події в адмінці — лише перегляд. Закриття перевірки подвійного списання — окремий дозвіл `properties.review_payment`.

### 6.4 CSRF і валідація форм

- `CsrfViewMiddleware` увімкнено. Вихід, обране, старт і скасування оплати працюють лише через POST.
- `csrf_exempt` мають лише:
  - LiqPay callback — автентичність перевіряється підписом;
  - сторінка повернення з LiqPay — тіло запиту повністю ігнорується.
- Форми валідуються Django Forms: ціна, кімнати, поверхи, фото, бали аудиту. Обмеження дублюються на рівні БД (`CheckConstraint`).
- Дані користувачів у швидкому перегляді каталогу екрануються (виправлено stored XSS — тест `test_catalog_escapes_listing_name`).

### 6.5 Захист платіжних callback і повторного зарахування

Див. [5.3](#53-перевірка-callback)–[5.7](#57-повторні-списання-та-needs_review): підпис у постійному часі, звірка `order_id`, `public_key`, суми й валюти, ідемпотентне зарахування під блокуванням рядків, індекс однієї відкритої оплати, ALERT і `needs_review` для подвійних списань.

### 6.6 Rate limiting

| Що | Ліміт | Де |
|---|---|---|
| Невдалі входи (сайт і адмінка) | 5 на логін / 20 на IP за 900 с (налаштовується) | [properties/security.py](properties/security.py) |
| Створення платежів | 10 на користувача за годину | `MAX_PAYMENTS_PER_HOUR`, [properties/payment_views.py](properties/payment_views.py) |
| Callback із недійсним підписом | 20 на IP за 600 с (далі 429); не більше 50 записів у журнал за вікно | `INVALID_CALLBACK_*` |
| Розмір callback | 32 КБ | `MAX_CALLBACK_BYTES` |
| Запит Status API зі сторінки результату | раз на 10 с на замовлення | `STATUS_CHECK_INTERVAL_SECONDS` |

Справжні callback LiqPay обробляються **до** перевірки ліміту, тож засмічення endpoint не блокує реальні повідомлення. Обмеження зберігаються в кеші, а він за замовчуванням локальний для процесу (див. [розділ 12](#12-поточний-стан-і-майбутні-завдання)).

### 6.7 Персональні дані

- Дані картки сайт не отримує.
- `PaymentEvent.payload` зберігає лише технічні поля (`SAFE_EVENT_FIELDS`). Ім'я, телефон і маска картки платника відкидаються, значення обрізаються до 255 символів.
- ALERT-події містять лише ідентифікатори (замовлення, транзакції, записи, оголошення).
- `Payment.user_snapshot` зберігає `username <email>`, щоб фінансовий запис лишався зрозумілим після видалення користувача (`SET_NULL`).
- Лог відхилених callback містить лише причину, розмір і IP.

### 6.8 Production settings і HTTPS

При `DJANGO_DEBUG=False` за замовчуванням увімкнено:

- `SECURE_SSL_REDIRECT`;
- secure-cookies для сесії та CSRF;
- `SESSION_COOKIE_HTTPONLY`, `SECURE_CONTENT_TYPE_NOSNIFF`;
- `SECURE_REFERRER_POLICY="same-origin"`, `X_FRAME_OPTIONS="DENY"`.

HSTS вмикається вручну (`DJANGO_SECURE_HSTS_SECONDS`). За reverse-proxy потрібно `DJANGO_BEHIND_PROXY=True` (`SECURE_PROXY_SSL_HEADER`). `ALLOWED_HOSTS` при `DEBUG=False` за замовчуванням порожній, тому його треба задати явно.

### 6.9 Інше

- `AppConfig.ready()` більше не виконує міграцій і масового схвалення (раніше це обходило модерацію).
- Оголошення з платежами захищене від видалення на рівні БД (`PROTECT`) і архівується.
- System checks (`properties.E001`–`E003`, `W001`) ловлять неузгоджену конфігурацію оплати.

---

## 7. Встановлення та локальний запуск (Arch Linux)

> Старі файли [README.md](README.md), [run.bat](run.bat) і [ЗАПУСК.txt](ЗАПУСК.txt) описують запуск на **Windows** з іншим шляхом (`Duplom_dominik`) і частково застарілі. Для Linux використовуйте інструкцію нижче.

### 7.1 Системні залежності

```bash
sudo pacman -S --needed python git
```

Встановлює інтерпретатор Python 3 (з модулем `venv`) і Git. Окремий пакет pip не потрібен: pip встановлюється всередину venv. Додаткові системні бібліотеки для Pillow зазвичай не потрібні, бо pip бере готове колесо (на Python 3.14 у цьому середовищі встановилось без проблем).

### 7.2 Отримання коду

```bash
cd ~/Desktop/mobihome
```

Переходить у папку проєкту. Якщо проєкт клонується з віддаленого репозиторію, URL репозиторію **невідомий** (remote не налаштовано), тому підставте свій:

```bash
git clone <URL-репозиторію> mobihome && cd mobihome
```

### 7.3 Віртуальне середовище та залежності

```bash
python -m venv venv
```

Створює ізольоване середовище в `venv/` (папка ігнорується Git). Якщо `venv/` уже існує (а в поточній копії він є), цей крок можна пропустити.

```bash
source venv/bin/activate
```

Активує venv у поточному терміналі: команди `python` і `pip` беруться з нього. Для fish-оболонки: `source venv/bin/activate.fish`.

```bash
pip install -r requirements.txt
```

Встановлює Django, Pillow і gunicorn.

### 7.4 Файл `.env`

```bash
cp -n .env.example .env
```

Створює `.env` з шаблону. Прапорець `-n` **не перезаписує** наявний `.env` (у поточній копії він уже є, і його вміст не варто втрачати).

У шаблоні вже є безпечні значення для локальної розробки:

- `DJANGO_DEBUG=True`;
- `DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1`;
- `PAYMENTS_ENABLED=False`, `PLACEMENT_PAYMENT_REQUIRED=False`;
- порожні ключі Google, Telegram і LiqPay (ці функції просто вимкнені).

Для локального запуску **достатньо** цього. `DJANGO_SECRET_KEY` у `DEBUG` можна лишити порожнім.

Особливості завантажувача `.env`:

- формат `KEY=VALUE`, рядки з `#` пропускаються;
- лапки навколо значення знімаються;
- коментар у тому ж рядку **не** підтримується;
- змінні, вже задані в оболонці, **не перезаписуються**.

### 7.5 База даних (SQLite)

Проєкт використовує **лише SQLite**. Шлях за замовчуванням — `data/db.sqlite3`, змінити можна через `DJANGO_SQLITE_PATH`. Каталог створюється автоматично.

Якщо працюєте з **наявною** базою, спершу зробіть резервну копію і запустіть перевірку блокерів міграцій (лише читання):

```bash
cp data/db.sqlite3 data/db.sqlite3.bak.$(date +%Y%m%d-%H%M%S)
python manage.py check_data_integrity
```

Потім застосуйте міграції:

```bash
python manage.py migrate
```

> ⚠ **Змінює базу даних.** Створює таблиці в новій БД або застосовує нові міграції до наявної. Міграція 0022 додає тариф 99 UAH / 30 днів, якщо його ще немає.

### 7.6 Адміністратор

```bash
python manage.py createsuperuser
```

Інтерактивно створює суперкористувача (пароль не потрапляє в історію оболонки).

> ⚠ Альтернатива `python manage.py create_or_reset_superuser --username <логін> --password <пароль>` **перезаписує пароль** наявного користувача з таким логіном і робить його суперкористувачем. Пароль при цьому лишається в історії оболонки.

### 7.7 Запуск сервера

```bash
python manage.py runserver 127.0.0.1:8000
```

Запускає dev-сервер Django. Відкривайте:

- сайт: <http://127.0.0.1:8000/>
- каталог: <http://127.0.0.1:8000/catalog/>
- адмінка: <http://127.0.0.1:8000/admin/>

Зупинка — `Ctrl+C`.

Якщо порт 8000 зайнятий:

```bash
python manage.py runserver 127.0.0.1:8001
```

### 7.8 Демо-дані (необов'язково)

```bash
python manage.py add_features_and_levels
python manage.py load_sample_properties
```

> ⚠ **Змінюють базу.**
> - Перша команда створює зручності й перераховує рівні доступності для об'єктів з аудитом.
> - Друга додає демо-оголошення з аудитами. Вони потрапляють на модерацію, тож у каталозі з'являться лише після схвалення адміністратором.
> - **Ніколи не запускайте `load_sample_properties --clear` на реальних даних:** воно видаляє всі оголошення та аудити. Для оголошень з платежами видалення завершиться помилкою `ProtectedError`.

### 7.9 Локальний запуск із `DEBUG=False` (production-подібний)

Не змінюйте production-конфігурацію і `.env` заради цього: задайте змінні лише для однієї команди (вони мають пріоритет над `.env`).

```bash
DJANGO_DEBUG=False \
DJANGO_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(50))')" \
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost \
DJANGO_SECURE_SSL_REDIRECT=False \
DJANGO_SECURE_COOKIES=False \
python manage.py runserver 127.0.0.1:8000 --insecure
```

Що тут робиться:

- `DJANGO_SECURE_SSL_REDIRECT=False` і `DJANGO_SECURE_COOKIES=False` вимикають HTTPS-редирект і secure-cookies, без яких локальний HTTP не працює;
- `--insecure` дозволяє `runserver` віддавати статичні файли.

Обмеження:

- завантажені фото (media) у такому режимі **не показуються**;
- ключ генерується щоразу новий, тому сесії не переживають перезапуск;
- якщо в оболонці чи `.env` увімкнено `PAYMENTS_ENABLED=True` з неповною конфігурацією, сервер зупиниться з `ImproperlyConfigured`.

### 7.10 PostgreSQL

**З коробки не підтримується:** `DATABASES` у [config/settings.py](config/settings.py) жорстко задає SQLite, а драйвера `psycopg` у `requirements.txt` немає.

Під час розробки сумісність коду з PostgreSQL 18 перевірялась лише **тестами** на тимчасовому кластері з окремим файлом налаштувань поза репозиторієм. Наприклад, тимчасовий модуль, що імпортує `config.test_settings` і перевизначає `DATABASES` на `django.db.backends.postgresql` + встановлений `psycopg[binary]`. Ці тести перевіряли частковий унікальний індекс, блокування рядків і паралельні callback.

Для повноцінного запуску на PostgreSQL потрібна окрема зміна налаштувань (наприклад, `DATABASES` з env-змінних) — це **майбутнє завдання**.

### 7.11 Production (стисло)

Готових файлів розгортання немає. Мінімально потрібно:

- `DJANGO_DEBUG=False`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`;
- HTTPS (за проксі — `DJANGO_BEHIND_PROXY=True`);
- `python manage.py collectstatic` (збирає статику в `DJANGO_STATIC_ROOT`, за замовчуванням `staticfiles/`);
- віддача `/static/` і `/media/` вебсервером, бо Django у production їх не віддає;
- запуск через `gunicorn config.wsgi:application`;
- SMTP для відновлення пароля;
- cron для `liqpay_sync_pending` (якщо ввімкнено оплату).

Ці кроки **не перевірялись** на реальному сервері.

---

## 8. Змінні середовища

Назви перевірено в [config/settings.py](config/settings.py) і [.env.example](.env.example). Секрети показані плейсхолдерами.

| Змінна | Призначення | Обов'язкова | Приклад без секретів |
|---|---|---|---|
| `DJANGO_DEBUG` | Режим налагодження | Ні (за замовч. `False`) | `True` локально, `False` на сервері |
| `DJANGO_SECRET_KEY` | Секретний ключ Django | **Так при `DEBUG=False`** | `<випадковий-рядок-50+>` |
| `DJANGO_ALLOWED_HOSTS` | Дозволені хости, через кому | **Так при `DEBUG=False`** | `example.com,www.example.com` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Довірені origin зі схемою | Для HTTPS-домену / тунелю | `https://example.com` |
| `DJANGO_SQLITE_PATH` | Шлях до SQLite | Ні (`data/db.sqlite3`) | `/var/lib/mobihome/db.sqlite3` |
| `DJANGO_MEDIA_ROOT` | Каталог завантажень | Ні (`media/`) | `/var/lib/mobihome/media` |
| `DJANGO_STATIC_ROOT` | Каталог `collectstatic` | Ні (`staticfiles/`) | `/var/www/mobihome/static` |
| `DJANGO_SECURE_COOKIES` | Secure-cookies сесії та CSRF | Ні (= `not DEBUG`) | `True` |
| `DJANGO_SECURE_SSL_REDIRECT` | Редирект на HTTPS | Ні (= `not DEBUG`) | `True` |
| `DJANGO_SECURE_HSTS_SECONDS` | HSTS, секунди | Ні (`0`) | `31536000` |
| `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS` | HSTS для піддоменів | Ні (`False`) | `False` |
| `DJANGO_BEHIND_PROXY` | Довіряти `X-Forwarded-Proto` | Ні (`False`) | `True` за nginx / тунелем |
| `DJANGO_LOG_LEVEL` | Рівень логування | Ні (`INFO`) | `WARNING` |
| `DJANGO_EMAIL_BACKEND` | Бекенд пошти | Ні (console у DEBUG, SMTP інакше) | `django.core.mail.backends.smtp.EmailBackend` |
| `DJANGO_EMAIL_HOST` | SMTP-сервер | Для листів у production | `smtp.example.com` |
| `DJANGO_EMAIL_PORT` | Порт SMTP | Ні (`587`) | `587` |
| `DJANGO_EMAIL_HOST_USER` | Логін SMTP | Для SMTP | `<smtp-user>` |
| `DJANGO_EMAIL_HOST_PASSWORD` | Пароль SMTP | Для SMTP | `<smtp-password>` |
| `DJANGO_EMAIL_USE_TLS` | TLS для SMTP | Ні (`True`) | `True` |
| `DJANGO_DEFAULT_FROM_EMAIL` | Адреса відправника | Ні | `mobi home <noreply@example.com>` |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID | Ні (порожньо = вимкнено) | `<client-id>.apps.googleusercontent.com` |
| `GOOGLE_CLIENT_SECRET` | Google OAuth secret | Разом з ID | `<google-client-secret>` |
| `TELEGRAM_BOT_TOKEN` | Токен бота для перевірки підпису | Ні (порожньо = вимкнено) | `<telegram-bot-token>` |
| `TELEGRAM_BOT_NAME` | Ім'я бота для віджета | Разом з токеном | `<bot_name>` |
| `LOGIN_THROTTLE_MAX_ATTEMPTS` | Невдалих спроб до блокування | Ні (`5`) | `5` |
| `LOGIN_THROTTLE_WINDOW_SECONDS` | Вікно блокування, с | Ні (`900`) | `900` |
| `PAYMENTS_ENABLED` | Увімкнути оплату LiqPay | Ні (`False`) | `False` |
| `PLACEMENT_PAYMENT_REQUIRED` | Каталог лише з оплаченими | Ні (`False`) | `False` |
| `LIQPAY_PUBLIC_KEY` | Публічний ключ LiqPay | **Так, якщо `PAYMENTS_ENABLED=True`** | `<sandbox_... або бойовий>` |
| `LIQPAY_PRIVATE_KEY` | Приватний ключ LiqPay | **Так, якщо `PAYMENTS_ENABLED=True`** | `<приватний-ключ>` |
| `LIQPAY_SANDBOX` | Тестовий режим | Ні (`True`) | `True` |
| `LIQPAY_API_VERSION` | Версія API | Ні (`7`) | `7` |
| `LIQPAY_SIGNATURE_ALGORITHM` | Алгоритм підпису | Ні (`sha3_256`) | `sha3_256` |
| `LIQPAY_PAYMENT_TTL_MINUTES` | Час на оплату, хв | Ні (`60`) | `60` |
| `SITE_URL` | Публічна адреса сайту без `/` у кінці | **Так, якщо `PAYMENTS_ENABLED=True`** (`https://` при `DEBUG=False`) | `https://example.com` |
| `DJANGO_SETTINGS_MODULE` | Модуль налаштувань | Ні (`config.settings`) | `config.test_settings` для тестів |

---

## 9. Management-команди

Усі команди — у [properties/management/commands/](properties/management/commands/).

| Команда | Що робить | Змінює дані? |
|---|---|---|
| `python manage.py check_data_integrity` | Шукає записи, що заблокують міграції 0021 і 0025, статистику платежів, старий ТОП. Виводить лише id і кількості | Ні (лише читання) |
| `python manage.py liqpay_sync_pending [--min-age 5] [--limit 200]` | Для незавершених платежів, старших за `--min-age` хв, запитує LiqPay Status API і застосовує результат. Прострочені закриває лише після достовірної відповіді | ⚠ **Так**: статуси платежів, зарахування. Робить мережеві запити до LiqPay |
| `python manage.py apply_placement_transition` | Показує, яких оголошень торкнеться перехідний період | Ні |
| `python manage.py apply_placement_transition --apply [--start YYYY-MM-DD]` | Одноразово ставить `placement_until` = дата активації + 30 днів опублікованим оголошенням без періоду. Повторне застосування заблоковане | ⚠ **Так**, одноразово й незворотно |
| `python manage.py add_features_and_levels` | Створює зручності, перераховує рівні доступності | ⚠ Так |
| `python manage.py load_sample_properties [--clear]` | Демо-оголошення. `--clear` **видаляє всі оголошення й аудити** | ⚠ Так. `--clear` — **небезпечно** |
| `python manage.py create_or_reset_superuser --password <пароль> [--username admin] [--email …]` | Створює суперкористувача або **скидає пароль** наявного | ⚠ Так |

Приклад cron для синхронізації платежів (раз на 10 хвилин). Він **не налаштований** ніде в проєкті, це лише приклад:

```cron
*/10 * * * * cd /шлях/до/mobihome && venv/bin/python manage.py liqpay_sync_pending >> /var/log/mobihome-liqpay-sync.log 2>&1
```

Допоміжні скрипти в корені:

- [create_admin.py](create_admin.py) — інтерактивне створення адміністратора;
- [check_users.py](check_users.py) — виводить логіни й прапорці staff усіх користувачів. Це персональні дані, тож не запускайте його на production без потреби.

---

## 10. Тестування

Тести — у [properties/tests/](properties/tests/), використовують **окремі налаштування** [config/test_settings.py](config/test_settings.py):

- `DEBUG=False`, але без HTTPS-редиректу;
- тимчасова SQLite у системному temp-каталозі;
- тимчасовий `MEDIA_ROOT`, `LocMemCache`;
- оплата вимкнена, а тести вмикають її локально через `override_settings` з фіктивними ключами `sandbox_…`.

Реальна БД і реальний LiqPay не використовуються: мережеві виклики LiqPay в тестах замінено моками.

> Без `--settings=config.test_settings` тести запускаються з `config.settings` і можуть падати (наприклад, через HTTPS-редирект 301).

### 10.1 Команди

```bash
# Усі тести застосунку properties
python manage.py test properties --settings=config.test_settings

# Один модуль
python manage.py test properties.tests.test_liqpay --settings=config.test_settings

# Один клас
python manage.py test properties.tests.test_liqpay.TryAgainTests --settings=config.test_settings

# Один метод
python manage.py test properties.tests.test_liqpay.CallbackTests.test_expired_payment_late_success_still_credited --settings=config.test_settings

# Детальний вивід (видно пропущені тести)
python manage.py test properties --settings=config.test_settings -v 2
```

Необов'язково, щоб гарантувати відсутність мережевих запитів (Linux, без root):

```bash
unshare -rn python manage.py test properties --settings=config.test_settings
```

`pyproject.toml` містить налаштування для pytest (`DJANGO_SETTINGS_MODULE = config.test_settings`), але `pytest` і `pytest-django` **не входять** у `requirements.txt`. Їх треба встановити окремо, якщо потрібні.

**Пропущені тести:** 4 тести запуску production-конфігурації (`A3FailFastTests` і `test_production_refuses_to_start_without_secret_key`) **автоматично пропускаються, якщо в корені є `.env`**, бо він впливає на конфігурацію підпроцесу. Щоб їх виконати, тимчасово приберіть `.env` (не видаляйте!) або запустіть тести в копії проєкту без `.env`.

### 10.2 Підтверджені результати

| Коли | Що | Результат |
|---|---|---|
| 19.11.2026, під час підготовки цього документа | `python manage.py test properties --settings=config.test_settings` (venv проєкту, без мережі) | **263 тести, OK, 4 пропущено** (через наявний `.env`) |
| 19.11.2026, під час розробки | `test_liqpay` + `test_liqpay_hardening` на тимчасовому PostgreSQL 18 | 158 тестів, OK (3 пропущено); паралельні тести тричі поспіль OK |
| Історія комітів | Кількість тестів за етапами | 44 → 79 → 85 → 105 → 159 (повідомлення комітів) |

Результати PostgreSQL отримано на тимчасовому кластері поза репозиторієм і без нього не відтворюються.

### 10.3 Що покрито тестами

| Модуль | Основні сценарії |
|---|---|
| [test_auth_security.py](properties/tests/test_auth_security.py) | Видалені небезпечні endpoint (OTP, QR, Google verify_token); Google OAuth (state, claims, заборона входу адміна, без автоприв'язки); Telegram (підпис, застарілий `auth_date`); open redirect; блокування після невдалих входів (сайт і адмінка); скидання пароля без витоку коду |
| [test_listings_access.py](properties/tests/test_listings_access.py) | Модерація (нове оголошення чекає схвалення, чужий не публікує); відсутність полів картки; видимість прихованих оголошень; адмін-дії; XSS у каталозі; унікальні slug; відмова старту без `SECRET_KEY` |
| [test_listing_display.py](properties/tests/test_listing_display.py) | Нейтральна заглушка замість стокових фото; галерея; картка каталогу; відсутність N+1; карта лише для відомих міст; схожі оголошення; валідація поверхів і додаткових фото |
| [test_placement_admin.py](properties/tests/test_placement_admin.py) | Тариф і знімок умов; **30 днів, продовження від поточної дати, старт після публікації, повернення простроченого на модерацію**; **ідемпотентна обробка**; невідповідність суми; захист фінансових записів (архівування, `PROTECT`, обмеження БД); старий ТОП; вихід лише POST; адмінка платежів (лише перегляд); перехідний період (dry-run, одноразовість) |
| [test_liqpay.py](properties/tests/test_liqpay.py) | Підпис; checkout із серверною сумою; **callback** (підробка, повтори, сума/валюта); production-режим; **сторінка результату і Status API** (зокрема `payment_not_found` лише за точним кодом, помилки API, не-ASCII поля); бізнес-потік модерація → публікація → 30 днів; `try_again`; **пізній успіх після `cancelled`/`expired`**; **подвійна оплата з різними `order_id`** (порядок, межі timestamp, один ALERT на пару, оновлення ALERT); скасування; **паралельні callback** (один і різні `order_id`) |
| [test_liqpay_hardening.py](properties/tests/test_liqpay_hardening.py) | Мережеві помилки Status API; зловживання callback (розмір, ліміти); fail-fast конфігурації; строгий підпис; пари версії/алгоритму; одна відкрита оплата і лише власник; **конкурентне створення платежів**; **duplicate_charge / duplicate_refund** (зокрема паралельно); адмін-дія «Позначити перевіреним» і паралельне закриття |

Окремо щодо ключових вимог:

- **Повторні callback:** `test_paid_order_id_is_credited_once`, `test_same_payment_id_repeat_is_idempotent`, `test_two_order_ids_repeated_callbacks_add_nothing`.
- **Подвійне зарахування:** `ParallelCallbackTests`, `DuplicateChargeTests`, `test_two_order_ids_*`, `CrossOrderBoundaryTests`.
- **Відновлення через Status API:** `test_status_check_success_credits_and_waits_moderation`, `test_status_api_success_credits_old_payment_and_no_new_one`, `test_sync_command_*`.
- **Модерація і початок 30 днів:** `test_payment_for_new_listing_starts_on_publication`, `test_new_listing_waits_moderation_then_30_days_from_publication`.
- **Ідемпотентна активація:** `test_idempotent_double_processing`, `test_two_payments_before_publication_chain`, `test_rejected_listing_does_not_activate`.

---

## 11. Структура проєкту

```
mobihome/
├── config/
│   ├── settings.py            # налаштування з env/.env, безпека, LiqPay, fail-fast
│   ├── test_settings.py       # налаштування для тестів
│   ├── urls.py                # admin/ + properties.urls
│   ├── wsgi.py / asgi.py      # точки входу сервера
├── properties/                # єдиний застосунок
│   ├── models.py              # усі моделі (оголошення, аудит, тарифи, платежі, журнали)
│   ├── views.py               # сторінки сайту, профіль, модерація
│   ├── payment_views.py       # старт оплати, callback, результат, скасування
│   ├── liqpay.py              # підпис, checkout, обробка повідомлень, Status API
│   ├── placement.py           # правила строків, зарахування, дублікати, перехідний період
│   ├── payment_config.py      # перевірка конфігурації оплати (без Django)
│   ├── social_auth.py         # Google OAuth, Telegram
│   ├── security.py            # обмеження спроб входу
│   ├── forms.py               # форми (реєстрація, оголошення, аудит, профіль, відгуки)
│   ├── admin.py               # Django admin і кастомні дії
│   ├── checks.py              # system checks конфігурації оплати
│   ├── signals.py             # активація оплаченого періоду при публікації
│   ├── mobility.py            # опис 5 рівнів доступності
│   ├── context_processors.py  # current_url_name для шаблонів
│   ├── management/commands/   # management-команди (розділ 9)
│   ├── migrations/            # 0001–0027
│   └── tests/                 # тести (розділ 10)
├── templates/
│   ├── base.html              # загальний макет
│   ├── properties/            # сторінки сайту (index.html — головна, home.html — каталог)
│   ├── registration/          # відновлення пароля
│   └── admin/properties/payment/confirm_review.html
├── static/                    # img/, sw.js, manifest.json
├── workflows/main.yml         # CI-конфіг (див. розділ 12 — фактично не активний)
├── .env.example               # шаблон змінних середовища
├── requirements.txt, pyproject.toml, .flake8, .isort.cfg
├── manage.py
├── create_admin.py, check_users.py      # допоміжні скрипти
├── run.bat, ЗАПУСТИТИ_ДОСТУПНИЙ_ДІМ.bat, ЗАПУСК.txt   # запуск на Windows (застаріле)
├── README.md                  # застарілий опис (Windows)
├── PROJECT_AUDIT.md           # аудит безпеки від 15.11.2026 (стан ДО виправлень)
└── PROJECT_DOCUMENTATION.md   # цей документ
```

Не в Git:

- `data/` — SQLite;
- `media/` — завантаження;
- `venv/`;
- `.env`, `.env.sandbox`;
- `staticfiles/`;
- `.claude/`, `graphify-out/` — службові каталоги інструментів.

### Міграції

| Міграції | Зміст |
|---|---|
| 0001–0014 | Базові моделі, тип оголошення, кімнати, площа, зручності й категорії, рівень доступності, власник, фото-галерея, обране, відгуки |
| 0015–0020 | Модерація, заявки на аудит, фотоперевірка, консультації, профіль користувача, auth-сесії, `google_sub` |
| 0021 | Тарифи, платежі, журнал подій, поверхи, `placement_until`, архів, індекси, обмеження цілісності |
| 0022 | Тариф «Розміщення квартири» 99 UAH / 30 днів |
| 0023 | Активація платежу (`activated_at`) і журнал перехідного періоду |
| 0024 | `Payment.is_test` |
| 0025 | Одна незавершена оплата на оголошення (частковий унікальний індекс) |
| 0026 | `Payment.needs_review` |
| 0027 | Дозвіл `review_payment` |

---

## 12. Поточний стан і майбутні завдання

### Реалізовано

- Каталог з фільтрами доступності, сторінка об'єкта, галерея, карта рівня міста, схожі оголошення, порівняння, швидкий перегляд, обране, паспорт доступності.
- Аудит доступності з автоматичним балом і рівнем 1–5, заявки на аудит і консультацію, відгуки про користувачів.
- Реєстрація та вхід за паролем, Google OAuth, Telegram, відновлення пароля email-ом, обмеження спроб входу.
- Обов'язкова модерація і фотоперевірка оголошень, архівування замість видалення оголошень з платежами.
- Тариф і платне розміщення через LiqPay:
  - Hosted Checkout, перевірка callback, Status API, команда синхронізації;
  - правила строків (старт після публікації, продовження від поточної дати);
  - ідемпотентність, одна відкрита оплата на оголошення;
  - виявлення подвійних списань із `needs_review` і ALERT, адмін-дія закриття перевірки з окремим дозволом;
  - обробка `try_again`.
- Адмінка: модерація, архів, тарифи, платежі лише для перегляду, користувачі, перехідний період.
- Fail-fast перевірка production-конфігурації оплати, system checks, налаштування HTTPS і cookies.
- Автотести: 263 на SQLite.

### Потребує уваги / майбутні завдання

**Підтверджені проблеми (перевірено в коді):**

1. **Незакомічені зміни LiqPay.** Останній коміт `b4ce42d` не містить пізніших доопрацювань: `try_again`, перевірка Status API перед EXPIRED, виявлення подвійних оплат з різними `order_id`, строгий `err_code`, тести. Вони лише в робочій копії (9 змінених файлів). Потрібен окремий коміт.
2. **CI фактично не працює.** Конфіг лежить у `workflows/main.yml`, а GitHub читає лише `.github/workflows/` (каталог `.github/` порожній). Крім того, workflow:
   - запускається для гілки `main`, якої немає (є `master` і `stage/*`);
   - встановлює зайві MySQL-бібліотеки;
   - вимагає покриття ≥ 60 % через pytest, якого немає в `requirements.txt`.
3. **README і скрипти запуску застарілі:** Windows-шлях `Duplom_dominik`, опис неіснуючих сторінок.
4. **PWA manifest** посилається на `/static/images/logo.png`, а файл лежить у `static/img/logo.png`, тож іконки не завантажуються. Service worker віддає `/` з кешу без оновлення: після першого відвідування головна може не оновлюватись.
5. **Форма «Контакти»** нічого не зберігає й не надсилає. `track_click` нічого не записує.
6. **Обмеження спроб і ліміти callback** зберігаються в кеші за замовчуванням (`LocMemCache`, окремо для кожного процесу). При кількох воркерах gunicorn ліміти слабшають. За проксі чи тунелем `REMOTE_ADDR` — це адреса проксі, тож ліміт на IP стає спільним для всіх.
7. **PostgreSQL не налаштовується** через env (лише SQLite).
8. **Немає власної форми редагування оголошення** (лише через адмінку).
9. **Немає файлів розгортання** (Docker, nginx, systemd) і налаштованого cron для `liqpay_sync_pending`.
10. **Email при реєстрації не підтверджується** (код це враховує: немає автоприв'язки Google-входу).
11. **Ціна в USD** рахується за фіксованим курсом 41.5.
12. **Координати карти** є лише для 10 міст.
13. **`create_or_reset_superuser`** приймає пароль аргументом командного рядка, тож він потрапляє в історію оболонки.
14. **`signature_valid=True`** у подій `[ALERT duplicate_charge]` / `[ALERT duplicate_refund]`, хоча це внутрішні події (задокументовано в докстрінгу `PaymentEvent`). Внутрішні події відрізняються лише префіксом `note`.
15. **Подія `[ALERT duplicate_property_charge]` оновлюється на місці.** Попередній вміст не зберігається, хоча ID транзакцій лишаються в незмінних подіях `[callback]` і `[status]`.
16. **У корені лежить залишковий `db.sqlite3-journal`** (ігнорується Git).

**Відомі обмеження та ризики LiqPay:**

- **Платіж, що «застряг» в обробці.** Якщо LiqPay постійно відповідає «в обробці», помилкою або недоступний, стара оплата не закривається, і нову для оголошення створити не можна. Власник може скасувати оплату сам (`/payments/<order_id>/cancel/`). Адмінка платежів не дозволяє змінювати статус.
- **Синхронний запит до Status API** у `start_payment` (тайм-аут 15 с) без throttling, тож при недоступності LiqPay кнопка оплати «висить».
- **Хибні позначки `needs_review`:**
  - пізній успіх A, поки новіший B лише відкритий і так і не оплачений;
  - точний збіг timestamp.
- **Залежність від годинника сервера** (порівняння `created_at` і `applied_at`).

**Не перевірено:**

- значення `err_code` для неіснуючого замовлення в **реальному** LiqPay Sandbox (код очікує точно `payment_not_found`);
- повний сценарій у реальному Sandbox (успіх, повтор, дві вкладки, відмова, повернення коштів) і тестові картки, що використовувались;
- бойові ключі LiqPay і production-режим на реальному сервері;
- увімкнення `PLACEMENT_PAYMENT_REQUIRED=True` і застосування `apply_placement_transition --apply` на реальних даних;
- запуск через gunicorn за nginx, `collectstatic`, SMTP;
- чи працює black / isort / bandit з CI на поточному коді (не запускались).

**Припущення (не підтверджено):** правила дизайну головної сторінки (`index.html`) розробник вимагав не змінювати. Це обмеження процесу розробки, а не функція коду.

---

## 13. Історія розробки

| Коміт | Дата | Етап |
|---|---|---|
| `28f2bcd` | 15.11.2026 | Baseline: знімок проєкту до виправлень + [PROJECT_AUDIT.md](PROJECT_AUDIT.md) (12 critical, 14 high проблем) |
| `e1a0787` | 15.11.2026 | Етап 1: критичні виправлення безпеки — міграції, налаштування з env, справжній Google OAuth, перевірка Telegram, видалення OTP/Дії/фальшивої оплати, throttling, захист від open redirect, видимість, XSS (44 тести) |
| `116ad8a` | 15.11.2026 | Етапи 3–4: тариф, структура платежів, адмінка, цілісність БД, архівування, `check_data_integrity` (79 тестів) |
| `279cab1` | 15.11.2026 | Період від публікації, перехідний період (85 тестів) |
| `6cd00fb` | 15.11.2026 | Етап 5: відображення оголошень без фейкових фото й даних, схожі оголошення, валідація фото (105 тестів) |
| `47b3d45` | 15.11.2026 | Виправлення горизонтального переповнення каталогу на мобільних |
| `693b26f` | 15.11.2026 | Етап 2: інтеграція LiqPay (sandbox-ready, оплата вимкнена) (159 тестів) |
| `b4ce42d` | 18.11.2026 | Посилення LiqPay: одна відкрита оплата, лише власник, строгі пари версії/алгоритму, виявлення другого списання, `reversed`, адмін-дія «Позначити перевіреним» |
| — (не закомічено) | 18–19.11.2026 | `try_again`, тести скасування, пізній успіх після EXPIRED, Status API перед EXPIRED, подвійна оплата з різними `order_id`, строгий `err_code`, семантика внутрішніх ALERT |
