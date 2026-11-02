from django.contrib.auth import views as auth_views
from django.urls import path

from . import web_views

app_name = "web"

urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="registration/login.html",
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", web_views.dashboard, name="dashboard"),
    path("upload/", web_views.upload, name="upload"),
    path("results/", web_views.results_latest, name="results-latest"),
    path("batches/<int:pk>/", web_views.results, name="results"),
    path("history/", web_views.history, name="history"),
    path("review/", web_views.review, name="review"),
    path("review/<int:pk>/submit/", web_views.review_submit, name="review-submit"),
    path("audit/", web_views.audit, name="audit"),
    path("settings/", web_views.settings_page, name="settings"),
]
