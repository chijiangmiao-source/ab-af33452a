"""End-to-end tests against the running service (APP_BASE_URL).

Covers: HTTP smoke, interval splitting with residuals, merge of adjacent
equal-content segments, history invariance/recomputability, idempotent
replay, operation-id conflicts, stale revisions (incl. concurrent race),
uncovered batches and failure atomicity.
"""

import concurrent.futures

# ---------------------------------------------------------------- helpers


def publish(client, device_id, op, seen, start, end, content):
    return client.post(
        f"/v1/devices/{device_id}/calibrations",
        json={
            "operation_id": op,
            "seen_revision": seen,
            "interval": {"start": start, "end": end},
            "content": content,
        },
    )


def effective(client, device_id, batch, revision=None):
    params = {"batch": batch}
    if revision is not None:
        params["revision"] = revision
    return client.get(
        f"/v1/devices/{device_id}/calibrations/effective", params=params
    )


def check_effective(client, device_id, batch, revision, content, start, end):
    resp = effective(client, device_id, batch, revision)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revision"] == revision
    assert body["content"] == content
    assert body["interval"] == {"start": start, "end": end}


# ---------------------------------------------------------------- smoke


def test_health_smoke(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_publish_and_query_smoke(client, device_id):
    resp = publish(client, device_id, "op-1", 0, 100, 200, {"k": "v1"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["revision"] == 1
    assert body["interval"] == {"start": 100, "end": 200}
    assert body["content"] == {"k": "v1"}
    check_effective(client, device_id, 150, 1, {"k": "v1"}, 100, 200)


# ------------------------------------------------------- split / history


def test_interval_splitting_residuals_and_history(client, device_id):
    assert publish(client, device_id, "op-1", 0, 0, 100, {"v": "A"}).status_code == 201
    assert publish(client, device_id, "op-2", 1, 40, 60, {"v": "B"}).status_code == 201
    assert publish(client, device_id, "op-3", 2, 20, 80, {"v": "C"}).status_code == 201

    # Revision 1 must stay intact (历史不变).
    check_effective(client, device_id, 50, 1, {"v": "A"}, 0, 100)

    # Revision 2: left/right residuals around [40, 60).
    check_effective(client, device_id, 10, 2, {"v": "A"}, 0, 40)
    check_effective(client, device_id, 50, 2, {"v": "B"}, 40, 60)
    check_effective(client, device_id, 90, 2, {"v": "A"}, 60, 100)

    # Revision 3: middle segment fully replaced, outer residuals kept.
    check_effective(client, device_id, 10, 3, {"v": "A"}, 0, 20)
    check_effective(client, device_id, 50, 3, {"v": "C"}, 20, 80)
    check_effective(client, device_id, 90, 3, {"v": "A"}, 80, 100)

    # Latest revision is used when none is given.
    body = effective(client, device_id, 50).json()
    assert body["revision"] == 3
    assert body["content"] == {"v": "C"}


def test_adjacent_same_content_merges(client, device_id):
    assert publish(client, device_id, "op-1", 0, 0, 100, {"v": "A"}).status_code == 201
    assert publish(client, device_id, "op-2", 1, 100, 200, {"v": "A"}).status_code == 201

    # Neighbouring equal-content segments collapse into one interval.
    check_effective(client, device_id, 150, 2, {"v": "A"}, 0, 200)

    # Split the middle, then re-cover it with the same content: the map
    # merges back into a single [0, 200) segment.
    assert publish(client, device_id, "op-3", 2, 40, 60, {"v": "B"}).status_code == 201
    check_effective(client, device_id, 50, 3, {"v": "B"}, 40, 60)
    assert publish(client, device_id, "op-4", 3, 40, 60, {"v": "A"}).status_code == 201
    check_effective(client, device_id, 50, 4, {"v": "A"}, 0, 200)
    # ... while revision 3 still shows the split (历史可复算).
    check_effective(client, device_id, 10, 3, {"v": "A"}, 0, 40)


# ------------------------------------------------------------- idempotent


def test_idempotent_replay_returns_first_result(client, device_id):
    first = publish(client, device_id, "op-1", 0, 0, 50, {"a": 1})
    assert first.status_code == 201
    first_body = first.json()
    assert first_body["revision"] == 1

    replay = publish(client, device_id, "op-1", 0, 0, 50, {"a": 1})
    assert replay.status_code == 200
    assert replay.json() == first_body  # 同操作同内容 → 首次结果

    # The replay must not have advanced the revision counter.
    device = client.get(f"/v1/devices/{device_id}").json()
    assert device["current_revision"] == 1


def test_operation_id_reused_with_other_params_conflicts(client, device_id):
    assert publish(client, device_id, "op-x", 0, 0, 10, {"a": 1}).status_code == 201

    for kwargs in ({"content": {"a": 2}}, {"start": 0, "end": 20}, {"seen": 1}):
        args = {"op": "op-x", "seen": 0, "start": 0, "end": 10, "content": {"a": 1}}
        args.update(kwargs)
        resp = publish(client, device_id, **args)
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "OPERATION_CONFLICT"


# ------------------------------------------------------------------ stale


def test_stale_revision_rejected(client, device_id):
    assert publish(client, device_id, "op-1", 0, 0, 10, {"a": 1}).status_code == 201
    resp = publish(client, device_id, "op-2", 0, 10, 20, {"a": 2})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "STALE_REVISION"


def test_concurrent_publish_single_winner(client, device_id):
    assert publish(client, device_id, "op-init", 0, 0, 100, {"v": 1}).status_code == 201

    def attempt(i):
        return publish(
            client, device_id, f"op-race-{i}", 1, 100 + i, 200 + i, {"v": 100 + i}
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))

    winners = [r for r in results if r.status_code == 201]
    losers = [r for r in results if r.status_code == 409]
    # 相同所见修订的并发发布只有一个可以成功。
    assert len(winners) == 1, [r.status_code for r in results]
    assert len(losers) == 7
    for loser in losers:
        assert loser.json()["error"]["code"] == "STALE_REVISION"

    device = client.get(f"/v1/devices/{device_id}").json()
    assert device["current_revision"] == 2


# ------------------------------------------------------- errors / atomic


def test_uncovered_batch_and_unknown_device(client, device_id):
    resp = effective(client, device_id, 5)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DEVICE_NOT_FOUND"

    assert publish(client, device_id, "op-1", 0, 10, 20, {"a": 1}).status_code == 201

    resp = effective(client, device_id, 5)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BATCH_NOT_COVERED"

    # Half-open interval: the end boundary is excluded.
    resp = effective(client, device_id, 20)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BATCH_NOT_COVERED"

    resp = effective(client, device_id, 15, revision=99)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "REVISION_NOT_FOUND"


def test_invalid_interval_rejected(client, device_id):
    resp = publish(client, device_id, "op-bad", 0, 50, 50, {"v": "X"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_INTERVAL"

    resp = publish(client, device_id, "op-bad-2", 0, 60, 50, {"v": "X"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_INTERVAL"


def test_failed_publish_leaves_no_partial_state(client, device_id):
    assert publish(client, device_id, "op-1", 0, 0, 100, {"v": "A"}).status_code == 201

    # Failing publishes of different kinds must not leave partial splits
    # nor advance the revision counter.
    assert publish(client, device_id, "op-bad", 1, 50, 50, {"v": "X"}).status_code == 400
    assert publish(client, device_id, "op-bad-2", 0, 0, 10, {"v": "X"}).status_code == 409
    assert publish(client, device_id, "op-1", 0, 0, 100, {"v": "DIFFERENT"}).status_code == 409

    device = client.get(f"/v1/devices/{device_id}").json()
    assert device["current_revision"] == 1
    check_effective(client, device_id, 50, 1, {"v": "A"}, 0, 100)

    # A well-formed publish with the right seen_revision still succeeds.
    assert publish(client, device_id, "op-2", 1, 100, 150, {"v": "B"}).status_code == 201
