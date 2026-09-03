"""Redis Streams implementation of the submission job queue contract."""

from __future__ import annotations

import math
import os
import re
import socket
import uuid
from collections.abc import Mapping
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlparse

from redis import Redis
from redis.exceptions import ResponseError

from .submission_jobs import JobDelivery, JobMessage

_SHA256 = re.compile(r"[0-9a-f]{64}")
_NAMESPACE = re.compile(r"[A-Za-z0-9:_-]+")
_MESSAGE_FIELDS = {
    "submission_id",
    "evaluation_identity_sha256",
    "contract_snapshot_sha256",
}
_LOOPBACK_REDIS_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_MAX_REDIS_CREDENTIAL_BYTES = 4096
_MINIMUM_REDIS_VERSION = (6, 2)
_PUBLISH_SCRIPT = """
local active_id = redis.call('HGET', KEYS[2], ARGV[1])
if active_id then
    local active_entry = redis.call('XRANGE', KEYS[1], active_id, active_id)
    if #active_entry ~= 0 then
        return false
    end
    redis.call('HDEL', KEYS[2], ARGV[1])
end
local new_id = redis.call(
    'XADD', KEYS[1], '*',
    'submission_id', ARGV[1],
    'evaluation_identity_sha256', ARGV[2],
    'contract_snapshot_sha256', ARGV[3]
)
redis.call('HSET', KEYS[2], ARGV[1], new_id)
return new_id
"""
_REGISTER_RECEIPT_SCRIPT = """
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1)
if #pending == 0 or pending[1][2] ~= ARGV[3] then
    return 0
end
redis.call('HSET', KEYS[2], ARGV[2], ARGV[4])
return 1
"""
_CLAIM_STALE_SCRIPT = """
local claimed = redis.call(
    'XAUTOCLAIM', KEYS[1], ARGV[1], ARGV[2], ARGV[3], ARGV[4], 'COUNT', 1
)
if #claimed[2] ~= 0 then
    redis.call('HSET', KEYS[2], claimed[2][1][1], ARGV[5])
end
if #claimed > 2 then
    for _, deleted_id in ipairs(claimed[3]) do
        redis.call('HDEL', KEYS[2], deleted_id)
    end
end
return claimed
"""
_ACK_SCRIPT = """
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1)
if #pending == 0 or pending[1][2] ~= ARGV[3]
        or redis.call('HGET', KEYS[3], ARGV[2]) ~= ARGV[4] then
    return 0
end
local entry = redis.call('XRANGE', KEYS[1], ARGV[2], ARGV[2])
local acknowledged = redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
if acknowledged ~= 1 then
    return 0
end
redis.call('XDEL', KEYS[1], ARGV[2])
redis.call('HDEL', KEYS[3], ARGV[2])
if #entry ~= 0 then
    local fields = entry[1][2]
    for index = 1, #fields, 2 do
        if fields[index] == 'submission_id' then
            if redis.call('HGET', KEYS[2], fields[index + 1]) == ARGV[2] then
                redis.call('HDEL', KEYS[2], fields[index + 1])
            end
            break
        end
    end
end
return 1
"""
_REQUEUE_SCRIPT = """
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1)
if #pending == 0 or pending[1][2] ~= ARGV[3]
        or redis.call('HGET', KEYS[2], ARGV[2]) ~= ARGV[4] then
    return false
end
local entry = redis.call('XRANGE', KEYS[1], ARGV[2], ARGV[2])
if #entry == 0 then
    return false
end
local new_id = redis.call('XADD', KEYS[1], '*', unpack(entry[1][2]))
local fields = entry[1][2]
for index = 1, #fields, 2 do
    if fields[index] == 'submission_id' then
        redis.call('HSET', KEYS[3], fields[index + 1], new_id)
        break
    end
end
redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
redis.call('XDEL', KEYS[1], ARGV[2])
redis.call('HDEL', KEYS[2], ARGV[2])
return new_id
"""


class RedisQueueMessageError(RuntimeError):
    """Raised when a Redis Stream entry violates the queue message contract."""


def resolve_redis_url(
    *,
    inline_url: str | None,
    credential_file: Path | None,
    allow_inline_credentials: bool,
) -> str:
    """Resolve a Redis URL without requiring secrets in process arguments."""

    if (inline_url is None) == (credential_file is None):
        raise ValueError("configure exactly one Redis URL source")
    if credential_file is not None:
        try:
            if not credential_file.is_file():
                raise ValueError("Redis credential path must be a regular file")
            if credential_file.stat().st_size > _MAX_REDIS_CREDENTIAL_BYTES:
                raise ValueError("Redis credential file is too large")
            redis_url = credential_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise ValueError("Redis credential file cannot be read") from error
        if not redis_url or "\n" in redis_url or "\r" in redis_url:
            raise ValueError("Redis credential file must contain exactly one URL")
    else:
        redis_url = inline_url
    if not isinstance(redis_url, str) or not redis_url:
        raise ValueError("Redis URL must not be empty")
    if not allow_inline_credentials and inline_url is not None:
        parsed = urlparse(redis_url)
        if parsed.password is not None:
            raise ValueError("production Redis credentials must use --redis-url-file")
    return redis_url


class RedisJobQueue:
    """At-least-once queue using one Redis Stream per contract snapshot."""

    def __init__(
        self,
        *,
        redis_url: str,
        routing_key: str,
        visibility_timeout_seconds: float = 45.0,
        consumer_name: str | None = None,
        namespace: str = "linguistic-oj",
        group_name: str = "submission-workers-v1",
    ) -> None:
        if not isinstance(redis_url, str) or not redis_url.strip():
            raise ValueError("redis_url must not be empty")
        parsed_url = urlparse(redis_url)
        if parsed_url.scheme not in {"redis", "rediss", "unix"}:
            raise ValueError("redis_url must use redis, rediss, or unix")
        if parsed_url.scheme == "redis" and parsed_url.hostname not in _LOOPBACK_REDIS_HOSTS:
            raise ValueError("non-loopback Redis connections must use rediss")
        if "decode_responses" in parse_qs(parsed_url.query):
            raise ValueError("redis_url must not configure decode_responses")
        if not isinstance(routing_key, str) or _SHA256.fullmatch(routing_key) is None:
            raise ValueError("routing_key must be a lowercase SHA-256 value")
        if (
            type(visibility_timeout_seconds) not in {int, float}
            or not math.isfinite(visibility_timeout_seconds)
            or visibility_timeout_seconds <= 0
        ):
            raise ValueError("visibility_timeout_seconds must be positive")
        if not isinstance(namespace, str) or _NAMESPACE.fullmatch(namespace) is None:
            raise ValueError("namespace contains unsupported characters")
        if not isinstance(group_name, str) or not group_name.strip():
            raise ValueError("group_name must not be empty")

        consumer_prefix = consumer_name or f"{socket.gethostname()}-{os.getpid()}"
        if not isinstance(consumer_prefix, str) or not consumer_prefix.strip():
            raise ValueError("consumer_name must not be empty")
        resolved_consumer = f"{consumer_prefix}-{uuid.uuid4().hex}"

        self._routing_key = routing_key
        self._visibility_timeout_ms = math.ceil(visibility_timeout_seconds * 1000)
        self._stream_name = f"{namespace}:jobs:{{{routing_key}}}"
        self._active_key = f"{self._stream_name}:active"
        self._receipt_key = f"{self._stream_name}:receipts"
        self._group_name = group_name
        self._consumer_name = resolved_consumer
        self._autoclaim_cursor = "0-0"
        self._prefer_reclaimed = True
        self._receive_lock = Lock()
        self._client = Redis.from_url(
            redis_url,
            decode_responses=False,
            socket_connect_timeout=5,
            socket_timeout=5,
            health_check_interval=30,
        )
        self._ensure_group()
        self._enforce_single_group()

    @property
    def routing_key(self) -> str:
        return self._routing_key

    @property
    def stream_name(self) -> str:
        return self._stream_name

    @property
    def visibility_timeout_seconds(self) -> float:
        return self._visibility_timeout_ms / 1000

    def health_check(self) -> None:
        if not self._client.ping():
            raise RuntimeError("Redis health check failed")
        server_info = self._client.info(section="server")
        raw_version = server_info.get("redis_version")
        try:
            version = tuple(int(part) for part in raw_version.split(".")[:2])
        except (AttributeError, TypeError, ValueError):
            raise RuntimeError("Redis did not report a valid version") from None
        if version < _MINIMUM_REDIS_VERSION:
            raise RuntimeError("Redis 6.2 or later is required")
        if self._client.eval("return 1", 0) != 1:
            raise RuntimeError("Redis EVAL capability check failed")

    def _ensure_group(self) -> None:
        try:
            self._client.xgroup_create(
                self._stream_name,
                self._group_name,
                id="0-0",
                mkstream=True,
            )
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    def _enforce_single_group(self) -> None:
        groups = self._client.xinfo_groups(self._stream_name)
        group_names = {
            self._text(group.get("name", group.get(b"name"))) for group in groups
        }
        if group_names != {self._group_name}:
            raise ValueError("a queue stream must have exactly one consumer group")

    def publish(self, message: JobMessage) -> None:
        if not isinstance(message, JobMessage):
            raise TypeError("message must be a JobMessage")
        if not message.submission_id:
            raise ValueError("submission_id must not be empty")
        if _SHA256.fullmatch(message.evaluation_identity_sha256) is None:
            raise ValueError("evaluation identity must be a lowercase SHA-256 value")
        if message.contract_snapshot_sha256 != self._routing_key:
            raise ValueError("message does not match the queue routing key")
        self._client.eval(
            _PUBLISH_SCRIPT,
            2,
            self._stream_name,
            self._active_key,
            message.submission_id,
            message.evaluation_identity_sha256,
            message.contract_snapshot_sha256,
        )

    def receive(self) -> JobDelivery | None:
        with self._receive_lock:
            return self._receive_locked()

    def _receive_locked(self) -> JobDelivery | None:
        if self._prefer_reclaimed:
            reclaimed = self._take_reclaimed()
            if reclaimed is not None:
                self._prefer_reclaimed = False
                entry, receipt_token = reclaimed
                return self._checked_delivery(entry, receipt_token=receipt_token)

        entry = self._take_fresh()
        if entry is not None:
            self._prefer_reclaimed = True
            return self._checked_delivery(entry)

        if not self._prefer_reclaimed:
            reclaimed = self._take_reclaimed()
            if reclaimed is not None:
                self._prefer_reclaimed = False
                entry, receipt_token = reclaimed
                return self._checked_delivery(entry, receipt_token=receipt_token)
        return None

    def _take_reclaimed(self) -> tuple[Any, str] | None:
        receipt_token = uuid.uuid4().hex
        reclaimed = self._client.eval(
            _CLAIM_STALE_SCRIPT,
            2,
            self._stream_name,
            self._receipt_key,
            self._group_name,
            self._consumer_name,
            self._visibility_timeout_ms,
            self._autoclaim_cursor,
            receipt_token,
        )
        self._autoclaim_cursor = self._text(reclaimed[0])
        reclaimed_messages = reclaimed[1] if len(reclaimed) > 1 else []
        if not reclaimed_messages:
            return None
        return reclaimed_messages[0], receipt_token

    def _take_fresh(self) -> Any | None:
        fresh = self._client.xreadgroup(
            self._group_name,
            self._consumer_name,
            {self._stream_name: ">"},
            count=1,
        )
        if not fresh:
            return None
        if isinstance(fresh, Mapping):
            entries = next(iter(fresh.values()), [])
            if entries and isinstance(entries[0], list):
                entries = entries[0]
        else:
            entries = fresh[0][1]
        return entries[0] if entries else None

    def ack(self, delivery: JobDelivery) -> bool:
        if not isinstance(delivery, JobDelivery):
            raise TypeError("delivery must be a JobDelivery")
        if delivery.message.contract_snapshot_sha256 != self._routing_key:
            return False
        acknowledged = self._client.eval(
            _ACK_SCRIPT,
            3,
            self._stream_name,
            self._active_key,
            self._receipt_key,
            self._group_name,
            delivery.delivery_id,
            self._consumer_name,
            delivery.receipt_token,
        )
        return acknowledged == 1

    def nack(self, delivery: JobDelivery) -> bool:
        if not isinstance(delivery, JobDelivery):
            raise TypeError("delivery must be a JobDelivery")
        if delivery.message.contract_snapshot_sha256 != self._routing_key:
            return False
        new_id = self._client.eval(
            _REQUEUE_SCRIPT,
            3,
            self._stream_name,
            self._receipt_key,
            self._active_key,
            self._group_name,
            delivery.delivery_id,
            self._consumer_name,
            delivery.receipt_token,
        )
        return bool(new_id)

    def close(self) -> None:
        self._client.close()

    def _checked_delivery(
        self,
        entry: Any,
        *,
        receipt_token: str | None = None,
    ) -> JobDelivery | None:
        delivery_id = self._entry_id(entry)
        if receipt_token is None:
            receipt_token = uuid.uuid4().hex
            registered = self._client.eval(
                _REGISTER_RECEIPT_SCRIPT,
                2,
                self._stream_name,
                self._receipt_key,
                self._group_name,
                delivery_id,
                self._consumer_name,
                receipt_token,
            )
            if registered != 1:
                return None
        try:
            return self._delivery_from_entry(entry, delivery_id, receipt_token)
        except RedisQueueMessageError:
            self._client.eval(
                _ACK_SCRIPT,
                3,
                self._stream_name,
                self._active_key,
                self._receipt_key,
                self._group_name,
                delivery_id,
                self._consumer_name,
                receipt_token,
            )
            raise

    def _delivery_from_entry(
        self,
        entry: Any,
        delivery_id: str,
        receipt_token: str,
    ) -> JobDelivery:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise RedisQueueMessageError("Redis Stream returned an invalid entry")
        fields = entry[1]
        if isinstance(fields, Mapping):
            field_pairs = list(fields.items())
        elif isinstance(fields, (list, tuple)) and len(fields) % 2 == 0:
            field_pairs = list(zip(fields[::2], fields[1::2], strict=True))
        else:
            raise RedisQueueMessageError("Redis Stream returned an invalid entry")
        if len(field_pairs) != len(_MESSAGE_FIELDS):
            raise RedisQueueMessageError("Redis Stream entry has invalid fields")
        try:
            decoded_fields = {
                self._text(field): self._text(value) for field, value in field_pairs
            }
        except (TypeError, UnicodeDecodeError) as error:
            raise RedisQueueMessageError(
                "Redis Stream entry fields must be valid UTF-8"
            ) from error
        if set(decoded_fields) != _MESSAGE_FIELDS or any(
            not value for value in decoded_fields.values()
        ):
            raise RedisQueueMessageError("Redis Stream entry has invalid fields")
        if _SHA256.fullmatch(decoded_fields["evaluation_identity_sha256"]) is None:
            raise RedisQueueMessageError("evaluation identity is not a SHA-256 value")
        if decoded_fields["contract_snapshot_sha256"] != self._routing_key:
            raise RedisQueueMessageError("Redis Stream entry has the wrong routing key")
        return JobDelivery(
            delivery_id=delivery_id,
            message=JobMessage(
                submission_id=decoded_fields["submission_id"],
                evaluation_identity_sha256=decoded_fields[
                    "evaluation_identity_sha256"
                ],
                contract_snapshot_sha256=decoded_fields["contract_snapshot_sha256"],
            ),
            receipt_token=receipt_token,
        )

    @classmethod
    def _entry_id(cls, entry: Any) -> str:
        if not isinstance(entry, (list, tuple)) or not entry:
            raise RedisQueueMessageError("Redis Stream returned an invalid entry")
        try:
            return cls._text(entry[0])
        except (TypeError, UnicodeDecodeError) as error:
            raise RedisQueueMessageError("Redis Stream returned an invalid entry ID") from error

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8")
        raise TypeError("Redis value is not text")
