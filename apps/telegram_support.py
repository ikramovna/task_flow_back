import json
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class TelegramSupportError(Exception):
    pass


def _request(token, method, *, data, content_type):
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise TelegramSupportError("Telegram service is unavailable.") from exc
    if not payload.get("ok"):
        raise TelegramSupportError(payload.get("description", "Telegram rejected the message."))


def _multipart(fields, file):
    boundary = f"TaskFlowBoundary{uuid.uuid4().hex}"
    extension = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }.get(file.content_type, "bin")
    chunks = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode("utf-8"),
            b"\r\n",
        ])
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="photo"; filename="screenshot.{extension}"\r\n'.encode(),
        f"Content-Type: {file.content_type}\r\n\r\n".encode(),
        file.read(),
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def send_support_message(*, token, chat_id, text, screenshot=None):
    if screenshot:
        if len(text) > 1024:
            message_body = urlencode({"chat_id": chat_id, "text": text}).encode()
            _request(
                token,
                "sendMessage",
                data=message_body,
                content_type="application/x-www-form-urlencoded",
            )
            caption = "Screenshot for the support request above."
        else:
            caption = text
        body, content_type = _multipart(
            {"chat_id": chat_id, "caption": caption},
            screenshot,
        )
        _request(token, "sendPhoto", data=body, content_type=content_type)
        return

    body = urlencode({"chat_id": chat_id, "text": text}).encode()
    _request(
        token,
        "sendMessage",
        data=body,
        content_type="application/x-www-form-urlencoded",
    )
