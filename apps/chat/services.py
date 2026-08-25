import json

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.utils import timezone
from rest_framework.renderers import JSONRenderer

from apps.models import Conversation, Message
from apps.notifications import notify_new_message
from apps.serializers import MessageSerializer


def conversation_group(conversation_id):
    return f"conversation.{conversation_id}"


def serialize_message(message):
    # Channel layers only accept JSON-safe primitives. DRF renderers also
    # normalize UUID and datetime values exactly as the REST API does.
    return json.loads(JSONRenderer().render(MessageSerializer(message).data))


def broadcast_message(message, *, client_id=None):
    payload = {
        "type": "chat.message",
        "message": serialize_message(message),
    }
    if client_id:
        payload["client_id"] = client_id
    async_to_sync(get_channel_layer().group_send)(
        conversation_group(message.conversation_id), payload
    )


@transaction.atomic
def create_message(*, conversation, sender, body="", attachment=None, client_id=None):
    message = Message.objects.create(
        conversation=conversation,
        sender=sender,
        body=body,
        **({"attachment": attachment} if attachment else {}),
    )
    Conversation.objects.filter(pk=conversation.pk).update(updated_at=timezone.now())
    notify_new_message(message)
    transaction.on_commit(lambda: broadcast_message(message, client_id=client_id))
    return message
