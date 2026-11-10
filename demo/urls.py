from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("", RedirectView.as_view(url="/insights/", permanent=False)),
    path("insights/", include("my_insights.urls")),
]
