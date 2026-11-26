"""Приватні файли перевірок житла: зберігаються поза MEDIA_ROOT, віддаються лише персоналу з правом перегляду."""

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied, SuspiciousFileOperation
from django.core.files.storage import FileSystemStorage
from django.http import FileResponse, Http404
from django.urls import reverse


class PrivateReportStorage(FileSystemStorage):
    def __init__(self, **kwargs):
        super().__init__(location=settings.PRIVATE_MEDIA_ROOT, **kwargs)

    def url(self, name):
        # Посилання в адмінці веде на захищений view, а не на публічний /media/.
        return reverse("properties:private_report", args=[name])


@staff_member_required
def private_report(request, name):
    if not request.user.has_perm("properties.view_listingverification"):
        raise PermissionDenied
    storage = PrivateReportStorage()
    try:
        if not storage.exists(name):
            raise Http404
        handle = storage.open(name, "rb")
    except SuspiciousFileOperation:
        raise Http404
    return FileResponse(handle, as_attachment=True, filename=name.rsplit("/", 1)[-1])
