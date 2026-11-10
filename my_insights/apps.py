from django.apps import AppConfig


class MyInsightsConfig(AppConfig):
    name = "my_insights"
    verbose_name = "My Insights"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from django.contrib.auth.signals import user_logged_out

        from .workspace import forget_on_logout

        # Host apps with a login: logging out wipes that user's in-memory data.
        user_logged_out.connect(forget_on_logout, dispatch_uid="my_insights_logout")
