# Оплата розміщення через LiqPay — експлуатація

Практичний посібник для власника проєкту й адміністратора: як влаштована оплата, як її налаштувати,
перевірити, відновити загублені повідомлення і безпечно ввімкнути бойові платежі.
Історію розробки описано в [PAYMENTS_DEVELOPMENT_REPORT.md](PAYMENTS_DEVELOPMENT_REPORT.md).

> Справжніх ключів у цьому документі немає і бути не повинно. Приватний ключ LiqPay живе лише у `.env`
> на сервері: його не надсилають у чати, листи чи тікети.

## 1. Як це працює

| Крок | Що відбувається | Код |
|---|---|---|
| 1 | Власник натискає «Оплатити 99 грн» на сторінці `/payment/<id>/` (POST, CSRF) | `payment_views.start_payment` |
| 2 | Сервер перевіряє, що це власник активного оголошення, і створює `Payment` (або продовжує незавершений, той самий `order_id`). Сума, валюта й термін беруться з тарифу і фіксуються в записі | `placement.get_or_create_open_payment` |
| 3 | Сервер формує `data` (base64 JSON) і `signature = base64(sha3_256(private + data + private))`, браузер автоматично відправляє форму на `https://www.liqpay.ua/api/3/checkout` | `liqpay.build_checkout`, `liqpay_redirect.html` |
| 4 | Користувач платить на сторінці LiqPay (картку наш сайт не бачить) | — |
| 5 | LiqPay надсилає server-to-server POST на `/payments/liqpay/callback/` | `payment_views.liqpay_callback` |
| 6 | Перевіряються розмір (≤ 32 КБ), підпис (`hmac.compare_digest`), `order_id`, `public_key`, `action`, режим sandbox/бойовий, сума й валюта | `liqpay.process_payment_fields` |
| 7 | Успіх зараховується рівно один раз під блокуванням рядків БД; дні розміщення — за правилами модерації (розд. 4) | `placement.apply_successful_payment` |
| 8 | Користувача повертає на `/payments/<order_id>/result/`. Ця сторінка **нічого не підтверджує**: показує стан із БД і, якщо платіж не завершено, сама питає LiqPay (Status API, не частіше ніж раз на 10 с) | `payment_views.payment_result` |
| 9 | Якщо callback загубився — `liqpay_sync_pending` за розкладом питає Status API (розд. 6) | `management/commands/liqpay_sync_pending.py` |

Оплату підтверджує **лише** підписаний callback або відповідь Status API на наш підписаний запит.
Параметри в адресі сторінки результату (`?status=success`, `data`, `signature`) ігноруються.

## 2. Змінні середовища

Шаблон — [`.env.example`](.env.example) (лише заглушки). `.env` і `.env.*` ігноруються Git.

| Змінна | Призначення | Sandbox | Production |
|---|---|---|---|
| `DJANGO_DEBUG` | Режим налагодження | `True` (локально) | `False` |
| `DJANGO_SECRET_KEY` | Секрет Django | будь-який довгий | обов'язково власний; без нього сервер не стартує |
| `SITE_URL` | Публічна адреса без `/` в кінці; з неї будуються `server_url` і `result_url` | HTTPS-адреса тунелю | `https://<домен>` (не `http://` — інакше оплата вимкнена) |
| `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` | Домени сайту | хост тунелю | бойові домени |
| `DJANGO_SECURE_SSL_REDIRECT`, `DJANGO_SECURE_COOKIES` | HTTPS-редирект і secure-cookies | — | за замовчуванням увімкнені при `DEBUG=False` |
| `DJANGO_SECURE_HSTS_SECONDS` | HSTS | `0` | `31536000` лише після перевірки стабільного HTTPS |
| `DJANGO_BEHIND_PROXY` | Довіряти `X-Forwarded-Proto` від nginx | за потреби | `True`, якщо Django за проксі |
| `LIQPAY_PUBLIC_KEY` / `LIQPAY_PRIVATE_KEY` | Ключі магазину | `sandbox_…` | бойові (без `sandbox_`) |
| `LIQPAY_SANDBOX` | Тестовий режим | `True` | `False` |
| `LIQPAY_API_VERSION` / `LIQPAY_SIGNATURE_ALGORITHM` | Протокол | `7` / `sha3_256` | `7` / `sha3_256` |
| `LIQPAY_PAYMENT_TTL_MINUTES` | Термін дії форми оплати | `60` | `60` |
| `PAYMENTS_ENABLED` | Користувачі можуть платити | `True` | `True` лише після кроків розд. 9 |
| `PLACEMENT_PAYMENT_REQUIRED` | Каталог показує лише оплачені оголошення | `False` | вмикати останнім (розд. 9) |

Перевірки конфігурації:
- ключ `sandbox_…` при `LIQPAY_SANDBOX=False` (і навпаки) — оплата вимикається;
- дозволені лише пари `7 + sha3_256` або `3 + sha1`;
- `SITE_URL` має бути `https://` при `DEBUG=False`;
- при `DEBUG=False` сервер **не стартує**, якщо `PAYMENTS_ENABLED=True` з помилковою конфігурацією або
  `PLACEMENT_PAYMENT_REQUIRED=True` без `PAYMENTS_ENABLED=True`;
- тексти помилок не містять значень ключів.

```bash
venv/bin/python manage.py check            # system checks, зокрема конфігурація оплати
venv/bin/python manage.py check --deploy   # на сервері з бойовим .env
```

## 3. Налаштування Sandbox

1. Візьміть sandbox-ключі в кабінеті LiqPay (обидва починаються з `sandbox_`).
2. Створіть `.env.sandbox` (ігнорується Git) за зразком `.env.example`: `LIQPAY_SANDBOX=True`,
   `PAYMENTS_ENABLED=True`, `PLACEMENT_PAYMENT_REQUIRED=False`, `SITE_URL` — HTTPS-адреса тунелю,
   `DJANGO_SQLITE_PATH` — окрема база для тестів.
3. Запустіть тунель (наприклад, `cloudflared tunnel --url http://127.0.0.1:8003`) і впишіть його адресу
   в `SITE_URL`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`.
4. Запустіть сервер **у тому самому терміналі**:
   ```bash
   set -a; . ./.env.sandbox; set +a
   venv/bin/python manage.py migrate
   venv/bin/python manage.py runserver 127.0.0.1:8003
   ```
   Змінні з оболонки мають пріоритет над `.env`.

## 4. Оплата і модерація

Оплата і модерація — окремі процеси. Оплата **ніколи** не публікує і не схвалює оголошення.
Логіка зосереджена в `properties/placement.py` (`_apply_period_rules`, `activate_on_publication`).

| Стан оголошення в момент зарахування | Результат |
|---|---|
| Опубліковане, схвалене, період діє | +30 днів до поточної дати завершення одразу, без повторної модерації |
| Схвалене, ще без періоду | 30 днів від зарахування |
| Період закінчився | Повертається на модерацію; 30 днів — від повторної публікації |
| Чернетка / на модерації / відхилене | Оплату збережено (`success`, без періоду); 30 днів почнуться при схваленні |
| Кілька зарахованих оплат до публікації | Активуються послідовно при схваленні (2 × 30 днів) |

- Активація при схваленні спрацьовує з будь-якого місця: сигнал `post_save` моделі `Property` і масові
  дії адмінки викликають `activate_on_publication`. Повторне схвалення днів не додає.
- Відхилення активного оголошення ховає його з каталогу. Дата завершення не змінюється і продовжує спливати.
- Сторінка результату пояснює власнику, що робити: надіслати на модерацію, дочекатися схвалення
  або виправити відхилене оголошення.

## 5. Стани платежу

| Статус | Значення | Можливі переходи |
|---|---|---|
| `created` | Створено, форма LiqPay відкривалась або ні | → `pending`, `success`, `failure`, `cancelled`, `expired` |
| `pending` | LiqPay обробляє (3-D Secure, `processing`, `wait_*`) | → `success`, `failure`, `cancelled`, `expired`; `try_again` → `created` |
| `pending` + `error_message` + `needs_review` | LiqPay підтвердив **іншу** суму/валюту: не зараховано, потрібна ручна перевірка | лише рішення адміністратора |
| `success` | Зараховано (`applied_at`) — один раз | → `reversed` |
| `failure` / `cancelled` / `expired` | Не зараховано | пізній підтверджений успіх **зараховується** (кошти списано) |
| `reversed` | LiqPay повернув кошти. Якщо платіж був зарахований — `needs_review` і одна подія `[ALERT payment_reversed]`; період, публікація й модерація автоматично не змінюються | — |

Пізні та повторні повідомлення: старий `processing` чи `failure` ніколи не перезаписує зарахований успіх.
Повтор того самого `success` нічого не змінює. `success` з іншим `payment_id` для вже зарахованого платежу
не зараховується: ставиться `needs_review` і подія `[ALERT duplicate_charge]`.

## 6. Відновлення загублених callback: `liqpay_sync_pending`

```bash
venv/bin/python manage.py liqpay_sync_pending                 # типовий запуск
venv/bin/python manage.py liqpay_sync_pending --min-age 2 --limit 50 --delay 1 --max-failures 3
```

Команда обробляє:
- `created` / `pending` старші за `--min-age` хвилин (типово 5);
- незараховані `cancelled` / `expired` молодші за термін форми + 30 хв: форму могли оплатити в іншій
  вкладці, а callback — загубитись.

Як трактується відповідь:

| Відповідь Status API | Дія |
|---|---|
| `success` / `sandbox` (з нашими `order_id`, `public_key`, сумою, валютою) | Зараховується ідемпотентно |
| `failure` / `error` | `failure` |
| `processing`, `wait_*`, `*_verify` | Відкритий → `pending`; закритий (`cancelled`/`expired`) не «відкривається» |
| `try_again` | Залишається відкритим; після закінчення терміну — `expired` |
| `err_code == "payment_not_found"` (точний рядок) | Після закінчення терміну — `expired`; до того — без змін |
| Інша помилка API, тайм-аут, мережа, некоректний JSON, чужий `order_id` | **Без змін** — платіж ніколи не вважається невдалим через недоступність API |

Захист від rate limit і збоїв: пауза `--delay` (типово 0,5 с) між запитами. Після `--max-failures`
(типово 5) помилок мережі чи API поспіль команда зупиняється з кодом виходу 1, нічого не змінюючи.
Вивід — лише лічильники (`credited=1, pending=2, …`), без ключів і підписів.

Приклад розкладу (не встановлюється автоматично; `flock` не дає запускам накластися):

```cron
*/10 * * * * cd /srv/mobihome && flock -n /tmp/liqpay_sync.lock venv/bin/python manage.py liqpay_sync_pending >> /var/log/mobihome/liqpay_sync.log 2>&1
```

systemd-варіант: `liqpay-sync.service` (`Type=oneshot`, `WorkingDirectory=/srv/mobihome`,
`EnvironmentFile=/srv/mobihome/.env`, `ExecStart=/srv/mobihome/venv/bin/python manage.py liqpay_sync_pending`)
+ `liqpay-sync.timer` з `OnUnitActiveSec=10min`.

## 7. Ручна перевірка платежів (`needs_review`)

Адмінка: **Платежі → фільтр «Потребує перевірки адміністратором: так»**. На сторінках платежів
показується червоне попередження з кількістю таких платежів.

| ALERT-подія | Причина | Що зробити |
|---|---|---|
| `[ALERT duplicate_charge]` | Другий успіх за тим самим `order_id` з іншим `payment_id` | Перевірити обидва `payment_id` у кабінеті LiqPay, повернути друге списання |
| `[ALERT duplicate_property_charge]` | Дві успішні оплати одного оголошення через різні `order_id`, що накладаються в часі | Вирішити, чи це подвійне списання, і повернути зайве |
| `[ALERT duplicate_refund]` | Повернення другого списання; зарахований платіж не змінено | Звірити з кабінетом LiqPay |
| `[ALERT amount_mismatch]` | LiqPay підтвердив іншу суму або валюту; не зараховано | Звірити в кабінеті LiqPay і повернути кошти |
| `[ALERT payment_reversed]` | LiqPay повернув кошти за зарахованим платежем; дані зарахування й оголошення не змінено | Вирішити, чи залишати оплачений період (за потреби — деактивувати оголошення вручну) |

Закрити перевірку може лише користувач із дозволом **«Може закривати перевірку подвійного списання»**
(`properties.review_payment`). Для цього є дія «Позначити перевіреним». Вона показує сторінку підтвердження
з ALERT-подіями і не закриває перевірку, якщо після відкриття сторінки з'явилась нова подія. Дія знімає
лише прапорець, пише подію `[REVIEW closed]` і запис у журнал адмінки.

Платежі в адмінці доступні лише для перегляду. Дії «позначити успішним» немає, і з'явитися вона не повинна:
статус змінює тільки підтвердження LiqPay. Журнал `PaymentEvent` містить лише безпечні поля
(без імені, телефону, маски картки, підписів).

Пошук у консолі (лише читання):

```bash
venv/bin/python manage.py shell -c "
from properties.models import Payment
for p in Payment.objects.filter(needs_review=True):
    print(p.pk, p.order_id, p.status, p.provider_transaction_id, p.error_message[:80])
"
```

## 8. Типові проблеми

| Симптом | Причина | Рішення |
|---|---|---|
| «Онлайн-оплата тимчасово недоступна» | `PAYMENTS_ENABLED=False` або помилка конфігурації | `manage.py check` покаже причину (без ключів) |
| Оплата в LiqPay пройшла, а на сайті «Очікуємо підтвердження» | Callback не дійшов (тунель упав, сервер недоступний) | `liqpay_sync_pending --min-age 0` або відкрити сторінку результату; перевірити публічну доступність `SITE_URL/payments/liqpay/callback/` |
| Callback отримує 400 `invalid` | Неправильний приватний ключ або алгоритм/версія | Звірити ключі; `PaymentEvent` з `signature_valid=False` |
| Callback 429 | Понад 20 неправильних запитів з однієї IP за 10 хв | Шукати джерело підробок; справжні повідомлення з правильним підписом не блокуються |
| «Платіж не пройшов. Ви можете спробувати ще раз» | LiqPay `try_again` (напр. AntiFraud) | Повторити тим самим замовленням, поки форма дійсна |
| «Нову оплату можна створити після HH:MM» | Попередню форму ще можна оплатити в іншій вкладці | Дочекатися, щоб уникнути подвійного списання |
| Оплачено, але оголошення не в каталозі | Чекає модерації, відхилене або не надіслане | Сторінка результату / профіль показують, що зробити |
| `liqpay_sync_pending` завершується з кодом 1 | LiqPay недоступний або обмежує запити | Повторити пізніше; платежі не змінено |

## 9. Безпечне ввімкнення бойових платежів

Виконує лише власник проєкту.

1. Повний набір тестів на сервері або в CI:
   ```bash
   venv/bin/python manage.py test properties --settings=config.test_settings
   ```
2. Ручний E2E у Sandbox: успіх, відмова, скасування, `try_again`, втрачений callback
   (зупинити тунель → `liqpay_sync_pending`), оплата оголошення на модерації → схвалення → старт 30 днів.
3. Окремі **бойові** ключі. Тестові й бойові ключі ніколи не змішуються: перевірка конфігурації це забороняє.
4. На сервері: `DJANGO_DEBUG=False`, `SITE_URL=https://<домен>`, `LIQPAY_SANDBOX=False`, бойові ключі,
   `PAYMENTS_ENABLED=True`, `PLACEMENT_PAYMENT_REQUIRED=False`. Потім `manage.py check --deploy`.
5. Перевірити ззовні, що `https://<домен>/payments/liqpay/callback/` доступний: GET має повертати 405,
   а POST без підпису — 400.
6. Увімкнути розклад `liqpay_sync_pending` (розд. 6).
7. Одна контрольна бойова оплата власником (99 грн) → перевірити callback, `PaymentEvent`, адмінку.
   Повернути кошти в кабінеті LiqPay за потреби.
8. Лише після цього: `venv/bin/python manage.py apply_placement_transition` (спершу без `--apply`
   для перегляду), потім `PLACEMENT_PAYMENT_REQUIRED=True`.

Тестові платежі (`is_test=True`) у бойовому режимі ніколи не зараховуються. Статус `sandbox` у бойовому
режимі відхиляється.
