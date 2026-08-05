import re

from drf_spectacular.openapi import AutoSchema


class TaskFlowAutoSchema(AutoSchema):
    """Group Swagger operations by their business area."""

    TAGS = {
        "auth": "Auth",
        "me": "Auth",
        "departments": "Departments",
        "members": "Members",
        "tasks": "Tasks",
        "events": "Events",
        "conversations": "Conversations",
        "messages": "Messages",
        "reports": "Reports",
        "time-entries": "Time Entries",
        "analytics": "Analytics",
    }

    def get_tags(self):
        path_parts = [
            part
            for part in self.path.strip("/").split("/")
            if part and not part.startswith("{")
        ]
        if path_parts and path_parts[0].lower() == "api":
            path_parts.pop(0)
        if path_parts and re.fullmatch(r"v\d+", path_parts[0], re.IGNORECASE):
            path_parts.pop(0)

        if not path_parts:
            return super().get_tags()

        resource = path_parts[0]
        return [self.TAGS.get(resource, resource.replace("-", " ").title())]
