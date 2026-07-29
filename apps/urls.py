from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import AccountDeleteView, AnalyticsView, ConversationViewSet, EventViewSet, FAQViewSet, MembershipViewSet, MessageViewSet, PasswordChangeView, PasswordResetConfirmView, PasswordResetRequestView, PreferenceView, ProfileView, ProjectViewSet, ReportViewSet, SupportTicketViewSet, TaskViewSet, TimeEntryViewSet, TwoFactorView, WorkspaceViewSet

router = DefaultRouter()
router.register("workspaces", WorkspaceViewSet, basename="workspace")
router.register("members", MembershipViewSet, basename="member")
router.register("projects", ProjectViewSet, basename="project")
router.register("tasks", TaskViewSet, basename="task")
router.register("events", EventViewSet, basename="event")
router.register("conversations", ConversationViewSet, basename="conversation")
router.register("messages", MessageViewSet, basename="message")
router.register("reports", ReportViewSet, basename="report")
router.register("time-entries", TimeEntryViewSet, basename="time-entry")
router.register("faqs", FAQViewSet, basename="faq")
router.register("support-tickets", SupportTicketViewSet, basename="support-ticket")

urlpatterns = [
    path("auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    # Password reset is temporarily disabled. Uncomment when the frontend and SMTP are ready.
    # path("auth/password-reset/", PasswordResetRequestView.as_view(), name="password-reset"),
    # path("auth/password-reset/confirm/", PasswordResetConfirmView.as_view(), name="password-reset-confirm"),
    path("me/", ProfileView.as_view(), name="profile"),
    path("me/preferences/", PreferenceView.as_view(), name="preferences"),
    path("me/change-password/", PasswordChangeView.as_view(), name="change-password"),
    path("me/two-factor/", TwoFactorView.as_view(), name="two-factor"),
    path("me/delete-account/", AccountDeleteView.as_view(), name="delete-account"),
    path("analytics/", AnalyticsView.as_view(), name="analytics"),
    path("", include(router.urls)),
]
