from django.urls import path

from catalog import views


urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("flujo-etl/", views.etl_flow, name="etl_flow"),
]
