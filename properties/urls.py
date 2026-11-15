from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import views

app_name = "properties"

urlpatterns = [
    path("", views.index, name="index"),
    path("catalog/", views.home, name="home"),
    path("catalog/", views.home, name="catalog"),
    path("about/", views.about, name="about"),
    path("services/", views.services, name="services"),
    path("faq/", views.faq, name="faq"),
    path("contact/", views.contact, name="contact"),
    path("rivni-dostupnosti/", views.accessibility_levels, name="accessibility_levels"),
    path("property/<int:pk>/", views.property_detail, name="property_detail"),
    path("property/<int:pk>/download-passport/", views.download_passport, name="download_passport"),
    path("property/<int:pk>/quick-view/", views.quick_view_api, name="quick_view_api"),
    path("compare/", views.compare_properties, name="compare_properties"),
    path("property/<int:pk>/favorite/", views.toggle_favorite, name="toggle_favorite"),
    path("property/<int:pk>/delete/", views.delete_property, name="delete_property"),
    path("audit/request/", views.request_audit, name="request_audit"),
    path("audit/request/<int:property_id>/", views.request_audit, name="request_audit_property"),
    path("audit/new/", views.auditor_form, name="auditor_form_new"),
    path("audit/<int:pk>/", views.auditor_form, name="auditor_form"),
    path("auth/", views.auth_page, name="auth"),
    path("auth/google/login/", views.google_login, name="google_login"),
    path("auth/google/callback/", views.google_callback, name="google_callback"),
    path("auth/telegram/callback/", views.telegram_callback, name="telegram_callback"),
    # Дія тимчасово вимкнена: старі посилання ведуть на повідомлення про недоступність.
    path("auth/diia/login/", views.diia_disabled, name="diia_login"),
    path("auth/diia/callback/", views.diia_disabled, name="diia_callback"),
    path("auth/coming-soon/", views.coming_soon, name="coming_soon"),
    path(
        "auth/password-reset/",
        auth_views.PasswordResetView.as_view(
            template_name="registration/password_reset_form.html",
            email_template_name="registration/password_reset_email.html",
            success_url=reverse_lazy("properties:password_reset_done"),
            subject_template_name="registration/password_reset_subject.txt",
        ),
        name="password_reset",
    ),
    path(
        "auth/password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="registration/password_reset_done.html",
        ),
        name="password_reset_done",
    ),
    path(
        "auth/reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="registration/password_reset_confirm.html",
            success_url=reverse_lazy("properties:password_reset_complete"),
        ),
        name="password_reset_confirm",
    ),
    path(
        "auth/reset/done/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="registration/password_reset_complete.html",
        ),
        name="password_reset_complete",
    ),
    path("register/", views.register, name="register"),
    path("login/", views.login_view, name="login"),
    path("add-listing/", views.add_listing, name="add_listing"),
    path("logout/", views.logout_view, name="logout"),
    path("profile/", views.profile, name="profile"),
    path("profile/<str:username>/", views.public_profile, name="public_profile"),
    path("payment/<int:pk>/", views.payment, name="payment"),
    path("payment/<int:pk>/paid/", views.payment_paid, name="payment_paid"),
    path("property/<int:pk>/promote/", views.promote_listing, name="promote_listing"),
    path("property/<int:pk>/track-click/", views.track_click, name="track_click"),
    path("privacy-policy/", views.privacy_policy, name="privacy_policy"),
    path("terms-of-service/", views.terms_of_service, name="terms_of_service"),
    path("admin-action/property/<int:pk>/approve/", views.admin_approve_property, name="admin_approve_property"),
    path("admin-action/property/<int:pk>/verify-photos/", views.admin_verify_photos, name="admin_verify_photos"),
    path("admin-action/property/<int:pk>/reject/", views.admin_reject_property, name="admin_reject_property"),
    path("admin-action/audit-request/<int:pk>/update/", views.admin_update_audit_request, name="admin_update_audit_request"),
    path("consultation/submit/", views.submit_consultation_request, name="submit_consultation_request"),
]

