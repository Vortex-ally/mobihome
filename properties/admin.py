import hashlib
import json

from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import (
    AccessibilityAudit,
    Payment,
    PaymentEvent,
    PaymentStatus,
    PlacementTransition,
    Tariff,
    AuditRequest,
    ConsultationRequest,
    Feature,
    ListingVerification,
    ModerationEvent,
    ModerationQueue,
    ModerationStatus,
    ProfileReview,
    Property,
    PropertyFavorite,
    PropertyPhoto,
)


class PropertyPhotoInline(admin.TabularInline):
    model = PropertyPhoto
    extra = 1
    fields = ("image", "image_preview", "order")
    readonly_fields = ("image_preview",)

    def image_preview(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="max-height: 60px; max-width: 100px; border-radius: 4px; object-fit: cover;" />',
                obj.image.url,
            )
        return "—"

    image_preview.short_description = "Попередній перегляд"


class AccessibilityAuditInline(admin.StackedInline):
    model = AccessibilityAudit
    can_delete = False
    verbose_name = "Аудит доступності"
    verbose_name_plural = "Аудит доступності (з замірами в см та балами)"
    extra = 0
    fields = (
        ("entrance_access", "entrance_status", "entrance_comment"),
        ("lift_width_cm", "lift_score", "lift_comment"),
        ("bathroom_type", "bathroom_comment"),
        ("thresholds_max_height_cm", "thresholds_score", "thresholds_comment"),
        ("turning_radius_exists", "turning_comment"),
        "total_score",
    )
    readonly_fields = ("entrance_status", "total_score")


@admin.register(Feature)
class FeatureAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "slug")
    list_filter = ("category",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    list_editable = ("category",)


class PlacementStatusFilter(admin.SimpleListFilter):
    title = "Оплачене розміщення"
    parameter_name = "placement"

    def lookups(self, request, model_admin):
        return (("active", "Діє"), ("expired", "Завершилось"), ("never", "Не оплачувалось"))

    def queryset(self, request, queryset):
        now = timezone.now()
        if self.value() == "active":
            return queryset.filter(placement_until__gt=now)
        if self.value() == "expired":
            return queryset.filter(placement_until__lte=now)
        if self.value() == "never":
            return queryset.filter(placement_until__isnull=True)
        return queryset


class ArchivedFilter(admin.SimpleListFilter):
    title = "Архів"
    parameter_name = "archived"

    def lookups(self, request, model_admin):
        return (("no", "Не в архіві"), ("yes", "В архіві"))

    def queryset(self, request, queryset):
        if self.value() == "no":
            return queryset.filter(archived_at__isnull=True)
        if self.value() == "yes":
            return queryset.filter(archived_at__isnull=False)
        return queryset


class PromotionFilter(admin.SimpleListFilter):
    """Діюче платне просування (ТОП №1 / ТОП №3 / активна публікація) — за анотацією promo_rank."""

    title = "Просування"
    parameter_name = "promotion"

    def lookups(self, request, model_admin):
        return (("any", "Діє будь-яке"), *((str(rank), label) for rank, label in Tariff.PROMOTION_RANKS),
                ("none", "Немає"))

    def queryset(self, request, queryset):
        value = self.value()
        if value == "any":
            return queryset.filter(promo_rank__isnull=False)
        if value == "none":
            return queryset.filter(promo_rank__isnull=True)
        if value and value.isdigit():
            return queryset.filter(promo_rank=int(value))
        return queryset


class PaymentInline(admin.TabularInline):
    """Платежі оголошення — лише перегляд."""

    model = Payment
    extra = 0
    can_delete = False
    show_change_link = True
    fields = ("order_id", "tariff_name", "status", "amount", "currency", "paid_amount", "paid_at",
              "activated_at", "period_start", "period_end", "provider_transaction_id")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    def changelist_view(self, request, extra_context=None):
        warn_duplicate_charges(request)  # модерація — сторінка, яку адміністратор бачить найчастіше
        return super().changelist_view(request, extra_context)

    list_display = (
        "name",
        "owner",
        "listing_type",
        "city",
        "price",
        "moderation_status",
        "is_published",
        "is_verified_by_admin",
        "placement_status",
        "promotion_status",
        "is_verified",
        "view_count",
        "created_at",
    )
    list_filter = (
        "moderation_status",
        "is_published",
        "is_verified_by_admin",
        PlacementStatusFilter,
        PromotionFilter,
        ArchivedFilter,
        "is_verified",
        "listing_type",
        "city",
        "mobility_level",
        "created_at",
    )
    search_fields = ("=id", "name", "address", "city", "description", "contact_phone",
                     "owner__username", "owner__email")
    list_select_related = ("owner",)
    list_per_page = 50
    date_hierarchy = "created_at"
    # Без list_editable: зміна статусу лише через сервіс модерації (причина, історія, захист від гонок).
    form = None  # встановлюється нижче (PropertyAdminForm)
    filter_horizontal = ("features",)
    raw_id_fields = ("owner",)
    readonly_fields = ("placement_until", "is_featured", "featured_until", "archived_at",
                       "view_count", "verified_at", "created_at", "updated_at")
    fieldsets = (
        ("Основне", {"fields": ("name", "owner", "listing_type", "price", "description", "photo")}),
        ("Розташування та характеристики", {
            "fields": ("city", "address", ("rooms", "area_sqm"), ("floor", "total_floors"),
                       "mobility_level", "features", "contact_phone"),
        }),
        ("Модерація та публікація", {
            "fields": ("moderation_status", "moderation_note", "is_published",
                       ("is_verified_by_admin", "verified_at"), "is_verified", "archived_at"),
        }),
        ("Оплачене розміщення", {
            "fields": ("placement_until",),
            "description": "Змінюється лише успішною оплатою (див. «Платежі»).",
        }),
        ("Стара система ТОП (лише історія)", {
            "classes": ("collapse",),
            "fields": ("is_featured", "featured_until"),
        }),
        ("Службове", {"classes": ("collapse",), "fields": ("view_count", "created_at", "updated_at")}),
    )
    inlines = [PropertyPhotoInline, AccessibilityAuditInline, None, None, PaymentInline]  # див. нижче
    actions = [
        "make_approved",
        "make_rejected",
        "make_verified_by_admin",
        "unverify_by_admin",
        "make_verified",
        "activate_listings",
        "deactivate_listings",
        "archive_listings",
        "restore_listings",
    ]

    def get_queryset(self, request):
        from .placement import active_promotions

        return active_promotions(super().get_queryset(request))

    # --- Ролі: модератор (не суперкористувач) змінює ЛИШЕ поля модерації (серверне обмеження форми) ---

    def get_fieldsets(self, request, obj=None):
        if request.user.is_superuser:
            return self.fieldsets + (("Модерація (службове)", {
                "fields": ("moderation_internal_note", "expected_version", "moderation_version",
                           "moderation_submitted_at")}),)
        return (
            ("Оголошення (лише перегляд)", {"fields": (
                "name", "owner", "listing_type", "price", "city", "address", ("rooms", "area_sqm"),
                ("floor", "total_floors"), "description", "photo", "features", "contact_phone",
                "is_published", "placement_until", "created_at")}),
            ("Модерація", {"fields": (
                "moderation_status", "moderation_note", "moderation_internal_note", "mobility_level",
                "is_verified_by_admin", "is_verified", "expected_version", "moderation_version",
                "moderation_submitted_at")}),
        )

    def get_readonly_fields(self, request, obj=None):
        base = tuple(self.readonly_fields) + ("moderation_version", "moderation_submitted_at")
        if request.user.is_superuser:
            return base
        editable = set(MODERATOR_EDITABLE_FIELDS) | {"expected_version"}
        names = {f.name for f in self.model._meta.get_fields() if getattr(f, "concrete", False)}
        names |= {f.name for f in self.model._meta.many_to_many}
        return tuple(sorted(set(base) | (names - editable)))

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            actions = {k: v for k, v in actions.items() if k in ("make_approved", "make_rejected")}
        return actions

    def get_inline_instances(self, request, obj=None):
        return [i for i in super().get_inline_instances(request, obj) if i is not None]

    def save_model(self, request, obj, form, change):
        from . import moderation

        new_status = obj.moderation_status
        old_status = form.initial.get("moderation_status") if change else None
        if not change or "moderation_status" not in form.changed_data:
            return super().save_model(request, obj, form, change)
        obj.moderation_status = old_status  # статус змінює лише сервіс
        super().save_model(request, obj, form, change)
        version = form.cleaned_data.get("expected_version")
        try:
            if new_status == ModerationStatus.APPROVED:
                moderation.approve(obj.pk, actor=request.user, source=moderation.ModerationSource.ADMIN,
                                   expected_version=version,
                                   allowed_from=(ModerationStatus.PENDING, ModerationStatus.REJECTED))
            elif new_status == ModerationStatus.REJECTED:
                moderation.reject(obj.pk, actor=request.user, reason=obj.moderation_note,
                                  source=moderation.ModerationSource.ADMIN, expected_version=version,
                                  allowed_from=(ModerationStatus.PENDING, ModerationStatus.APPROVED))
            elif new_status == ModerationStatus.PENDING:
                if not moderation.can_moderate(request.user):
                    raise moderation.ModerationError("Немає прав на модерацію.")
                moderation.submit(obj.pk, actor=request.user, source=moderation.ModerationSource.ADMIN)
        except moderation.ModerationError as exc:
            self.message_user(request, f"Статус модерації не змінено: {exc}", messages.ERROR)
        obj.refresh_from_db()

    @admin.display(description="Просування", ordering="promo_rank")
    def promotion_status(self, obj):
        rank = getattr(obj, "promo_rank", None)
        if not rank:
            return "—"
        return f"{dict(Tariff.PROMOTION_RANKS).get(rank, rank)} до {timezone.localtime(obj.promo_until):%d.%m.%Y %H:%M}"

    @admin.display(description="Розміщення")
    def placement_status(self, obj):
        if obj.archived_at:
            return "архів"
        if not obj.placement_until:
            return "—"
        label = "до" if obj.placement_active else "завершилось"
        return f"{label} {timezone.localtime(obj.placement_until):%d.%m.%Y}"

    def _activate_paid(self, queryset):
        # update() не надсилає post_save — запускаємо оплачені періоди явно.
        from .placement import activate_on_publication

        for pk in queryset.values_list("pk", flat=True):
            activate_on_publication(pk)

    @admin.action(description="✓ Підтвердити фотомодерацію")
    def make_verified_by_admin(self, request, queryset):
        updated = queryset.update(is_verified_by_admin=True, verified_at=timezone.now())
        self._activate_paid(queryset)
        self.message_user(request, f"Підтверджено фотомодерацію для {updated} оголошень.")

    @admin.action(description="✗ Скасувати фотомодерацію")
    def unverify_by_admin(self, request, queryset):
        updated = queryset.update(is_verified_by_admin=False, verified_at=None)
        self.message_user(request, f"Скасовано фотомодерацію для {updated} оголошень.")

    @admin.action(description="Схвалити та опублікувати (з фотоперевіркою)", permissions=["moderate"])
    def make_approved(self, request, queryset):
        from . import moderation

        done, errors = 0, []
        for prop in queryset.filter(archived_at__isnull=True):
            try:
                moderation.approve(prop.pk, actor=request.user, source=moderation.ModerationSource.ADMIN,
                                   allowed_from=(ModerationStatus.PENDING, ModerationStatus.REJECTED))
                done += 1
            except moderation.ModerationError as exc:
                errors.append(f"#{prop.pk}: {exc}")
        self.message_user(request, f"Схвалено та опубліковано {done} оголошень.")
        for err in errors[:10]:
            self.message_user(request, err, messages.WARNING)

    @admin.action(description="Відхилити (з причиною)", permissions=["moderate"])
    def make_rejected(self, request, queryset):
        from . import moderation

        reason = (request.POST.get("reason") or "").strip()
        if request.POST.get("post") != "yes" or not reason:
            return TemplateResponse(request, "admin/properties/property/reject_reason.html", {
                **self.admin_site.each_context(request), "title": "Причина відхилення",
                "opts": self.model._meta, "queryset": queryset, "reason": reason,
                "error": request.POST.get("post") == "yes" and not reason,
                "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
            })
        done, errors = 0, []
        for prop in queryset:
            try:
                moderation.reject(prop.pk, actor=request.user, reason=reason, source=moderation.ModerationSource.ADMIN,
                                  allowed_from=(ModerationStatus.PENDING, ModerationStatus.APPROVED))
                done += 1
            except moderation.ModerationError as exc:
                errors.append(f"#{prop.pk}: {exc}")
        self.message_user(request, f"Відхилено {done} оголошень.")
        for err in errors[:10]:
            self.message_user(request, err, messages.WARNING)
        return None

    def has_moderate_permission(self, request):
        from .moderation import can_moderate

        return can_moderate(request.user)

    @admin.action(description="Позначити як перевірене житло (Verified)")
    def make_verified(self, request, queryset):
        updated = queryset.update(is_verified=True)
        self.message_user(request, f"Позначено як перевірені {updated} оголошень.")

    @admin.action(description="Активувати (показувати, якщо схвалено)")
    def activate_listings(self, request, queryset):
        updated = queryset.filter(archived_at__isnull=True).update(is_published=True)
        self._activate_paid(queryset)
        self.message_user(request, f"Активовано {updated} оголошень.")

    @admin.action(description="Деактивувати (приховати з каталогу)")
    def deactivate_listings(self, request, queryset):
        updated = queryset.update(is_published=False)
        self.message_user(request, f"Деактивовано {updated} оголошень.")

    @admin.action(description="Перемістити в архів (без втрати платежів)")
    def archive_listings(self, request, queryset):
        updated = queryset.filter(archived_at__isnull=True).update(
            archived_at=timezone.now(), is_published=False
        )
        self.message_user(request, f"В архів переміщено {updated} оголошень.")

    @admin.action(description="Повернути з архіву (неопубліковане)")
    def restore_listings(self, request, queryset):
        updated = queryset.filter(archived_at__isnull=False).update(archived_at=None)
        self.message_user(request, f"Повернуто з архіву {updated} оголошень.")


@admin.register(PropertyPhoto)
class PropertyPhotoAdmin(admin.ModelAdmin):
    list_display = ("id", "property", "image_preview", "order")
    list_filter = ("property__city",)
    search_fields = ("property__name",)
    raw_id_fields = ("property",)

    def image_preview(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="max-height: 50px; max-width: 80px; border-radius: 4px; object-fit: cover;" />',
                obj.image.url,
            )
        return "—"

    image_preview.short_description = "Прев'ю"


@admin.register(PropertyFavorite)
class PropertyFavoriteAdmin(admin.ModelAdmin):
    list_display = ("user", "property", "created_at")
    list_filter = ("created_at",)
    search_fields = ("user__username", "user__email", "property__name")
    raw_id_fields = ("user", "property")


@admin.register(AccessibilityAudit)
class AccessibilityAuditAdmin(admin.ModelAdmin):
    list_display = (
        "property",
        "entrance_access",
        "entrance_status",
        "lift_width_cm",
        "bathroom_type",
        "thresholds_max_height_cm",
        "turning_radius_exists",
        "total_score",
        "updated_at",
    )
    list_filter = ("entrance_status", "bathroom_type", "turning_radius_exists")
    search_fields = ("property__name", "entrance_comment", "lift_comment", "bathroom_comment")
    raw_id_fields = ("property",)


@admin.register(ProfileReview)
class ProfileReviewAdmin(admin.ModelAdmin):
    list_display = ("author", "target_user", "rating", "created_at")
    list_filter = ("rating", "created_at")
    search_fields = ("author__username", "target_user__username", "text")
    raw_id_fields = ("author", "target_user")


@admin.register(AuditRequest)
class AuditRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "phone", "email", "property_address", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("name", "phone", "email", "property_address", "comments")
    list_editable = ("status",)
    raw_id_fields = ("property", "applicant")


@admin.register(ConsultationRequest)
class ConsultationRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "phone", "email", "service_type", "status", "created_at")
    list_filter = ("service_type", "status", "created_at")
    search_fields = ("name", "phone", "email", "message")
    list_editable = ("status",)
    readonly_fields = ("created_at",)


from .models import AuthSession, UserProfile


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "auth_provider", "telegram_id", "google_sub", "phone")
    list_filter = ("auth_provider",)
    search_fields = ("user__username", "user__email", "telegram_id", "diia_id", "phone")
    raw_id_fields = ("user",)


@admin.register(AuthSession)
class AuthSessionAdmin(admin.ModelAdmin):
    list_display = ("short_token", "session_type", "status", "user", "created_at", "expires_at")
    list_filter = ("session_type", "status", "created_at")
    search_fields = ("user__username", "user__email")
    raw_id_fields = ("user",)
    exclude = ("otp_code", "token")
    readonly_fields = ("session_type", "status", "user", "payload", "created_at", "expires_at")

    @admin.display(description="Токен")
    def short_token(self, obj):
        return f"{obj.token[:8]}…" if obj.token else "—"

    def has_add_permission(self, request):
        return False




# --- Обмеження спроб входу в адмінку (захист від перебору пароля) ---
from django.http import HttpResponse  # noqa: E402

from .security import login_is_locked, register_login_failure, reset_login_failures  # noqa: E402

_original_admin_login = admin.site.login


def _throttled_admin_login(request, extra_context=None):
    if request.method != "POST":
        return _original_admin_login(request, extra_context)
    username = request.POST.get("username", "")
    if login_is_locked(request, username):
        return HttpResponse(
            "Забагато невдалих спроб входу. Спробуйте пізніше.", status=429, content_type="text/plain; charset=utf-8"
        )
    response = _original_admin_login(request, extra_context)
    if request.user.is_authenticated and request.user.is_staff:
        reset_login_failures(request, username)
    else:
        register_login_failure(request, username)
    return response


admin.site.login = _throttled_admin_login



# --- Тарифи та платежі ---


@admin.register(Tariff)
class TariffAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "price", "currency", "duration_days", "duration_hours", "promotion_rank",
                    "is_active", "updated_at")
    list_filter = ("is_active", "promotion_rank")
    search_fields = ("name", "code")
    readonly_fields = ("created_at", "updated_at")

    def get_readonly_fields(self, request, obj=None):
        # Код тарифу використовується в логіці оплати — після створення не змінюється.
        return self.readonly_fields + (("code",) if obj else ())

    def has_delete_permission(self, request, obj=None):
        # Тариф з платежами не видаляється (і на рівні БД: PROTECT). Вимкнути — через is_active.
        if obj is not None and obj.payments.exists():
            return False
        return super().has_delete_permission(request, obj)


class PaymentEventInline(admin.TabularInline):
    model = PaymentEvent
    extra = 0
    can_delete = False
    fields = ("created_at", "provider_status", "signature_valid", "note")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


def warn_duplicate_charges(request):
    """
    Помітне попередження про платежі з needs_review: можливе подвійне списання — або друге успішне
    списання за тим самим order_id (не зараховане), або кілька успішних оплат одного оголошення
    через різні order_id (зараховані за чинними правилами) — або підтверджена LiqPay оплата з іншою
    сумою/валютою (не зарахована) — або повернення коштів за зарахованим платежем. Рішення — за адміністратором.
    """
    count = Payment.objects.filter(needs_review=True).count()
    if count:
        url = reverse("admin:properties_payment_changelist") + "?needs_review__exact=1"
        messages.error(request, format_html(
            "Увага: {} платіж(ів) потребують перевірки можливого подвійного списання — друге успішне "
            "списання за тим самим order_id або кілька успішних оплат одного оголошення через різні order_id — "
            "або невідповідності суми/валюти, підтвердженої LiqPay, або повернення коштів за зарахованим платежем. "
            'Перевірте в LiqPay ALERT-події платежу і за потреби поверніть зайве списання вручну. '
            '<a href="{}">Показати</a>', count, url))


REVIEW_CLOSED_NOTE = "[REVIEW closed]"


def _alert_events(payment):
    return PaymentEvent.objects.filter(payment=payment, note__startswith="[ALERT").order_by("id")


def _latest_alert_id(payment):
    """
    Версія ALERT-подій платежу для сторінки підтвердження: id останньої + відбиток вмісту. Подія
    [ALERT duplicate_property_charge] оновлюється на місці (стає відомим ID транзакції другого
    платежу), тож самого id недостатньо, щоб помітити нову інформацію.
    """
    events = list(_alert_events(payment).values_list("id", "note", "payload"))
    if not events:
        return "0"
    digest = hashlib.sha256(json.dumps(events, sort_keys=True, ensure_ascii=False, default=str).encode())
    return f"{events[-1][0]}-{digest.hexdigest()[:16]}"


def _close_review(request, model_admin, payment_pk, seen_alert_id):
    """
    Атомарно знімає лише needs_review. Рядок блокується (select_for_update): паралельні
    спроби — одна подія. Якщо після сторінки підтвердження з'явилась нова ALERT-подія — не закриває.
    """
    with transaction.atomic():
        payment = Payment.objects.select_for_update().filter(pk=payment_pk, needs_review=True).first()
        if payment is None:
            return "already"
        if str(_latest_alert_id(payment)) != str(seen_alert_id):
            return "changed"
        Payment.objects.filter(pk=payment.pk).update(needs_review=False)  # лише цей прапорець
        now = timezone.now()
        alerts = list(_alert_events(payment).values_list("id", flat=True))
        PaymentEvent.objects.create(
            payment=payment, order_id=payment.order_id, signature_valid=False,
            payload={"reviewed_by": request.user.get_username(), "reviewed_at": now.isoformat(),
                     "alert_event_ids": alerts},
            note=(f"{REVIEW_CLOSED_NOTE} Перевірку закрив {request.user.get_username()} "
                  f"{timezone.localtime(now):%d.%m.%Y %H:%M}.")[:255],
        )
        model_admin.log_change(request, payment, "Закрито перевірку подвійного списання (needs_review знято).")
    return "closed"


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    """Фінансові записи: лише перегляд. Статус змінюється тільки підтвердженням провайдера."""

    list_display = ("order_id", "tariff_name", "status", "needs_review", "is_test", "property_link", "user_display",
                    "amount",
                    "currency", "paid_amount", "provider_transaction_id", "paid_at", "period_end", "created_at",
                    "updated_at")
    list_filter = ("needs_review", "status", "tariff", "promotion_rank", "is_test", "provider", "currency",
                   "created_at", "paid_at", "period_end")
    search_fields = ("order_id", "provider_transaction_id", "user__username", "user__email",
                     "user_snapshot", "property__name", "=property__id")
    list_select_related = ("property", "user", "tariff")
    date_hierarchy = "created_at"
    list_per_page = 50
    inlines = [PaymentEventInline]
    actions = ["mark_reviewed"]

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False  # лише перегляд (view permission)

    def has_review_permission(self, request):
        return request.user.has_perm("properties.review_payment")

    def changelist_view(self, request, extra_context=None):
        # Прямий POST дії без дозволу — 403 (інакше Django мовчки відхилив би невідому дію).
        if request.method == "POST" and request.POST.get("action") == "mark_reviewed" \
                and not self.has_review_permission(request):
            raise PermissionDenied
        warn_duplicate_charges(request)
        return super().changelist_view(request, extra_context)

    @admin.action(description="Позначити перевіреним (закрити перевірку подвійного списання)",
                  permissions=["review"])
    def mark_reviewed(self, request, queryset):
        if not self.has_review_permission(request):
            raise PermissionDenied
        flagged = list(queryset.filter(needs_review=True).order_by("pk"))
        if not flagged:
            self.message_user(request, "Серед вибраних немає платежів, що потребують перевірки.", messages.WARNING)
            return None
        if request.POST.get("post") != "yes":
            rows = [(p, list(_alert_events(p)), _latest_alert_id(p)) for p in flagged]
            return TemplateResponse(request, "admin/properties/payment/confirm_review.html", {
                **self.admin_site.each_context(request), "title": "Підтвердьте закриття перевірки",
                "opts": self.model._meta, "rows": rows, "skipped": queryset.count() - len(flagged),
                "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
            })
        closed, changed = 0, 0
        for p in flagged:
            seen = request.POST.get(f"alerts_{p.pk}", "")
            result = _close_review(request, self, p.pk, seen)
            closed += result == "closed"
            changed += result == "changed"
        if closed:
            self.message_user(request, f"Перевірку закрито: {closed}.", messages.SUCCESS)
        if changed:
            self.message_user(request, f"Не закрито {changed}: з'явились нові ALERT-події — перегляньте їх.",
                              messages.ERROR)
        return None

    def has_delete_permission(self, request, obj=None):
        return False

    def change_view(self, request, object_id, form_url="", extra_context=None):
        warn_duplicate_charges(request)
        return super().change_view(request, object_id, form_url, extra_context)

    @admin.display(description="Оголошення", ordering="property__name")
    def property_link(self, obj):
        url = reverse("admin:properties_property_change", args=[obj.property_id])
        return format_html('<a href="{}">#{} {}</a>', url, obj.property_id, obj.property.name)

    @admin.display(description="Користувач")
    def user_display(self, obj):
        return obj.user.username if obj.user else (obj.user_snapshot or "—")


@admin.register(PaymentEvent)
class PaymentEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "order_id", "provider_status", "signature_valid", "payment", "note")
    list_filter = ("signature_valid", "provider_status", "created_at")
    search_fields = ("order_id",)
    list_select_related = ("payment",)

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# --- Користувачі: оголошення, платежі, профіль ---


class UserPropertyInline(admin.TabularInline):
    model = Property
    fk_name = "owner"
    extra = 0
    can_delete = False
    show_change_link = True
    fields = ("name", "city", "moderation_status", "is_published", "placement_until", "archived_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class UserPaymentInline(admin.TabularInline):
    model = Payment
    fk_name = "user"
    extra = 0
    can_delete = False
    show_change_link = True
    fields = ("order_id", "status", "property", "amount", "currency", "paid_at", "period_end")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    extra = 0
    fields = ("auth_provider", "phone", "telegram_id", "google_sub", "avatar_url")
    readonly_fields = ("auth_provider", "telegram_id", "google_sub")


admin.site.unregister(User)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "email", "first_name", "last_name", "is_active", "is_staff",
                    "listings_count", "successful_payments", "date_joined", "last_login")
    list_filter = ("is_active", "is_staff", "is_superuser", "profile__auth_provider", "date_joined")
    search_fields = ("username", "email", "first_name", "last_name", "profile__phone")
    inlines = [UserProfileInline, UserPropertyInline, UserPaymentInline]
    actions = ["deactivate_users", "activate_users"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _listings=Count("properties", distinct=True),
            _paid=Count("payments", filter=Q(payments__status=PaymentStatus.SUCCESS), distinct=True),
        )

    @admin.display(description="Оголошень", ordering="_listings")
    def listings_count(self, obj):
        return obj._listings

    @admin.display(description="Успішних оплат", ordering="_paid")
    def successful_payments(self, obj):
        return obj._paid

    @admin.action(description="Деактивувати (заблокувати вхід)")
    def deactivate_users(self, request, queryset):
        qs = queryset.exclude(pk=request.user.pk)
        if not request.user.is_superuser:
            qs = qs.exclude(is_superuser=True)
        updated = qs.update(is_active=False)
        self.message_user(request, f"Деактивовано {updated} користувачів.")

    @admin.action(description="Активувати")
    def activate_users(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"Активовано {updated} користувачів.")


@admin.register(PlacementTransition)
class PlacementTransitionAdmin(admin.ModelAdmin):
    list_display = ("applied_at", "starts_at", "ends_at", "listings_count", "applied_by")

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.site_header = "mobi Home — адміністрування"
admin.site.site_title = "mobi Home"
admin.site.index_title = "Керування сайтом"


# --- Модерація: форма оголошення, перевірки житла, історія, черга ---

MODERATOR_EDITABLE_FIELDS = ("moderation_status", "moderation_note", "moderation_internal_note", "mobility_level",
                             "is_verified_by_admin", "is_verified")


class PropertyAdminForm(forms.ModelForm):
    expected_version = forms.IntegerField(widget=forms.HiddenInput, required=False)

    class Meta:
        model = Property
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["expected_version"].initial = self.instance.moderation_version

    def clean(self):
        cleaned = super().clean()
        if not (self.instance and self.instance.pk):
            return cleaned
        current = Property.objects.filter(pk=self.instance.pk).values(
            "moderation_version", "moderation_status").first()
        if "moderation_status" in self.changed_data:
            if cleaned.get("expected_version") is not None and current \
                    and cleaned["expected_version"] != current["moderation_version"]:
                raise forms.ValidationError("Оголошення вже змінив інший модератор. Оновіть сторінку.")
            if cleaned.get("moderation_status") == ModerationStatus.REJECTED \
                    and not (cleaned.get("moderation_note") or "").strip():
                self.add_error("moderation_note", "Вкажіть причину відхилення — її побачить власник.")
        if cleaned.get("is_verified") and "is_verified" in self.changed_data and not \
                ListingVerification.objects.filter(property_id=self.instance.pk, result="passed").exists():
            self.add_error("is_verified", "«Перевірене житло» — лише після пройденої перевірки (онлайн або виїзної).")
        return cleaned


PropertyAdmin.form = PropertyAdminForm


class ListingVerificationInline(admin.StackedInline):
    model = ListingVerification
    extra = 0
    fields = (("kind", "result", "checked_on"), ("photos_checked", "video_checked", "owner_contacted"), "video_link",
              ("inspector", "inspector_name"), "checklist", "report", ("inspector_fee", "payout_status"),
              "internal_comment", "recorded_by")
    readonly_fields = ("recorded_by",)
    raw_id_fields = ("inspector",)


class ModerationEventInline(admin.TabularInline):
    model = ModerationEvent
    extra = 0
    can_delete = False
    fields = ("created_at", "action", "from_status", "to_status", "actor_snapshot", "source", "reason")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


PropertyAdmin.inlines = [PropertyPhotoInline, AccessibilityAuditInline, ListingVerificationInline,
                         ModerationEventInline, PaymentInline]


def _save_verification_formset(self, request, form, formset, change):
    instances = formset.save(commit=False)
    for inst in instances:
        if isinstance(inst, ListingVerification) and not inst.recorded_by_id:
            inst.recorded_by = request.user
        inst.save()
    for inst in formset.deleted_objects:
        inst.delete()
    formset.save_m2m()


PropertyAdmin.save_formset = _save_verification_formset


class VerificationKindFilter(admin.SimpleListFilter):
    title = "Формат перевірки"
    parameter_name = "verification"

    def lookups(self, request, model_admin):
        return (("online", "Онлайн"), ("onsite", "Виїзна"), ("none", "Ще не перевірялось"))

    def queryset(self, request, queryset):
        if self.value() in ("online", "onsite"):
            return queryset.filter(verifications__kind=self.value()).distinct()
        if self.value() == "none":
            return queryset.filter(verifications__isnull=True)
        return queryset


PropertyAdmin.list_filter = (*PropertyAdmin.list_filter, VerificationKindFilter)


@admin.register(ModerationQueue)
class ModerationQueueAdmin(PropertyAdmin):
    """Черга нових і повторних заявок: лише «На перевірці», найстаріші — першими."""

    list_display = ("id", "name", "city", "owner", "moderation_submitted_at", "moderation_version",
                    "verification_kinds", "is_verified")
    list_display_links = ("id", "name")
    list_filter = ("moderation_submitted_at", "created_at", VerificationKindFilter, "city")
    ordering = ("moderation_submitted_at", "pk")
    date_hierarchy = None

    def get_queryset(self, request):
        return super().get_queryset(request).filter(moderation_status=ModerationStatus.PENDING,
                                                    archived_at__isnull=True)

    @admin.display(description="Перевірки")
    def verification_kinds(self, obj):
        kinds = sorted({v.get_kind_display() + ": " + v.get_result_display() for v in obj.verifications.all()})
        return "; ".join(kinds) or "—"

    def has_add_permission(self, request):
        return False


@admin.register(ModerationEvent)
class ModerationEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "property", "action", "from_status", "to_status", "actor_snapshot", "source")
    list_filter = ("action", "source", "to_status", "created_at")
    search_fields = ("=property__id", "property__name", "actor_snapshot", "reason")
    list_select_related = ("property",)

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ListingVerification)
class ListingVerificationAdmin(admin.ModelAdmin):
    list_display = ("property", "kind", "result", "checked_on", "inspector_name", "inspector_fee", "payout_status",
                    "recorded_by", "created_at")
    list_filter = ("kind", "result", "payout_status", "checked_on")
    search_fields = ("=property__id", "property__name", "inspector_name")
    raw_id_fields = ("property", "inspector")
    readonly_fields = ("recorded_by", "created_at", "updated_at")

    def save_model(self, request, obj, form, change):
        if not obj.recorded_by_id:
            obj.recorded_by = request.user
        super().save_model(request, obj, form, change)
