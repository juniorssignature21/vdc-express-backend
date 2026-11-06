from django.apps import AppConfig


class TrackingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tracking"
    verbose_name = "TrackPro"

    def ready(self):
        from . import signals  # noqa: F401
