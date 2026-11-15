import io

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from properties.models import ModerationStatus, Property

PASSWORD = "Str0ng!Passw0rd-xyz"


def make_image(name="photo.png"):
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (200, 100, 50)).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


def make_user(username, **extra):
    return User.objects.create_user(username, f"{username}@example.com", PASSWORD, **extra)


def make_admin(username="boss"):
    return User.objects.create_superuser(username, f"{username}@example.com", PASSWORD)


def make_property(owner=None, public=True, **extra):
    defaults = dict(name="Квартира", address="вул. Тестова, 1", city="Київ", price=1000000)
    if public:
        defaults.update(
            is_published=True,
            moderation_status=ModerationStatus.APPROVED,
            is_verified_by_admin=True,
        )
    else:
        defaults.update(is_published=False, moderation_status=ModerationStatus.PENDING)
    defaults.update(extra)
    return Property.objects.create(owner=owner, **defaults)
