from django.urls import include, path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    path("", RedirectView.as_view(url="/findata/", permanent=False)),
    path("pick-company/", views.pick_company, name="pick_company"),
    path("demo/api/ledgers/", views.sample_ledgers),  # a pretend ERP API
    path("findata/", include("findata.urls")),
]
