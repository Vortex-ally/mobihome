"""Контекст-процесори для шаблонів."""


def current_url_name(request):
    """Додає в контекст current_url_name для безпечного підсвічування поточної вкладки."""
    url_name = ""
    if getattr(request, "resolver_match", None) is not None:
        url_name = getattr(request.resolver_match, "url_name", "") or ""
    return {"current_url_name": url_name}


def _format_phone(phone):
    """+380991234567 -> +38 (099) 123-45-67; інший формат показується як є."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 12 and digits.startswith("380"):
        d = digits[2:]
        return f"+38 ({d[:3]}) {d[3:6]}-{d[6:8]}-{d[8:]}"
    return phone


def _tel(phone):
    """Значення для href="tel:…": лише + і цифри."""
    return "".join(ch for ch in phone if ch.isdigit() or ch == "+")


def site_contacts(request):
    """Єдине джерело контактів сайту (settings / .env)."""
    from django.conf import settings

    phone, office = settings.SITE_PHONE, settings.SITE_OFFICE_PHONE
    return {"site_contacts": {
        "phone_tel": _tel(phone), "phone": _format_phone(phone),
        "office_tel": _tel(office), "office": _format_phone(office),
        "email": settings.SITE_EMAIL, "support_email": settings.SITE_SUPPORT_EMAIL,
    }}
