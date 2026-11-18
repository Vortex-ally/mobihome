"""
Перевірка цілісності даних (ЛИШЕ ЧИТАННЯ). Запускати перед `migrate` на реальній БД:

    python manage.py check_data_integrity

Працює через сирий SQL по колонках, що існували до міграції 0021, тож підходить
і для бази зі старою схемою. Нічого не змінює. Персональні дані не виводяться
(лише id оголошень та кількості).
"""

from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

CHECKS = [
    ("Від'ємна ціна (порушить property_price_non_negative)",
     "SELECT id FROM properties_property WHERE price < 0"),
    ("Кімнат < 1 (порушить property_rooms_positive)",
     "SELECT id FROM properties_property WHERE rooms IS NOT NULL AND rooms < 1"),
    ("Площа <= 0 (порушить property_area_positive)",
     "SELECT id FROM properties_property WHERE area_sqm IS NOT NULL AND area_sqm <= 0"),
    ("Рівень доступності поза 1–5 (порушить property_mobility_level_range)",
     "SELECT id FROM properties_property WHERE mobility_level IS NOT NULL "
     "AND (mobility_level < 1 OR mobility_level > 5)"),
]

INFO = [
    ("Оголошення без власника", "SELECT id FROM properties_property WHERE owner_id IS NULL"),
    ("Опубліковано, але не схвалено/не перевірено фото (у каталозі не видно)",
     "SELECT id FROM properties_property WHERE is_published = TRUE AND "
     "(moderation_status <> 'approved' OR is_verified_by_admin = FALSE)"),
    ("Зручності з порожнім slug", "SELECT id FROM properties_feature WHERE slug = '' OR slug IS NULL"),
]


class Command(BaseCommand):
    help = "Перевірка цілісності даних перед міграціями (лише читання)."

    def _ids(self, sql, params=None):
        with connection.cursor() as cur:
            cur.execute(sql, params or [])
            return [row[0] for row in cur.fetchall()]

    def _report(self, title, ids, style):
        if ids:
            shown = ", ".join(str(i) for i in ids[:50]) + (" …" if len(ids) > 50 else "")
            self.stdout.write(style(f"[{len(ids)}] {title}: id {shown}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"[0] {title}"))

    def handle(self, *args, **options):
        tables = connection.introspection.table_names()
        if "properties_property" not in tables:
            self.stdout.write("Таблиці properties_property немає — перевіряти нічого.")
            return

        self.stdout.write(self.style.MIGRATE_HEADING("Блокери міграцій 0021 і 0025:"))
        blockers = 0
        for title, sql in CHECKS:
            ids = self._ids(sql)
            blockers += len(ids)
            self._report(title, ids, self.style.ERROR)
        if "properties_payment" in tables:
            # Блокер 0025: кілька незавершених оплат одного оголошення. Вирішує адміністратор вручну.
            ids = self._ids(
                "SELECT property_id FROM properties_payment WHERE status IN ('created', 'pending') "
                "AND applied_at IS NULL AND error_message = '' GROUP BY property_id HAVING COUNT(*) > 1"
            )
            blockers += len(ids)
            self._report("Кілька незавершених оплат одного оголошення (блокує 0025)", ids, self.style.ERROR)

        self.stdout.write(self.style.MIGRATE_HEADING("Інформація:"))
        for title, sql in INFO:
            self._report(title, self._ids(sql), self.style.WARNING)

        self.stdout.write(self.style.MIGRATE_HEADING("Стара система ТОП (продаж припинено):"))
        now = timezone.now()
        active = self._ids(
            "SELECT id FROM properties_property WHERE is_featured = TRUE "
            "AND (featured_until IS NULL OR featured_until > %s)",
            [now],
        )
        unlimited = self._ids(
            "SELECT id FROM properties_property WHERE is_featured = TRUE AND featured_until IS NULL"
        )
        self._report("Чинний ТОП (діятиме до своєї дати завершення)", active, self.style.WARNING)
        self._report("ТОП без дати завершення (безстроковий — потрібне рішення)", unlimited,
                     self.style.WARNING)

        if "properties_payment" in tables:
            with connection.cursor() as cur:
                cur.execute("SELECT status, COUNT(*) FROM properties_payment GROUP BY status")
                rows = cur.fetchall()
            self.stdout.write(self.style.MIGRATE_HEADING("Платежі за статусами:"))
            for status, count in rows or [("—", 0)]:
                self.stdout.write(f"  {status}: {count}")

        if blockers:
            self.stdout.write(self.style.ERROR(
                f"\nЗнайдено {blockers} записів, що заблокують міграції 0021/0025. "
                "Виправте їх вручну (через адмінку) перед `migrate`. Команда нічого не змінювала."
            ))
        else:
            self.stdout.write(self.style.SUCCESS("\nБлокерів міграції не знайдено."))
