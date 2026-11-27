from django import template

from properties.seo import listing_seo as _listing_seo

register = template.Library()


@register.simple_tag(takes_context=True)
def listing_seo(context, prop):
    """SEO-дані оголошення; кешуються на об'єкті, бо тег викликається з кількох блоків."""
    cached = getattr(prop, "_seo_cache", None)
    if cached is None:
        cached = prop._seo_cache = _listing_seo(context["request"], prop)
    return cached
