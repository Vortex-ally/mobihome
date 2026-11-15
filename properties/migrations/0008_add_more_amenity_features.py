import uuid

from django.db import migrations
from django.utils.text import slugify

FEATURES_TO_ADD = [
    "Можна з тваринами",
    "Бойлер",
    "Пральна машина",
    "Є резервне живлення",
    "Кондиціонер",
    "Балкон/лоджія",
    "Посудомийна машина",
    "Вантажний ліфт",
    "Центральне опалення",
    "Wi-Fi",
    "Холодильник",
]


def _unique_slug(Feature, name):
    base = slugify(name, allow_unicode=True) or uuid.uuid4().hex[:8]
    slug, counter = base, 1
    while Feature.objects.filter(slug=slug).exists():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def add_features(apps, schema_editor):
    # Історична модель не викликає кастомний Feature.save(), тому slug генерується тут.
    # Попередня версія створювала записи з порожнім slug і падала на UNIQUE при чистій БД.
    Feature = apps.get_model("properties", "Feature")
    for name in FEATURES_TO_ADD:
        if not Feature.objects.filter(name=name).exists():
            Feature.objects.create(name=name, slug=_unique_slug(Feature, name))


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("properties", "0007_add_featured_until_view_count_contact_verified")]
    operations = [migrations.RunPython(add_features, noop)]
