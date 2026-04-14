"""
Synology Chat platform adapter for Hermes Agent.

Connects to Synology Chat via webhook (inbound) and the External Chat API (outbound).

Inbound flow:
  1. Synology Chat sends POST form data to /synology-chat/webhook
  2. Adapter validates the token, extracts user_id/username/text
  3. Creates a MessageEvent and forwards to the agent

Outbound flow:
  1. Agent generates a response
  2. Adapter POSTs to Synology Chat External API (entry.cgi)
  3. Response is delivered to the specified user

Configuration (config.yaml):
  platforms:
    synology_chat:
      enabled: true
      token: "your_synology_chat_bot_token"
      extra:
        host: "0.0.0.0"          # Listen address (default: 0.0.0.0)
        port: 8086               # Listen port (default: 8086)
        api_endpoint: "https://nas-ip:5001/webapi/entry.cgi"
        ssl_verify: false        # Verify Synology SSL cert (default: false)
        webhook_path: "/synology-chat/webhook"

Requires:
  - aiohttp (already in messaging extras)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

try:
    from aiohttp import web
    import aiohttp as _aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    is_network_accessible,
)

logger = logging.getLogger(__name__)


def _safe_timestamp(ts: int) -> datetime:
    """Convert a Unix timestamp to datetime, with fallback for bad values."""
    try:
        if ts <= 0:
            return datetime.now()
        return datetime.fromtimestamp(ts)
    except (OSError, ValueError, OverflowError):
        return datetime.now()

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8086
DEFAULT_WEBHOOK_PATH = "/synology-chat/webhook"
DEFAULT_API_ENDPOINT = "https://127.0.0.1:5001/webapi/entry.cgi"
MAX_MESSAGE_LENGTH = 16384  # Synology Chat max message length


def check_synology_chat_requirements() -> bool:
    """Check if Synology Chat adapter dependencies are available."""
    return AIOHTTP_AVAILABLE


class SynologyChatAdapter(BasePlatformAdapter):
    """Synology Chat platform adapter.

    Receives messages from Synology Chat via webhook POST, and sends
    replies back via the Synology External Chat API.
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.SYNOLOGY_CHAT)

        extra = config.extra or {}
        self._host: str = extra.get("host", DEFAULT_HOST)
        self._port: int = int(extra.get("port", DEFAULT_PORT))
        self._webhook_path: str = extra.get("webhook_path", DEFAULT_WEBHOOK_PATH)
        self._api_endpoint: str = extra.get("api_endpoint", DEFAULT_API_ENDPOINT)
        self._ssl_verify: bool = extra.get("ssl_verify", False) in (True, "true", "1", "yes")
        self._token: str = config.token or ""

        # aiohttp server runner
        self._runner = None

        # HTTP session for outbound API calls
        self._http_session: Optional["_aiohttp.ClientSession"] = None

        # Delivery info: map chat_id -> user_id for replies
        # (Synology Chat webhooks don't include a reply URL, we must
        #  call the External API to send messages back)
        self._user_map: Dict[str, str] = {}  # chat_id -> synology user_id
        self._user_map_created: Dict[str, float] = {}
        self._user_map_ttl: int = 3600  # 1 hour

        # Idempotency: prevent duplicate processing of retries
        self._seen_messages: Dict[str, float] = {}
        self._idempotency_ttl: int = 300  # 5 minutes

        # Rate limiting
        self._rate_timestamps: list = []
        self._rate_limit: int = int(extra.get("rate_limit", 30))  # per minute

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Start the webhook HTTP server."""
        if not AIOHTTP_AVAILABLE:
            logger.warning("[synology_chat] aiohttp not installed. Run: pip install aiohttp")
            return False

        if not self._token:
            logger.warning("[synology_chat] No token configured. Set platforms.synology_chat.token in config.yaml")
            return False

        # Create HTTP session for outbound calls
        connector = _aiohttp.TCPConnector(ssl=self._ssl_verify)
        self._http_session = _aiohttp.ClientSession(connector=connector)

        # Port conflict detection
        import socket as _socket
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(("127.0.0.1", self._port))
            logger.error("[synology_chat] Port %d already in use. Set a different port in config.yaml", self._port)
            return False
        except (ConnectionRefusedError, OSError):
            pass  # port is free

        # Start aiohttp server
        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_post(self._webhook_path, self._handle_webhook)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        self._mark_connected()

        logger.info(
            "[synology_chat] Listening on %s:%d — webhook: %s — api: %s",
            self._host, self._port, self._webhook_path, self._api_endpoint,
        )
        return True

    async def disconnect(self) -> None:
        """Stop the webhook server and close HTTP session."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None
        self._mark_disconnected()
        logger.info("[synology_chat] Disconnected")

    # ------------------------------------------------------------------
    # Inbound: Webhook handler
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health — simple health check."""
        return web.json_response({"status": "ok", "platform": "synology_chat"})

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":
        """POST /synology-chat/webhook — receive messages from Synology Chat.

        Synology Chat sends form-encoded POST with:
          - token: bot token for validation
          - user_id: numeric user ID
          - username: display name
          - text: message content
          - timestamp: unix timestamp
        """
        # Rate limiting
        now = time.time()
        self._rate_timestamps[:] = [t for t in self._rate_timestamps if now - t < 60]
        if len(self._rate_timestamps) >= self._rate_limit:
            return web.json_response({"error": "Rate limit exceeded"}, status=429)
        self._rate_timestamps.append(now)

        # Read form data
        try:
            form_data = await request.post()
        except Exception as e:
            logger.error("[synology_chat] Failed to read POST data: %s", e)
            return web.json_response({"error": "Bad request"}, status=400)

        # Validate token
        incoming_token = form_data.get("token", "")
        if self._token and not hmac.compare_digest(incoming_token, self._token):
            logger.warning("[synology_chat] Invalid token received")
            return web.json_response({"error": "Unauthorized"}, status=401)

        # Extract fields
        user_id = str(form_data.get("user_id", ""))
        username = form_data.get("username", "unknown")
        text = form_data.get("text", "")
        timestamp = int(form_data.get("timestamp", "0"))
        # Synology Chat may send timestamp in milliseconds — normalize to seconds
        if timestamp > 1e12:
            timestamp = timestamp // 1000

        if not user_id or not text:
            return web.json_response({"status": "ignored", "reason": "missing fields"}, status=200)

        # Idempotency: skip duplicate messages (Synology may retry)
        msg_hash = hashlib.md5(f"{user_id}:{timestamp}:{text}".encode()).hexdigest()
        # Prune expired entries
        self._seen_messages = {k: v for k, v in self._seen_messages.items() if now - v < self._idempotency_ttl}
        if msg_hash in self._seen_messages:
            logger.debug("[synology_chat] Skipping duplicate message %s", msg_hash)
            return web.json_response({"status": "duplicate"}, status=200)
        self._seen_messages[msg_hash] = now

        # Prune old user map entries
        stale = [k for k, t in self._user_map_created.items() if now - t > self._user_map_ttl]
        for k in stale:
            self._user_map.pop(k, None)
            self._user_map_created.pop(k, None)

        # Build unique chat_id and store user_id mapping for replies
        chat_id = f"synology_chat:{user_id}"
        self._user_map[chat_id] = user_id
        self._user_map_created[chat_id] = now

        # Build source and event
        source = self.build_source(
            chat_id=chat_id,
            chat_name=f"Synology Chat ({username})",
            chat_type="dm",
            user_id=f"synology:{user_id}",
            user_name=username,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=dict(form_data),
            message_id=msg_hash,
            timestamp=_safe_timestamp(timestamp),
        )

        logger.info(
            "[synology_chat] Message from %s (user_id=%s): %s",
            username, user_id, text[:100],
        )

        # Non-blocking — return 200 immediately
        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        return web.json_response({"status": "ok"}, status=200)

    # ------------------------------------------------------------------
    # Outbound: Send replies via Synology External Chat API
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a reply to a Synology Chat user via the External API.

        The External Chat API endpoint is:
          POST https://nas:5001/webapi/entry.cgi
          Form data:
            api: SYNO.Chat.External
            method: chatbot
            version: 2
            token: <bot_token>
            payload: {"text": "...", "user_ids": [<user_id>]}
        """
        # Resolve target user_id from chat_id
        user_id = self._user_map.get(chat_id)
        if not user_id:
            # Try to extract from chat_id format: synology_chat:<user_id>
            parts = chat_id.split(":", 1)
            if len(parts) == 2:
                user_id = parts[1]

        if not user_id:
            logger.warning("[synology_chat] Cannot resolve user_id for chat_id=%s", chat_id)
            return SendResult(success=False, error=f"No user_id mapping for {chat_id}")

        # Truncate if needed
        text = content[:MAX_MESSAGE_LENGTH]

        # Build the API request
        # Try to convert user_id to int, but keep as string if it fails
        try:
            user_id_int = int(user_id)
            user_ids = [user_id_int]
        except (ValueError, TypeError):
            # If user_id is not a valid integer, use it as-is
            user_ids = [user_id]
        
        post_data = {
            "api": "SYNO.Chat.External",
            "method": "chatbot",
            "version": "2",
            "token": self._token,
            "payload": json.dumps({
                "text": text,
                "user_ids": user_ids,
            }),
        }

        try:
            if self._http_session:
                async with self._http_session.post(
                    self._api_endpoint,
                    data=post_data,
                    timeout=_aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status < 300:
                        response_data = await resp.json()
                        if response_data.get("success"):
                            return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
                        else:
                            error_msg = response_data.get("error", "Unknown error")
                            logger.error("[synology_chat] API error: %s", error_msg)
                            return SendResult(success=False, error=str(error_msg))
                    else:
                        body = await resp.text()
                        logger.error("[synology_chat] HTTP %d: %s", resp.status, body[:200])
                        return SendResult(success=False, error=f"HTTP {resp.status}: {body[:200]}")
            else:
                async with _aiohttp.ClientSession() as session:
                    async with session.post(
                        self._api_endpoint,
                        data=post_data,
                        timeout=_aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status < 300:
                            response_data = await resp.json()
                            if response_data.get("success"):
                                return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
                            else:
                                error_msg = response_data.get("error", "Unknown error")
                                return SendResult(success=False, error=str(error_msg))
                        else:
                            body = await resp.text()
                            return SendResult(success=False, error=f"HTTP {resp.status}: {body[:200]}")

        except asyncio.TimeoutError:
            return SendResult(success=False, error="Timeout sending to Synology Chat")
        except Exception as e:
            logger.error("[synology_chat] Send failed: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """No typing indicator for Synology Chat."""
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about the Synology Chat conversation."""
        user_id = self._user_map.get(chat_id, "unknown")
        return {
            "name": f"Synology Chat ({chat_id})",
            "type": "dm",
            "user_id": user_id,
        }
