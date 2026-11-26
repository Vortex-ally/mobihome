from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("properties.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
elif getattr(settings, "SERVE_MEDIA", False):
    # static() при DEBUG=False нічого не додає — явний маршрут (serve захищає від виходу за MEDIA_ROOT).
    from django.urls import re_path
    from django.views.static import serve

    urlpatterns += [re_path(r"^%s(?P<path>.*)$" % settings.MEDIA_URL.lstrip("/"), serve,
                            {"document_root": settings.MEDIA_ROOT})]
