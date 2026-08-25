from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase, override_settings
from rest_framework_simplejwt.tokens import AccessToken

from apps.models import Conversation, ConversationParticipant, Department, Message, User
from root.asgi import application


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class ConversationWebSocketTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.department = Department.objects.create(name="Product", code="ws-product")
        self.sender = User.objects.create_user(
            username="ws-sender",
            email="ws-sender@example.com",
            password="pass12345",
            department=self.department,
        )
        self.recipient = User.objects.create_user(
            username="ws-recipient",
            email="ws-recipient@example.com",
            password="pass12345",
            department=self.department,
        )
        self.outsider = User.objects.create_user(
            username="ws-outsider",
            email="ws-outsider@example.com",
            password="pass12345",
            department=self.department,
        )
        self.conversation = Conversation.objects.create(
            department=self.department,
            title="Release",
        )
        ConversationParticipant.objects.create(
            conversation=self.conversation, user=self.sender
        )
        ConversationParticipant.objects.create(
            conversation=self.conversation, user=self.recipient
        )

    def socket(self, user=None):
        token = f"?token={AccessToken.for_user(user)}" if user else ""
        return WebsocketCommunicator(
            application,
            f"/ws/chat/{self.conversation.pk}/{token}",
            headers=[
                (b"host", b"testserver"),
                (b"origin", b"http://testserver"),
            ],
        )

    async def connect_pair(self):
        sender_socket = self.socket(self.sender)
        recipient_socket = self.socket(self.recipient)
        self.assertTrue((await sender_socket.connect())[0])
        sender_snapshot = await sender_socket.receive_json_from()
        self.assertEqual(sender_snapshot["type"], "presence.snapshot")

        self.assertTrue((await recipient_socket.connect())[0])
        recipient_snapshot = await recipient_socket.receive_json_from()
        self.assertEqual(recipient_snapshot["type"], "presence.snapshot")
        recipient_online = await sender_socket.receive_json_from()
        self.assertEqual(recipient_online["type"], "presence.changed")
        self.assertEqual(recipient_online["user_id"], str(self.recipient.pk))
        self.assertTrue(recipient_online["is_online"])
        return sender_socket, recipient_socket

    async def test_rejects_anonymous_and_non_participant(self):
        anonymous = self.socket()
        connected, code = await anonymous.connect()
        self.assertFalse(connected)
        self.assertEqual(code, 4401)

        outsider = self.socket(self.outsider)
        connected, code = await outsider.connect()
        self.assertFalse(connected)
        self.assertEqual(code, 4403)

    async def test_broadcasts_and_persists_message(self):
        sender_socket, recipient_socket = await self.connect_pair()

        await sender_socket.send_json_to(
            {
                "type": "message.send",
                "body": "Ready to ship",
                "client_id": "local-123",
            }
        )
        sender_event = await sender_socket.receive_json_from()
        recipient_event = await recipient_socket.receive_json_from()

        self.assertEqual(sender_event["type"], "message.created")
        self.assertEqual(sender_event["client_id"], "local-123")
        self.assertEqual(recipient_event["message"]["body"], "Ready to ship")
        self.assertEqual(recipient_event["message"]["sender"], self.sender.pk)
        self.assertEqual(await Message.objects.acount(), 1)

        await sender_socket.disconnect()
        await recipient_socket.disconnect()

    async def test_validates_events_and_broadcasts_read_state(self):
        sender_socket, recipient_socket = await self.connect_pair()

        await sender_socket.send_json_to({"type": "message.send", "body": "   "})
        error = await sender_socket.receive_json_from()
        self.assertEqual(error["code"], "invalid_message")

        await recipient_socket.send_json_to({"type": "conversation.read"})
        sender_event = await sender_socket.receive_json_from()
        recipient_event = await recipient_socket.receive_json_from()
        self.assertEqual(sender_event["type"], "conversation.read")
        self.assertEqual(sender_event["user_id"], str(self.recipient.pk))
        self.assertEqual(recipient_event["type"], "conversation.read")

        await sender_socket.disconnect()
        await recipient_socket.disconnect()

    async def test_broadcasts_offline_after_last_connection_closes(self):
        sender_socket, recipient_socket = await self.connect_pair()

        await recipient_socket.disconnect()
        offline = await sender_socket.receive_json_from()
        self.assertEqual(offline["type"], "presence.changed")
        self.assertEqual(offline["user_id"], str(self.recipient.pk))
        self.assertFalse(offline["is_online"])
        self.assertIsNotNone(offline["last_seen"])

        await sender_socket.disconnect()
