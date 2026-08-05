from rest_framework.permissions import BasePermission


class IsDepartmentMember(BasePermission):
    message = "You are not a member of this department."

    def has_permission(self, request, view):
        department_id = request.data.get("department") or request.query_params.get("department")
        if not department_id:
            return True
        if not request.user.is_authenticated:
            return False
        return request.user.role in ("owner", "admin") or str(request.user.department_id) == str(department_id)
