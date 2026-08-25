from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from django.db import close_old_connections
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError


@database_sync_to_async
def _user_for_token(raw_token):
    close_old_connections()
    authentication = JWTAuthentication()
    try:
        validated_token = authentication.get_validated_token(raw_token)
        return authentication.get_user(validated_token)
    except (InvalidToken, TokenError):
        return AnonymousUser()


class JwtAuthMiddleware:
    """Authenticate browser WebSockets with ``?token=<JWT access token>``."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        query = parse_qs(scope.get("query_string", b"").decode("utf-8"))
        tokens = query.get("token", [])
        scope["user"] = await _user_for_token(tokens[0]) if tokens else AnonymousUser()
        return await self.app(scope, receive, send)
