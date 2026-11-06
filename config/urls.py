from pathlib import Path

from django.conf import settings
from django.contrib import admin
from django.http import FileResponse, Http404, JsonResponse
from django.urls import include, path, re_path
from django.views.static import serve


def health(_request):
    return JsonResponse({"ok": True})


def media(request, path):
    return serve(request, path, document_root=settings.MEDIA_ROOT)


def spa(request, path=""):
    """Serve the built React app (frontend/dist) with an index.html fallback for client-side routes."""
    dist: Path = settings.FRONTEND_DIST
    if not dist.is_dir():
        raise Http404("Frontend not built – run `npm run build` in frontend/ or use the Vite dev server.")
    candidate = (dist / path).resolve()
    if path and candidate.is_file() and dist.resolve() in candidate.parents:
        return FileResponse(open(candidate, "rb"))
    return FileResponse(open(dist / "index.html", "rb"), content_type="text/html")


urlpatterns = [
    path("api/health", health),
    path("api/", include("tracking.urls")),
    path("django-admin/", admin.site.urls),
    # uploaded package photos (served by Django; put a web server / CDN in front in production)
    re_path(r"^uploads/(?P<path>.*)$", media),
    re_path(r"^(?!api/|django-admin/|uploads/)(?P<path>.*)$", spa),
]
