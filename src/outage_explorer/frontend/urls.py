from django.urls import path

from . import views

app_name = "frontend"
urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login, name="login"),
    path("assets/<str:filename>", views.asset, name="asset"),
    path("assets/outage_explorer/<str:filename>", views.asset, name="static_asset"),
]
