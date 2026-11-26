# Матриця вимог замовника — mobi Home

Статуси й пояснення — у [FINAL_CLIENT_REPORT.md](FINAL_CLIENT_REPORT.md). Тут — докази: автотести (модуль · клас) і ручні перевірки.

| # | Вимога | Статус | Автотести | Інші докази |
|---|---|---|---|---|
| 1 | Адмінка з CSS при `DEBUG=False` | PASS — прийнято замовником (25.11) | `test_moderation · ContactsAndStaticTests.test_admin_static_served_without_debug` | Chrome: `/static/admin/css/base.css` 200, 0× 404 статики, знімки адмінки (сайт і модератор) |
| 2 | Контакти замовника | PASS | `test_moderation · ContactsAndStaticTests.test_client_contacts_everywhere` | Пошук старих контактів у шаблонах/коді/JS — 0 |
| 3 | PostgreSQL | PASS | Увесь набір (396) на PostgreSQL 18; конкурентні тести 3× | SQLite→PostgreSQL і `pg_dump`/`pg_restore` на тестових копіях — кількості збігаються |
| 4 | Роль «Модератори» | PASS | `test_moderation · ModeratorRoleTests` (13) | Chrome: модератор бачить лише дозволені розділи; поля оголошення — перегляд |
| 5 | Черга й життєвий цикл модерації | PASS | `ModerationLifecycleTests.test_full_cycle_…`, `test_release · CreateListingTests` | Chrome: «Черга модерації» |
| 6 | Причина відхилення, видима власнику | PASS | `ModeratorRoleTests.test_reject_requires_reason_…`, `test_reject_action_asks_reason`, повний цикл | — |
| 7 | Історія модерації | PASS | повний цикл (послідовність подій і джерел) | — |
| 8 | Одночасні рішення | PASS | `test_concurrent_decisions_only_first_wins`, `test_stale_form_version_rejected` | — |
| 9 | Онлайн-перевірка | PASS | `VerificationTests` | — |
| 10 | Виїзна перевірка, приватні звіти | PASS | `VerificationTests`, `test_verified_flag_requires_passed_verification` | — |
| 11 | Telegram-бот | PASS | `TelegramBotTests` (7), `test_telegram_photos · TelegramPhotoTests` (19), імітація Bot API | Реальна приватна група з demo-базою — **підтверджено замовником (25.11)**; окремого підтвердження кожного сценарію з фото (1 фото, альбом, понад 10 фото, пошкоджений файл) не зафіксовано — позначено окремо |
| 12 | Безкоштовна публікація | PASS | `test_promotions · test_publication_page_lists_free_and_all_tariffs`, `LegacyTopTests` | Chrome: сторінка публікації |
| 13 | Тарифи 80/100/120/200 | PASS | `test_promotions` (18) | — |
| 14 | Тариф 99 «Найкращі оголошення» (7 днів) | PARTIAL | `test_best_block_prepared_but_not_sold`, `test_best_block_payment_never_approves_or_verifies` | Міграції 0032–0033; продаж вимкнено до підтвердження |
| 15 | Оплата ≠ модерація / «Перевірене» | PASS | `test_payment_never_approves`, `test_promotion_waits_for_moderation` | — |
| 16 | Політика ТОП | PASS | `test_catalog_order_top1_top3_active_then_regular` | Описано в FINAL_CLIENT_REPORT.md |
| 17 | LiqPay реальні платежі | BLOCKED | `test_liqpay`, `test_liqpay_hardening`, `test_payment_lifecycle` | Sandbox Status API 22.11; callback/production — потрібні HTTPS і мерчант |
| 18 | Темна тема | PASS | `ContactsAndStaticTests` (адмінка), `test_release · test_custom_404_page` | Chrome: 0 світлих блоків на сторінках сайту |
| 19 | Мобільна версія | PASS (емуляція) | — | Chrome 320–1440 px: 0 px горизонтального скролу на сторінках сайту; адмінка 768 px — стандартна таблиця Django |
| 20 | Українська мова | PASS | `DemoFindingsRegressionTests` | Вичитано 693 тексти, виправлено ~28 помилок (зокрема 3 за дозволом 25.11) |
| 21 | Квартири, пошук, фільтри | PASS | `test_release · CatalogSearchFilterTests`, `test_listing_display` | — |
| 22 | CSS карток | PASS | — | Chrome 320–1440 px: без скролу; кнопки карток на одному рівні (1440 px) |
| 23 | DOM.RIA / AUTO.RIA | PARTIAL | — | AUTO.RIA — 3 сторінки прочитано; DOM.RIA — DNS-помилка |
| 24 | Хостинг | PASS | — | `systemd-analyze verify`, `check --deploy`, `collectstatic` |
| 25 | Тестування | PASS | 396 тестів | [TEST_REPORT.md](TEST_REPORT.md) |

Браузерна перевірка demo-сервера (Chrome, `DEBUG=False`, 25.11): повний сценарій — створення → «На перевірці» →
немає в каталозі → черга модератора → відхилення з причиною → власник бачить причину → виправлення →
схвалення → у каталозі → модератору заборонено платежі/користувачів/тарифи/групи/видалення (403) — **усе PASS**.
