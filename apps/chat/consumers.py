from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.utils import timezone

from apps.chat.services import conversation_group, create_message
from apps.models import Conversation, ConversationParticipant


class ConversationConsumer(AsyncJsonWebsocketConsumer):
    MAX_MESSAGE_LENGTH = 10_000

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

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

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
