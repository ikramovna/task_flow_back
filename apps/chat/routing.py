from django.urls import path

from apps.chat.consumers import ConversationConsumer

websocket_urlpatterns = [
    path("ws/chat/<uuid:conversation_id>/", ConversationConsumer.as_asgi()),
]
