# -*- coding: utf-8 -*-
"""API-level lock-conflict simulation: concurrent user actions stay clean.

Simulates the user-visible conflict shapes: parallel project creation, copy
racing creation (the old global name-lock timeout), and snapshot polling
hammering a project while edits are applied.  The invariant everywhere is
"no busy/5xx caused by lock contention".
"""
from __future__ import annotations

import asyncio

from services.project_files.json_pointer import hash_json_value


def _create_payload(request_id: str, name: str) -> dict:
    return {
        "clientRequestId": request_id,
        "name": name,
        "scenario": "general",
        "aspectRatio": "16:9",
        "resolution": "720P",
    }


def test_parallel_creates_and_copies_never_hit_lock_timeouts(
    app,
    run_scenario,
):
    async def scenario(client):
        source = await client.post(
            "/projects",
            json=_create_payload("request-source", "Source"),
        )
        assert source.status_code == 201
        source_id = source.json()["projectId"]

        creates = [
            client.post(
                "/projects",
                json=_create_payload(f"request-{index}", f"Storm {index}"),
            )
            for index in range(6)
        ]
        copies = [
            client.post(
                f"/projects/{source_id}/copy",
                headers={"Idempotency-Key": f"copy-{index}"},
            )
            for index in range(2)
        ]
        listings = [client.get("/projects") for _ in range(10)]
        responses = await asyncio.gather(*creates, *copies, *listings)
        listed = await client.get("/projects")
        return responses, listed

    responses, listed = run_scenario(app, scenario)
    for response in responses:
        assert response.status_code < 500, response.text
    creates, copies = responses[:6], responses[6:8]
    assert all(item.status_code == 201 for item in creates)
    assert all(item.status_code == 201 for item in copies)
    # Distinct durable Projects, addressed by id.
    copy_ids = {item.json()["projectId"] for item in copies}
    assert len(copy_ids) == 2
    items = listed.json()["items"]
    names = {item["name"] for item in items}
    assert {"Source", "Source copy"} <= names
    assert {f"Storm {index}" for index in range(6)} <= names
    # Accepted tradeoff of publishing outside the global name lock: two
    # simultaneous copies of one Project can pick the same display name
    # (the suffix scan cannot see a sibling that has not published yet).
    # Names are never an addressing key — Projects are addressed by id — so
    # this is cosmetic, and it buys a name lock that never spans the asset
    # tree copy (which used to cause routine 10s lock timeouts).
    copy_named = [
        item for item in items if item["name"].startswith("Source copy")
    ]
    assert len({item["projectId"] for item in copy_named}) == 2


def test_snapshot_polling_during_edits_never_returns_busy(app, run_scenario):
    async def scenario(client):
        created = await client.post(
            "/projects",
            json=_create_payload("request-edit", "Edited"),
        )
        assert created.status_code == 201
        project_id = created.json()["projectId"]
        project_url = f"/projects/{project_id}/project"

        async def edit_loop() -> list[int]:
            statuses: list[int] = []
            name = "Edited"
            for index in range(10):
                current = await client.get(project_url)
                assert current.status_code == 200
                snapshot = current.json()
                new_name = f"Edited {index}"
                response = await client.patch(
                    project_url,
                    json={
                        "clientCommandId": f"command-{index}",
                        "editSessionId": "edit",
                        "baseGeneration": snapshot["generation"],
                        "baseEtag": snapshot["etag"],
                        "operations": [
                            {
                                "op": "replace",
                                "path": "/name",
                                "value": new_name,
                                "expectedValueHash": hash_json_value(name),
                            },
                        ],
                    },
                )
                statuses.append(response.status_code)
                if response.status_code == 200:
                    name = new_name
            return statuses

        async def poll_loop() -> list[int]:
            statuses: list[int] = []
            for _ in range(60):
                response = await client.get(project_url)
                statuses.append(response.status_code)
            return statuses

        edit_statuses, *poll_statuses = await asyncio.gather(
            edit_loop(),
            poll_loop(),
            poll_loop(),
            poll_loop(),
        )
        final = await client.get(project_url)
        return edit_statuses, poll_statuses, final

    edit_statuses, poll_statuses, final = run_scenario(app, scenario)
    # Every edit lands (the loop rebases on the fresh etag each round).
    assert edit_statuses == [200] * 10
    # Polling never sees busy (409/423/503) or server errors.
    flattened = [status for loop in poll_statuses for status in loop]
    assert set(flattened) == {200}
    assert final.json()["project"]["name"] == "Edited 9"


def test_duplicate_name_race_degrades_cleanly(app, run_scenario):
    async def scenario(client):
        responses = await asyncio.gather(
            *[
                client.post(
                    "/projects",
                    json=_create_payload(f"race-{index}", "Same Name"),
                )
                for index in range(4)
            ],
        )
        return responses

    responses = run_scenario(app, scenario)
    codes = sorted(response.status_code for response in responses)
    # At least one wins; losers fail with a client error, never a 5xx and
    # never a lock timeout mapped to busy.
    assert codes[0] == 201
    assert all(code < 500 for code in codes)
