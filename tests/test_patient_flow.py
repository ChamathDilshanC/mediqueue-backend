"""Registration, consultation and dispensary handoffs preserve owned visits."""
import uuid
from test_api import api, onboard, create, token

async def test_owned_queue_journey_and_reception_boundary(api):
    client, _ = api
    _, admin, hospital = await onboard(client)
    branch = hospital["branch"]["id"]
    owner = {"Authorization": f"Bearer {token(uuid.uuid4())}", "Idempotency-Key": "patient-issue"}
    stranger = {"Authorization": f"Bearer {token(uuid.uuid4())}", "Idempotency-Key": "stranger"}
    await client.post("/v1/patient/profiles", headers=owner, json={"branch_id": branch, "full_name": "Patient One", "mobile": "0771234567"})
    room = await create(client, "rooms", admin, {"name": "Doctor Room 3"})
    registration = await create(client, "queues", admin, {"name": "Registration Counter 1", "service_type": "REGISTRATION"})
    consultation = await create(client, "queues", admin, {"name": "Doctor 3", "service_type": "CONSULTATION", "room_id": room["id"]})
    pharmacy = await create(client, "queues", admin, {"name": "Dispensary", "service_type": "DISPENSARY"})
    assert (await client.post("/v1/queues", headers=admin, json={"name": "Bad", "service_type": "CONSULTATION"})).status_code == 422
    queues = (await client.get(f"/v1/patient/queues/{branch}", headers=owner)).json()
    assert [q["id"] for q in queues] == [registration["id"]]
    assert (await client.post(f'/v1/patient/queues/{consultation["id"]}/tickets', headers=owner)).status_code == 404
    assert (await client.post(f'/v1/patient/queues/{registration["id"]}/tickets', headers=stranger)).status_code == 403
    first = await client.post(f'/v1/patient/queues/{registration["id"]}/tickets', headers=owner)
    assert first.status_code == 201, first.text
    first = first.json()
    duplicate = await client.post(f'/v1/patient/queues/{registration["id"]}/tickets', headers={**owner,"Idempotency-Key":"different-key"})
    assert duplicate.json()["id"] == first["id"]
    assert (await client.get("/v1/patient/tickets", headers=stranger)).json() == []
    own = (await client.get("/v1/patient/tickets", headers=owner)).json()
    assert len(own) == 1 and own[0]["ahead"] == 0
    reception_user = uuid.uuid4()
    await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token(reception_user)}"})
    await create(client, "memberships", admin, {"user_id": str(reception_user), "role": "reception"})
    reception = {**admin, "Authorization": f"Bearer {token(reception_user)}", "Idempotency-Key": "reception-call"}
    body = {"queue_id": consultation["id"]}
    early = await client.post(f'/v1/journey/tokens/{first["id"]}/handoff', headers={**reception,"Idempotency-Key":"early"}, json=body)
    assert early.status_code == 409
    called = await client.post(f'/v1/queues/{registration["id"]}/call-next', headers=reception)
    assert called.status_code == 200, called.text
    await client.post(f'/v1/tokens/{first["id"]}/start', headers=reception, json={})
    complete = await client.post(f'/v1/tokens/{first["id"]}/complete', headers=reception, json={})
    assert complete.status_code == 200, complete.text
    routed = await client.post(f'/v1/journey/tokens/{first["id"]}/handoff', headers={**reception,"Idempotency-Key":"route"}, json=body)
    assert routed.status_code == 201, routed.text
    next_id = routed.json()["id"]
    retry = await client.post(f'/v1/journey/tokens/{first["id"]}/handoff', headers={**reception,"Idempotency-Key":"route"}, json=body)
    assert retry.json()["id"] == next_id
    duplicate_route = await client.post(f'/v1/journey/tokens/{first["id"]}/handoff', headers={**reception,"Idempotency-Key":"route-again"}, json=body)
    assert duplicate_route.status_code == 409
    tickets = (await client.get("/v1/patient/tickets", headers=owner)).json()
    consultation_ticket = next(t for t in tickets if t["id"] == next_id)
    assert consultation_ticket["room"] == "Doctor Room 3"
    assert len({t["visit_id"] for t in tickets}) == 1
    assert (await client.post(f'/v1/queues/{consultation["id"]}/call-next', headers={**reception,"Idempotency-Key":"no"})).status_code == 403
    called = await client.post(f'/v1/queues/{consultation["id"]}/call-next', headers={**admin,"Idempotency-Key":"doctor-call"})
    assert called.status_code == 200
    assert (await client.post(f'/v1/tokens/{next_id}/complete', headers=admin, json={})).status_code == 200
    pharmacy_ticket = await client.post(f'/v1/journey/tokens/{next_id}/handoff', headers={**admin,"Idempotency-Key":"pharmacy"}, json={"queue_id":pharmacy["id"]})
    assert pharmacy_ticket.status_code == 201, pharmacy_ticket.text
    _, other_admin, _ = await onboard(client, "Other Hospital")
    denied = await client.post(f'/v1/journey/tokens/{next_id}/handoff', headers={**other_admin,"Idempotency-Key":"foreign"}, json={"queue_id":pharmacy["id"]})
    assert denied.status_code == 404
    # Patient identities cannot invoke staff handoff or call commands.
    assert (await client.post(f'/v1/journey/tokens/{next_id}/handoff', headers=owner, json={"queue_id":pharmacy["id"]})).status_code == 403
