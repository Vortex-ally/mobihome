from django.contrib import admin
from django.utils.html import format_html

from .models import (
    AccessibilityAudit,
    AuditRequest,
    ConsultationRequest,
    Feature,
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


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "owner",
        "listing_type",
        "city",
        "price",
        "mobility_level",
        "moderation_status",
        "is_published",
        "is_verified_by_admin",
        "verified_at",
        "is_verified",
        "is_featured",
        "view_count",
        "created_at",
    )
    list_filter = (
        "is_verified_by_admin",
        "moderation_status",
        "is_published",
        "is_verified",
        "is_featured",
        "listing_type",
        "city",
        "mobility_level",
        "created_at",
    )
    search_fields = ("name", "address", "city", "description", "contact_phone", "owner__username", "owner__email")
    list_editable = (
        "moderation_status",
        "is_published",
        "is_verified_by_admin",
        "is_verified",
        "is_featured",
    )
    filter_horizontal = ("features",)
    raw_id_fields = ("owner",)
    inlines = [PropertyPhotoInline, AccessibilityAuditInline]
    actions = [
        "make_verified_by_admin",
        "unverify_by_admin",
        "make_approved",
        "make_rejected",
        "make_verified",
    ]

    @admin.action(description="✓ Підтвердити фотомодерацію (is_verified_by_admin = True)")
    def make_verified_by_admin(self, request, queryset):
        now = timezone.now()
        updated = queryset.update(is_verified_by_admin=True, verified_at=now)
        self.message_user(request, f"Успішно підтверджено фотомодерацію для {updated} оголошень.")

    @admin.action(description="✗ Скасувати фотомодерацію (is_verified_by_admin = False)")
    def unverify_by_admin(self, request, queryset):
        updated = queryset.update(is_verified_by_admin=False, verified_at=None)
        self.message_user(request, f"Скасовано фотомодерацію для {updated} оголошень.")

    @admin.action(description="Схвалити та опублікувати обрані оголошення")
    def make_approved(self, request, queryset):
        now = timezone.now()
        updated = queryset.update(
            moderation_status=ModerationStatus.APPROVED,
            is_published=True,
            is_verified_by_admin=True,
            verified_at=now,
        )
        self.message_user(request, f"Успішно схвалено, опубліковано та підтверджено фото для {updated} оголошень.")

    @admin.action(description="Відхилити обрані оголошення")
    def make_rejected(self, request, queryset):
        updated = queryset.update(moderation_status=ModerationStatus.REJECTED)
        self.message_user(request, f"Відхилено {updated} оголошень.")

    @admin.action(description="Позначити обрані як перевірене житло (Verified)")
    def make_verified(self, request, queryset):
        updated = queryset.update(is_verified=True)
        self.message_user(request, f"Позначено як перевірені {updated} оголошень.")


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
    list_display = ("user", "auth_provider", "telegram_id", "diia_id", "phone")
    list_filter = ("auth_provider",)
    search_fields = ("user__username", "user__email", "telegram_id", "diia_id", "phone")
    raw_id_fields = ("user",)


@admin.register(AuthSession)
class AuthSessionAdmin(admin.ModelAdmin):
    list_display = ("token", "session_type", "status", "user", "otp_code", "created_at", "expires_at")
    list_filter = ("session_type", "status", "created_at")
    search_fields = ("token", "user__username", "user__email", "otp_code")
    raw_id_fields = ("user",)


