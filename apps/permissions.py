from rest_framework.permissions import BasePermission


class IsDepartmentMember(BasePermission):
    message = "You are not a member of this department."

    def has_permission(self, request, view):
        department_id = request.data.get("department") or request.query_params.get("department")
        if not department_id:
            return True
        if not request.user.is_authenticated:
            return False
        memberships = request.user.memberships.filter(is_active=True)
        return memberships.filter(role__in=("owner", "admin")).exists() or memberships.filter(
            department_id=department_id
        ).exists()
