# TaskFlow Backend

DRF backend for the Tasks, Projects, Analytics, Calendar and Team Members Figma screens.

## Run locally

```powershell
.\.venv312\Scripts\python.exe manage.py migrate
.\.venv312\Scripts\python.exe manage.py seed_demo
.\.venv312\Scripts\python.exe manage.py runserver
```

Demo login: `admin@taskflow.local` / `TaskFlow123!`

- Swagger UI: `http://127.0.0.1:8000/api/docs/`
- OpenAPI schema: `http://127.0.0.1:8000/api/schema/`
- Admin: `http://127.0.0.1:8000/admin/`

## Main API

All list endpoints support pagination. Tasks, projects, members and events accept `?department=<uuid>`; search and ordering are available where appropriate.

| Screen | Endpoint |
|---|---|
| Login | `POST /api/v1/auth/token/` |
| Departments | `/api/v1/departments/` |
| Tasks | `/api/v1/tasks/` |
| Projects | `/api/v1/projects/` |
| Project cards | `GET /api/v1/projects/summary/?department=<uuid>` |
| Team members | `/api/v1/members/` |
| Team cards | `GET /api/v1/members/summary/?department=<uuid>` |
| Calendar | `/api/v1/events/` |
| Analytics | `GET /api/v1/analytics/?department=<uuid>` |
| Reports | `/api/v1/reports/` and `/api/v1/reports/<id>/download/` |
| Time tracking | `/api/v1/time-entries/` |
| Conversations | `/api/v1/conversations/` |
| Messages | `/api/v1/messages/?conversation=<uuid>` |
| Profile | `GET/PATCH /api/v1/me/` |
| Notifications/appearance | `GET/PATCH /api/v1/me/preferences/` |
| Password | `POST /api/v1/me/change-password/` |
| 2FA preference | `PATCH /api/v1/me/two-factor/` |
| Delete account | `POST /api/v1/me/delete-account/` |
| Help FAQ | `GET /api/v1/faqs/` |
| Support ticket | `/api/v1/support-tickets/` |

Statuses use API-safe values: `not_started`, `in_progress`, `completed`, `at_risk`, `archived`. Priorities are `low`, `medium`, `high`.
