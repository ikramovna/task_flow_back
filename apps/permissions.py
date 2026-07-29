from rest_framework.permissions import BasePermission


class IsWorkspaceMember(BasePermission):
    message = "You are not a member of this workspace."

    def has_permission(self, request, view):
        workspace_id = view.kwargs.get("workspace_pk") or request.data.get("workspace")
        if not workspace_id:
            return True
        return request.user.is_authenticated and request.user.memberships.filter(
            workspace_id=workspace_id, is_active=True
        ).exists()

