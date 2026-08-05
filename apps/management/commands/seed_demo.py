from datetime import date, datetime, time, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.models import Conversation, ConversationParticipant, Department, Event, FAQ, Message, Report, Task, User, UserPreference


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
        owner.department = department
        owner.role = User.Role.OWNER
        owner.save(update_fields=["department", "role"])

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
            if user.department_id is None:
                user.department = department
                user.save(update_fields=["department"])
            members.append(user)

        tasks_data = [
            ("Update database schema", Task.Status.COMPLETED, Task.Priority.HIGH, 100, "Development"),
            ("Implement user authentication", Task.Status.IN_PROGRESS, Task.Priority.HIGH, 80, "Development"),
            ("Design new landing page", Task.Status.IN_PROGRESS, Task.Priority.MEDIUM, 65, "Design"),
            ("Fix payment gateway bug", Task.Status.COMPLETED, Task.Priority.HIGH, 100, "Testing"),
            ("Write API documentation", Task.Status.NOT_STARTED, Task.Priority.LOW, 0, "Documentation"),
            ("Create mobile app mockups", Task.Status.IN_PROGRESS, Task.Priority.MEDIUM, 40, "Design"),
        ]
        for index, (title, task_status, priority, progress, category) in enumerate(tasks_data):
            task, _ = Task.objects.get_or_create(department=department, title=title, defaults={"status": task_status, "priority": priority, "progress": progress, "category": category, "due_date": date.today() + timedelta(days=index + 1), "created_by": owner, "completed_at": timezone.now() if task_status == Task.Status.COMPLETED else None})
            task.assignees.set([members[index % len(members)]])

        start = timezone.make_aware(datetime.combine(date.today(), time(9)))
        Event.objects.get_or_create(department=department, title="Sprint Planning", starts_at=start, defaults={"ends_at": start + timedelta(minutes=90), "event_type": Event.Type.MEETING, "location": "Conference room A / Virtual", "created_by": owner})

        conversation, _ = Conversation.objects.get_or_create(department=department, title="Sarah Johnson", is_group=False)
        ConversationParticipant.objects.get_or_create(conversation=conversation, user=owner)
        ConversationParticipant.objects.get_or_create(conversation=conversation, user=members[0])
        if not conversation.messages.exists():
            Message.objects.create(conversation=conversation, sender=members[0], body="Hi! I wanted to update you on the database schema changes.")
            Message.objects.create(conversation=conversation, sender=owner, body="Great! What are the main changes?")

        for name, report_type in [("Weekly Productivity Summary", Report.Type.WEEKLY_PROGRESS), ("Department Status", Report.Type.DEPARTMENT_STATUS), ("Team Utilization Report", Report.Type.TEAM_PERFORMANCE)]:
            Report.objects.get_or_create(department=department, name=name, defaults={"report_type": report_type, "status": Report.Status.READY, "generated_by": owner})

        faq_items = [
            ("How do I create a new task?", "Open Tasks, select Create Task, fill in the details and assign it to a team member."),
            ("How do I add team members?", "Open Team Members and use Add Team Member. Department managers and admins can invite users."),
            ("How can I track department progress?", "The Analytics screen calculates progress from completed department tasks."),
            ("How do I assign tasks to team members?", "Choose one or more department members in the task assignees field."),
        ]
        for order, (question, answer) in enumerate(faq_items):
            FAQ.objects.get_or_create(question=question, defaults={"answer": answer, "sort_order": order})
        self.stdout.write(self.style.SUCCESS("Demo ready: admin@taskflow.local / TaskFlow123!"))
