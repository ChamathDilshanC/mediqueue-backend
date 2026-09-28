# MediQueue Backend

FastAPI service deployed on Vercel with the explicit entrypoint
`backend.main:app`.

## Endpoints

- `/` and `/docs` - responsive white/lime API reference with live endpoint search, request/response examples and copy controls
- `/swagger` - interactive Swagger UI with bearer authorization
- `/redoc` - ReDoc
- `/openapi.json` - generated OpenAPI contract
- `/health` - liveness
- `/health/ready` - database readiness

## Run locally

```powershell
python -m pip install -e '.[test]'
Copy-Item .env.example .env
python -m backend.init_db
python -m uvicorn backend.main:app --reload
```

The initializer is for SQLite only; PostgreSQL deployments must run `python -m alembic upgrade head`. SQLite uses a main database plus persistent `.iam.db`, `.queue.db`, `.scheduling.db` and `.notifications.db` sidecars. SQLite is for development and does not provide PostgreSQL row-lock concurrency guarantees.

### Vercel database connection

For Vercel, set `DATABASE_URL` to the Supabase **Session Pooler** URL from
Supabase **Project Settings → Database → Connect**. Use port `5432` and the
`postgres.<project-ref>` username, for example:

```text
postgresql://postgres.<project-ref>:PASSWORD@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```

Do not use `db.<project-ref>.supabase.co:5432` on Vercel: Supabase direct
database hosts are IPv6-only on projects without the IPv4 add-on, while Vercel
functions may not have outbound IPv6 connectivity. URL-encode special password
characters (`@` as `%40`, `#` as `%23`, and `%` as `%25`), set the variable for
the Production environment, and redeploy. Run `/health/ready` after deployment
to verify the database connection.

## Authentication and hospital onboarding

Set `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and either `SUPABASE_JWT_SECRET` (HS256) or `SUPABASE_JWKS_URL` (RS256/ES256). Asymmetric projects can use the default JWKS URL derived from the Supabase URL. No service-role key is needed. Configure confirmation/recovery redirect URLs and email delivery in Supabase. Authentication endpoints return 503 if the provider is not configured; they never create fake sessions.

1. `POST /v1/auth/register` with email, password and display_name. Confirm the email when `confirmation_required` is true.
2. `POST /v1/auth/login` to get the access/refresh tokens. Use `Authorization: Bearer <access_token>`.
3. `GET /v1/auth/me` loads/creates the local user profile. `PATCH /v1/users/me` updates its display name.
4. `POST /v1/hospitals` with name, branch_name and timezone creates the hospital, first branch, and caller's admin membership in one database transaction. Hospital registration requires an authenticated identity, not an existing staff membership.
5. Send `X-Tenant-ID` (hospital ID) and `X-Branch-ID` for scoped operations. One active membership is selected automatically; multiple memberships require explicit selection.
6. Other staff register their own accounts and call `/auth/me`. A branch admin assigns their returned user UUID through `POST /v1/memberships`. Public registration cannot assign staff roles. Revoked memberships stop working on the next scoped request; the final branch admin cannot be removed.

JWT signatures, algorithm allowlists, issuer, audience, expiry and subject are verified. Authorization uses database memberships, **not user metadata or role claims**. Existing installations that relied only on JWT tenant/role claims must provision memberships before enabling this version.

`POST /v1/auth/refresh` rotates sessions. `/v1/auth/logout` revokes refresh tokens; already issued JWTs remain valid until expiry. `/v1/auth/forgot-password` and `PUT /v1/auth/password` support Supabase recovery sessions. Auth responses carrying tokens are marked `Cache-Control: no-store`. Keep tokens out of logs and URLs. Supabase supplies auth rate limits; configure the deployment edge for additional abuse limits.

## API coverage

All paths below have `/v1` prefix. The live OpenAPI contract is the complete field-level reference (67 method/path operations).

| Resource | Operations |
| --- | --- |
| Authentication | register, login, refresh, logout, forgot-password, change password |
| Users | current profile/memberships, edit current profile, admin branch-user list |
| Hospitals | register with first branch, list accessible hospitals, detail, rename |
| Branches | create, list accessible branches, detail, update |
| Memberships | admin list, add registered user, update role or revoke/reactivate |
| Departments, rooms, doctors, schedules | scoped list, detail, create, full editable-field update (PUT), delete unused records |
| Patients | hospital-scoped reference registration, list, detail, update, delete unused records |
| Queues | branch-scoped setup CRUD, today's snapshot, check-in, call-next |
| Tokens | scoped state detail; recall, skip, start, complete, cancel |
| Visits | create and immutable history list/detail |
| Appointments | book, list, detail, status transition/cancellation |
| Audit | admin-only immutable branch history |

Hospital/branch/account erasure is intentionally not a generic DELETE operation: it would remove identity and care-flow history. Revoke staff through membership `active=false`; cancel appointments/tokens via their status transitions. Audit/outbox/idempotency records are internal infrastructure, not writable CRUD resources. Patients store non-clinical references only and are shared across branches of the same hospital. Public endpoints do not expose patient data. The frontend submodule remains a placeholder; this change styles the API documentation only.

Schedules enforce timezone-aware ranges, positive capacity, and doctor/room overlap prevention. Schedules with appointment history cannot be rewritten. Appointment state transitions are `BOOKED → CHECKED_IN → COMPLETED`, or `BOOKED → CANCELLED / NO_SHOW`; cancellation frees capacity. Mutations lock the branch/schedule in PostgreSQL to prevent overbooking.

Queue check-in and call-next require a unique `Idempotency-Key`; token transitions accept one. Keys are bound to actor, branch and operation, and request-body changes on a repeated key return 409. Replaying a successful request returns its saved response. Legacy records from the old unscoped-key format are not replayed across this upgrade; drain in-flight retries during rollout. Queue commands create audit and outbox records transactionally. Call-next uses today's branch business date. `start` enables `IN_SERVICE`; completion also accepts CALLED/RECALLED for compatibility.

## Verification

```powershell
python -m pytest -q
# Optional real PostgreSQL tests, using a dedicated test database:
$env:MEDIQUEUE_TEST_DATABASE_URL = 'postgresql+asyncpg://test_user:password@localhost/mediqueue_test'
python -m pytest -q
```

PostgreSQL tests create isolated, randomly named schemas and remove those test schemas afterward. They exercise simultaneous check-ins, idempotent retries, call-next locking and appointment capacity. Migrations must also be exercised against a disposable PostgreSQL database before deployment. Do not point test settings at a production database.

Provider contract: [Supabase Auth REST API](https://supabase.github.io/auth/).
