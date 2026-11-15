"""
Представлення платформи mobi home.
"""

import json

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core.paginator import Paginator
from django.db.models import Avg, Case, Count, IntegerField, Q, Value, When
from django.contrib.auth.models import User
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from django.conf import settings

from .forms import (
    AccessibilityAuditForm,
    AddListingForm,
    AuditRequestForm,
    RegisterForm,
    ReviewForm,
    UserProfileForm,
)
from .mobility import MOBILITY_LEVELS
from .models import (
    AccessibilityAudit,
    AuditRequest,
    AuditRequestStatus,
    BathroomType,
    ConsultationRequest,
    ConsultationServiceType,
    EntranceStatus,
    Feature,
    ModerationStatus,
    Property,
    PropertyFavorite,
    PropertyPhoto,
    ProfileReview,
    Tariff,
    score_to_status,
)
from .security import login_is_locked, register_login_failure, reset_login_failures
from .social_auth import (  # noqa: F401 (реекспорт для urls.py)
    diia_disabled,
    google_callback,
    google_enabled,
    google_login,
    safe_next_url,
    telegram_callback,
    telegram_enabled,
)

# Координати міст для карти (приблизно)
CITY_COORDINATES = {
    "Київ": (50.4501, 30.5234),
    "Львів": (49.8397, 24.0297),
    "Одеса": (46.4825, 30.7233),
    "Дніпро": (48.4647, 35.0462),
    "Харків": (49.9935, 36.2304),
    "Запоріжжя": (47.8388, 35.1396),
    "Вінниця": (49.2328, 28.4681),
    "Чернівці": (48.2917, 25.9352),
    "Івано-Франківськ": (48.9226, 24.7111),
    "Долобів": (49.6365, 23.4728),
}

PROPERTIES_PER_PAGE = 9


def index(request):
    """Головна лендінг-сторінка: герой, статистика, обрані оголошення, CTA."""
    now_index = timezone.now()
    featured = (
        Property.objects.public()
        .select_related("audit")
        .prefetch_related("extra_photos")
        .annotate(
            is_featured_active=Case(
                When(
                    Q(is_featured=True)
                    & (Q(featured_until__isnull=True) | Q(featured_until__gt=now_index)),
                    then=Value(1),
                ),
                default=Value(0),
                output_field=IntegerField(),
            )
        )
        .order_by("-is_featured_active", "-created_at")[:6]
    )
    _assign_listing_photo_urls(featured)
    total_count = Property.objects.public().count()
    cities_count = (
        Property.objects.public()
        .filter(city__isnull=False)
        .exclude(city="")
        .values("city")
        .distinct()
        .count()
    )
    context = {
        "featured": featured,
        "total_count": total_count,
        "cities_count": cities_count,
    }
    return render(request, "properties/index.html", context)


def about(request):
    """Сторінка «Про компанію»: місія, цінності, команда."""
    return render(request, "properties/about.html")


def services(request):
    """Сторінка послуг."""
    return render(request, "properties/services.html")


def faq(request):
    """Сторінку FAQ прибрано — редірект на головну (питання є в кінці головної)."""
    return redirect("properties:index")


def contact(request):
    """Контакти та форма зворотного зв'язку, карта."""
    if request.method == "POST":
        messages.success(
            request, "Дякуємо! Ваше повідомлення надіслано. Ми зв'яжемося з вами найближчим часом."
        )
        return redirect("properties:contact")
    return render(request, "properties/contact.html")


def privacy_policy(request):
    """Сторінка політики конфіденційності."""
    return render(request, "properties/privacy_policy.html")


def terms_of_service(request):
    """Сторінка правил сервісу."""
    return render(request, "properties/terms_of_service.html")


def home(request):
    """
    Головна: каталог квартир з фільтрами (тип, місто, ціна, кімнати, доступність), сортування, пагінація.
    """
    qs = (
        Property.objects.public()
        .select_related("audit")
        .prefetch_related("features", "extra_photos")
    )

    # Фільтр: тип оголошення (продаж / оренда)
    listing_type = request.GET.get("type", "").strip()
    if listing_type in ("sale", "rent"):
        qs = qs.filter(listing_type=listing_type)

    # Фільтр: місто (точна відповідність, без дублікатів у списку)
    city = request.GET.get("city", "").strip()
    if city:
        qs = qs.filter(city=city)

    # Фільтр: ціна від / до
    try:
        price_min = request.GET.get("price_min")
        if price_min:
            qs = qs.filter(price__gte=float(price_min))
    except (TypeError, ValueError):
        pass
    try:
        price_max = request.GET.get("price_max")
        if price_max:
            qs = qs.filter(price__lte=float(price_max))
    except (TypeError, ValueError):
        pass

    # Фільтр: мінімум кімнат
    try:
        rooms = request.GET.get("rooms")
        if rooms:
            qs = qs.filter(rooms__gte=int(rooms))
    except (TypeError, ValueError):
        pass

    # Фільтр: максимум кімнат
    try:
        rooms_max = request.GET.get("rooms_max")
        if rooms_max:
            qs = qs.filter(rooms__lte=int(rooms_max))
    except (TypeError, ValueError):
        pass

    # Фільтр: площа від / до (м²)
    try:
        area_min = request.GET.get("area_min")
        if area_min:
            qs = qs.filter(area_sqm__gte=float(area_min))
    except (TypeError, ValueError):
        pass
    try:
        area_max = request.GET.get("area_max")
        if area_max:
            qs = qs.filter(area_sqm__lte=float(area_max))
    except (TypeError, ValueError):
        pass

    # Критичні точки доступності
    has_audit = Q(audit__isnull=False)
    lift_ok = Q(audit__lift_width_cm__gt=80)
    shower_drain = Q(audit__bathroom_type=BathroomType.SHOWER_DRAIN)
    no_thresholds = (
        Q(audit__thresholds_max_height_cm__lte=2)
        | Q(audit__thresholds_max_height_cm__isnull=True)
        | Q(audit__thresholds_score__gte=9)
    )
    filter_lift = request.GET.get("lift_80") == "1"
    filter_shower = request.GET.get("shower_drain") == "1"
    filter_no_thresholds = request.GET.get("no_thresholds") == "1"
    if filter_lift or filter_shower or filter_no_thresholds:
        qs = qs.filter(has_audit)
        if filter_lift:
            qs = qs.filter(lift_ok)
        if filter_shower:
            qs = qs.filter(shower_drain)
        if filter_no_thresholds:
            qs = qs.filter(no_thresholds)

    # Фільтр: радіус для розвороту (мобільність)
    filter_turning = request.GET.get("turning_radius") == "1"
    if filter_turning:
        qs = qs.filter(has_audit, audit__turning_radius_exists=True)

    # Фільтр: тип санвузлу (мобільність)
    bathroom_type = request.GET.get("bathroom_type", "").strip()
    if bathroom_type in ("bath", "shower_tray", "shower_drain"):
        qs = qs.filter(has_audit, audit__bathroom_type=bathroom_type)

    # Фільтр: лише з проведеним аудитом
    filter_has_audit = request.GET.get("has_audit") == "1"
    if filter_has_audit:
        qs = qs.filter(has_audit)

    # Фільтр: мінімальний бал аудиту (1–10)
    audit_score_min_val = request.GET.get("audit_score_min", "").strip()
    try:
        if audit_score_min_val:
            score = float(audit_score_min_val)
            if 1 <= score <= 10:
                qs = qs.filter(has_audit, audit__total_score__gte=score)
    except (TypeError, ValueError):
        audit_score_min_val = ""

    # Фільтр: лише перевірені оголошення
    filter_verified = request.GET.get("verified") == "1"
    if filter_verified:
        qs = qs.filter(is_verified=True)

    # Фільтр: рівень мобільності / доступності (1–5)
    active_level = (
        request.GET.get("accessibility_level")
        or request.GET.get("level")
        or request.GET.get("mobility")
        or ""
    ).strip()
    if active_level in ("1", "2", "3", "4", "5"):
        level_val = int(active_level)
        if request.GET.get("accessibility_level") or request.GET.get("level"):
            qs = qs.filter(mobility_level=level_val)
        else:
            qs = qs.filter(mobility_level__isnull=False, mobility_level__gte=level_val)

    # Фільтр: зручності (фішки)
    feature_ids = request.GET.getlist("feature")
    if feature_ids:
        for fid in feature_ids:
            try:
                qs = qs.filter(features__id=int(fid))
            except (TypeError, ValueError):
                pass
        qs = qs.distinct()

    # Текстовий пошук
    query = request.GET.get("q", "").strip()
    if query:
        qs = qs.filter(
            Q(name__icontains=query)
            | Q(address__icontains=query)
            | Q(city__icontains=query)
            | Q(description__icontains=query)
        )

    # Сортування (реклама: лише з активною featured_until)
    now = timezone.now()
    qs = qs.annotate(
        is_featured_active=Case(
            When(
                Q(is_featured=True) & (Q(featured_until__isnull=True) | Q(featured_until__gt=now)),
                then=Value(1),
            ),
            default=Value(0),
            output_field=IntegerField(),
        )
    )
    sort = request.GET.get("sort", "newest")
    if sort == "price_asc":
        qs = qs.order_by("price")
    elif sort == "price_desc":
        qs = qs.order_by("-price")
    elif sort == "score":
        qs = qs.order_by("-audit__total_score")
    else:
        qs = qs.order_by("-is_featured_active", "-created_at")

    total_count = qs.count()
    paginator = Paginator(qs, PROPERTIES_PER_PAGE)
    page_number = request.GET.get("page", 1)
    try:
        page_obj = paginator.page(int(page_number))
    except (ValueError, TypeError):
        page_obj = paginator.page(1)
    _assign_listing_photo_urls(page_obj.object_list)

    # Список міст для фільтра (унікальні, відсортовані, нормалізовані)
    cities = sorted(
        set(
            (c or "").strip()
            for c in Property.objects.public()
            .filter(city__isnull=False)
            .exclude(city="")
            .values_list("city", flat=True)
            if (c or "").strip()
        )
    )

    # Усі зручності для фільтра
    all_features = Feature.objects.all().order_by("name")

    # Дані для карти: при виборі міста показуємо лише його, інакше — усі міста з оголошеннями
    city_counts = (
        Property.objects.public()
        .filter(city__isnull=False)
        .exclude(city="")
        .values("city")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    all_map_cities = []
    for c in city_counts:
        city_name = (c["city"] or "").strip()
        if not city_name:
            continue
        coords = CITY_COORDINATES.get(city_name)
        if coords is None:
            # Немає координат міста — не ставимо маркер у випадкове місце (раніше — центр Києва).
            continue
        all_map_cities.append(
            {
                "city": city_name,
                "count": c["count"],
                "lat": coords[0],
                "lng": coords[1],
            }
        )
    if city:
        map_cities = [m for m in all_map_cities if m["city"] == city]
        if map_cities:
            map_center = (map_cities[0]["lat"], map_cities[0]["lng"])
            map_zoom = 11
        else:
            map_cities = all_map_cities
            map_center = (49.0, 32.0)
            map_zoom = 6
    else:
        map_cities = all_map_cities
        map_center = (49.0, 32.0)
        map_zoom = 6
    map_cities_json = json.dumps(map_cities, ensure_ascii=False)
    map_center_json = json.dumps(map_center)

    filter_keys = [
        "q", "type", "city", "rooms", "rooms_max",
        "price_min", "price_max", "area_min", "area_max",
        "mobility", "lift_80", "shower_drain", "no_thresholds",
        "turning_radius", "bathroom_type", "has_audit", "audit_score_min",
        "verified", "feature"
    ]
    has_active_filters = any(request.GET.get(key) for key in filter_keys)

    context = {
        "properties": page_obj,
        "page_obj": page_obj,
        "has_active_filters": has_active_filters,
        "query": query,
        "listing_type": listing_type,
        "city": city,
        "price_min": request.GET.get("price_min", ""),
        "price_max": request.GET.get("price_max", ""),
        "rooms": request.GET.get("rooms", ""),
        "rooms_max": request.GET.get("rooms_max", ""),
        "area_min": request.GET.get("area_min", ""),
        "area_max": request.GET.get("area_max", ""),
        "filter_lift": filter_lift,
        "filter_shower": filter_shower,
        "filter_no_thresholds": filter_no_thresholds,
        "filter_turning": filter_turning,
        "bathroom_type": bathroom_type,
        "filter_has_audit": filter_has_audit,
        "audit_score_min": audit_score_min_val,
        "filter_verified": filter_verified,
        "bathroom_type_choices": BathroomType.choices,
        "active_level": active_level,
        "mobility": active_level,
        "mobility_levels": MOBILITY_LEVELS,
        "selected_features": [int(x) for x in feature_ids if x.isdigit()],
        "features": all_features,
        "sort": sort,
        "total_count": total_count,
        "cities": cities,
        "map_cities": map_cities,
        "map_cities_json": map_cities_json,
        "map_center": map_center,
        "map_center_json": map_center_json,
        "map_zoom": map_zoom,
    }
    return render(request, "properties/home.html", context)


def accessibility_levels(request):
    """Сторінка-довідник: який рівень мобільності для кого підходить."""
    context = {"mobility_levels": MOBILITY_LEVELS}
    return render(request, "properties/accessibility_levels.html", context)


# Нейтральна заглушка для оголошень без фото (жодних «чужих» фотографій квартир).
NO_PHOTO_STATIC_PATH = "img/no-photo.svg"


def _no_photo_url():
    from django.templatetags.static import static

    return static(NO_PHOTO_STATIC_PATH)


def _safe_url(field):
    try:
        return field.url if field else None
    except ValueError:
        return None


def _property_photo_urls(prop):
    """Усі реальні фото оголошення: головне + галерея (без дублікатів), у порядку показу."""
    urls = []
    main = _safe_url(prop.photo)
    if main:
        urls.append(main)
    for extra in prop.extra_photos.all():
        url = _safe_url(extra.image)
        if url and url not in urls:
            urls.append(url)
    return urls


def _assign_listing_photo_urls(properties):
    """
    Для карток: listing_photo_url — перше реальне фото або нейтральна заглушка;
    has_real_photo та photo_count — для бейджа кількості фото.
    """
    placeholder = _no_photo_url()
    for p in properties:
        urls = _property_photo_urls(p)
        p.has_real_photo = bool(urls)
        p.photo_count = len(urls)
        p.listing_photo_url = urls[0] if urls else placeholder


SIMILAR_LISTINGS_LIMIT = 4


def _similar_listings(prop):
    """Схожі публічні оголошення: те саме місто й тип; найближчі за кількістю кімнат, потім нові."""
    qs = (
        Property.objects.public()
        .exclude(pk=prop.pk)
        .filter(listing_type=prop.listing_type)
        .select_related("audit")
        .prefetch_related("extra_photos")
    )
    if prop.city:
        qs = qs.filter(city=prop.city)
    if prop.rooms:
        qs = qs.annotate(
            same_rooms=Case(When(rooms=prop.rooms, then=Value(1)), default=Value(0),
                            output_field=IntegerField())
        ).order_by("-same_rooms", "-created_at")
    else:
        qs = qs.order_by("-created_at")
    similar = list(qs[:SIMILAR_LISTINGS_LIMIT])
    _assign_listing_photo_urls(similar)
    return similar


def property_detail(request, pk):
    """Сторінка об'єкта: повна інформація та результати аудиту. Фото: головне + додаткові; якщо немає — placeholder."""
    prop = get_object_or_404(
        Property.objects.select_related("owner", "audit").prefetch_related("features", "extra_photos"),
        pk=pk,
    )

    # Неперевірені оголошення (або ті, що не пройшли фотоперевірку) доступні лише власнику або адмінам
    is_owner_or_staff = request.user.is_authenticated and (
        request.user.is_staff
        or request.user.is_superuser
        or (getattr(prop, "owner_id", None) and prop.owner_id == request.user.id)
    )
    if prop.archived_at and not _is_staff(request.user):
        raise Http404("Оголошення не знайдено.")
    if not is_owner_or_staff and not Property.objects.public().filter(pk=prop.pk).exists():
        raise Http404("Оголошення не знайдено або не пройшло фотоперевірку адміністратора.")

    audit = getattr(prop, "audit", None)
    can_edit_audit = (
        _can_manage_property(request.user, prop) if request.user.is_authenticated else False
    )
    # Лічильник переглядів: один раз за сесію на оголошення
    session_key = "viewed_property_%s" % pk
    if not request.session.get(session_key):
        prop.view_count = (prop.view_count or 0) + 1
        prop.save(update_fields=["view_count"])
        request.session[session_key] = True
    property_photos = _property_photo_urls(prop)

    now = timezone.now()
    # Стара система ТОП: продаж припинено; лише показ чинного ТОП до його завершення.
    featured_until_display = (
        prop.featured_until if prop.featured_until and prop.featured_until > now else None
    )

    rows = []
    if audit:
        status_choices = dict(EntranceStatus.choices)

        def get_status_display(score):
            if score is None:
                return "—"
            k = score_to_status(score)
            return status_choices.get(k, k or "—")

        rows = [
            (
                "Вхід до будинку (пандус, сходи)",
                f"{audit.entrance_access}/10" if audit.entrance_access else "—",
                get_status_display(audit.entrance_access),
                audit.entrance_comment,
            ),
            (
                "Ширина дверного прорізу ліфта",
                f"{audit.lift_width_cm} см" if audit.lift_width_cm else "—",
                get_status_display(audit.lift_score),
                audit.lift_comment,
            ),
            (
                "Зручність санвузлу",
                audit.get_bathroom_type_display() if audit.bathroom_type else "—",
                "Ідеально" if audit.bathroom_type == BathroomType.SHOWER_DRAIN else ("Потребує покращення" if audit.bathroom_type else "—"),
                audit.bathroom_comment,
            ),
            (
                "Висота порогів у приміщенні",
                f"{audit.thresholds_max_height_cm} см" if audit.thresholds_max_height_cm is not None else "—",
                get_status_display(audit.thresholds_score),
                audit.thresholds_comment,
            ),
            (
                "Простір для розвороту на візку",
                "Достатньо" if audit.turning_radius_exists else "Обмежений",
                "Ідеально" if audit.turning_radius_exists else "Важкодоступно",
                audit.turning_comment,
            ),
        ]

    is_favorite = False
    if request.user.is_authenticated:
        is_favorite = PropertyFavorite.objects.filter(user=request.user, property=prop).exists()

    context = {
        "property": prop,
        "audit": audit,
        "audit_rows": rows,
        "can_edit_audit": can_edit_audit,
        "property_photos": property_photos,
        "featured_until_display": featured_until_display,
        "is_favorite": is_favorite,
        "similar_listings": _similar_listings(prop),
        # Координати лише рівня міста (геокодування адреси немає) — показуються як приблизні.
        "map_center": list(CITY_COORDINATES[prop.city]) if prop.city in CITY_COORDINATES else None,
        "map_query": ", ".join(x for x in (prop.address, prop.city, "Україна") if x),
    }
    return render(request, "properties/property_detail.html", context)


def _is_staff(user):
    return user.is_authenticated and (user.is_staff or user.is_superuser)


def _visible_properties(user):
    """Оголошення, які користувач має право бачити: публічні + свої; адмін бачить усі."""
    if _is_staff(user):
        return Property.objects.all()
    if user.is_authenticated:
        return Property.objects.filter(
            Q(pk__in=Property.objects.public().values("pk"))
            | Q(owner=user, archived_at__isnull=True)
        )
    return Property.objects.public()


def _can_manage_property(user, prop):
    """Чи може користувач редагувати/видаляти оголошення (власник або адмін)."""
    if not user.is_authenticated:
        return False
    return user.is_staff or (getattr(prop, "owner_id", None) and prop.owner_id == user.id)


@login_required(login_url="/auth/")
def delete_property(request, pk):
    """Видалення оголошення. Доступно лише власнику або адміну."""
    prop = get_object_or_404(Property, pk=pk)
    if not _can_manage_property(request.user, prop):
        messages.error(request, "Видаляти оголошення можуть лише його власник або адміністратор.")
        return redirect("properties:property_detail", pk=pk)
    if request.method == "POST":
        name = prop.name
        if prop.payments.exists():
            # Є фінансові записи — не видаляємо фізично, а архівуємо (історія платежів зберігається).
            prop.archived_at = timezone.now()
            prop.is_published = False
            prop.save(update_fields=["archived_at", "is_published", "updated_at"])
        else:
            prop.delete()
        messages.success(request, f"Оголошення «{name}» видалено.")
        return redirect("properties:home")
    return render(request, "properties/property_confirm_delete.html", {"property": prop})


@login_required(login_url="/auth/")
def auditor_form(request, pk=None):
    """Форма аудитора для додавання/редагування результатів перевірки."""
    if not request.user.is_staff:
        messages.error(request, "Форму аудиту може заповнювати тільки адміністратор.")
        if pk:
            return redirect("properties:property_detail", pk=pk)
        return redirect("properties:home")

    if pk:
        prop = get_object_or_404(Property, pk=pk)
        audit = getattr(prop, "audit", None)
        if not audit:
            audit = AccessibilityAudit(property=prop)
    else:
        prop = None
        audit = None

    if request.method == "POST":
        form = AccessibilityAuditForm(request.POST, instance=audit)
        if form.is_valid():
            obj = form.save(commit=False)
            if not obj.property_id:
                prop_id = request.POST.get("property", "")
                target = (
                    Property.objects.filter(pk=int(prop_id)).first() if prop_id.isdigit() else None
                )
                if target is not None and not hasattr(target, "audit"):
                    obj.property = target
                    obj.save()
                    return redirect("properties:property_detail", pk=obj.property_id)
                form.add_error(None, "Оберіть коректний об'єкт нерухомості без наявного аудиту.")
            else:
                obj.save()
                return redirect("properties:property_detail", pk=obj.property_id)
    else:
        form = AccessibilityAuditForm(instance=audit)

    context = {
        "form": form,
        "property": prop,
        "audit": audit,
        "properties_list": (
            Property.objects.filter(is_published=True).order_by("-created_at") if not prop else None
        ),
    }
    return render(request, "properties/auditor_form.html", context)


def _prepare_login_form(form):
    form.fields["username"].label = "Ім'я користувача"
    form.fields["password"].label = "Пароль"
    form.fields["username"].widget.attrs.update(
        {
            "class": "form-control",
            "placeholder": "Телефон або e-mail / логін",
            "autocomplete": "username",
        }
    )
    form.fields["password"].widget.attrs.update(
        {"class": "form-control", "placeholder": "Пароль", "autocomplete": "current-password"}
    )


def auth_page(request):
    """Об'єднана сторінка: Вхід та Реєстрація на одній сторінці (таби)."""
    if request.user.is_authenticated:
        return redirect("properties:add_listing")
    next_url = safe_next_url(
        request, (request.GET.get("next") or request.POST.get("next") or "")[:500], default=""
    )
    if next_url:
        # Використовується callback-ами Google/Telegram після підтвердження особи.
        request.session["auth_next_url"] = next_url
    active_tab = request.GET.get("tab", "login")
    login_form = AuthenticationForm(request)
    register_form = RegisterForm()
    if request.method == "POST":
        form_type = request.POST.get("form_type", "login")
        redirect_to = next_url or reverse("properties:add_listing")
        if form_type == "register":
            register_form = RegisterForm(request.POST)
            if register_form.is_valid():
                user = register_form.save()
                login(request, user)
                messages.success(request, "Реєстрація успішна. Додайте ваше оголошення.")
                return redirect(redirect_to)
            messages.error(request, "Виправте помилки у формі реєстрації.")
            active_tab = "register"
        else:
            username = request.POST.get("username", "")
            login_form = AuthenticationForm(request, data=request.POST)
            if login_is_locked(request, username):
                messages.error(
                    request, "Забагато невдалих спроб входу. Спробуйте пізніше або відновіть пароль."
                )
                _prepare_login_form(login_form)
                return render(
                    request,
                    "properties/auth.html",
                    _auth_context(login_form, register_form, "login", next_url),
                    status=429,
                )
            if login_form.is_valid():
                user = login_form.get_user()
                reset_login_failures(request, username)
                login(request, user)
                if request.POST.get("remember_me"):
                    request.session.set_expiry(60 * 60 * 24 * 14)
                else:
                    request.session.set_expiry(0)
                messages.success(request, "Ви успішно увійшли.")
                return redirect(redirect_to)
            register_login_failure(request, username)
            messages.error(request, "Невірний логін або пароль.")
            active_tab = "login"
    _prepare_login_form(login_form)
    return render(
        request,
        "properties/auth.html",
        _auth_context(login_form, register_form, active_tab, next_url),
    )


def _auth_context(login_form, register_form, active_tab, next_url):
    return {
        "login_form": login_form,
        "register_form": register_form,
        "active_tab": active_tab,
        "next_url": next_url,
        "google_enabled": google_enabled(),
        "telegram_enabled": telegram_enabled(),
        "telegram_bot_name": settings.TELEGRAM_BOT_NAME,
    }


def coming_soon(request):
    """Редірект зі старого роуту на єдину головну форму входу."""
    return redirect("properties:auth")


def register(request):
    """Редірект на об'єднану сторінку входу/реєстрації."""
    if request.user.is_authenticated:
        return redirect("properties:add_listing")
    from urllib.parse import urlencode

    next_url = safe_next_url(request, request.GET.get("next", ""), default="")
    url = reverse("properties:auth")
    if next_url:
        return redirect(f"{url}?{urlencode({'tab': 'register', 'next': next_url})}")
    return redirect(f"{url}?tab=register")


def login_view(request):
    """Редірект на об'єднану сторінку входу/реєстрації."""
    if request.user.is_authenticated:
        return redirect("properties:add_listing")
    qs = request.GET.urlencode()
    url = reverse("properties:auth")
    return redirect(f"{url}?{qs}" if qs else url)


@login_required(login_url="/auth/")
def add_listing(request):
    """Форма додавання оголошення (всі поля + рівні доступності). Після збереження — редірект на оплату."""
    if request.method == "POST":
        form = AddListingForm(request.POST, request.FILES)
        if form.is_valid():
            prop = form.save(commit=False)
            prop.owner = request.user
            prop.save()
            form.save_m2m()
            for i, f in enumerate(form.cleaned_extra_photos):
                PropertyPhoto.objects.create(property=prop, image=f, order=i)
            messages.success(request, "Оголошення збережено. Оберіть спосіб публікації.")
            return redirect("properties:payment", pk=prop.pk)
        else:
            messages.error(request, "Виправте помилки у формі.")
    else:
        form = AddListingForm()
        
    # Групування зручностей для шаблону
    from .models import Feature
    features = Feature.objects.all().order_by('category', 'name')
    raw_groups = {}
    for feature in features:
        cat = feature.category or 'other'
        if cat not in raw_groups:
            raw_groups[cat] = []
        raw_groups[cat].append(feature)
        
    ordered_groups = []
    category_names = {
        'apartment': 'В квартирі',
        'building': 'В будинку та на території',
        'rules': 'Правила проживання',
    }
    for cat_key in ['apartment', 'building', 'rules', 'other']:
        if cat_key in raw_groups:
            ordered_groups.append({
                'name': category_names.get(cat_key, 'Інше'),
                'features': raw_groups[cat_key]
            })
            
    # Список вибраних зручностей (для збереження стану при помилках валідації)
    checked_features = form['features'].value() or []
    checked_features = [str(x) for x in checked_features]

    context = {
        "form": form,
        "grouped_features": ordered_groups,
        "checked_features": checked_features,
    }
    return render(request, "properties/add_listing.html", context)


def logout_view(request):
    """
    Вихід з облікового запису — лише POST (CSRF-захищений).
    GET (напр. без JavaScript) показує сторінку підтвердження з кнопкою.
    """
    if request.method == "POST":
        logout(request)
        messages.info(request, "Ви вийшли з облікового запису.")
        return redirect("properties:index")
    if not request.user.is_authenticated:
        return redirect("properties:index")
    return render(request, "properties/logout_confirm.html")


@require_POST
def toggle_favorite(request, pk):
    """Додати або прибрати оголошення з обраного. POST; повертає JSON { is_favorite: bool }."""
    if not request.user.is_authenticated:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accepts(
            "application/json"
        ):
            return JsonResponse({"error": "login_required"}, status=401)
        from urllib.parse import urlencode

        return redirect(
            reverse("properties:auth") + "?" + urlencode({"next": request.build_absolute_uri()})
        )

    prop = get_object_or_404(_visible_properties(request.user), pk=pk)
    fav, created = PropertyFavorite.objects.get_or_create(user=request.user, property=prop)
    if not created:
        fav.delete()
        is_favorite = False
    else:
        is_favorite = True
    return JsonResponse({"is_favorite": is_favorite})


@login_required(login_url="/auth/")
def profile(request):
    """Сторінка профілю: дані користувача, його оголошення, обрані та адмін-панель."""
    if request.method == "POST":
        form = UserProfileForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Профіль оновлено.")
            return redirect("properties:profile")
    else:
        form = UserProfileForm(instance=request.user)

    user_listings = Property.objects.filter(
        owner=request.user, archived_at__isnull=True
    ).order_by("-created_at")
    favorite_listings = (
        Property.objects.filter(favorited_by__user=request.user)
        .order_by("-favorited_by__created_at")
        .distinct()
    )
    
    user_payments = request.user.payments.select_related("property").order_by("-created_at")[:50]
    reviews = ProfileReview.objects.filter(target_user=request.user)
    avg_rating = reviews.aggregate(Avg("rating"))["rating__avg"]

    pending_properties = None
    audit_requests = None
    if request.user.is_staff or request.user.is_superuser:
        pending_properties = (
            Property.objects.filter(moderation_status=ModerationStatus.PENDING)
            .select_related("owner")
            .prefetch_related("extra_photos", "features")
            .order_by("-created_at")
        )
        audit_requests = AuditRequest.objects.all().select_related("property", "applicant").order_by("-created_at")

    context = {
        "user_listings": user_listings, 
        "favorite_listings": favorite_listings,
        "user_payments": user_payments,
        "form": form,
        "reviews": reviews,
        "avg_rating": avg_rating,
        "pending_properties": pending_properties,
        "audit_requests": audit_requests,
    }
    return render(request, "properties/profile.html", context)


@login_required(login_url="/auth/")
def admin_approve_property(request, pk):
    """Схвалення оголошення адміністратором та підтвердження фотомодерації."""
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, "Доступ заборонено. Потрібні права адміністратора.")
        return redirect("properties:profile")
    if request.method == "POST":
        prop = get_object_or_404(Property, pk=pk)
        prop.moderation_status = ModerationStatus.APPROVED
        prop.is_published = True
        prop.is_verified_by_admin = True
        prop.verified_at = timezone.now()
        prop.moderation_note = ""
        prop.save(
            update_fields=[
                "moderation_status",
                "is_published",
                "is_verified_by_admin",
                "verified_at",
                "moderation_note",
            ]
        )
        messages.success(request, f"Оголошення «{prop.name}» успішно схвалено та пройшло фотоперевірку!")
    return redirect("properties:profile")


@login_required(login_url="/auth/")
def admin_verify_photos(request, pk):
    """Підтвердження лише фотомодерації для оголошення адміністратором."""
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, "Доступ заборонено. Потрібні права адміністратора.")
        return redirect("properties:profile")
    if request.method == "POST":
        prop = get_object_or_404(Property, pk=pk)
        prop.is_verified_by_admin = True
        prop.verified_at = timezone.now()
        prop.save(update_fields=["is_verified_by_admin", "verified_at"])
        messages.success(request, f"Фотоперевірку для «{prop.name}» успішно підтверджено!")
    return redirect("properties:profile")


@login_required(login_url="/auth/")
def admin_reject_property(request, pk):
    """Відхилення оголошення адміністратором із коментарем."""
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, "Доступ заборонено. Потрібні права адміністратора.")
        return redirect("properties:profile")
    if request.method == "POST":
        prop = get_object_or_404(Property, pk=pk)
        note = request.POST.get("moderation_note", "").strip()
        prop.moderation_status = ModerationStatus.REJECTED
        prop.moderation_note = note
        prop.save(update_fields=["moderation_status", "moderation_note"])
        messages.warning(request, f"Оголошення «{prop.name}» відхилено.")
    return redirect("properties:profile")


@login_required(login_url="/auth/")
def admin_update_audit_request(request, pk):
    """Оновлення заявки на аудит та встановлення оцінки доступності адміністратором."""
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, "Доступ заборонено. Потрібні права адміністратора.")
        return redirect("properties:profile")
    if request.method == "POST":
        audit_req = get_object_or_404(AuditRequest, pk=pk)
        new_status = request.POST.get("status")
        mobility_level = request.POST.get("mobility_level")
        total_score = request.POST.get("total_score")
        
        valid_statuses = [choice[0] for choice in AuditRequestStatus.choices]
        if new_status and new_status in valid_statuses:
            audit_req.status = new_status
            audit_req.save(update_fields=["status", "updated_at"])
            
        if audit_req.property:
            prop = audit_req.property
            # Спершу бал аудиту: AccessibilityAudit.save() перераховує mobility_level об'єкта,
            # тому ручний рівень застосовуємо після нього. update() не викликає перерахунок.
            if total_score:
                try:
                    score_val = float(total_score)
                except ValueError:
                    score_val = None
                if score_val is not None and 1 <= score_val <= 10:
                    audit = AccessibilityAudit.objects.filter(property=prop).first()
                    if audit is None:
                        audit = AccessibilityAudit.objects.create(property=prop)
                    AccessibilityAudit.objects.filter(pk=audit.pk).update(total_score=score_val)
                else:
                    messages.error(request, "Загальний бал має бути числом від 1 до 10.")
            prop.refresh_from_db()
            updated_fields = []
            if mobility_level in ("1", "2", "3", "4", "5"):
                prop.mobility_level = int(mobility_level)
                updated_fields.append("mobility_level")
            elif mobility_level == "":
                prop.mobility_level = None
                updated_fields.append("mobility_level")
            if new_status == "completed":
                prop.is_verified = True
                updated_fields.append("is_verified")
            if updated_fields:
                prop.save(update_fields=updated_fields)

        messages.success(request, f"Заявку на аудит #{audit_req.pk} успішно оновлено!")
    return redirect("properties:profile")


def public_profile(request, username):
    """Публічний профіль користувача для перегляду та оцінки."""
    target_user = get_object_or_404(User, username=username)
    user_listings = Property.objects.public().filter(owner=target_user).order_by("-created_at")
    reviews = ProfileReview.objects.filter(target_user=target_user)
    avg_rating = reviews.aggregate(Avg("rating"))["rating__avg"]

    if request.method == "POST":
        if not request.user.is_authenticated:
            messages.error(request, "Увійдіть, щоб залишити відгук.")
            return redirect("properties:public_profile", username=username)
        if request.user == target_user:
            messages.error(request, "Ви не можете оцінювати самі себе.")
            return redirect("properties:public_profile", username=username)
            
        form = ReviewForm(request.POST)
        if form.is_valid():
            review = form.save(commit=False)
            review.author = request.user
            review.target_user = target_user
            existing = ProfileReview.objects.filter(author=request.user, target_user=target_user).first()
            if existing:
                existing.rating = review.rating
                existing.text = review.text
                existing.save()
                messages.success(request, "Ваш відгук оновлено.")
            else:
                review.save()
                messages.success(request, "Ваш відгук додано.")
            return redirect("properties:public_profile", username=username)
    else:
        form = ReviewForm()

    context = {
        "target_user": target_user,
        "user_listings": user_listings,
        "reviews": reviews,
        "avg_rating": avg_rating,
        "form": form,
    }
    return render(request, "properties/public_profile.html", context)


def payment(request, pk):
    """
    Публікація та оплата розміщення оголошення (власник або адмін):
    - надіслати неопубліковане оголошення на модерацію;
    - оплатити розміщення (99 грн / 30 днів) або продовжити його через LiqPay;
    - історія платежів цього оголошення.
    """
    from .liqpay import payments_active

    prop = get_object_or_404(Property, pk=pk)
    if not request.user.is_authenticated:
        messages.error(request, "Увійдіть, щоб опублікувати оголошення.")
        return redirect("properties:property_detail", pk=pk)
    if not _can_manage_property(request.user, prop) or prop.archived_at:
        messages.error(request, "Керувати можна лише своїм активним оголошенням.")
        return redirect("properties:property_detail", pk=pk)
    if request.method == "POST" and request.POST.get("action") == "free" and not prop.is_published:
        # Публікація лише після перевірки адміністратором (модерація + фотоперевірка).
        prop.is_published = True
        prop.moderation_status = ModerationStatus.PENDING
        prop.is_verified_by_admin = False
        prop.save(update_fields=["is_published", "moderation_status", "is_verified_by_admin"])
        messages.success(
            request,
            "Оголошення надіслано на модерацію. Після перевірки адміністратором "
            "воно з'явиться в каталозі.",
        )
        return redirect("properties:property_detail", pk=pk)
    return render(
        request,
        "properties/payment.html",
        {
            "property": prop,
            "payments_enabled": payments_active(),
            "tariff": Tariff.placement(),
            "property_payments": prop.payments.order_by("-created_at")[:20],
            "awaiting_activation": prop.payments.filter(
                status="success", activated_at__isnull=True
            ).exists(),
        },
    )


def payment_paid(request, pk):
    """Старий маршрут (PayPal-імітація ТОП). Перенаправляє на оголошення."""
    get_object_or_404(Property, pk=pk)
    return redirect("properties:property_detail", pk=pk)


def request_audit(request, property_id=None):
    """Обробка замовлення професійного аудиту доступності житла."""
    target_property = None
    initial_data = {}
    if property_id:
        target_property = get_object_or_404(_visible_properties(request.user), pk=property_id)
        initial_data["property_address"] = f"{target_property.city}, {target_property.address}".strip(", ")

    if request.user.is_authenticated:
        full_name = f"{request.user.first_name} {request.user.last_name}".strip() or request.user.username
        initial_data["name"] = full_name
        if request.user.email:
            initial_data["email"] = request.user.email

    if request.method == "POST":
        form = AuditRequestForm(request.POST)
        if form.is_valid():
            audit_req = form.save(commit=False)
            if target_property:
                audit_req.property = target_property
            if request.user.is_authenticated:
                audit_req.applicant = request.user
            audit_req.save()
            messages.success(
                request,
                "Дякуємо! Ваша заявка на аудит успішно відправлена. Наш спеціаліст зв'яжеться з вами найближчим часом.",
            )
            if target_property:
                return redirect("properties:property_detail", pk=target_property.pk)
            return redirect("properties:services")
        else:
            messages.error(request, "Виправте помилки у формі замовлення аудиту.")
    else:
        form = AuditRequestForm(initial=initial_data)

    return render(
        request,
        "properties/audit_request.html",
        {"form": form, "property": target_property},
    )


@require_POST
def submit_consultation_request(request):
    """
    AJAX/JSON ендпоінт submit_consultation_request для обробки заявки на консалтинг.
    Валідація вхідних даних (телефон, обов'язкові поля, тип послуги) та збереження у базу.
    """
    if request.content_type == "application/json":
        try:
            data = json.loads(request.body)
        except Exception:
            return JsonResponse({"success": False, "errors": {"_all_": "Некоректний формат JSON даних."}}, status=400)
    else:
        data = request.POST

    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    email = (data.get("email") or "").strip()
    service_type = (data.get("service_type") or "").strip()
    message = (data.get("message") or data.get("comment") or "").strip()

    errors = {}
    if not name:
        errors["name"] = "Будь ласка, вкажіть ваше ім'я або ПІБ."

    if not phone:
        errors["phone"] = "Будь ласка, вкажіть номер телефону."
    elif len(phone) < 7:
        errors["phone"] = "Вкажіть коректний номер телефону."

    valid_service_types = [choice[0] for choice in ConsultationServiceType.choices]
    if not service_type or service_type not in valid_service_types:
        errors["service_type"] = "Будь ласка, оберіть коректний тип послуги."

    if errors:
        return JsonResponse({"success": False, "errors": errors}, status=400)

    consultation = ConsultationRequest.objects.create(
        name=name,
        phone=phone,
        email=email,
        service_type=service_type,
        message=message,
    )

    return JsonResponse({
        "success": True,
        "message": "Заявку успішно надіслано! Ми зв'яжемося з вами найближчим часом.",
        "id": consultation.id,
    })


def download_passport(request, pk):
    """
    Генерація та відображення офіційного "Паспорта доступності" об'єкта нерухомості за нормами ДБН.
    """
    prop = get_object_or_404(
        _visible_properties(request.user)
        .select_related("audit", "owner")
        .prefetch_related("features", "extra_photos"),
        pk=pk,
    )
    context = {
        "property": prop,
        "audit": getattr(prop, "audit", None),
        "now": timezone.now(),
    }
    return render(request, "properties/passport_pdf.html", context)


def compare_properties(request):
    """
    Сторінка порівняння 2–4 об'єктів нерухомості за параметрами ціни, площі та чеклистом доступності.
    """
    raw_ids = request.GET.get("ids", "").strip()
    property_ids = []
    if raw_ids:
        for item in raw_ids.split(","):
            if item.strip().isdigit():
                property_ids.append(int(item.strip()))

    properties = []
    if property_ids:
        properties = list(
            _visible_properties(request.user)
            .filter(id__in=property_ids)
            .select_related("audit")
            .prefetch_related("features", "extra_photos")[:4]
        )
        _assign_listing_photo_urls(properties)

    context = {
        "properties": properties,
        "property_ids": ",".join(str(p.id) for p in properties),
    }
    return render(request, "properties/compare.html", context)


def quick_view_api(request, pk):
    """
    API Quick View для швидкого миттєвого перегляду картки об'єкта в модальному вікні (< 50 мс).
    """
    prop = get_object_or_404(
        _visible_properties(request.user).select_related("audit").prefetch_related("extra_photos"),
        pk=pk,
    )
    photos = _property_photo_urls(prop)

    audit_score = prop.audit.total_score if hasattr(prop, "audit") and prop.audit else None

    data = {
        "id": prop.id,
        "name": prop.name,
        "price": f"{prop.price:,.0f}".replace(",", " ") if prop.price else "—",
        "listing_type_display": prop.get_listing_type_display(),
        "city": prop.city or "",
        "address": prop.address or "",
        "rooms": prop.rooms or "—",
        "area_sqm": prop.area_sqm or "—",
        "floor": prop.floor or "—",
        "total_floors": prop.total_floors or "—",
        "mobility_level": prop.mobility_level,
        "photos": photos,
        "audit_score": audit_score,
        "url": reverse("properties:property_detail", kwargs={"pk": prop.pk}),
    }
    return JsonResponse(data, json_dumps_params={"ensure_ascii": False})


def promote_listing(request, pk):
    """Стара система ТОП: продаж припинено. Маршрут залишено, щоб старі посилання не ламались."""
    get_object_or_404(Property, pk=pk)
    messages.info(request, "Послуга «ТОП» більше не надається.")
    return redirect("properties:property_detail", pk=pk)


@require_POST
def track_click(request, pk):
    """
    ЕНДПОІНТ для відстеження кліків («Показати телефон», «Контакт з продавцем»).
    """
    prop = get_object_or_404(Property, pk=pk)
    action = request.POST.get("action", "phone_click")
    return JsonResponse({"success": True, "property_id": prop.pk, "action": action})




