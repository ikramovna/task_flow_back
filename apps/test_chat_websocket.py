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
        sender_socket = self.socket(self.sender)
        recipient_socket = self.socket(self.recipient)
        self.assertTrue((await sender_socket.connect())[0])
        self.assertTrue((await recipient_socket.connect())[0])

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
        sender_socket = self.socket(self.sender)
        recipient_socket = self.socket(self.recipient)
        self.assertTrue((await sender_socket.connect())[0])
        self.assertTrue((await recipient_socket.connect())[0])

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
