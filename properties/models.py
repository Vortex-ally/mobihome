"""
Моделі платформи «Доступний Дім».
"""

from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

SCORE_VALIDATORS = [MinValueValidator(1), MaxValueValidator(10)]


def score_to_status(score):
    """
    Перетворює бал (1–10) на текстовий статус доступності.
    9–10: Ідеально
    6–8: Потребує покращення
    3–5: Важкодоступно
    1–2: Недоступно
    """
    if score is None:
        return None
    if score >= 9:
        return "ideal"
    if score >= 6:
        return "needs_improvement"
    if score >= 3:
        return "difficult"
    return "inaccessible"


class ListingType(models.TextChoices):
    """Тип оголошення."""

    SALE = "sale", "Продаж"
    RENT = "rent", "Оренда"


class Feature(models.Model):
    """Фішка / зручність об'єкта (балкон, паркінг, пандус тощо)."""
    CATEGORY_CHOICES = [
        ('apartment', 'В квартирі'),
        ('building', 'В будинку та на території'),
        ('rules', 'Правила проживання'),
    ]

    name = models.CharField("Назва", max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True, blank=True)
    category = models.CharField("Категорія", max_length=20, choices=CATEGORY_CHOICES, default='apartment')

    class Meta:
        verbose_name = "Зручність"
        verbose_name_plural = "Зручності"
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify
            import uuid
            self.slug = slugify(self.name, allow_unicode=True)
            if not self.slug:
                self.slug = str(uuid.uuid4())[:8]
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class ModerationStatus(models.TextChoices):
    PENDING = "pending", "Очікує модерації"
    APPROVED = "approved", "Схвалено"
    REJECTED = "rejected", "Відхилено"


class PropertyQuerySet(models.QuerySet):
    """Кастомний QuerySet для Property з підтримкою жорсткого фільтра публічного каталогу."""

    def public(self):
        """
        Публічний каталог: опубліковано, схвалено модерацією, фото перевірено адміном,
        не в архіві. Якщо settings.PLACEMENT_PAYMENT_REQUIRED=True — ще й оплачене
        розміщення діє (placement_until > now).
        """
        from django.utils import timezone

        qs = self.filter(
            is_published=True,
            moderation_status=ModerationStatus.APPROVED,
            is_verified_by_admin=True,
            archived_at__isnull=True,
        )
        if getattr(settings, "PLACEMENT_PAYMENT_REQUIRED", False):
            qs = qs.filter(placement_until__gt=timezone.now())
        return qs


class PropertyManager(models.Manager):
    """Менеджер для моделі Property."""

    def get_queryset(self):
        return PropertyQuerySet(self.model, using=self._db)

    def public(self):
        return self.get_queryset().public()


class Property(models.Model):
    """Об'єкт нерухомості."""

    name = models.CharField("Назва", max_length=255)
    listing_type = models.CharField(
        "Тип", max_length=10, choices=ListingType.choices, default=ListingType.SALE
    )
    address = models.CharField("Адреса", max_length=500)
    city = models.CharField("Місто", max_length=100, blank=True)
    price = models.DecimalField(
        "Ціна (грн)",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    rooms = models.PositiveSmallIntegerField(
        "Кількість кімнат",
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(50)],
    )
    area_sqm = models.FloatField(
        "Площа (м²)", null=True, blank=True, validators=[MinValueValidator(1)]
    )
    description = models.TextField("Опис", blank=True)
    photo = models.ImageField("Фото", upload_to="properties/", null=True, blank=True)
    features = models.ManyToManyField(Feature, blank=True, verbose_name="Зручності")
    mobility_level = models.PositiveSmallIntegerField(
        "Рівень доступності (1–5)",
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="1 = батьки з дитиною, 2 = без руки, 3 = крісло, 4 = без руки/ноги, 5 = універсальний",
    )
    floor = models.PositiveSmallIntegerField("Поверх", null=True, blank=True)
    total_floors = models.PositiveSmallIntegerField("Поверхів у будинку", null=True, blank=True)
    is_published = models.BooleanField("Опубліковано", default=False)
    moderation_status = models.CharField(
        "Статус модерації",
        max_length=20,
        choices=ModerationStatus.choices,
        default=ModerationStatus.PENDING,
        help_text="Перевірка оголошення та фотографій житла на доступність/мобільність адміністратором.",
    )
    moderation_note = models.TextField(
        "Коментар модератора",
        blank=True,
        help_text="Зауваження або причина відхилення оголошення",
    )
    is_verified_by_admin = models.BooleanField(
        "Фото перевірено адміном",
        default=False,
        help_text="Оголошення та фотографії підтверджені адміністратором.",
    )
    verified_at = models.DateTimeField(
        "Дата перевірки фото",
        null=True,
        blank=True,
        help_text="Дата та час підтвердження фотомодерації адміністратором.",
    )
    # Стара система ТОП: продаж припинено. Поля збережено для історії та чинних ТОП до їх завершення.
    is_featured = models.BooleanField(
        "ТОП (застаріле)",
        default=False,
        help_text="Стара система ТОП. Новий продаж припинено; чинні ТОП діють до дати завершення.",
    )
    featured_until = models.DateTimeField(
        "ТОП до (застаріле)",
        null=True,
        blank=True,
        help_text="До якої дати діє ТОП (стара система).",
    )
    placement_until = models.DateTimeField(
        "Розміщення оплачено до",
        null=True,
        blank=True,
        db_index=True,
        help_text="Кінець оплаченого періоду розміщення. Змінюється лише успішною оплатою.",
    )
    archived_at = models.DateTimeField(
        "В архіві з",
        null=True,
        blank=True,
        help_text="Оголошення приховане/«видалене» без втрати історії платежів.",
    )
    view_count = models.PositiveIntegerField("Перегляди", default=0)
    contact_phone = models.CharField("Контактний телефон", max_length=30, blank=True)
    is_verified = models.BooleanField(
        "Перевірене оголошення",
        default=False,
        help_text="Позначка, що нерухомість реальна (перевірено адміністрацією).",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="properties",
        verbose_name="Власник оголошення",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PropertyManager()

    class Meta:
        verbose_name = "Об'єкт нерухомості"
        verbose_name_plural = "Об'єкти нерухомості"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["is_published", "moderation_status", "is_verified_by_admin"],
                name="property_public_idx",
            ),
            models.Index(fields=["city"], name="property_city_idx"),
            models.Index(fields=["listing_type", "price"], name="property_type_price_idx"),
            models.Index(fields=["created_at"], name="property_created_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(price__isnull=True) | models.Q(price__gte=0),
                name="property_price_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(rooms__isnull=True) | models.Q(rooms__gte=1),
                name="property_rooms_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(area_sqm__isnull=True) | models.Q(area_sqm__gt=0),
                name="property_area_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(mobility_level__isnull=True)
                | models.Q(mobility_level__gte=1, mobility_level__lte=5),
                name="property_mobility_level_range",
            ),
            models.CheckConstraint(
                condition=models.Q(floor__isnull=True)
                | models.Q(total_floors__isnull=True)
                | models.Q(floor__lte=models.F("total_floors")),
                name="property_floor_lte_total",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def placement_active(self):
        from django.utils import timezone

        return bool(self.placement_until and self.placement_until > timezone.now())

    @property
    def is_archived(self):
        return self.archived_at is not None

    @property
    def featured_active(self):
        """Перевірка чи активна реклама (підвищена видимість)."""
        if hasattr(self, "_featured_active") and self._featured_active is not None:
            return bool(self._featured_active)
        from django.utils import timezone
        if not self.is_featured:
            return False
        if self.featured_until is None:
            return True
        return self.featured_until >= timezone.now()

    @featured_active.setter
    def featured_active(self, value):
        self._featured_active = value

    @property
    def price_per_sqm(self):
        """Розрахунок ціни за 1 м² у гривнях."""
        try:
            if self.price and self.area_sqm and float(self.area_sqm) > 0:
                return round(float(self.price) / float(self.area_sqm), 2)
        except (ValueError, TypeError, ZeroDivisionError):
            pass
        return None

    @property
    def price_per_sqm_usd(self):
        """Розрахунок ціни за 1 м² у USD (за курсом NBU ~41.5 UAH/USD)."""
        per_sqm = self.price_per_sqm
        if per_sqm:
            return round(per_sqm / 41.5, 0)
        return None

    @property
    def price_usd(self):
        """Розрахунок загальної вартості у USD."""
        try:
            if self.price:
                return round(float(self.price) / 41.5, 0)
        except (ValueError, TypeError):
            pass
        return None


class PropertyPhoto(models.Model):
    """Додаткове фото оголошення (галерея)."""

    property = models.ForeignKey(
        Property,
        on_delete=models.CASCADE,
        related_name="extra_photos",
        verbose_name="Оголошення",
    )
    image = models.ImageField("Фото", upload_to="properties/gallery/%Y/%m/")
    order = models.PositiveSmallIntegerField("Порядок", default=0)

    class Meta:
        verbose_name = "Фото оголошення"
        verbose_name_plural = "Фото оголошень"
        ordering = ["order", "id"]

    def __str__(self):
        return f"Фото #{self.order} — {self.property.name}"


class PropertyFavorite(models.Model):
    """Обране оголошення користувача."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="property_favorites",
        verbose_name="Користувач",
    )
    property = models.ForeignKey(
        Property,
        on_delete=models.CASCADE,
        related_name="favorited_by",
        verbose_name="Оголошення",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Обране оголошення"
        verbose_name_plural = "Обрані оголошення"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "property"], name="unique_user_property_favorite"
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.username} — {self.property.name}"


class EntranceStatus(models.TextChoices):
    """Статус доступності входу."""

    INACCESSIBLE = "inaccessible", "Недоступно"
    DIFFICULT = "difficult", "Важкодоступно"
    NEEDS_IMPROVEMENT = "needs_improvement", "Потребує покращення"
    IDEAL = "ideal", "Ідеально"


class BathroomType(models.TextChoices):
    """Тип санвузлу."""

    BATH = "bath", "Ванна"
    SHOWER_TRAY = "shower_tray", "Душ з піддоном"
    SHOWER_DRAIN = "shower_drain", "Душ-трап у підлогу"


class AccessibilityAudit(models.Model):
    """Аудит доступності об'єкта (OneToOne з Property)."""

    property = models.OneToOneField(
        Property, on_delete=models.CASCADE, related_name="audit", verbose_name="Об'єкт"
    )
    # Вхід
    entrance_access = models.PositiveSmallIntegerField(
        "Доступність входу (1–10)", null=True, blank=True, validators=SCORE_VALIDATORS
    )
    entrance_status = models.CharField(
        "Статус входу", max_length=20, choices=EntranceStatus.choices, blank=True
    )
    entrance_comment = models.CharField("Коментар (вхід)", max_length=500, blank=True)
    # Ліфт
    lift_width_cm = models.PositiveIntegerField("Ширина ліфта (см)", null=True, blank=True)
    lift_score = models.PositiveSmallIntegerField(
        "Бал ліфта (1–10)", null=True, blank=True, validators=SCORE_VALIDATORS
    )
    lift_comment = models.CharField("Коментар (ліфт)", max_length=500, blank=True)
    # Санвузол
    bathroom_type = models.CharField(
        "Тип санвузлу", max_length=20, choices=BathroomType.choices, blank=True
    )
    bathroom_comment = models.CharField("Коментар (санвузол)", max_length=500, blank=True)
    # Пороги
    thresholds_max_height_cm = models.FloatField("Макс. висота порогів (см)", null=True, blank=True)
    thresholds_score = models.PositiveSmallIntegerField(
        "Бал порогів (1–10)", null=True, blank=True, validators=SCORE_VALIDATORS
    )
    thresholds_comment = models.CharField("Коментар (пороги)", max_length=500, blank=True)
    # Поворотний діаметр
    turning_radius_exists = models.BooleanField("Наявність радіусу для розвороту", default=False)
    turning_comment = models.CharField("Коментар (розворот)", max_length=500, blank=True)
    # Загальний бал (середнє)
    total_score = models.FloatField("Загальний бал", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Аудит доступності"
        verbose_name_plural = "Аудити доступності"

    def __str__(self):
        return f"Аудит: {self.property.name}"

    def _scores_for_average(self):
        """Збирає бали для розрахунку середнього (тільки числові 1–10)."""
        parts = []
        if self.entrance_access is not None:
            parts.append(self.entrance_access)
        if self.lift_score is not None:
            parts.append(self.lift_score)
        if self.thresholds_score is not None:
            parts.append(self.thresholds_score)
        if self.turning_radius_exists is not None:
            parts.append(10 if self.turning_radius_exists else 0)
        return parts

    def recalculate_total_score(self):
        """Розраховує та зберігає total_score як середнє балів."""
        parts = self._scores_for_average()
        if not parts:
            self.total_score = None
            return
        self.total_score = round(sum(parts) / len(parts), 1)

    def get_status_from_score(self, score):
        """Повертає ключ статусу за балом (для відображення)."""
        return score_to_status(score)

    def compute_mobility_level(self):
        """
        Визначає рівень доступності (1–5).
        1 = базова, 2–4 = проміжні, 5 = універсальна доступність (найвищі вимоги).
        """
        if self.entrance_access is None or self.entrance_access < 3:
            return None
        # Рівень 1: мінімум — вхід >= 3
        level = 1
        if self.entrance_access >= 4:
            level = 2
        # Рівень 3: ліфт >= 70, душ трап/піддон, пороги <= 3 см, вхід >= 5
        lift_ok3 = (self.lift_width_cm or 0) >= 70
        bath_ok3 = self.bathroom_type in (BathroomType.SHOWER_DRAIN, BathroomType.SHOWER_TRAY)
        thresh_ok3 = self.thresholds_max_height_cm is None or self.thresholds_max_height_cm <= 3
        if self.entrance_access >= 5 and lift_ok3 and bath_ok3 and thresh_ok3:
            level = 3
        # Рівень 4: ще краще — пороги до 2 см, вхід >= 7
        thresh_ok4 = self.thresholds_max_height_cm is None or self.thresholds_max_height_cm <= 2
        if (
            self.entrance_access >= 7
            and lift_ok3
            and bath_ok3
            and thresh_ok4
            and self.turning_radius_exists
        ):
            level = 4
        # Рівень 5: ліфт > 80, душ-трап, пороги <= 1, розворот, вхід >= 8
        lift_ok5 = (self.lift_width_cm or 0) > 80
        bath_ok5 = self.bathroom_type == BathroomType.SHOWER_DRAIN
        thresh_ok5 = (
            self.thresholds_max_height_cm is None or self.thresholds_max_height_cm <= 1
        ) or (self.thresholds_score or 0) >= 9
        if (
            self.entrance_access >= 8
            and lift_ok5
            and bath_ok5
            and thresh_ok5
            and self.turning_radius_exists
        ):
            level = 5
        return level

    def save(self, *args, **kwargs):
        if self.entrance_access is not None:
            self.entrance_status = self.get_status_from_score(self.entrance_access) or ""
        self.recalculate_total_score()
        super().save(*args, **kwargs)
        if self.property_id:
            self.property.mobility_level = self.compute_mobility_level()
            self.property.save(update_fields=["mobility_level"])


class ProfileReview(models.Model):
    """Відгук на користувача (профіль)."""

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="given_reviews",
        verbose_name="Автор відгуку",
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="received_reviews",
        verbose_name="Оцінюваний користувач",
    )
    rating = models.PositiveSmallIntegerField(
        "Оцінка", validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    text = models.TextField("Відгук")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Відгук профілю"
        verbose_name_plural = "Відгуки профілів"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["author", "target_user"], name="unique_profile_review"
            ),
        ]

    def __str__(self):
        return f"Відгук {self.author.username} -> {self.target_user.username} ({self.rating}/5)"


class AuditRequestStatus(models.TextChoices):
    NEW = "new", "Нова"
    IN_PROGRESS = "in_progress", "В обробці"
    COMPLETED = "completed", "Завершено"
    CANCELLED = "cancelled", "Скасовано"


class AuditRequest(models.Model):
    """Заявка на замовлення професійного аудиту доступності житла."""

    StatusChoices = AuditRequestStatus
    STATUS_CHOICES = AuditRequestStatus.choices

    name = models.CharField("ПІБ або ім'я замовника", max_length=150)
    phone = models.CharField("Контактний телефон", max_length=30)
    email = models.EmailField("Email", blank=True)
    property_address = models.CharField("Адреса об'єкта нерухомості", max_length=500)
    property = models.ForeignKey(
        Property,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_requests",
        verbose_name="Об'єкт нерухомості (якщо обрано)",
    )
    applicant = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_requests",
        verbose_name="Користувач",
    )
    comments = models.TextField("Побажання / Коментар", blank=True)
    status = models.CharField(
        "Статус обробки",
        max_length=20,
        choices=AuditRequestStatus.choices,
        default=AuditRequestStatus.NEW,
    )
    created_at = models.DateTimeField("Дата створення", auto_now_add=True)
    updated_at = models.DateTimeField("Дата оновлення", auto_now=True)

    class Meta:
        verbose_name = "Заявка на аудит"
        verbose_name_plural = "Заявки на аудит"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Заявка #{self.id} — {self.name} ({self.get_status_display()})"


class ConsultationServiceType(models.TextChoices):
    REALTOR_CERTIFICATION = "realtor_certification", "Для ріелторів"
    DEVELOPER_AUDIT = "developer_audit", "Для забудовників"
    LEGAL_SUPPORT = "legal_support", "Юридичні поради"


class ConsultationRequestStatus(models.TextChoices):
    NEW = "NEW", "Нова"
    IN_PROGRESS = "IN_PROGRESS", "В обробці"
    COMPLETED = "COMPLETED", "Завершено"


class ConsultationRequest(models.Model):
    """Заявка на консалтингові послуги (сертифікація ріелторів, аудит забудовників, юридична підтримка)."""

    service_type = models.CharField(
        "Тип послуги",
        max_length=50,
        choices=ConsultationServiceType.choices,
        default=ConsultationServiceType.REALTOR_CERTIFICATION,
    )
    name = models.CharField("ПІБ", max_length=150)
    phone = models.CharField("Телефон", max_length=30)
    email = models.EmailField("Email", blank=True)
    message = models.TextField("Повідомлення / коментар", blank=True)
    status = models.CharField(
        "Статус обробки",
        max_length=20,
        choices=ConsultationRequestStatus.choices,
        default=ConsultationRequestStatus.NEW,
    )
    created_at = models.DateTimeField("Дата створення", auto_now_add=True)

    class Meta:
        verbose_name = "Заявка на консультацію"
        verbose_name_plural = "Заявки на консультації"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Заявка #{self.id} ({self.get_service_type_display()}) — {self.name} ({self.phone})"


class AuthProvider(models.TextChoices):
    LOCAL = "local", "Локальна реєстрація"
    GOOGLE = "google", "Google OAuth"
    TELEGRAM = "telegram", "Telegram Login"
    DIIA = "diia", "Дія.Підпис"


class UserProfile(models.Model):
    """Розширений профіль користувача з прив'язкою до провайдера авторизації."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
        verbose_name="Користувач",
    )
    auth_provider = models.CharField(
        "Провайдер авторизації",
        max_length=20,
        choices=AuthProvider.choices,
        default=AuthProvider.LOCAL,
    )
    telegram_id = models.CharField(
        "Telegram ID", max_length=64, unique=True, null=True, blank=True
    )
    diia_id = models.CharField(
        "ID у Дії", max_length=64, unique=True, null=True, blank=True
    )
    google_sub = models.CharField(
        "Google ID (sub)", max_length=64, unique=True, null=True, blank=True
    )
    avatar_url = models.URLField(
        "URL Аватарки", max_length=500, null=True, blank=True
    )
    phone = models.CharField("Телефон", max_length=30, blank=True, null=True)

    class Meta:
        verbose_name = "Профіль користувача"
        verbose_name_plural = "Профілі користувачів"

    def __str__(self):
        return f"Профіль {self.user.username} ({self.get_auth_provider_display()})"


from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_or_update_user_profile(sender, instance, created, **kwargs):
    """Автоматичне створення або оновлення UserProfile при збереженні User."""
    UserProfile.objects.get_or_create(user=instance)


class AuthSessionType(models.TextChoices):
    TELEGRAM_QR = "telegram_qr", "Telegram QR Вхід"
    GOOGLE_VERIFY = "google_verify", "Google Verification"
    OTP_RESET = "otp_reset", "Відновлення пароля OTP"


class AuthSessionStatus(models.TextChoices):
    PENDING = "pending", "Очікує підтвердження"
    VERIFIED = "verified", "Підтверджено"
    EXPIRED = "expired", "Застаріло"
    USED = "used", "Використано"


class AuthSession(models.Model):
    """Тимчасова сесія для QR-авторизації Telegram, Google One Tap та OTP відновлення пароля."""

    token = models.CharField("Токен сесії", max_length=64, unique=True, db_index=True)
    session_type = models.CharField(
        "Тип сесії",
        max_length=30,
        choices=AuthSessionType.choices,
        default=AuthSessionType.TELEGRAM_QR,
    )
    status = models.CharField(
        "Статус",
        max_length=20,
        choices=AuthSessionStatus.choices,
        default=AuthSessionStatus.PENDING,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="auth_sessions",
        verbose_name="Прив'язаний користувач",
    )
    otp_code = models.CharField("OTP Код (6 цифр)", max_length=6, null=True, blank=True)
    payload = models.JSONField("Додаткові дані", default=dict, blank=True)
    created_at = models.DateTimeField("Створено", auto_now_add=True)
    expires_at = models.DateTimeField("Діє до")

    class Meta:
        verbose_name = "Сесія авторизації / OTP"
        verbose_name_plural = "Сесії авторизації / OTP"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Сесія {self.token[:8]}... ({self.get_session_type_display()}) — {self.get_status_display()}"

    def is_valid(self):
        from django.utils import timezone
        return self.status == AuthSessionStatus.PENDING and timezone.now() < self.expires_at


# ---------------------------------------------------------------------------
# Тарифи та платежі (платне розміщення квартири)
# ---------------------------------------------------------------------------


class Tariff(models.Model):
    """
    Тариф платного розміщення. Змінювати ціну/термін можна в адмінці: платежі зберігають
    копію умов на момент створення, тож старі платежі від цього не змінюються.
    """

    PLACEMENT_CODE = "placement"

    code = models.SlugField("Код", max_length=50, unique=True)
    name = models.CharField("Назва", max_length=150)
    price = models.DecimalField(
        "Ціна", max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    currency = models.CharField("Валюта", max_length=3, default="UAH")
    duration_days = models.PositiveSmallIntegerField(
        "Термін (днів)", validators=[MinValueValidator(1), MaxValueValidator(366)]
    )
    is_active = models.BooleanField("Доступний для оплати", default=True)
    created_at = models.DateTimeField("Створено", auto_now_add=True)
    updated_at = models.DateTimeField("Оновлено", auto_now=True)

    class Meta:
        verbose_name = "Тариф"
        verbose_name_plural = "Тарифи"
        ordering = ["price"]
        constraints = [
            models.CheckConstraint(condition=models.Q(price__gt=0), name="tariff_price_positive"),
            models.CheckConstraint(
                condition=models.Q(duration_days__gte=1), name="tariff_duration_positive"
            ),
        ]

    def __str__(self):
        return f"{self.name} — {self.price} {self.currency} / {self.duration_days} дн."

    @classmethod
    def placement(cls):
        """Активний тариф «Розміщення квартири» або None."""
        return cls.objects.filter(code=cls.PLACEMENT_CODE, is_active=True).first()


class PaymentStatus(models.TextChoices):
    CREATED = "created", "Створено"
    PENDING = "pending", "Очікує підтвердження"
    SUCCESS = "success", "Успішно"
    FAILURE = "failure", "Відхилено"
    CANCELLED = "cancelled", "Скасовано"
    EXPIRED = "expired", "Не завершено (протерміновано)"
    REVERSED = "reversed", "Повернено"


class Payment(models.Model):
    """
    Платіж за розміщення одного оголошення на один період.

    Фінансовий запис: не видаляється разом з користувачем чи оголошенням
    (user — SET_NULL зі знімком, property/tariff — PROTECT). Реквізити картки НЕ зберігаються.
    """

    FINAL_STATUSES = (
        PaymentStatus.SUCCESS,
        PaymentStatus.FAILURE,
        PaymentStatus.CANCELLED,
        PaymentStatus.EXPIRED,
        PaymentStatus.REVERSED,
    )

    order_id = models.CharField("Номер замовлення", max_length=64, unique=True)
    provider = models.CharField("Платіжний провайдер", max_length=20, default="liqpay")
    is_test = models.BooleanField(
        "Тестовий (sandbox)", default=False,
        help_text="Створено в тестовому режимі LiqPay. У бойовому режимі ніколи не зараховується.",
    )
    provider_transaction_id = models.CharField(
        "ID транзакції провайдера", max_length=64, unique=True, null=True, blank=True
    )
    status = models.CharField(
        "Статус", max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.CREATED,
        db_index=True,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments",
        verbose_name="Користувач",
    )
    user_snapshot = models.CharField(
        "Користувач (знімок)", max_length=255, blank=True,
        help_text="Логін та email на момент оплати — зберігаються, навіть якщо акаунт видалено.",
    )
    property = models.ForeignKey(
        Property,
        on_delete=models.PROTECT,
        related_name="payments",
        verbose_name="Оголошення",
    )
    tariff = models.ForeignKey(
        Tariff, on_delete=models.PROTECT, related_name="payments", verbose_name="Тариф"
    )
    # Знімок умов тарифу на момент створення платежу
    tariff_name = models.CharField("Назва тарифу (знімок)", max_length=150)
    amount = models.DecimalField("Сума до оплати", max_digits=10, decimal_places=2)
    currency = models.CharField("Валюта", max_length=3, default="UAH")
    duration_days = models.PositiveSmallIntegerField("Термін (днів, знімок)")
    # Дані підтвердженої оплати (з callback провайдера)
    paid_amount = models.DecimalField(
        "Фактично сплачено", max_digits=10, decimal_places=2, null=True, blank=True
    )
    paid_currency = models.CharField("Валюта оплати", max_length=3, blank=True)
    paid_at = models.DateTimeField("Дата оплати", null=True, blank=True)
    # Оплачений період (заповнюється один раз, при застосуванні успішного платежу)
    period_start = models.DateTimeField("Період з", null=True, blank=True)
    period_end = models.DateTimeField("Період до", null=True, blank=True)
    applied_at = models.DateTimeField(
        "Оплату зараховано", null=True, blank=True,
        help_text="Маркер ідемпотентності: успішна оплата зараховується лише один раз.",
    )
    activated_at = models.DateTimeField(
        "Період активовано", null=True, blank=True,
        help_text="Коли оплачені дні почали діяти: одразу (продовження активного оголошення) "
        "або в момент публікації після модерації.",
    )
    error_message = models.CharField("Повідомлення про помилку", max_length=255, blank=True)
    created_at = models.DateTimeField("Створено", auto_now_add=True)
    updated_at = models.DateTimeField("Оновлено", auto_now=True)

    class Meta:
        verbose_name = "Платіж"
        verbose_name_plural = "Платежі"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["property", "status"], name="payment_property_status_idx"),
            models.Index(fields=["user", "created_at"], name="payment_user_created_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="payment_amount_positive"),
            models.CheckConstraint(
                condition=models.Q(duration_days__gte=1), name="payment_duration_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(period_start__isnull=True)
                | models.Q(period_end__gt=models.F("period_start")),
                name="payment_period_valid",
            ),
        ]

    def __str__(self):
        return f"{self.order_id} — {self.amount} {self.currency} ({self.get_status_display()})"

    def is_final(self):
        # Звичайний метод: ім'я поля `property` у цьому класі перекриває декоратор @property.
        return self.status in self.FINAL_STATUSES


class PaymentEvent(models.Model):
    """Журнал повідомлень від провайдера (callback). Лише додавання, без змін."""

    payment = models.ForeignKey(
        Payment,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="events",
        verbose_name="Платіж",
    )
    order_id = models.CharField("Номер замовлення", max_length=64, db_index=True, blank=True)
    signature_valid = models.BooleanField("Підпис дійсний", default=False)
    provider_status = models.CharField("Статус провайдера", max_length=32, blank=True)
    payload = models.JSONField(
        "Дані повідомлення", default=dict, blank=True,
        help_text="Дані callback без реквізитів картки (маскований номер провайдер передає сам).",
    )
    note = models.CharField("Примітка", max_length=255, blank=True)
    created_at = models.DateTimeField("Отримано", auto_now_add=True)

    class Meta:
        verbose_name = "Подія платежу"
        verbose_name_plural = "Події платежів"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.order_id or '—'} {self.provider_status} ({self.created_at:%d.%m.%Y %H:%M})"


class PlacementTransition(models.Model):
    """
    Журнал одноразового перехідного періоду для оголошень, опублікованих до запуску
    обов'язкової оплати. Запис існує => перехід уже застосовано, повторно не виконується.
    """

    applied_at = models.DateTimeField("Застосовано", auto_now_add=True)
    starts_at = models.DateTimeField("Дата активації платної системи")
    ends_at = models.DateTimeField("Перехідний період до")
    listings_count = models.PositiveIntegerField("Кількість оголошень")
    listing_ids = models.JSONField("ID оголошень", default=list)
    applied_by = models.CharField("Ким застосовано", max_length=150, blank=True)

    class Meta:
        verbose_name = "Перехідний період"
        verbose_name_plural = "Перехідний період (журнал)"

    def __str__(self):
        return f"Перехідний період до {self.ends_at:%d.%m.%Y} ({self.listings_count} оголошень)"
