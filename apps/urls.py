from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .serializers import SafeTokenRefreshSerializer
from .views import AccountDeleteView, AnalyticsView, ConversationViewSet, DashboardView, DepartmentViewSet, EventViewSet, MemberViewSet, MessageViewSet, NotificationViewSet, PasswordChangeView, PasswordResetConfirmView, PasswordResetRequestView, PreferenceView, ProfileView, ProjectViewSet, ReportViewSet, SupportBotView, TaskViewSet, TelegramIntegrationView, TelegramWebhookSetupView, TelegramWebhookView, TwoFactorView

router = DefaultRouter()
router.register("departments", DepartmentViewSet, basename="department")
router.register("members", MemberViewSet, basename="member")
router.register("projects", ProjectViewSet, basename="project")
router.register("tasks", TaskViewSet, basename="task")
router.register("events", EventViewSet, basename="event")
router.register("notifications", NotificationViewSet, basename="notification")
router.register("reports", ReportViewSet, basename="report")

chat_router = DefaultRouter()
chat_router.register("conversations", ConversationViewSet, basename="conversation")
chat_router.register("messages", MessageViewSet, basename="message")

urlpatterns = [
    path("auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(serializer_class=SafeTokenRefreshSerializer), name="token_refresh"),
    path("auth/password-reset/", PasswordResetRequestView.as_view(), name="password-reset"),
    path("auth/password-reset/confirm/", PasswordResetConfirmView.as_view(), name="password-reset-confirm"),
    path("me/", ProfileView.as_view(), name="profile"),
    path("me/preferences/", PreferenceView.as_view(), name="preferences"),
    path("me/change-password/", PasswordChangeView.as_view(), name="change-password"),
    path("me/two-factor/", TwoFactorView.as_view(), name="two-factor"),
    path("me/delete-account/", AccountDeleteView.as_view(), name="delete-account"),
    path("analytics/", AnalyticsView.as_view(), name="analytics"),
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("support/bot/", SupportBotView.as_view(), name="support-bot"),
    path("me/telegram/", TelegramIntegrationView.as_view(), name="telegram-integration"),
    path("telegram/webhook/", TelegramWebhookView.as_view(), name="telegram-webhook"),
    path("telegram/setup-webhook/", TelegramWebhookSetupView.as_view(), name="telegram-setup-webhook"),
    path("chat/", include(chat_router.urls)),
    path("", include(router.urls)),
]
