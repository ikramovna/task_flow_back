import asyncio
from contextlib import suppress
from datetime import timedelta

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.utils import timezone

from apps.chat.services import conversation_group, create_message
from apps.models import Conversation, ConversationParticipant, User, UserPresenceSession


class ConversationConsumer(AsyncJsonWebsocketConsumer):
    MAX_MESSAGE_LENGTH = 10_000
    PRESENCE_HEARTBEAT_SECONDS = 30
    PRESENCE_TIMEOUT_SECONDS = 75

    async def connect(self):
        user = self.scope["user"]
        if not user.is_authenticated:
            await self.close(code=4401)
            return

        self.conversation_id = self.scope["url_route"]["kwargs"]["conversation_id"]
        self.conversation = await self._get_conversation(user.pk, self.conversation_id)
        if self.conversation is None:
            await self.close(code=4403)
            return

        self.group_name = conversation_group(self.conversation_id)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        became_online = await self._register_presence()
        await self.send_json({"type": "presence.snapshot", "users": await self._presence_snapshot()})
        if became_online:
            await self._broadcast_presence(True)
        self.presence_task = asyncio.create_task(self._presence_heartbeat_loop())

    async def disconnect(self, close_code):
        presence_task = getattr(self, "presence_task", None)
        if presence_task:
            presence_task.cancel()
            with suppress(asyncio.CancelledError):
                await presence_task
        if self.scope.get("user") and self.scope["user"].is_authenticated:
            is_online, last_seen = await self._unregister_presence()
            if not is_online:
                await self._broadcast_presence(False, last_seen)
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def _presence_heartbeat_loop(self):
        while True:
            await asyncio.sleep(self.PRESENCE_HEARTBEAT_SECONDS)
            await self._touch_presence()

    async def _broadcast_presence(self, is_online, last_seen=None):
        event = {
            "type": "presence.changed",
            "user_id": str(self.scope["user"].pk),
            "is_online": is_online,
            "last_seen": last_seen.isoformat() if last_seen else None,
        }
        for conversation_id in await self._user_conversation_ids():
            await self.channel_layer.group_send(conversation_group(conversation_id), event)

    async def receive_json(self, content, **kwargs):
        event_type = content.get("type")
        if event_type == "message.send":
            await self._send_message(content)
        elif event_type == "typing.set":
            await self._set_typing(content)
        elif event_type == "conversation.read":
            await self._mark_read()
        else:
            await self._error("unsupported_event", "Unsupported event type.")

    async def _send_message(self, content):
        body = content.get("body", "")
        if not isinstance(body, str) or not body.strip():
            await self._error("invalid_message", "Message body is required.")
            return
        if len(body) > self.MAX_MESSAGE_LENGTH:
            await self._error("message_too_long", "Message body is too long.")
            return
        client_id = content.get("client_id")
        if client_id is not None and not isinstance(client_id, str):
            await self._error("invalid_client_id", "client_id must be a string.")
            return
        await self._create_message(body.strip(), client_id)

    async def _set_typing(self, content):
        is_typing = content.get("is_typing")
        if not isinstance(is_typing, bool):
            await self._error("invalid_typing_state", "is_typing must be a boolean.")
            return
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "chat.typing",
                "user_id": str(self.scope["user"].pk),
                "is_typing": is_typing,
            },
        )

    async def _mark_read(self):
        read_at = await self._update_last_read()
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "chat.read",
                "user_id": str(self.scope["user"].pk),
                "read_at": read_at.isoformat(),
            },
        )

    async def chat_message(self, event):
        payload = {"type": "message.created", "message": event["message"]}
        if event.get("client_id"):
            payload["client_id"] = event["client_id"]
        await self.send_json(payload)

    async def chat_typing(self, event):
        await self.send_json(
            {
                "type": "typing.updated",
                "user_id": event["user_id"],
                "is_typing": event["is_typing"],
            }
        )

    async def chat_read(self, event):
        await self.send_json(
            {
                "type": "conversation.read",
                "user_id": event["user_id"],
                "read_at": event["read_at"],
            }
        )

    async def presence_changed(self, event):
        if event["user_id"] == str(self.scope["user"].pk):
            return
        await self.send_json(
            {
                "type": "presence.changed",
                "user_id": event["user_id"],
                "is_online": event["is_online"],
                "last_seen": event.get("last_seen"),
            }
        )

    async def _error(self, code, detail):
        await self.send_json({"type": "error", "code": code, "detail": detail})

    @database_sync_to_async
    def _get_conversation(self, user_id, conversation_id):
        return Conversation.objects.filter(pk=conversation_id, participants=user_id).first()

    @database_sync_to_async
    def _create_message(self, body, client_id):
        return create_message(
            conversation=self.conversation,
            sender=self.scope["user"],
            body=body,
            client_id=client_id,
        )

    @database_sync_to_async
    def _update_last_read(self):
        link = ConversationParticipant.objects.get(
            conversation_id=self.conversation_id,
            user_id=self.scope["user"].pk,
        )
        link.last_read_at = timezone.now()
        link.save(update_fields=["last_read_at", "updated_at"])
        return link.last_read_at

    @database_sync_to_async
    def _register_presence(self):
        now = timezone.now()
        cutoff = now - timedelta(seconds=self.PRESENCE_TIMEOUT_SECONDS)
        sessions = UserPresenceSession.objects.filter(user_id=self.scope["user"].pk)
        sessions.filter(last_heartbeat__lt=cutoff).delete()
        was_online = sessions.filter(last_heartbeat__gte=cutoff).exists()
        UserPresenceSession.objects.update_or_create(
            channel_name=self.channel_name,
            defaults={"user_id": self.scope["user"].pk, "last_heartbeat": now},
        )
        User.objects.filter(pk=self.scope["user"].pk).update(last_seen_at=now)
        return not was_online

    @database_sync_to_async
    def _touch_presence(self):
        now = timezone.now()
        UserPresenceSession.objects.filter(channel_name=self.channel_name).update(last_heartbeat=now)
        User.objects.filter(pk=self.scope["user"].pk).update(last_seen_at=now)

    @database_sync_to_async
    def _unregister_presence(self):
        now = timezone.now()
        UserPresenceSession.objects.filter(channel_name=self.channel_name).delete()
        User.objects.filter(pk=self.scope["user"].pk).update(last_seen_at=now)
        cutoff = now - timedelta(seconds=self.PRESENCE_TIMEOUT_SECONDS)
        is_online = UserPresenceSession.objects.filter(
            user_id=self.scope["user"].pk,
            last_heartbeat__gte=cutoff,
        ).exists()
        return is_online, now

    @database_sync_to_async
    def _presence_snapshot(self):
        now = timezone.now()
        cutoff = now - timedelta(seconds=self.PRESENCE_TIMEOUT_SECONDS)
        participant_ids = list(
            ConversationParticipant.objects.filter(conversation_id=self.conversation_id)
            .values_list("user_id", flat=True)
        )
        online_ids = set(
            UserPresenceSession.objects.filter(
                user_id__in=participant_ids,
                last_heartbeat__gte=cutoff,
            ).values_list("user_id", flat=True)
        )
        return [
            {
                "user_id": str(user.pk),
                "is_online": user.pk in online_ids,
                "last_seen": user.last_seen_at.isoformat() if user.last_seen_at else None,
            }
            for user in User.objects.filter(pk__in=participant_ids)
        ]

    @database_sync_to_async
    def _user_conversation_ids(self):
        return list(
            ConversationParticipant.objects.filter(user_id=self.scope["user"].pk)
            .values_list("conversation_id", flat=True)
        )
