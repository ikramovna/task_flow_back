# TaskFlow Backend

DRF backend for the Tasks, Departments, Analytics, Calendar and Team Members screens.

## Run locally

Create a PostgreSQL database and configure the `POSTGRES_*` variables from
`.env.example`. PostgreSQL is required; this project does not use SQLite.

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
| Projects | `/api/v1/projects/` |
| Dashboard | `GET /api/v1/dashboard/` |
| Tasks | `/api/v1/tasks/` |
| Team members | `/api/v1/members/` |
| Team cards | `GET /api/v1/members/summary/?department=<uuid>` |
| Calendar | `/api/v1/events/` |
| Analytics | `GET /api/v1/analytics/?department=<uuid>` |
| Reports | `/api/v1/reports/` and `/api/v1/reports/<id>/download/` |
| Conversations | `/api/v1/chat/conversations/` |
| Messages | `/api/v1/chat/messages/?conversation=<uuid>` |
| Notifications | `/api/v1/notifications/` |
| Profile | `GET/PATCH /api/v1/me/` |
| Notifications/appearance | `GET/PATCH /api/v1/me/preferences/` |
| Password | `POST /api/v1/me/change-password/` |
| 2FA preference | `PATCH /api/v1/me/two-factor/` |
| Delete account | `POST /api/v1/me/delete-account/` |

Statuses use API-safe values: `not_started`, `in_progress`, `completed`, `at_risk`, `archived`. Priorities are `low`, `medium`, `high`.

Tasks may include an optional `project` UUID. Omitting it creates a standalone
department task. Project progress and task counts are calculated from the tasks
linked to that project.

## Notifications

Use `GET /api/v1/notifications/?unread=true` for unread items, `GET /api/v1/notifications/unread_count/` for the bell counter, `POST /api/v1/notifications/<id>/mark_read/`, and `POST /api/v1/notifications/mark_all_read/`.

Run `python manage.py generate_notifications` daily (cron or Task Scheduler) to create deadline reminders and overdue notifications. The command is idempotent and respects each user's notification preferences.

## Database backups

By default, the backup command creates one backup per day and keeps the latest
five successful backups. You can override this with `BACKUP_INTERVAL_DAYS` and
`BACKUP_RETENTION_COUNT` in `.env`; `BACKUP_DIR` defaults to `backups`. Run the
following command daily from cron or Windows Task Scheduler:

```powershell
.\.venv\Scripts\python.exe manage.py backup_database
```

The command skips the backup until the configured interval has elapsed. Use
`python manage.py backup_database --force` for an immediate backup. PostgreSQL's
`pg_dump` command must be installed and available on `PATH`.

## Real-time chat

Keep using the REST messages endpoint for history, pagination, and attachments. For
live text messages, connect with an access token returned by the login endpoint:

```text
ws://127.0.0.1:8000/ws/chat/<conversation-uuid>/?token=<access-token>
```

Send `{"type":"message.send","body":"Hello","client_id":"local-123"}`. The
server broadcasts a `message.created` event to every connected participant.
`typing.set` (with an `is_typing` boolean) and `conversation.read` are also
supported. Unauthorized users are closed with code `4401`; authenticated users
outside the conversation are closed with `4403`.

On connection the server sends a presence snapshot:

```json
{"type":"presence.snapshot","users":[{"user_id":"<uuid>","is_online":true,"last_seen":"<iso-datetime>"}]}
```

Status changes are broadcast to shared conversations as
`{"type":"presence.changed","user_id":"<uuid>","is_online":false,"last_seen":"<iso-datetime>"}`.
Presence sessions are heartbeat-backed, so multiple tabs/devices are handled
without marking a user offline until their final connection closes.

Local development falls back to an in-memory channel layer. Set `REDIS_URL` in
production so WebSocket events work across multiple Daphne/ASGI workers.

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

## AI task creation and changes (Tiko and Telegram)

Both channels use the same service, task permissions, assignee validation, and
assignment notifications. Configure `OPENAI_API_KEY`, `OPENAI_TASK_MODEL`
(default `gpt-5.4-mini`) and `OPENAI_TRANSCRIPTION_MODEL`
(default `gpt-4o-mini-transcribe`) in the backend environment, then run
`python manage.py migrate`. Text and voice contents are sent to OpenAI for
extraction/transcription; API keys stay on the server. The implementation follows
the official [structured output](https://developers.openai.com/api/docs/guides/structured-outputs)
and [speech transcription](https://developers.openai.com/api/docs/guides/speech-to-text)
APIs. Model availability depends on the configured API account.

### Telegram

Connect the account through the Profile link as described above. In a private
bot chat, `/start` displays an inline **Create task** button. Press it (or send `/create`), then
send text or a voice message, for example:

> Muslima Zokirjonovaga websiteni fix qilsin, deadline 23 may.

The bot creates a task, assigns the uniquely matched active employee, and replies
with its title, assignee, deadline, and link. Voice messages are limited to 5
minutes and 20 MB. Unlinked users and group chats cannot create tasks. A repeated
delivery of the same Telegram message reuses its stored result.
To change a task, send text or voice such as **“Edit my last created task: set
priority to high”**. To delete it, send **“Delete my last created task”** and
press the confirmation button within 10 minutes. The bot accepts Uzbek and
Russian requests too. Only an active Owner, Admin, or Manager can change tasks
they created through this AI flow. A task can also be identified by its exact
title or UUID; ambiguous targets are never changed.
For voice, transcription is guided with TaskFlow vocabulary and a uniquely
matched assignee may differ by one letter in either the first name or surname.
If uploaded audio still needs clarification, both Tiko and Telegram show the
words heard and the assignee name interpreted by the AI so the user can resend
the complete corrected task. "End of September" and equivalent named-month
phrases mean the last calendar day of that month. Task links go directly to
the frontend's `/?task=<id>#tasks` route.

After deploying the inline-button update, register the webhook again using
`POST /api/v1/telegram/setup-webhook/` so Telegram delivers `callback_query`
updates as well as messages. Send `/start` to remove the old reply keyboard and
display the English inline menu. Bot guidance, confirmations and errors are in
English; task input can still be English, Uzbek or Russian. The AI writes task
titles and descriptions in English while preserving employee and project names.

The bot also provides an English dashboard with **Create task**, **Voice task**,
**Template**, **Example**, and **Help** screens. Each button acknowledges the press
and sends a fresh screen so repeated selections remain visible. `/menu` opens a
fresh dashboard. Templates and examples use copyable
code blocks. Task confirmations display the assignee, deadline and priority.
Register the webhook again after deploying this menu update: setup also runs
`setMyCommands` and `setChatMenuButton` to enable Telegram's native **Menu** button
next to the message composer. This is a native bot menu, not a Mini App, and
requires no frontend deployment or extra website.

`/menu` sends one Telegram request; inline callbacks acknowledge the press, then
return the selected screen directly in the webhook response. Telegram API calls taking at least two seconds are
logged as `Telegram <method> took <seconds>s` without message text or tokens, so
production logs can distinguish Telegram network delay from AI processing time.

### Tiko frontend integration

This repository contains the backend only. Add a **Task yaratish** mode and a
microphone/audio upload control to the frontend Tiko widget. Keep the existing
feedback mode on `POST /api/v1/support/bot/`; send task requests directly to
`POST /api/v1/ai/tasks/` with the user's Bearer access token. Support feedback is
not interpreted as a task and the support Telegram chat is not an identity source.
Assignment notifications continue to reach connected Telegram accounts.

Text request (JSON):

```json
{
  "request_id": "2c1e3012-a7b3-4e2c-8ed8-ab5b48188e92",
  "text": "Muslima Zokirjonovaga websiteni fix qilsin, deadline 23 may"
}
```

For voice, send `multipart/form-data` with `request_id` and `audio` instead of
`text`. Accepted extensions: OGG, MP3, MP4, MPEG, MPGA, M4A, WAV, WEBM, FLAC;
maximum 20 MB. Browser MediaRecorder WEBM files work with this endpoint. Let the
browser set the multipart boundary. Send exactly one of `text` or `audio`.

Generate a UUID per submission and reuse it on network retries. The response is
`201` with `status: "created"`, `message`, `transcript`, and `task` (including its
ID, main assignee, department, optional project and ISO deadline). Replaying the
same request returns the same result; changing content with the same ID is `400`.

The same endpoint accepts edit and delete instructions as text or audio. For
example, submit `{"request_id": "<new UUID>", "text": "Edit my last created task:
set priority to high"}`. An edit returns HTTP `200` with `status: "updated"`,
`message`, `transcript`, and the updated `task`. A delete request returns HTTP
`200` with `status: "needs_confirmation"`, `confirmation_code`, `message`, and
the targeted `task` ID/title; the task still exists. Show a confirmation dialog
with the task title. Only after the user confirms, send a **new** request ID and
`text: "CONFIRM DELETE <confirmation_code>"`. A successful confirmation returns
`status: "deleted"` and the task ID/title. Codes expire after 10 minutes and
can be used once. Canceling the dialog requires no API call. Refresh the task
list/detail after `updated` or `deleted`.

An ambiguous/missing employee, invalid or past date, unknown project, or incomplete
request returns `200` with `status: "needs_clarification"`, `message`, and
`transcript`, without creating a task. Display the message and ask the user to
resubmit the **complete corrected request with a new UUID**. Follow-ups are
stateless: sending only a name or date will not complete a previous request.
Names match case-insensitively, ignoring apostrophe variants; uncertain spelling
is never auto-assigned. Use the employee's email to disambiguate duplicate names.
Dates without a year use their next occurrence in Asia/Tashkent. No deadline is
invented when omitted. Explicit projects must belong to the assignee's department.

Unauthenticated/unauthorized requests return `401/403`, invalid input returns
`400`, and provider/configuration failures return `503`. Error responses follow
the existing `{success: false, errors: ...}` API envelope. Audio is processed in
memory and not retained; the transcript and result are stored for deduplication.
Processing is synchronous (up to two 45-second AI calls plus Telegram download);
configure proxy request timeouts accordingly. PostgreSQL locks serialize AI
submissions by user across workers. Apply per-user/IP request limits at the
deployment gateway when exposing this paid API publicly.

Run the backend regression tests with `python manage.py test apps.test_ai_tasks
apps.tests`; tests mock external AI/Telegram calls and use PostgreSQL.
