import hashlib
import os
import time
import uuid

import pytest
from redis import Redis
from redis.exceptions import ResponseError

import linguistic_oj.redis_job_queue as redis_queue_module
from linguistic_oj.redis_job_queue import RedisJobQueue, RedisQueueMessageError
from linguistic_oj.submission_jobs import InMemoryJobQueue, JobMessage


class _FakeRedis:
    def __init__(self, *, resp3: bool = False) -> None:
        self.group_created = False
        self.fresh = []
        self.pending = {}
        self.reclaimable = set()
        self.active = {}
        self.receipts = {}
        self.next_id = 1
        self.closed = False
        self.resp3 = resp3

    def xgroup_create(self, stream, group, *, id, mkstream):
        if self.group_created:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        self.group_created = True

    def xinfo_groups(self, stream):
        return [{b"name": b"submission-workers-v1"}]

    def xadd(self, stream, fields):
        delivery_id = f"{self.next_id}-0"
        self.next_id += 1
        encoded_fields = {
            key.encode("utf-8") if isinstance(key, str) else key: (
                value.encode("utf-8") if isinstance(value, str) else value
            )
            for key, value in fields.items()
        }
        self.fresh.append((delivery_id, encoded_fields))
        return delivery_id

    def xautoclaim(self, stream, group, consumer, idle, *, start_id, count):
        for delivery_id in tuple(self.reclaimable):
            self.reclaimable.remove(delivery_id)
            fields, _ = self.pending[delivery_id]
            self.pending[delivery_id] = (fields, consumer)
            return ["0-0", [(delivery_id, fields)], []]
        return ["0-0", [], []]

    def xreadgroup(self, group, consumer, streams, *, count):
        if not self.fresh:
            return []
        entry = self.fresh.pop(0)
        self.pending[entry[0]] = (entry[1], consumer)
        if self.resp3:
            return {next(iter(streams)): [[entry]]}
        return [(next(iter(streams)), [entry])]

    def eval(self, script, key_count, *args):
        if script == redis_queue_module._PUBLISH_SCRIPT:
            stream, _, submission_id, identity, contract = args
            active_id = self.active.get(submission_id)
            if active_id is not None:
                active_exists = any(entry[0] == active_id for entry in self.fresh)
                active_exists = active_exists or active_id in self.pending
                if active_exists:
                    return None
                self.active.pop(submission_id)
            delivery_id = self.xadd(
                stream,
                {
                    "submission_id": submission_id,
                    "evaluation_identity_sha256": identity,
                    "contract_snapshot_sha256": contract,
                },
            )
            self.active[submission_id] = delivery_id
            return delivery_id
        if script == redis_queue_module._CLAIM_STALE_SCRIPT:
            _, _, group, consumer, idle, start_id, receipt_token = args
            claimed = self.xautoclaim(
                None,
                group,
                consumer,
                idle,
                start_id=start_id,
                count=1,
            )
            if claimed[1]:
                delivery_id, fields = claimed[1][0]
                self.receipts[delivery_id] = receipt_token
                flat_fields = [item for pair in fields.items() for item in pair]
                claimed[1] = [[delivery_id, flat_fields]]
            return claimed
        if script == redis_queue_module._REGISTER_RECEIPT_SCRIPT:
            _, _, _, delivery_id, consumer, receipt_token = args
            pending = self.pending.get(delivery_id)
            if pending is None or pending[1] != consumer:
                return 0
            self.receipts[delivery_id] = receipt_token
            return 1
        if script == redis_queue_module._ACK_SCRIPT:
            stream, _, _, _, delivery_id, consumer, receipt_token = args
        else:
            stream, _, _, _, delivery_id, consumer, receipt_token = args
        pending = self.pending.get(delivery_id)
        if (
            pending is None
            or pending[1] != consumer
            or self.receipts.get(delivery_id) != receipt_token
        ):
            return 0 if script == redis_queue_module._ACK_SCRIPT else None
        fields, _ = self.pending.pop(delivery_id)
        self.receipts.pop(delivery_id, None)
        self.reclaimable.discard(delivery_id)
        if script == redis_queue_module._ACK_SCRIPT:
            submission_id = fields.get(b"submission_id")
            if isinstance(submission_id, bytes):
                submission_id = submission_id.decode("utf-8")
            if self.active.get(submission_id) == delivery_id:
                self.active.pop(submission_id)
            return 1
        new_id = self.xadd(stream, fields)
        submission_id = fields.get(b"submission_id")
        if isinstance(submission_id, bytes):
            submission_id = submission_id.decode("utf-8")
        if submission_id is not None:
            self.active[submission_id] = new_id
        return new_id

    def close(self) -> None:
        self.closed = True


def _routing_key(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _message(routing_key: str) -> JobMessage:
    return JobMessage(
        submission_id="submission-1",
        evaluation_identity_sha256=_routing_key("identity"),
        contract_snapshot_sha256=routing_key,
    )


def test_in_memory_queue_recovers_abandoned_delivery() -> None:
    routing_key = _routing_key("contract")
    queue = InMemoryJobQueue(routing_key, visibility_timeout_seconds=0.01)
    queue.publish(_message(routing_key))
    abandoned = queue.receive()
    assert abandoned is not None

    time.sleep(0.02)
    recovered = queue.receive()
    assert recovered is not None
    assert recovered.delivery_id != abandoned.delivery_id
    assert recovered.message == abandoned.message
    assert queue.ack(abandoned) is False
    assert queue.ack(recovered) is True


def test_redis_queue_publish_reclaim_nack_and_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeRedis()
    monkeypatch.setattr(
        redis_queue_module.Redis,
        "from_url",
        lambda *args, **kwargs: client,
    )
    routing_key = _routing_key("contract")
    first = RedisJobQueue(
        redis_url="redis://localhost:6379/0",
        routing_key=routing_key,
        consumer_name="worker-1",
    )
    second = RedisJobQueue(
        redis_url="redis://localhost:6379/0",
        routing_key=routing_key,
        consumer_name="worker-1",
    )

    first.publish(_message(routing_key))
    first.publish(_message(routing_key))
    assert len(client.fresh) == 1
    delivery = first.receive()
    assert delivery is not None
    assert second.receive() is None

    client.reclaimable.add(delivery.delivery_id)
    reclaimed = second.receive()
    assert reclaimed is not None
    assert reclaimed.delivery_id == delivery.delivery_id
    assert reclaimed.message == delivery.message
    assert reclaimed.receipt_token != delivery.receipt_token
    assert first.ack(delivery) is False
    second.nack(reclaimed)
    redelivered = first.receive()
    assert redelivered is not None
    assert redelivered.delivery_id != delivery.delivery_id
    assert redelivered.message == delivery.message
    assert first.ack(redelivered) is True
    assert first.receive() is None

    first.close()
    assert client.closed is True


def test_redis_queue_rejects_wrong_contract_route(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeRedis()
    monkeypatch.setattr(
        redis_queue_module.Redis,
        "from_url",
        lambda *args, **kwargs: client,
    )
    queue = RedisJobQueue(
        redis_url="redis://localhost:6379/0",
        routing_key=_routing_key("contract-a"),
    )

    with pytest.raises(ValueError, match="routing key"):
        queue.publish(_message(_routing_key("contract-b")))

    with pytest.raises(ValueError, match="decode_responses"):
        RedisJobQueue(
            redis_url="redis://localhost:6379/0?decode_responses=true",
            routing_key=_routing_key("contract-a"),
        )


def test_redis_publish_repairs_missing_active_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeRedis()
    monkeypatch.setattr(
        redis_queue_module.Redis,
        "from_url",
        lambda *args, **kwargs: client,
    )
    routing_key = _routing_key("contract")
    queue = RedisJobQueue(redis_url="redis://localhost:6379/0", routing_key=routing_key)
    message = _message(routing_key)
    queue.publish(message)
    missing_id = client.fresh.pop()[0]

    queue.publish(message)

    assert len(client.fresh) == 1
    assert client.fresh[0][0] != missing_id


def test_redis_nack_moves_delivery_behind_fresh_work(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeRedis()
    monkeypatch.setattr(
        redis_queue_module.Redis,
        "from_url",
        lambda *args, **kwargs: client,
    )
    routing_key = _routing_key("contract")
    queue = RedisJobQueue(
        redis_url="redis://localhost:6379/0",
        routing_key=routing_key,
        consumer_name="worker-1",
    )
    first_message = _message(routing_key)
    second_message = JobMessage(
        submission_id="submission-2",
        evaluation_identity_sha256=first_message.evaluation_identity_sha256,
        contract_snapshot_sha256=routing_key,
    )
    queue.publish(first_message)
    queue.publish(second_message)

    first_delivery = queue.receive()
    assert first_delivery is not None
    assert queue.nack(first_delivery) is True
    next_delivery = queue.receive()
    assert next_delivery is not None
    assert next_delivery.message == second_message


def test_redis_same_instance_reclaim_rotates_receipt_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeRedis()
    monkeypatch.setattr(
        redis_queue_module.Redis,
        "from_url",
        lambda *args, **kwargs: client,
    )
    routing_key = _routing_key("contract")
    queue = RedisJobQueue(
        redis_url="redis://localhost:6379/0",
        routing_key=routing_key,
        consumer_name="worker",
    )
    queue.publish(_message(routing_key))
    stale = queue.receive()
    assert stale is not None
    client.reclaimable.add(stale.delivery_id)

    reclaimed = queue.receive()

    assert reclaimed is not None
    assert reclaimed.delivery_id == stale.delivery_id
    assert reclaimed.receipt_token != stale.receipt_token
    assert queue.ack(stale) is False
    assert queue.ack(reclaimed) is True


def test_redis_queue_removes_malformed_pending_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeRedis()
    monkeypatch.setattr(
        redis_queue_module.Redis,
        "from_url",
        lambda *args, **kwargs: client,
    )
    queue = RedisJobQueue(
        redis_url="redis://localhost:6379/0",
        routing_key=_routing_key("contract"),
        consumer_name="worker-1",
    )
    client.xadd(queue.stream_name, {"unexpected": "field"})

    with pytest.raises(RedisQueueMessageError, match="invalid fields"):
        queue.receive()
    assert client.pending == {}


def test_redis_queue_handles_resp3_and_removes_invalid_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeRedis(resp3=True)
    monkeypatch.setattr(
        redis_queue_module.Redis,
        "from_url",
        lambda *args, **kwargs: client,
    )
    routing_key = _routing_key("contract")
    queue = RedisJobQueue(
        redis_url="redis://localhost:6379/0?protocol=3",
        routing_key=routing_key,
        consumer_name="worker-1",
    )
    queue.publish(_message(routing_key))
    delivery = queue.receive()
    assert delivery is not None
    assert queue.ack(delivery) is True

    client.xadd(
        queue.stream_name,
        {
            b"submission_id": b"submission-invalid",
            b"evaluation_identity_sha256": b"\xff",
            b"contract_snapshot_sha256": routing_key.encode("ascii"),
        },
    )
    with pytest.raises(RedisQueueMessageError, match="valid UTF-8"):
        queue.receive()
    assert client.pending == {}


@pytest.mark.skipif(
    "REDIS_TEST_URL" not in os.environ,
    reason="REDIS_TEST_URL is required for the real Redis integration test",
)
def test_real_redis_visibility_recovery_and_ack() -> None:
    redis_url = os.environ["REDIS_TEST_URL"]
    routing_key = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    group_name = f"test-workers-{uuid.uuid4().hex}"
    first = RedisJobQueue(
        redis_url=redis_url,
        routing_key=routing_key,
        visibility_timeout_seconds=0.05,
        consumer_name="worker-1",
        namespace="linguistic-oj-test",
        group_name=group_name,
    )
    second = RedisJobQueue(
        redis_url=redis_url,
        routing_key=routing_key,
        visibility_timeout_seconds=0.05,
        consumer_name="worker-2",
        namespace="linguistic-oj-test",
        group_name=group_name,
    )
    cleanup = Redis.from_url(redis_url, decode_responses=True)
    resp3 = None

    try:
        first.publish(_message(routing_key))
        first.publish(_message(routing_key))
        delivery = first.receive()
        assert delivery is not None
        assert second.receive() is None

        deadline = time.monotonic() + 2
        reclaimed = None
        while reclaimed is None and time.monotonic() < deadline:
            time.sleep(0.02)
            reclaimed = second.receive()
        assert reclaimed is not None
        assert reclaimed.delivery_id == delivery.delivery_id
        assert reclaimed.message == delivery.message
        assert reclaimed.receipt_token != delivery.receipt_token
        assert first.ack(delivery) is False
        assert second.nack(reclaimed) is True
        redelivered = first.receive()
        assert redelivered is not None
        assert redelivered.delivery_id != delivery.delivery_id
        assert redelivered.message == delivery.message
        assert first.ack(redelivered) is True
        assert second.receive() is None

        for index in range(12):
            first.publish(
                JobMessage(
                    submission_id=f"bulk-{index}",
                    evaluation_identity_sha256=_routing_key("identity"),
                    contract_snapshot_sha256=routing_key,
                )
            )
        pending = [first.receive() for _ in range(12)]
        assert all(item is not None for item in pending)
        time.sleep(0.08)
        recovered_ids = set()
        recovery_deadline = time.monotonic() + 3
        while len(recovered_ids) < 12 and time.monotonic() < recovery_deadline:
            recovered = second.receive()
            if recovered is None:
                time.sleep(0.02)
                continue
            recovered_ids.add(recovered.message.submission_id)
            assert second.ack(recovered) is True
        assert recovered_ids == {f"bulk-{index}" for index in range(12)}

        cleanup.xadd(first.stream_name, {"unexpected": "field"})
        with pytest.raises(RedisQueueMessageError):
            first.receive()
        assert cleanup.xpending(first.stream_name, group_name)["pending"] == 0

        cleanup.xadd(
            first.stream_name,
            {
                b"submission_id": b"submission-invalid",
                b"evaluation_identity_sha256": b"\xff",
                b"contract_snapshot_sha256": routing_key.encode("ascii"),
            },
        )
        with pytest.raises(RedisQueueMessageError, match="valid UTF-8"):
            first.receive()
        assert cleanup.xpending(first.stream_name, group_name)["pending"] == 0

        separator = "&" if "?" in redis_url else "?"
        resp3 = RedisJobQueue(
            redis_url=f"{redis_url}{separator}protocol=3",
            routing_key=routing_key,
            consumer_name="worker-resp3",
            namespace="linguistic-oj-test",
            group_name=group_name,
        )
        resp3_message = JobMessage(
            submission_id="submission-resp3",
            evaluation_identity_sha256=_routing_key("identity"),
            contract_snapshot_sha256=routing_key,
        )
        resp3.publish(resp3_message)
        resp3_delivery = resp3.receive()
        assert resp3_delivery is not None
        assert resp3_delivery.message == resp3_message
        assert resp3.ack(resp3_delivery) is True
    finally:
        cleanup.delete(
            first.stream_name,
            f"{first.stream_name}:active",
            f"{first.stream_name}:receipts",
        )
        cleanup.close()
        if resp3 is not None:
            resp3.close()
        first.close()
        second.close()
