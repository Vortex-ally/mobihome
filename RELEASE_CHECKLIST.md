# Release checklist — mobi Home

Позначки: ✅ перевірено (як — у [TEST_REPORT.md](TEST_REPORT.md)) · ⬜ потребує дії власника / зовнішньої перевірки.

## Код і тести
- ✅ Повний набір тестів (420) на SQLite і PostgreSQL 18 — без провалів; SQLite→PostgreSQL і backup/restore перевірено.
- ✅ `manage.py check`, `makemigrations --check`, `check --deploy` (production-подібні змінні), `collectstatic`.
- ✅ Браузерна перевірка (емуляція) 20 сторінок, зокрема адмінки й модератора, на 320/360/390/430/768/1024/1440 px:
  без горизонтального скролу на сторінках сайту. Фізичні пристрої — не перевірялись.
- ✅ Перенесення SQLite → PostgreSQL (dumpdata/loaddata) на тестових даних.

## Перед першим запуском
- ✅ Ліміти входу й форм — окремо для кожного відвідувача (X-Real-IP від nginx), спільні для воркерів (Redis); перевірено 26.11 через nginx + Gunicorn + Redis.
- ⬜ Redis на сервері: лише localhost, `DJANGO_REDIS_URL` у `.env` ([DEPLOYMENT.md](DEPLOYMENT.md) §5а); без нього служба не стартує.
- ⬜ Сервер: Python ≥ 3.10, PostgreSQL, nginx, certbot ([DEPLOYMENT.md](DEPLOYMENT.md) §1–4).
- ⬜ `.env` на сервері: `DJANGO_DEBUG=False`, власний `DJANGO_SECRET_KEY`, домени в `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS`,
  `DJANGO_BEHIND_PROXY=True`, `DJANGO_DB_*`, `SITE_URL=https://…`, права `600`.
- ✅ Контакти замовника (099 105 3309, dominikyarem@gmail.com) — за замовчуванням; змінюються в `.env`.
- ✅ Адмінка зі стилями при `DEBUG=False` (WhiteNoise) — **прийнята замовником (25.11)**, не змінюється.
- ⬜ `python manage.py setup_moderators`; акаунти модераторів (`--add <username>`), їхні Telegram ID (`--telegram`).
- ✅ Telegram-бот: перевірено в реальній приватній групі з demo-базою, підтверджено замовником (25.11).
- ⬜ Telegram на production: `TELEGRAM_*` у серверному `.env`, служба `mobihome-telegram-bot` ([TELEGRAM_SETUP.md](TELEGRAM_SETUP.md));
  перевірити: нове оголошення → повідомлення в групі → «Підтвердити»/«Відхилити». Один polling-процес на токен.
- ⬜ Тариф «Найкращі оголошення» (99 грн, 7 днів, підготовлено): підтвердження замовника → увімкнути в адмінці «Тарифи».
- ⬜ Посилання на соцмережі в підвалі (`facebook.com/mobi.home.ua`, `instagram.com/mobi.home.ua`) — підтвердити, що сторінки існують.
- ⬜ SMTP (`DJANGO_EMAIL_*`) — інакше листи відновлення пароля не надсилаються.
- ⬜ HTTPS працює → `DJANGO_SECURE_HSTS_SECONDS=31536000`.
- ✅ Локальні резервні копії перед релізом (25.11): `data/backups/db-20261125-2259.sqlite3` (integrity ok), `data/backups/media-20261125-2259.tar.gz` (gzip ok, 7 файлів).
- ⬜ Резервні копії БД/медіа за розкладом + копія поза сервером; перевірити відновлення з копії.
- ⬜ `GET /healthz/` під зовнішнім моніторингом.
- ⬜ Перенесення наявних даних із `data/db.sqlite3` (якщо потрібні) — [DEPLOYMENT.md](DEPLOYMENT.md) §7.
- ⬜ Створити адміністраторів; дозвіл «Може закривати перевірку подвійного списання» — лише відповідальним.

## Оплата (окреме рішення)
- ✅ Логіка LiqPay, просування, звірка — автотести; реальний Sandbox Status API (`payment_not_found`, формат відповіді).
- ⬜ Ручний Sandbox E2E з callback через публічний HTTPS (тунель — [DEPLOYMENT.md](DEPLOYMENT.md) §8):
  успіх, відмова, скасування, `try_again`, втрачений callback + `liqpay_sync_pending`, ТОП, схвалення оплаченого оголошення.
- ✅ Ціни затверджені замовником: 80 / 100 / 120 / 200 грн (ТОП-1 на добу — 100 грн).
- ⬜ Реальне зарахування коштів: оформлення мерчанта (ФОП/юрособа, рахунок), підключення й погодження магазину в LiqPay.
- ⬜ Бойові ключі, `LIQPAY_SANDBOX=False`, `PAYMENTS_ENABLED=True`, таймер `liqpay-sync`, контрольна оплата + повернення.
- ⬜ `PLACEMENT_PAYMENT_REQUIRED` лишати `False`: обов'язкова оплата розміщення суперечить вимозі безкоштовної публікації.

## Після запуску
- ⬜ Перевірити callback ззовні: GET → 405, POST без підпису → 400.
- ⬜ Перші реальні оголошення: модерація, показ у каталозі, мобільні пристрої (iOS Safari, Android Chrome).
- ⬜ Адмінка → Платежі → «Потребує перевірки» — переглядати регулярно.
