# TaskFlow Backend

DRF backend for the Tasks, Departments, Analytics, Calendar and Team Members screens.

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

All list endpoints support pagination. Tasks, members and events accept `?department=<uuid>`; search and ordering are available where appropriate.

| Screen | Endpoint |
|---|---|
| Login | `POST /api/v1/auth/token/` |
| Departments | `/api/v1/departments/` |
| Dashboard | `GET /api/v1/dashboard/` |
| Tasks | `/api/v1/tasks/` |
| Team members | `/api/v1/members/` |
| Team cards | `GET /api/v1/members/summary/?department=<uuid>` |
| Calendar | `/api/v1/events/` |
| Analytics | `GET /api/v1/analytics/?department=<uuid>` |
| Reports | `/api/v1/reports/` and `/api/v1/reports/<id>/download/` |
| Conversations | `/api/v1/conversations/` |
| Messages | `/api/v1/messages/?conversation=<uuid>` |
| Notifications | `/api/v1/notifications/` |
| Profile | `GET/PATCH /api/v1/me/` |
| Notifications/appearance | `GET/PATCH /api/v1/me/preferences/` |
| Password | `POST /api/v1/me/change-password/` |
| 2FA preference | `PATCH /api/v1/me/two-factor/` |
| Delete account | `POST /api/v1/me/delete-account/` |

Statuses use API-safe values: `not_started`, `in_progress`, `completed`, `at_risk`, `archived`. Priorities are `low`, `medium`, `high`.

## Notifications

Use `GET /api/v1/notifications/?unread=true` for unread items, `GET /api/v1/notifications/unread_count/` for the bell counter, `POST /api/v1/notifications/<id>/mark_read/`, and `POST /api/v1/notifications/mark_all_read/`.

Run `python manage.py generate_notifications` daily (cron or Task Scheduler) to create deadline reminders and overdue notifications. The command is idempotent and respects each user's notification preferences.
