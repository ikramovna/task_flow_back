from django import forms
from django.contrib.admin.widgets import FilteredSelectMultiple

from .models import Membership, User


class BulkMembershipAdminForm(forms.ModelForm):
    ROLE_FIELDS = (
        ("owners", Membership.Role.OWNER),
        ("admins", Membership.Role.ADMIN),
        ("managers", Membership.Role.MANAGER),
        ("members", Membership.Role.MEMBER),
    )

    owners = forms.ModelMultipleChoiceField(
        queryset=User.objects.all(),
        required=False,
        widget=FilteredSelectMultiple("owners", is_stacked=False),
        help_text="Select all users who should be Owners.",
    )
    admins = forms.ModelMultipleChoiceField(
        queryset=User.objects.all(),
        required=False,
        widget=FilteredSelectMultiple("admins", is_stacked=False),
        help_text="Select all users who should be Admins.",
    )
    managers = forms.ModelMultipleChoiceField(
        queryset=User.objects.all(),
        required=False,
        widget=FilteredSelectMultiple("managers", is_stacked=False),
        help_text="Select all users who should be Managers.",
    )
    members = forms.ModelMultipleChoiceField(
        queryset=User.objects.all(),
        required=False,
        widget=FilteredSelectMultiple("members", is_stacked=False),
        help_text="Select all users who should be Members.",
    )

    class Meta:
        model = Membership
        fields = ("workspace", "department", "is_active")

    def clean(self):
        cleaned_data = super().clean()
        workspace = cleaned_data.get("workspace")
        department = cleaned_data.get("department")

        if workspace and department and department.workspace_id != workspace.id:
            self.add_error("department", "Department must belong to the selected workspace.")

        selected_roles = {}
        for field_name, role in self.ROLE_FIELDS:
            for user in cleaned_data.get(field_name) or ():
                if user.pk in selected_roles:
                    self.add_error(
                        field_name,
                        f"{user} is already selected as {selected_roles[user.pk]}. "
                        "A user can have only one role in a workspace.",
                    )
                else:
                    selected_roles[user.pk] = Membership.Role(role).label

        if not selected_roles:
            raise forms.ValidationError("Select at least one user for one of the roles.")

        return cleaned_data

    def save(self, commit=True):
        selections = [
            (user, role)
            for field_name, role in self.ROLE_FIELDS
            for user in self.cleaned_data[field_name]
        ]
        first_user, first_role = selections[0]
        workspace = self.cleaned_data["workspace"]
        existing_membership = Membership.objects.filter(
            workspace=workspace,
            user=first_user,
        ).first()
        if existing_membership:
            self.instance = existing_membership

        self.instance.workspace = workspace
        self.instance.department = self.cleaned_data.get("department")
        self.instance.is_active = self.cleaned_data["is_active"]
        self.instance.user = first_user
        self.instance.role = first_role
        self._remaining_memberships = selections[1:]
        return super().save(commit=commit)

    def save_remaining_memberships(self):
        for user, role in self._remaining_memberships:
            Membership.objects.update_or_create(
                workspace=self.instance.workspace,
                user=user,
                defaults={
                    "department": self.instance.department,
                    "role": role,
                    "is_active": self.instance.is_active,
                },
            )
        return len(self._remaining_memberships)
