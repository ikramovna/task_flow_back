from datetime import date, datetime, time, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.models import Conversation, ConversationParticipant, Department, Event, FAQ, Membership, Message, Project, Report, Task, User, UserPreference


class Command(BaseCommand):
    help = "Create idempotent TaskFlow demo data"

    def handle(self, *args, **options):
        owner, _ = User.objects.get_or_create(email="admin@taskflow.local", defaults={"username": "taskflow-admin", "first_name": "Be", "last_name": "Confidency"})
        owner.set_password("TaskFlow123!")
        owner.save()
        UserPreference.objects.get_or_create(user=owner)
        department, _ = Department.objects.get_or_create(
            code="engineering",
            defaults={"name": "Engineering"},
        )
        owner_membership, _ = Membership.objects.get_or_create(
            user=owner,
            defaults={"role": Membership.Role.OWNER, "department": department},
        )
        if owner_membership.department_id is None:
            owner_membership.department = department
            owner_membership.save(update_fields=["department", "updated_at"])

        people = [
            ("sarah@taskflow.local", "Sarah", "Johnson", "Senior Developer"),
            ("mike@taskflow.local", "Mike", "Chen", "Full Stack Developer"),
            ("emily@taskflow.local", "Emily", "Davis", "UI/UX Designer"),
            ("alex@taskflow.local", "Alex", "Kumar", "Backend Developer"),
            ("lisa@taskflow.local", "Lisa", "Park", "Frontend Developer"),
        ]
        members = []
        for index, (email, first, last, title) in enumerate(people):
            user, _ = User.objects.get_or_create(email=email, defaults={"username": f"demo-{index}", "first_name": first, "last_name": last, "job_title": title})
            membership, _ = Membership.objects.get_or_create(
                user=user,
                defaults={"department": department},
            )
            if membership.department_id is None:
                membership.department = department
                membership.save(update_fields=["department", "updated_at"])
            members.append(user)

        projects_data = [
            ("Website Redesign", Project.Status.IN_PROGRESS, Project.Priority.HIGH, 15),
            ("Mobile App Development", Project.Status.IN_PROGRESS, Project.Priority.HIGH, 30),
            ("API Integration", Project.Status.IN_PROGRESS, Project.Priority.MEDIUM, 20),
            ("Database Migration", Project.Status.COMPLETED, Project.Priority.HIGH, 10),
            ("Security Audit", Project.Status.IN_PROGRESS, Project.Priority.LOW, 5),
            ("Marketing Dashboard", Project.Status.NOT_STARTED, Project.Priority.MEDIUM, 25),
        ]
        projects = []
        for name, project_status, priority, days in projects_data:
            project, _ = Project.objects.get_or_create(department=department, name=name, defaults={"status": project_status, "priority": priority, "due_date": date.today() + timedelta(days=days), "created_by": owner})
            if project.department_id is None:
                project.department = department
                project.save(update_fields=["department", "updated_at"])
            project.members.set(members[:3])
            projects.append(project)

        tasks_data = [
            ("Update database schema", Task.Status.COMPLETED, Task.Priority.HIGH, 100, "Development"),
            ("Implement user authentication", Task.Status.IN_PROGRESS, Task.Priority.HIGH, 80, "Development"),
            ("Design new landing page", Task.Status.IN_PROGRESS, Task.Priority.MEDIUM, 65, "Design"),
            ("Fix payment gateway bug", Task.Status.COMPLETED, Task.Priority.HIGH, 100, "Testing"),
            ("Write API documentation", Task.Status.NOT_STARTED, Task.Priority.LOW, 0, "Documentation"),
            ("Create mobile app mockups", Task.Status.IN_PROGRESS, Task.Priority.MEDIUM, 40, "Design"),
        ]
        for index, (title, task_status, priority, progress, category) in enumerate(tasks_data):
            task, _ = Task.objects.get_or_create(project=projects[index % len(projects)], title=title, defaults={"status": task_status, "priority": priority, "progress": progress, "category": category, "due_date": date.today() + timedelta(days=index + 1), "created_by": owner, "completed_at": timezone.now() if task_status == Task.Status.COMPLETED else None})
            task.assignees.set([members[index % len(members)]])

        start = timezone.make_aware(datetime.combine(date.today(), time(9)))
        Event.objects.get_or_create(department=department, title="Sprint Planning", starts_at=start, defaults={"ends_at": start + timedelta(minutes=90), "event_type": Event.Type.MEETING, "location": "Conference room A / Virtual", "created_by": owner})

        conversation, _ = Conversation.objects.get_or_create(department=department, title="Sarah Johnson", is_group=False)
        ConversationParticipant.objects.get_or_create(conversation=conversation, user=owner)
        ConversationParticipant.objects.get_or_create(conversation=conversation, user=members[0])
        if not conversation.messages.exists():
            Message.objects.create(conversation=conversation, sender=members[0], body="Hi! I wanted to update you on the database schema changes.")
            Message.objects.create(conversation=conversation, sender=owner, body="Great! What are the main changes?")

        for name, report_type in [("Weekly Productivity Summary", Report.Type.WEEKLY_PROGRESS), ("Project Risk Analysis", Report.Type.PROJECT_STATUS), ("Team Utilization Report", Report.Type.TEAM_PERFORMANCE)]:
            Report.objects.get_or_create(department=department, name=name, defaults={"report_type": report_type, "status": Report.Status.READY, "generated_by": owner})

        faq_items = [
            ("How do I create a new task?", "Open Tasks, select Create Task, fill in the details and assign it to a team member."),
            ("How do I add team members?", "Open Team Members and use Add Team Member. Department managers and admins can invite users."),
            ("How can I track project progress?", "The Projects and Analytics screens calculate progress from completed project tasks."),
            ("How do I assign tasks to team members?", "Choose one or more department members in the task assignees field."),
        ]
        for order, (question, answer) in enumerate(faq_items):
            FAQ.objects.get_or_create(question=question, defaults={"answer": answer, "sort_order": order})
        self.stdout.write(self.style.SUCCESS("Demo ready: admin@taskflow.local / TaskFlow123!"))
