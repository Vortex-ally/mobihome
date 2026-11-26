# Розгортання MobiHome на Ubuntu Server

Схема: **nginx (HTTPS) → Gunicorn (systemd, unix-сокет) → Django → PostgreSQL + Redis (ліміти)**; Telegram-бот і звірка LiqPay —
окремі systemd-служби. Шаблони — у [`deploy/`](deploy/). Нижче: проєкт у `/srv/mobihome`, системний користувач
`mobihome`, домен `mobihome.example` — замініть на свої. Підходить Ubuntu 22.04 / 24.04 LTS (Python 3.10 / 3.12).

> **Не виконувалось на реальному сервері.** Локально перевірено (26.11.2026): `check --deploy`, `collectstatic`,
> Gunicorn через unix-сокет за nginx (`nginx -t` і запити в ізольованому контейнері nginx), міграції 0028–0034
> вперед/назад на копії основної бази, перенесення копії основної SQLite у PostgreSQL 18 (кількості збігаються),
> `systemd-analyze verify` юнітів. Кроки з позначкою **[сервер]** можна перевірити лише на сервері.

> Ліміти спроб входу й форм рахуються **окремо для кожного відвідувача** (X-Real-IP від nginx) у **спільному Redis**
> для всіх воркерів Gunicorn — перевірено 26.11 через nginx + Gunicorn + Redis (§19).

## 1. Системні залежності [сервер]

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-dev build-essential git nginx postgresql \
    redis-server certbot sqlite3 rsync ufw
sudo timedatectl set-timezone Europe/Kyiv
sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable
```

## 2. Користувач і клонування репозиторію [сервер]

```bash
sudo adduser --system --group --home /srv/mobihome --shell /usr/sbin/nologin mobihome
sudo -u mobihome git clone https://github.com/<власник>/mobihome.git /srv/mobihome
cd /srv/mobihome && sudo -u mobihome git checkout <гілка-або-тег-релізу>
# nginx (www-data) має читати static/ і media/, але не .env, data/, backups/:
sudo chown mobihome:www-data /srv/mobihome && sudo chmod 750 /srv/mobihome
sudo -u mobihome mkdir -p media staticfiles data/private backups
sudo chmod 750 data data/private backups
```

Репозиторій приватний → deploy key (лише читання) або HTTPS-токен; ключі — не в репозиторії.

## 3. Python virtual environment [сервер]

```bash
cd /srv/mobihome
sudo -u mobihome python3 -m venv venv
```

## 4. Залежності Python [сервер]

```bash
sudo -u mobihome venv/bin/pip install --upgrade pip
sudo -u mobihome venv/bin/pip install -r requirements.txt "psycopg[binary]"
```

`psycopg` потрібен лише для PostgreSQL, тому ставиться окремо.

## 5. PostgreSQL [сервер]

```bash
sudo -u postgres psql <<'SQL'
CREATE USER mobihome WITH PASSWORD '<надійний-пароль>';
CREATE DATABASE mobihome OWNER mobihome ENCODING 'UTF8' TEMPLATE template0;
SQL
# для pg_dump без пароля в командах (резервні копії):
sudo -u mobihome sh -c 'echo "127.0.0.1:5432:mobihome:mobihome:<надійний-пароль>" > ~/.pgpass && chmod 600 ~/.pgpass'
```

PostgreSQL слухає лише `localhost` (типово в Ubuntu) — порт 5432 назовні не відкривайте.

### 5а. Redis — спільний кеш лімітів [сервер]

```bash
sudo systemctl enable --now redis-server
grep -E '^(bind|protected-mode|port)' /etc/redis/redis.conf   # bind 127.0.0.1 -::1 · protected-mode yes · port 6379
sudo ss -ltnp | grep 6379                                       # лише 127.0.0.1 / [::1], НЕ 0.0.0.0
redis-cli ping                                                   # PONG
```

- Redis **не відкривати** в ufw/хмарному firewall; доступ лише з цього сервера.
- Пароль (рекомендовано): `requirepass <пароль>` у `/etc/redis/redis.conf`, `sudo systemctl restart redis-server`,
  у `.env`: `DJANGO_REDIS_URL=redis://:<пароль>@127.0.0.1:6379/1` (URL — секрет, лише в `.env`).
- Дані Redis тут — лише тимчасові лічильники; резервне копіювання не потрібне.
- Redis недоступний → сайт працює (вхід і форми відповідають за ~1 с), ліміти тимчасово не діють, у журналі
  `Кеш Redis недоступний` (`journalctl -u mobihome | grep Redis`). Без `DJANGO_REDIS_URL` служба `mobihome`
  не стартує (`properties.E010` у `check --deploy`) — тихого переходу на локальний кеш немає.

## 6. Production `.env` [сервер]

```bash
cd /srv/mobihome
sudo -u mobihome cp .env.example .env && sudo chmod 600 .env && sudo chown mobihome:mobihome .env
sudo -u mobihome nano .env
python3 -c "import secrets; print(secrets.token_urlsafe(64))"   # значення для DJANGO_SECRET_KEY
```

| Змінна | Значення |
|---|---|
| `DJANGO_DEBUG` | `False` |
| `DJANGO_SECRET_KEY` | новий випадковий ключ (не з локального `.env`) |
| `DJANGO_ALLOWED_HOSTS` | `mobihome.example,www.mobihome.example` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://mobihome.example,https://www.mobihome.example` |
| `DJANGO_BEHIND_PROXY` | `True` (за nginx) — адреса клієнта з `X-Real-IP` від nginx |
| `DJANGO_REDIS_URL` | `redis://127.0.0.1:6379/1` (з паролем — `redis://:<пароль>@127.0.0.1:6379/1`) — **обов'язково** |
| `DJANGO_TRUSTED_PROXY_IPS` | не задавати (типово `127.0.0.1,::1`; unix-сокет довірений завжди) |
| `DJANGO_SECURE_SSL_REDIRECT`, `DJANGO_SECURE_COOKIES` | не задавати або `True` (при `DEBUG=False` це типово) |
| `DJANGO_SECURE_HSTS_SECONDS` | `0` спершу; `31536000` після перевірки HTTPS (§13) |
| `DJANGO_SERVE_MEDIA` | `False` — `/media/` віддає nginx |
| `DJANGO_DB_ENGINE`, `DJANGO_DB_NAME`, `DJANGO_DB_USER`, `DJANGO_DB_PASSWORD`, `DJANGO_DB_HOST`, `DJANGO_DB_PORT` | `postgresql`, `mobihome`, `mobihome`, пароль, `127.0.0.1`, `5432` |
| `DJANGO_MEDIA_ROOT`, `DJANGO_STATIC_ROOT` | `/srv/mobihome/media`, `/srv/mobihome/staticfiles` |
| `DJANGO_PRIVATE_MEDIA_ROOT` | `/srv/mobihome/data/private` — звіти перевірок житла (вебсервер їх не роздає) |
| `SITE_URL` | `https://mobihome.example` (без `/` у кінці) |
| `SITE_PHONE`, `SITE_OFFICE_PHONE`, `SITE_EMAIL`, `SITE_SUPPORT_EMAIL` | необов'язково: типово +380991053309 / dominikyarem@gmail.com |
| `DJANGO_EMAIL_*`, `DJANGO_DEFAULT_FROM_EMAIL` | SMTP — інакше листи відновлення пароля не надсилаються |
| `TELEGRAM_BOT_ENABLED`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_MODERATION_CHAT_ID` | див. §14 |
| `PAYMENTS_ENABLED` / `PLACEMENT_PAYMENT_REQUIRED` / `LIQPAY_SANDBOX` | `False` / `False` / `True` до §18 |

Секрети — лише в `/srv/mobihome/.env` (права `600`). Сервер не стартує без `DJANGO_SECRET_KEY` при `DEBUG=False`
і при суперечливій конфігурації оплати.

## 7. Міграції [сервер]

```bash
cd /srv/mobihome
sudo -u mobihome venv/bin/python manage.py check --deploy
sudo -u mobihome venv/bin/python manage.py migrate --plan
sudo -u mobihome venv/bin/python manage.py migrate
sudo -u mobihome venv/bin/python manage.py setup_moderators     # група «Модератори» (ідемпотентно)
```

Якщо переносите наявні дані (§8) — виконайте §8 **замість** звичайного `migrate` на порожній базі
(у §8 `migrate` уже є). Новий сайт без даних: `sudo -u mobihome venv/bin/python manage.py createsuperuser`.

## 8. Безпечне перенесення основної SQLite-бази в PostgreSQL

Перевірено локально 26.11 на копії основної бази → PostgreSQL 18: 93 об'єкти; Users 4, Property 6, Payment 5,
PaymentEvent 40, Tariff 6, Feature 27 — збігаються; `check_data_integrity` без блокерів; нові id продовжуються.

**На комп'ютері розробника** (основна база лише читається, SQLite backup API):

```bash
cd ~/Desktop/mobihome
python3 -c "import sqlite3; s=sqlite3.connect('file:data/db.sqlite3?mode=ro', uri=True); d=sqlite3.connect('data/backups/db-for-server.sqlite3'); s.backup(d); d.close(); print('ok')"
sqlite3 data/backups/db-for-server.sqlite3 "PRAGMA integrity_check;"     # ok
scp data/backups/db-for-server.sqlite3 <user>@<server>:/tmp/
```

**На сервері** (сайт ще не запущено або зупинено: `sudo systemctl stop mobihome mobihome-telegram-bot`):

```bash
cd /srv/mobihome
sudo install -o mobihome -g mobihome -m 600 /tmp/db-for-server.sqlite3 backups/db-for-server.sqlite3 && sudo rm /tmp/db-for-server.sqlite3
# 1) копію SQLite довести до схеми цього коду (основна база локально — на 0027; потрібні 0028–0034)
sudo -u mobihome env DJANGO_DB_ENGINE= DJANGO_SQLITE_PATH=backups/db-for-server.sqlite3 venv/bin/python manage.py migrate
# 2) вивантаження з копії SQLite (порожній DJANGO_DB_ENGINE має пріоритет над .env → SQLite)
sudo -u mobihome env DJANGO_DB_ENGINE= DJANGO_SQLITE_PATH=backups/db-for-server.sqlite3 venv/bin/python manage.py dumpdata \
    --natural-foreign --natural-primary -e contenttypes -e auth.Permission -e admin.logentry -e sessions \
    --indent 1 -o backups/sqlite-dump.json
sudo chmod 600 backups/sqlite-dump.json
# 3) схема PostgreSQL (DJANGO_DB_* з .env) і завантаження
sudo -u mobihome venv/bin/python manage.py migrate
sudo -u mobihome venv/bin/python manage.py loaddata backups/sqlite-dump.json
sudo -u mobihome venv/bin/python manage.py check_data_integrity
sudo -u mobihome venv/bin/python manage.py setup_moderators
```

Порівняйте кількості (`manage.py shell`: `User`, `Property`, `Payment`, `PaymentEvent`, `Tariff`) з локальними.
Не переносяться: сесії (користувачі входять знову) і журнал дій адмінки (`admin.logentry`).
Файли `backups/db-for-server.sqlite3` і `backups/sqlite-dump.json` містять персональні дані — зберігати з правами `600`,
видалити після успішного запуску й перевірки резервної копії PostgreSQL.

## 9. Медіафайли [сервер]

```bash
# з комп'ютера розробника (фото оголошень; приватні звіти — лише якщо є data/private/)
rsync -av --chmod=D755,F644 ~/Desktop/mobihome/media/ <user>@<server>:/tmp/mobihome-media/
# на сервері
sudo rsync -a /tmp/mobihome-media/ /srv/mobihome/media/ && sudo chown -R mobihome:mobihome /srv/mobihome/media && sudo rm -rf /tmp/mobihome-media
# приватні звіти (якщо є): у /srv/mobihome/data/private/, власник mobihome, права 750/640 — НЕ в media/
```

Звірка: `find /srv/mobihome/media -type f | wc -l` = кількості локально (`find media -type f | wc -l`).

## 10. Collectstatic [сервер]

```bash
sudo -u mobihome venv/bin/python manage.py collectstatic --noinput     # локально: 136 файлів
```

`/static/` віддає nginx; якщо запит дійде до Django — віддасть WhiteNoise (адмінка не залишиться без стилів).

## 11. Gunicorn і systemd [сервер]

```bash
sudo cp deploy/mobihome.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mobihome
systemctl status mobihome --no-pager
sudo -u www-data curl -s --unix-socket /run/mobihome/gunicorn.sock -H 'Host: mobihome.example' -H 'X-Forwarded-Proto: https' http://localhost/healthz/
```

3 воркери, timeout 120 с, логи — у journald. Gunicorn слухає **лише unix-сокет** `/run/mobihome/gunicorn.sock`
(каталог `0750`, власник `mobihome:www-data`) — з мережі й від інших користувачів недоступний; TCP-порт не відкривається.
Перед стартом служба виконує `check --deploy --fail-level ERROR` (без Redis, ключа, з помилкою оплати — не стартує:
`journalctl -u mobihome -n 30`). Перезапуск без простою: `sudo systemctl reload mobihome`.

## 12. Nginx [сервер]

```bash
sudo sed 's/mobihome.example/<ваш-домен>/g' deploy/nginx-mobihome.conf | sudo tee /etc/nginx/sites-available/mobihome >/dev/null
sudo mkdir -p /var/www/certbot
sudo rm -f /etc/nginx/sites-enabled/default
```

Конфіг вмикається після сертифіката (§13), бо посилається на файли сертифіката. Локально `nginx -t` — успішно
(nginx 1.31 попереджає, що `listen … http2` застарілий; для nginx 1.18/1.24 з Ubuntu цей синтаксис правильний).
`/media/` — лише публічні фото; `data/private` nginx не роздає; ліміт завантаження — 220 МБ на запит.
nginx перезаписує `X-Real-IP` і `X-Forwarded-For` своїм `$remote_addr` (підробка клієнтом не діє); `proxy_read_timeout` — 120 с.

## 13. Домен і HTTPS [сервер]

1. У DNS-зоні домену: записи `A` (і `AAAA`, якщо є IPv6) для `mobihome.example` і `www` → IP сервера.
   Перевірка: `dig +short mobihome.example`.
2. Сертифікат (certbot сам зупиняє nginx на кілька секунд — зараз і при кожному автоматичному поновленні):

```bash
sudo certbot certonly --standalone -d mobihome.example -d www.mobihome.example \
    --pre-hook "systemctl stop nginx" --post-hook "systemctl start nginx" -m <email-власника> --agree-tos
sudo ln -sf /etc/nginx/sites-available/mobihome /etc/nginx/sites-enabled/mobihome
sudo nginx -t && sudo systemctl reload nginx
sudo certbot renew --dry-run
```

3. Перевірка: `curl -sI http://mobihome.example/` → `301` на https; `curl -s https://mobihome.example/healthz/` → `{"status": "ok"}`.
4. Після кількох днів стабільного HTTPS: `DJANGO_SECURE_HSTS_SECONDS=31536000` у `.env`, `sudo systemctl reload mobihome`.
   `check --deploy` далі показує лише W005/W021 (HSTS для піддоменів/preload — рішення власника домену).

## 14. Telegram-бот через systemd [сервер]

```bash
# у .env: TELEGRAM_BOT_ENABLED=True, TELEGRAM_BOT_TOKEN=..., TELEGRAM_MODERATION_CHAT_ID=...
sudo -u mobihome venv/bin/python manage.py setup_moderators --add <логін> --telegram <логін>=<Telegram ID>
sudo systemctl reload mobihome                           # сайт надсилає заявки в групу
sudo -u mobihome venv/bin/python manage.py telegram_moderation_bot --once   # перевірка токена/доступу
sudo cp deploy/telegram-bot.service /etc/systemd/system/mobihome-telegram-bot.service
sudo systemctl daemon-reload && sudo systemctl enable --now mobihome-telegram-bot
journalctl -u mobihome-telegram-bot -f
```

**Один polling-процес на токен.** Перед увімкненням на сервері зупиніть локального demo-бота з тим самим токеном
(інакше Telegram повертає `409 Conflict` і натискання обробляє випадковий процес). На production бот працює
з production-базою; demo-база на сервер не переноситься. Деталі — [TELEGRAM_SETUP.md](TELEGRAM_SETUP.md).

## 15. Резервне копіювання [сервер]

`crontab -e` від користувача `mobihome` (`sudo -u mobihome crontab -e`):

```cron
30 3 * * * pg_dump -Fc -h 127.0.0.1 -U mobihome mobihome > /srv/mobihome/backups/db-$(date +\%F).dump && find /srv/mobihome/backups -name 'db-*.dump' -mtime +14 -delete
45 3 * * 0 tar -czf /srv/mobihome/backups/media-$(date +\%F).tar.gz -C /srv/mobihome media data/private && find /srv/mobihome/backups -name 'media-*.tar.gz' -mtime +60 -delete
```

- Копії **поза сервером** (інший сервер/хмара): `rsync`/`rclone` каталогу `backups/` щодня; без цього пошкодження диска = втрата даних.
- Перевірка відновлення (раз на місяць, у тимчасову базу):
  `createdb -h 127.0.0.1 -U mobihome mobihome_restore_test && pg_restore -h 127.0.0.1 -U mobihome -d mobihome_restore_test backups/db-<дата>.dump && dropdb -h 127.0.0.1 -U mobihome mobihome_restore_test`
  (користувачу `mobihome` потрібне право `CREATEDB` або виконайте від `postgres`).

## 16. Перевірка працездатності [сервер]

```bash
curl -s https://mobihome.example/healthz/                                               # {"status": "ok"}
curl -s -o /dev/null -w '%{http_code}\n' http://mobihome.example/                       # 301
curl -s -o /dev/null -w '%{http_code}\n' https://mobihome.example/static/admin/css/base.css   # 200
curl -s -o /dev/null -w '%{http_code}\n' https://mobihome.example/payments/liqpay/callback/   # 405
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://mobihome.example/payments/liqpay/callback/   # 400
curl -s -o /dev/null -w '%{http_code}\n' 'https://mobihome.example/media/../data/private/x'   # 404
redis-cli -n 1 --scan --pattern '*login-fail*' | head                                   # ключі з різними IP, а не «unknown»
sudo ss -ltnp | grep -E ':(6379|8000)\b'                                                # лише 127.0.0.1/[::1]
systemctl --failed; journalctl -u mobihome -p warning -n 50 --no-pager
```

Вручну: головна, каталог, оголошення з фото, вхід, адмінка зі стилями, подання оголошення → заявка в Telegram →
«Підтвердити» → оголошення в каталозі. `GET /healthz/` — під зовнішній моніторинг (UptimeRobot тощо).

Логи: застосунок і Gunicorn — `journalctl -u mobihome`; бот — `journalctl -u mobihome-telegram-bot`;
звірка — `journalctl -u liqpay-sync`; nginx — `/var/log/nginx/`. Платежі «Потребує перевірки» — адмінка → Платежі.

## 17. План rollback

**Перед кожним оновленням:**

```bash
cd /srv/mobihome
PREV=$(git rev-parse HEAD); echo "$PREV" | sudo -u mobihome tee backups/prev-commit
sudo -u mobihome pg_dump -Fc -h 127.0.0.1 -U mobihome mobihome > backups/pre-deploy-$(date +%F-%H%M).dump
sudo -u mobihome git pull
sudo -u mobihome venv/bin/pip install -r requirements.txt "psycopg[binary]"
sudo -u mobihome venv/bin/python manage.py migrate --plan && sudo -u mobihome venv/bin/python manage.py migrate
sudo -u mobihome venv/bin/python manage.py collectstatic --noinput
sudo -u mobihome venv/bin/python manage.py check --deploy
sudo systemctl reload mobihome && sudo systemctl restart mobihome-telegram-bot
curl -s https://mobihome.example/healthz/
```

**Відкат коду** (міграції 0028–0034 лише додають поля/таблиці/тарифи; попередній код працює з новою схемою):
`sudo -u mobihome git checkout "$(cat backups/prev-commit)" && sudo systemctl reload mobihome && sudo systemctl restart mobihome-telegram-bot`.

**Відкат схеми** (якщо потрібно повернутись до коду до 0028): спершу `manage.py migrate properties 0027`
(локально перевірено: 0034→0027→0034 на копії основної бази без помилок), потім `git checkout`.

**Відкат даних** (лише якщо дані пошкоджено; втрачаються зміни після копії):

```bash
sudo systemctl stop mobihome mobihome-telegram-bot liqpay-sync.timer
sudo -u mobihome pg_restore --clean --if-exists -h 127.0.0.1 -U mobihome -d mobihome backups/pre-deploy-<дата>.dump
sudo -u mobihome git checkout "$(cat backups/prev-commit)"
sudo systemctl start mobihome mobihome-telegram-bot liqpay-sync.timer
```

Перший запуск: до перенесення даних можна повернутись на локальну SQLite — вона не змінюється (§8 лише читає копію).

## 18. LiqPay після отримання production-ключів

Передумови: оформлений мерчант (ФОП/юрособа, рахунок), магазин погоджено LiqPay, працює HTTPS (§13).

1. Тестовий режим на сервері: sandbox-ключі в `.env`, `LIQPAY_SANDBOX=True`, `PAYMENTS_ENABLED=True` →
   `reload` → ручний Sandbox E2E за [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) (успіх, відмова, скасування,
   втрачений callback + `liqpay_sync_pending`, ТОП, схвалення оплаченого оголошення).
2. Звірка за таймером:
   ```bash
   sudo cp deploy/liqpay-sync.service deploy/liqpay-sync.timer /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now liqpay-sync.timer
   systemctl list-timers liqpay-sync.timer
   ```
3. Бойовий режим — лише з окремого рішення власника: бойові `LIQPAY_PUBLIC_KEY`/`LIQPAY_PRIVATE_KEY`,
   `LIQPAY_SANDBOX=False`, `PAYMENTS_ENABLED=True`, `PLACEMENT_PAYMENT_REQUIRED=False` (публікація безкоштовна),
   `sudo systemctl reload mobihome`, контрольна оплата мінімального тарифу + повернення, перевірка в адмінці «Платежі».
4. Ціни: 80 / 100 / 120 / 200 грн; тариф 99 грн «Найкращі оголошення» — вимкнений до підтвердження замовника.

Сервер не стартує, якщо `PAYMENTS_ENABLED=True` без ключів або `PLACEMENT_PAYMENT_REQUIRED=True` без `PAYMENTS_ENABLED`
(перевірено локально 26.11).

Локальна перевірка callback через тунель (без сервера) — [PAYMENTS_OPERATIONS.md](PAYMENTS_OPERATIONS.md), розд. 3;
варіанти тунелю: ngrok (`ngrok http 8003`), localhost.run (`ssh -R 80:localhost:8003 nokey@localhost.run`),
тестовий піддомен на VPS з `.env.sandbox`, Cloudflare named tunnel.

## 19. Відомі обмеження перед запуском

**Виправлено 26.11.2026 (перевірено nginx 1.31 + Gunicorn 25.1 + Redis 7.4, клієнти з різних IP):**
1. Адреса клієнта: за unix-сокетом `REMOTE_ADDR` порожній, тож раніше всі відвідувачі мали один лічильник
   (до виправлення: клієнт B заблокований після помилок клієнта A). Тепер — `X-Real-IP` від nginx, лише при
   `DJANGO_BEHIND_PROXY=True` і лише від довіреного proxy (unix-сокет або `DJANGO_TRUSTED_PROXY_IPS`), перевірка IPv4/IPv6;
   `X-Forwarded-For` не використовується. Підроблені клієнтом заголовки nginx перезаписує (обійти ліміт не вдалося).
2. Спільний кеш: Redis (`properties.cache.ResilientRedisCache`): атомарний `incr` без створення лічильника без
   терміну дії; 2 воркери — спільні лічильники з TTL; недоступний Redis — сайт працює, ліміти тимчасово не діють.

**Залишкові обмеження:**
3. **Telegram під час збереження оголошення.** Заявка надсилається в тому ж запиті (після запису в БД). Ліміти
   мережевих запитів бота: 15 с на текстове повідомлення, 60 с на завантаження фото/альбому; до 21 фото —
   до 3 завантажень. Timeout Gunicorn і nginx — 120 с. Якщо Telegram дуже повільний і запит обірвано:
   оголошення **збережене** (на модерації), запис заявки лишається «надсилається», через 10 хв бот надсилає її повторно
   (до 5 спроб) — перевірено тестами. Якщо Telegram встиг прийняти частину альбому до обриву, повторне надсилання
   може дати **дубль** фото — при неоднозначному результаті мережевого запиту відсутність дублів не гарантується.
   Повне усунення — фонове надсилання (черга), не впроваджувалось на цьому етапі.
4. Ліміти для IPv6 рахуються на окрему адресу (не на мережу /64).
