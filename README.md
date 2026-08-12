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
| Forgot password | `POST /api/v1/auth/password-reset/` |
| Reset password | `POST /api/v1/auth/password-reset/confirm/` |
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

## Telegram integration

Create one bot with BotFather and configure these environment variables:

```env
TELEGRAM_BOT_TOKEN=123456:replace-with-botfather-token
TELEGRAM_BOT_USERNAME=TaskFlowBot
TELEGRAM_WEBHOOK_SECRET=replace-with-a-long-random-secret
FRONTEND_URL=https://taskflow.example.com
```

After deploying to a public HTTPS address, a superuser can register the webhook once with `POST /api/v1/telegram/setup-webhook/`. The user-facing flow is:

1. `POST /api/v1/me/telegram/` returns a 15-minute `connect_url`.
2. Open the URL and press **Start** in Telegram.
3. `GET /api/v1/me/telegram/` confirms the connection.

Use `PATCH /api/v1/me/telegram/` with `{"notifications_enabled": false}` to mute Telegram, or `DELETE /api/v1/me/telegram/` to disconnect. New assignments, deadline reminders, and overdue notifications are sent through the existing notification service.
