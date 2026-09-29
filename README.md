# Diagnostic Booking Service

A small backend service for browsing diagnostic centres/tests, booking a
diagnostic test, and paying for it via a **simulated** payment provider.
Built with **FastAPI + SQLAlchemy + PostgreSQL**.

## Stack

- **FastAPI** (Python 3.11) — async web framework, automatic OpenAPI docs
- **SQLAlchemy 2.0** — ORM (works against PostgreSQL in prod, SQLite in tests)
- **PostgreSQL 16** — primary datastore (via `docker-compose`)
- **python-jose** + **passlib[bcrypt]** — JWT auth + password hashing
- **slowapi** — simple per-IP rate limiting
- **pytest** + **httpx/TestClient** — 39 unit/integration tests

## Running locally

### Option A — Docker (recommended, includes Postgres)

```bash
docker compose up --build
```

The API will be available at `http://localhost:8000`, with interactive
docs at `http://localhost:8000/docs` (Swagger UI) and
`http://localhost:8000/redoc`.

### Option B — Local Python (default SQLite)

This project now defaults to a local SQLite database so it runs without a
separate Postgres installation. If you want to use Docker/Postgres instead,
set `DATABASE_URL` in `.env` to your Postgres DSN.

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1  # Windows PowerShell
# or: source .venv/bin/activate  # macOS/Linux

pip install -r requirements.txt
uvicorn app.main:app --reload
```

Tables are created automatically on startup via
`Base.metadata.create_all()` (see "What I'd improve" for why this isn't
ideal for production).

### Running tests

Tests use an isolated SQLite database and never touch your dev/prod
Postgres instance, so no setup is required:

```bash
pip install -r requirements.txt
pytest -v
```

39 tests cover signup/login, centre/test CRUD + authorization, booking
creation edge cases, payment simulation, and — most importantly — **webhook
idempotency** (duplicate delivery, race conditions, out-of-order/stale
events, unknown references).

## API overview

Full request/response schemas are in the interactive docs at `/docs`. Summary:

### Auth
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/signup` | – | Create an account |
| POST | `/auth/login` | – | Exchange email/password for a JWT |
| GET | `/auth/me` | Bearer | Current user profile |

```bash
curl -X POST localhost:8000/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email":"jane@example.com","full_name":"Jane Doe","password":"supersecret1"}'

curl -X POST localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"jane@example.com","password":"supersecret1"}'
# -> {"access_token": "...", "token_type": "bearer", "expires_in_minutes": 1440}
```

All authenticated endpoints below expect `Authorization: Bearer <token>`.

### Diagnostic centres & tests
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/centres/` | Admin | Create a centre (optionally with tests) |
| GET | `/centres/` | – | List centres, paginated, `?location=` filter |
| GET | `/centres/{id}` | – | Centre detail incl. its tests |
| POST | `/centres/{id}/tests` | Admin | Add a test to a centre |
| GET | `/tests/` | – | List tests, paginated, `?centre_id=` / `?name=` filters |
| GET | `/tests/{id}` | – | Test detail |

```bash
curl -X POST localhost:8000/centres/ \
  -H "Authorization: Bearer <admin_token>" -H "Content-Type: application/json" \
  -d '{"name":"City Diagnostics","location":"Delhi",
       "tests":[{"name":"Complete Blood Count","price":300},
                 {"name":"Lipid Profile","price":600}]}'

curl "localhost:8000/centres/?location=delhi&page=1&page_size=10"
```

> There's no separate "make me an admin" endpoint — see *Assumptions* below
> for how to seed one.

### Bookings
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/bookings/` | Bearer | Create a booking (status `PENDING`) |
| GET | `/bookings/` | Bearer | List **my** bookings, paginated, `?status=` filter |
| GET | `/bookings/{id}` | Bearer (owner/admin) | Booking detail |
| POST | `/bookings/{id}/cancel` | Bearer (owner/admin) | Cancel a booking |

```bash
curl -X POST localhost:8000/bookings/ \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"test_id":"<test-id>","centre_id":"<centre-id>",
       "appointment_time":"2026-10-05T10:00:00"}'
```

### Payments (simulated)
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/payments/` | Bearer (owner) | Simulate paying for a booking |
| POST | `/payments/webhook/` | – (provider callback) | Idempotent status update |

```bash
# Simulated payment — outcome is random (weighted by PAYMENT_SUCCESS_RATE),
# or forced deterministically for testing/demo via force_outcome.
curl -X POST localhost:8000/payments/ \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"booking_id":"<booking-id>","force_outcome":"SUCCESS"}'

# Simulated provider webhook — safe to send the same event_id any number
# of times.
curl -X POST localhost:8000/payments/webhook/ \
  -H "Content-Type: application/json" \
  -d '{"event_id":"evt_123","provider_reference":"pay_abc123","status":"SUCCESS"}'
```

`GET /health` is also available for uptime checks.

## Database / schema design

```
users
 ├─ id (uuid pk), email (unique), full_name, hashed_password, is_admin, created_at

diagnostic_centres
 ├─ id (uuid pk), name, location, created_at

diagnostic_tests
 ├─ id (uuid pk), centre_id (fk -> centres), name, price, created_at
 └─ unique(centre_id, name)  -- a centre can't offer the same test twice

bookings
 ├─ id (uuid pk), user_id (fk), test_id (fk), centre_id (fk),
 │  appointment_time, amount, status (PENDING|CONFIRMED|FAILED|CANCELLED),
 │  created_at, updated_at

payments
 ├─ id (uuid pk), booking_id (fk), provider_reference (unique),
 │  amount, status (PENDING|SUCCESS|FAILED), created_at, updated_at

webhook_events                       -- idempotency ledger, not exposed via API
 ├─ id (uuid pk), event_id (unique), provider_reference, status,
 │  raw_payload, created_at
```

**Why `amount` is copied onto `Booking` instead of only living on `Payment`
/ being derived from `DiagnosticTest.price` at read time:** test prices can
change after a booking is made; the booking should keep the price the
patient was quoted at booking time. This is a common "snapshot the price"
pattern in booking/e-commerce systems.

**Why a separate `webhook_events` table instead of just checking
`Payment.status != PENDING`:** relying solely on payment status for
idempotency breaks down for *retries of the same terminal outcome* sent
with different request bodies, and doesn't give you an audit trail of every
delivery attempt. A dedicated ledger keyed by the provider's `event_id`
(with a DB-level unique constraint) is the standard, race-safe way to
dedupe webhooks — see the next section.

## How webhook idempotency is implemented

`POST /payments/webhook/` (`app/services/payment_service.py::process_webhook_event`):

1. Look up `webhook_events` by `event_id`. If found, return immediately with
   `duplicate: true` and the booking's *current* status — no side effects
   are re-applied.
2. Otherwise, look up the `Payment` by `provider_reference` (404 if unknown).
3. Insert a new `webhook_events` row *first*, inside the same DB
   transaction as the state change. The column has a **unique index on
   `event_id`**, so if two identical webhook deliveries race each other
   concurrently, only one `INSERT` can succeed — the loser catches an
   `IntegrityError`, rolls back, and falls through to the same
   "duplicate, no-op" response as step 1. This closes the TOCTOU gap a
   plain "check-then-act" would leave open.
4. Only apply the SUCCESS/FAILED transition if the payment is still
   `PENDING`. This also protects against a *stale/out-of-order* webhook
   (different `event_id`, same `provider_reference`) trying to flip an
   already-resolved payment — e.g. a delayed `FAILED` arriving after a
   `SUCCESS` was already applied is safely ignored (see
   `test_webhook_does_not_flip_already_resolved_payment`).
5. Commit. The booking's status is derived from the payment outcome
   (`SUCCESS` → `CONFIRMED`, `FAILED` → `FAILED`), and cancelled bookings
   are never resurrected by a late webhook.

This is exercised directly in `tests/test_webhook.py`, including duplicate
`event_id` delivery, unknown references, and stale/out-of-order updates.

## Important assumptions

- **Admin bootstrapping**: there's no public "become admin" endpoint (that
  would be a privilege-escalation bug). For local testing, flip the flag
  directly: `UPDATE users SET is_admin = true WHERE email = '...';`, or set
  it in a seed script / initial migration in a real deployment.
- **One test per centre by name**: `(centre_id, name)` is unique — a centre
  can't list "CBC" twice at two different prices simultaneously.
- **Booking ⇄ payment cardinality**: a booking can have multiple `Payment`
  rows over time (e.g. one `FAILED` attempt followed by a retried
  `SUCCESS`), but only one can be in a non-terminal state at a time in
  practice, since a booking must be `PENDING` again before a new payment is
  initiated.
- **Payment retries**: if a booking's payment fails, the booking is reset
  to `PENDING` and can be paid for again. A `CONFIRMED` booking cannot be
  paid for twice (`409`); a `CANCELLED` booking can never be paid for
  (`400`).
- **Ownership checks return 404, not 403**, for another user's booking, so
  a user can't distinguish "doesn't exist" from "exists but isn't yours."
- **Webhook endpoint has no auth header** in this simulation — a real
  payment provider would sign the payload (e.g. HMAC over the raw body)
  instead of using a bearer token, since the caller isn't a logged-in user.
  See "What I'd improve."
- **IDs are UUIDs (strings)**, not auto-increment integers, so they're safe
  to expose and unguessable.
- **`Base.metadata.create_all()`** is used for schema setup instead of
  migrations, to keep the take-home runnable with zero extra steps.

## What I'd improve with more time

- **Alembic migrations** instead of `create_all()`, for real schema
  evolution and rollback safety.
- **HMAC-signed webhooks**: verify a `X-Signature` header derived from a
  shared secret before trusting the payload, and reject anything else.
- **Idempotency key on `POST /payments/`** too (not just the webhook) via
  a client-supplied `Idempotency-Key` header, so a network retry of the
  *initiating* call can't double-charge.
- **Background job for webhook retries**: move webhook *processing* (not
  just receipt) onto a Celery/RQ queue with exponential backoff, so a
  slow downstream update doesn't block the HTTP response, and failed
  processing can be retried safely (it already is idempotent, so retries
  are cheap).
- **Redis** for caching hot read paths (`GET /centres/`, `GET /tests/`)
  and for a distributed rate limiter (the current one is per-process/IP
  in memory, which doesn't work correctly across multiple app replicas).
- **Refresh tokens** + short-lived access tokens instead of one 24h JWT.
- **Role-based access control** beyond a single `is_admin` boolean (e.g.
  per-centre staff accounts) if centres are self-managed by partners.
  - **Optimistic locking / row versioning** on `Booking`/`Payment` to guard
  against lost updates under concurrent writers beyond the webhook path.
- **Structured audit log** of every booking/payment state transition
  (who/what/when), not just the current row state.
- **CI pipeline** (GitHub Actions) running lint + `pytest --cov` on every PR.
