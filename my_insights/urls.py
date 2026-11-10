from django.urls import path

from . import views

app_name = "my_insights"

urlpatterns = [
    path("", views.home, name="home"),
    path("upload/", views.upload, name="upload"),
    path("clear/", views.clear, name="clear"),
    path("keep-alive/", views.keep_alive, name="keep_alive"),
    path("datasets/<int:dataset_id>/", views.dataset_detail, name="dataset"),
    path("datasets/<int:dataset_id>/rename/", views.dataset_rename, name="dataset_rename"),
    path("datasets/<int:dataset_id>/remove/", views.dataset_remove, name="dataset_remove"),
]
