from django.db import migrations


def backfill_project_departments(apps, schema_editor):
    Project = apps.get_model("apps", "Project")
    Membership = apps.get_model("apps", "Membership")
    Department = apps.get_model("apps", "Department")

    for project in Project.objects.filter(department__isnull=True).iterator():
        department_id = (
            Membership.objects.filter(
                workspace_id=project.workspace_id,
                user_id=project.created_by_id,
                is_active=True,
                department__isnull=False,
            )
            .values_list("department_id", flat=True)
            .first()
        )

        if department_id is None:
            member_department_ids = list(
                Membership.objects.filter(
                    workspace_id=project.workspace_id,
                    user_id__in=project.members.values("id"),
                    is_active=True,
                    department__isnull=False,
                )
                .values_list("department_id", flat=True)
                .distinct()[:2]
            )
            if len(member_department_ids) == 1:
                department_id = member_department_ids[0]

        if department_id is None:
            workspace_department_ids = list(
                Department.objects.filter(
                    workspace_id=project.workspace_id,
                    is_active=True,
                ).values_list("id", flat=True)[:2]
            )
            if len(workspace_department_ids) == 1:
                department_id = workspace_department_ids[0]

        if department_id is not None:
            Project.objects.filter(pk=project.pk).update(department_id=department_id)


class Migration(migrations.Migration):
    dependencies = [
        ("apps", "0007_project_department"),
    ]

    operations = [
        migrations.RunPython(backfill_project_departments, migrations.RunPython.noop),
    ]
