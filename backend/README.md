# Eventra — Event Ticketing Platform API

A REST backend for an event ticketing platform with two roles. **Attendees** browse, search and book events. **Organizers** publish and manage events and see their sales. It is built with FastAPI, PostgreSQL, SQLAlchemy 2 and Alembic.

The core guarantee is that **an event is never oversold, even under concurrent load**. Real multi-threaded tests run against PostgreSQL to show it (see [Concurrency strategy](#concurrency-strategy)).

| | |
|---|---|
| **Live API** | https://eventra-api-rwzm.onrender.com |
| **Swagger UI** | https://eventra-api-rwzm.onrender.com/docs |
| **ReDoc** | https://eventra-api-rwzm.onrender.com/redoc |
| **Source** | https://github.com/johanderick562008/eventra |

The API is hosted on Render's free plan, which sleeps when idle, so the first request after a pause can take 30–60 seconds.

- [Features](#features) · [Stack](#technology-stack) · [Architecture](#architecture) · [Database](#database-schema)
- [Local setup](#local-setup) · [Docker](#docker) · [API usage](#api-usage-real-requests-and-responses) · [Business rules](#business-rules)
- [Concurrency](#concurrency-strategy) · [Testing](#testing) · [Deployment](#deployment) · [Security](#security-measures) · [Limitations](#known-limitations)

---

## Features

**Attendees**: register and log in; browse upcoming events with filters (city, category, date range, price range), full-text search, sorting and pagination; view event details and remaining capacity; book 1–10 tickets; view private booking history and booking details; cancel eligible bookings.

**Organizers**: create, edit, cancel and (soft-)delete their own events. They can also list their events with sales counters, list the bookings for each event, and view a sales summary for each event.

**Platform**
- JWT access tokens and rotating refresh tokens with reuse detection
- Argon2id password hashing
- Role-based and ownership-based authorization on every protected route
- Transactional booking engine with `SELECT … FOR UPDATE` locking and a DB `CHECK` constraint as a backstop
- One consistent JSON envelope for every success and every error
- Swagger UI, ReDoc and OpenAPI 3.1, with endpoint descriptions and error codes
- Bonus features:
  - per-user booking rate limit (5/min)
  - background job that persists `ONGOING`/`COMPLETED` statuses
  - PostgreSQL full-text search backed by a GIN index
  - refresh-token rotation
- Docker image (non-root), Docker Compose stack, Render blueprint, health and readiness probes

## Technology stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| Web framework / server | FastAPI 0.118, Uvicorn 0.37 |
| Database | PostgreSQL 17 (tested), any ≥ 13 |
| ORM / migrations | SQLAlchemy 2.0 (psycopg 3 driver), Alembic 1.16 |
| Validation / settings | Pydantic 2.11, pydantic-settings |
| Auth | PyJWT (HS256), argon2-cffi (Argon2id) |
| Tests | Pytest, HTTPX, a real Uvicorn server for race tests |
| Quality | Ruff (lint + format), mypy |
| Packaging | Docker, Docker Compose, Render blueprint |

Redis is **not** used: nothing in the core needs it. The rate limiter is in-process; see [limitations](#known-limitations).

## Architecture

The service is a modular monolith with a strict layering: **routes → services → models**. Routes stay thin and only handle HTTP. Services own the business rules and the transaction boundaries.

```
backend/
├── app/
│   ├── main.py                 # app factory, middleware, health probes, lifespan (status sweeper)
│   ├── api/
│   │   ├── dependencies.py     # get_current_user, CurrentAttendee/CurrentOrganizer, rate limiting
│   │   └── v1/                 # auth, users, events, bookings, organizer routers
│   ├── core/                   # config, security (hashing/JWT), exceptions + handlers, rate limiter
│   ├── db/                     # Base + naming convention, engine/session, run_in_transaction()
│   ├── models/                 # User, Event, Booking, RefreshToken (+ enums)
│   ├── schemas/                # Pydantic request/response models
│   ├── services/               # auth, user, event, booking, analytics (all business logic)
│   ├── middleware/             # request id, access log, security headers
│   └── tasks/                  # background event-status sweep
├── alembic/                    # migrations (0001_initial_schema)
├── tests/                      # unit / api / security / integration / concurrency
├── scripts/                    # start.sh, smoke_test.py, create-test-db.sql
├── Dockerfile, docker-compose.yml, render.yaml, pyproject.toml, requirements*.txt
```

There is no separate repository layer. The services use SQLAlchemy directly, which keeps the code small. Every query lives in exactly one service function.

## Database schema

```
users ───────────────┐ 1
  id (uuid, pk)      │
  name               ├──────< events (organizer_id → users.id, ON DELETE RESTRICT)
  email (unique, CHECK = lower(email))      id, title, description, category, city, venue,
  password_hash      │                      address, image_url, start_at, end_at (timestamptz),
  role ATTENDEE|ORGANIZER                   ticket_price numeric(10,2), capacity, tickets_sold,
  is_active          │                      status, cancelled_at, deleted_at, created_at, updated_at
  created_at/updated_at                        │ 1
                     │                         │
                     ├──────< bookings >───────┘   (user_id, event_id → RESTRICT)
                     │        id, quantity, unit_price, total_price, status,
                     │        cancelled_at, cancellation_reason, created_at, updated_at
                     │
                     └──────< refresh_tokens (user_id → CASCADE)
                              id, token_hash (sha256, unique), family_id, expires_at,
                              revoked_at, replaced_by_id → refresh_tokens.id
```

**Database-enforced invariants**

| Constraint | Purpose |
|---|---|
| `ck_events_tickets_sold_within_capacity` `tickets_sold <= capacity` | Last line of defence against overselling |
| `ck_events_tickets_sold_non_negative`, `ck_events_capacity_positive`, `ck_events_ticket_price_non_negative` | Inventory sanity |
| `ck_events_end_after_start` | Valid time range |
| `ck_bookings_quantity_positive`, `ck_bookings_total_matches_unit_price` (`total_price = unit_price * quantity`) | Booking integrity |
| `uq_users_email` + `ck_users_email_lowercase` | Case-insensitive unique email |
| `ON DELETE RESTRICT` on bookings and events | Booking and sales history can never be cascade-deleted |
| Enum `CHECK`s on role/category/status | Only valid states stored |

**Indexes**
- events: `(status, start_at)`, `start_at`, `category`, `ticket_price`, `organizer_id`, `lower(city)`
- a GIN full-text index on `to_tsvector('english', title || ' ' || description)`
- bookings: `(user_id, created_at)`, `(event_id, status)`, `status`
- refresh_tokens: `user_id`, `family_id`

**Deletion policy**
- Nothing financial is ever hard-deleted.
- Events are soft-deleted through `deleted_at`.
- Users are deactivated through `is_active = false`.
- Bookings are never deleted. Cancelling a booking changes its status.

**Inventory invariant**: for every event, `tickets_sold` equals the sum of `quantity` over all its bookings whose status is not `CANCELLED`. A randomized reconciliation test checks this.

## Local setup

**Prerequisites**: Python 3.12, Docker Desktop (used for PostgreSQL). You can use your own PostgreSQL instead by pointing `DATABASE_URL` at it.

```bash
cd backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env               # then set JWT_SECRET_KEY
```

Start PostgreSQL. Compose publishes it on **localhost:5433**, so it does not clash with a local PostgreSQL on 5432. On its first start it also creates the `eventra_test` database:

```bash
docker compose up -d db
```

Apply migrations and run the API:

```bash
alembic upgrade head
uvicorn app.main:app --reload
```

Open http://localhost:8000/docs.

### Environment variables

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://eventra:eventra@localhost:5433/eventra` | `postgres://` URLs are converted automatically |
| `JWT_SECRET_KEY` | insecure dev value | **Required in production**: ≥ 32 chars, and startup fails otherwise |
| `JWT_ALGORITHM` | `HS256` | Pinned on verification |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `14` | |
| `FRONTEND_ORIGINS` | `http://localhost:5173` | Comma-separated. `*` is rejected in production |
| `ENVIRONMENT` | `development` | `development` / `test` / `production` |
| `BOOKING_MAX_TICKETS_PER_BOOKING` | `10` | |
| `BOOKING_CANCELLATION_CUTOFF_HOURS` | `24` | |
| `BOOKING_RATE_LIMIT_PER_MINUTE` | `5` | `0` disables |
| `EVENT_STATUS_SWEEP_INTERVAL_SECONDS` | `300` | `0` disables |
| `DB_STATEMENT_TIMEOUT_MS` / `DB_LOCK_TIMEOUT_MS` | `15000` / `10000` | Set per connection |
| `TEST_DATABASE_URL` | `…localhost:5433/eventra_test` | The name must end in `_test`, because the suite wipes it |

### Migrations

```bash
alembic upgrade head        # apply
alembic downgrade base      # roll everything back
alembic check               # verify models and migrations are in sync (currently: no drift)
alembic revision --autogenerate -m "describe change"
```

`Base.metadata.create_all()` is never used. The schema comes only from migrations. In production the container runs `alembic upgrade head` on start. This is idempotent, and you can turn it off with `RUN_MIGRATIONS=false`.

## Docker

```bash
docker compose up -d --build          # db + api (migrations run automatically), http://localhost:8000
docker compose logs -f api            # view logs
docker compose exec api alembic current
docker compose down                   # stop (data volume is kept)
docker compose down -v                # reset the local database (deletes the pgdata volume)
```

The image is based on `python:3.12-slim` and runs as the non-root user `appuser`. Dependencies are installed in their own layer so rebuilds use the cache. It runs `uvicorn --workers $WEB_CONCURRENCY --proxy-headers`. Compose health-checks PostgreSQL with `pg_isready` and the API with `/ready`.

## API usage (real requests and responses)

Every example below was captured from the Docker stack. Tokens are truncated.

**Conventions**
- Success responses use `{"data": …}`. Lists use `{"data": [...], "meta": {"page", "limit", "total", "pages"}}`.
- Errors use `{"detail": "...", "code": "MACHINE_CODE"}`. Validation errors add an `errors` list.
- Times are ISO 8601 and always returned in UTC.
- Money is a decimal string such as `"499.00"`, so no floating-point rounding is involved.
- Send `Authorization: Bearer <access_token>` on protected routes.

### Endpoints

| Method | Path | Who | Purpose |
|---|---|---|---|
| POST | `/api/v1/auth/register` | public | Register (ATTENDEE / ORGANIZER) |
| POST | `/api/v1/auth/login` | public | Access + refresh token |
| POST | `/api/v1/auth/refresh` | public (refresh token) | Rotate tokens |
| POST | `/api/v1/auth/logout` | public (refresh token) | Revoke token family |
| GET/PATCH/DELETE | `/api/v1/users/me` | any user | Profile, name/password change, deactivate |
| GET | `/api/v1/users`, `/api/v1/users/{id}` | any user | Organizer directory / public profile |
| PATCH/DELETE | `/api/v1/users/{id}` | self only | Same as `/me` (403 for others) |
| GET | `/api/v1/events` | public | Filter / search / sort / paginate |
| GET | `/api/v1/events/{id}` | public | Details + remaining capacity |
| POST | `/api/v1/events` | organizer | Create |
| PATCH / DELETE | `/api/v1/events/{id}` | owner | Update / soft-delete |
| POST | `/api/v1/events/{id}/cancel` | owner | Cancel; bookings → REFUND_PENDING |
| GET | `/api/v1/events/{id}/bookings` | owner | Bookings for the event |
| GET | `/api/v1/events/{id}/summary` | owner | Sales summary |
| POST | `/api/v1/events/{id}/bookings` | attendee | Book tickets |
| GET | `/api/v1/bookings/me` | attendee | Booking history |
| GET | `/api/v1/bookings/{id}` | booker or event owner | Booking details |
| PATCH | `/api/v1/bookings/{id}/cancel` | booker | Cancel booking |
| GET | `/api/v1/organizer/events` | organizer | Own events + counters |
| GET | `/health`, `/ready` | public | Liveness / readiness (DB check) |

### Authentication

```http
POST /api/v1/auth/register
{"name": "Asha Organizer", "email": "asha@example.com", "password": "SecurePassword123!", "role": "ORGANIZER"}
```
```json
201 {"data": {"id": "81322ce5-c9d3-4ea9-abe2-fca7bce613ab", "name": "Asha Organizer", "email": "asha@example.com",
              "role": "ORGANIZER", "is_active": true, "created_at": "2026-10-01T13:56:26.409004Z"}}
```

```http
POST /api/v1/auth/login
{"email": "asha@example.com", "password": "SecurePassword123!"}
```
```json
200 {"data": {"access_token": "eyJhbGciOiJIUzI1NiIsInR5...", "refresh_token": "nF9hJTxtpUBr...",
              "token_type": "bearer", "expires_in": 1800, "user": {"id": "81322ce5-…", "role": "ORGANIZER", "…": "…"}}}
```

A wrong password and an unknown email return the same response: `401 {"detail": "Invalid email or password", "code": "INVALID_CREDENTIALS"}`.

### Events

```http
POST /api/v1/events            (organizer token)
{"title": "Python Workshop", "description": "Hands-on FastAPI and PostgreSQL workshop.", "category": "WORKSHOP",
 "city": "Chennai", "venue": "IIT Madras Research Park", "start_at": "2026-11-15T10:00:00+05:30",
 "end_at": "2026-11-15T17:00:00+05:30", "ticket_price": "499.00", "capacity": 2}
```
```json
201 {"data": {"id": "893c4804-0383-45c4-a848-9d62506fc3da", "title": "Python Workshop", "category": "WORKSHOP",
              "city": "Chennai", "venue": "IIT Madras Research Park", "start_at": "2026-11-15T04:30:00Z",
              "end_at": "2026-11-15T11:30:00Z", "ticket_price": "499.00", "capacity": 2, "tickets_sold": 0,
              "remaining_capacity": 2, "status": "UPCOMING", "is_bookable": true,
              "organizer": {"id": "81322ce5-…", "name": "Asha Organizer", "role": "ORGANIZER"}, "…": "…"}}
```

```http
GET /api/v1/events?city=Chennai&category=WORKSHOP&min_price=100&max_price=1000&page=1&limit=10
→ 200 {"data": [...], "meta": {"page": 1, "limit": 10, "total": 1, "pages": 1}}
```

Other query parameters:
- `q`: full-text search on title and description, using `websearch_to_tsquery` with English stemming
- `start_from`, `start_to`: date range filter
- `sort`: one of `start_at`, `-start_at`, `ticket_price`, `-ticket_price`, `-created_at`, `title`
- `include_past`, `include_cancelled`: include events that are normally hidden

Unknown parameters return 422. `limit` must not exceed 100.

### Bookings

```http
POST /api/v1/events/893c4804-…/bookings      (attendee token)
{"quantity": 2}
```
```json
201 {"data": {"id": "8d207daf-45ad-4ff5-b519-786a24dfd965", "quantity": 2, "unit_price": "499.00",
              "total_price": "998.00", "status": "CONFIRMED", "cancelled_at": null,
              "event": {"id": "893c4804-…", "title": "Python Workshop", "start_at": "2026-11-15T04:30:00Z", "status": "UPCOMING", "…": "…"}}}
```

| Situation | Response |
|---|---|
| Sold out / not enough tickets | `409 {"detail": "Not enough tickets available: 0 remaining", "code": "INSUFFICIENT_CAPACITY"}` |
| Organizer tries to book | `403 {"detail": "This action requires the ATTENDEE role", "code": "INSUFFICIENT_ROLE"}` |
| `{"quantity": 0}` | `422 {"detail": "Request validation failed", "code": "VALIDATION_ERROR", "errors": [{"loc": ["body","quantity"], "msg": "Input should be greater than or equal to 1", "type": "greater_than_equal"}]}` |
| Event cancelled / already started | `409 EVENT_CANCELLED` / `409 EVENT_ALREADY_STARTED` |
| More than 10 tickets | `422 QUANTITY_LIMIT_EXCEEDED` |
| > 5 attempts/minute | `429 RATE_LIMITED` + `Retry-After` |

```http
PATCH /api/v1/bookings/8d207daf-…/cancel     {"reason": "Plans changed"}
→ 200 {"data": {"status": "CANCELLED", "cancelled_at": "2026-10-01T13:56:26.916450Z", "cancellation_reason": "Plans changed", …}}
PATCH again → 409 {"detail": "Booking is already cancelled", "code": "BOOKING_ALREADY_CANCELLED"}
```

### Organizer summary

```json
GET /api/v1/events/893c4804-…/summary
200 {"data": {"capacity": 2, "tickets_sold": 0, "remaining_capacity": 2, "confirmed_bookings": 0, "confirmed_tickets": 0,
              "cancelled_bookings": 1, "cancelled_tickets": 2, "refund_pending_bookings": 0, "refund_pending_tickets": 0,
              "refunded_bookings": 0, "gross_booking_value": "998.00", "cancelled_booking_value": "998.00",
              "refund_pending_value": "0.00", "refunded_value": "0.00", "net_booking_value": "0.00", "…": "…"}}
```

All values are **booking totals, not money received**, because no payment provider is integrated:
- `gross_booking_value`: the sum of every booking ever made
- `cancelled_booking_value`: bookings cancelled by attendees
- `refund_pending_value`: what is owed back after an event cancellation
- `net_booking_value`: bookings that are still `CONFIRMED`

## Business rules

| Rule | Behaviour |
|---|---|
| Roles | Chosen at registration (`ATTENDEE`/`ORGANIZER`) and immutable. There is **no administrator role**. Account management is self-service only, and `PATCH/DELETE /users/{other_id}` returns 403. |
| Event creation | `start_at` must be in the future and before `end_at`. Datetimes must carry a UTC offset. The owner is always the caller, never taken from the payload. |
| Event status | Only `CANCELLED` is set by a user. `UPCOMING`/`ONGOING`/`COMPLETED` are **derived from the clock** in every response. A background sweep also persists them, but correctness never depends on that job. |
| Editing | Owner only. Not allowed once the event has started or after it is cancelled. Capacity cannot go below `tickets_sold`. Price changes do not affect existing bookings because the price is snapshotted at booking time. |
| Booking cutoff | No bookings at or after `start_at`. This is checked against the clock under the row lock, whatever the stored status says. |
| Booking size | 1–10 tickets per booking (configurable). A user may hold several bookings for the same event. |
| Cancellation window | An attendee may cancel a `CONFIRMED` booking up to **24 hours before the event starts** (`BOOKING_CANCELLATION_CUTOFF_HOURS`). The tickets go back to inventory exactly once. |
| Event cancellation | Owner only, before `end_at`. Atomically sets the event to `CANCELLED` and every `CONFIRMED` booking to `REFUND_PENDING`. Already-cancelled bookings are not touched. Cancelling twice returns 409. |
| Event deletion | Soft delete. Allowed for cancelled events, or for unstarted events with no active bookings. Otherwise it returns `409 EVENT_HAS_ACTIVE_BOOKINGS`: cancel the event instead. Attendees keep their booking history. |
| Booking states | `CONFIRMED → CANCELLED` (attendee) or `CONFIRMED → REFUND_PENDING` (event cancelled). `REFUNDED` is reserved for a future payment integration. No endpoint claims a refund has been paid. |
| Deactivation | Soft. Refused while an organizer has live events or an attendee has upcoming confirmed bookings. All refresh tokens are revoked, and access tokens stop working immediately because the user row is checked on every request. |

## Concurrency strategy

Every write that touches an event's inventory or status runs inside `run_in_transaction()` and follows the same protocol:

1. `SELECT … FROM events WHERE id = :id FOR UPDATE`. All concurrent writers to one event queue here, while readers are never blocked.
2. Re-read the state under the lock. Then check that the event is not cancelled, that `now < start_at`, and that `capacity - tickets_sold >= quantity`.
3. Insert the booking and update `tickets_sold` in the same transaction, then commit. Any error rolls back the whole transaction.

**Supporting measures**
- **Backstop**: `CHECK (tickets_sold <= capacity)` makes overselling impossible even if application code regresses.
- **Deadlock freedom**: locks are always taken in the order event row, then booking row. This applies to booking, booking cancellation and event cancellation alike.
- **Retries**: deadlocks (`40P01`) and serialization failures (`40001`) are retried up to 3 times with jitter.
- **Timeouts**: lock waits are capped at 10 s and statements at 15 s. If either is hit, the API returns `503` with `Retry-After` instead of hanging.
- **Isolation**: PostgreSQL's default READ COMMITTED is sufficient, because every decision is made on a row the transaction has locked.
- **Short transactions**: no network calls or other slow work happen while a lock is held.
- **Advisory figures**: `remaining_capacity` in listings is informational only. Availability is always re-checked under the lock.

**Proof.** `tests/concurrency/test_booking_race.py` starts a real Uvicorn server and releases N threads simultaneously through a barrier. Each thread has its own HTTP connection, and each request gets its own server worker thread and DB connection. After the race, the test queries PostgreSQL directly.

| Test | Scenario | Asserted outcome |
|---|---|---|
| `test_last_ticket_race_two_attendees` | capacity 1, two attendees | exactly one 201 and one 409 `INSUFFICIENT_CAPACITY`; 1 sold; 1 booking row |
| `test_last_ticket_race_99_of_100` | the brief's example: 99/100 sold, two concurrent requests | one 201, one 409; exactly 100 sold, 11 bookings |
| `test_many_parallel_requests_never_oversell` ×3 | capacity 10, 30 concurrent attendees | exactly 10×201, 20×409; `tickets_sold == confirmed rows == 10` |
| `test_mixed_quantities_never_oversell` | capacity 17, 20 requests of 1–4 tickets | sold == sum of granted quantities ≤ 17 |
| `test_concurrent_double_cancel_releases_once` | the same booking cancelled 6× in parallel | one 200, five 409; inventory released once |
| `test_event_cancellation_racing_bookings` | event cancel racing 15 bookings | no booking left `CONFIRMED`; `affected_bookings` matches |

**Mutation check.** As a check of the tests themselves, I temporarily removed `.with_for_update()` and re-ran the suite. **6 of the 8 race tests failed**, with lost updates and overselling attempts. With the lock restored, all 8 pass. So these tests do detect the race; they would not pass on luck.

## Testing

The tests need the Compose database to be running. Run them from `backend/`:

```bash
docker compose up -d db
pytest                         # everything
pytest tests/concurrency -v    # just the race tests
ruff check . && ruff format --check . && mypy app
python scripts/smoke_test.py http://localhost:8000   # end-to-end against a running server
```

Each run starts by wiping `eventra_test`. It then migrates it **from scratch with Alembic** and truncates the tables between tests.

**Results obtained on 2026-10-01** (Windows 11, Python 3.12.10, PostgreSQL 17 in Docker):

```
pytest            → 138 passed in 40.85s
  tests/api/test_auth.py              20   registration, login, token expiry/forgery, inactive accounts, refresh rotation + reuse
  tests/api/test_events.py            33   CRUD, validation, filters, search, pagination, ownership, lifecycle, cancellation
  tests/api/test_bookings.py          23   booking, capacity, quantity validation, history isolation, cancellation window, rate limit
  tests/api/test_users.py              7   profile, role-escalation / mass-assignment, password change
  tests/api/test_organizer.py          4   own events, own bookings, sales summary accuracy
  tests/api/test_platform.py           7   health/ready, docs, error envelope, security headers, CORS
  tests/security/test_authorization.py 14  alg=none, wrong key, tampered claims, DB role > token role, sensitive-field leaks
  tests/integration/test_database.py   6   migrations == models, DB constraints, randomized inventory reconciliation, status sweep
  tests/unit/test_units.py            16   hashing, JWT, schemas, settings validation, rate limiter, status derivation
  tests/concurrency/test_booking_race.py 8 real concurrent races (table above)
ruff check        → All checks passed!
ruff format       → 63 files already formatted
mypy app          → Success: no issues found in 44 source files
alembic check     → No new upgrade operations detected.
smoke_test.py     → all 19 steps PASS against the Docker image, including on a fresh (empty) database volume
```

## Deployment

**Deployed on Render:** https://eventra-api-rwzm.onrender.com (Swagger at [`/docs`](https://eventra-api-rwzm.onrender.com/docs)). It runs as a Docker web service with a managed PostgreSQL database, created from [`render.yaml`](../render.yaml) at the repository root.

**Checks run against the live deployment on 2026-10-02:**
- `python scripts/smoke_test.py https://eventra-api-rwzm.onrender.com`: all 19 steps PASS.
- Live race test: 12 attendees booked 1 ticket each, simultaneously, for an event with capacity 3. Result: exactly 3 × 201 and 9 × 409 `INSUFFICIENT_CAPACITY`; the summary showed `tickets_sold = 3` and `remaining_capacity = 0`.
- Production mode is active: the `Strict-Transport-Security` header is present.

### Render (blueprint)

1. Push the repository to GitHub. If `backend/` is a subfolder, move `render.yaml` to the repository root.
2. In Render, choose **New → Blueprint** and select the repo. The blueprint creates:
   - the `eventra-api` Docker web service, with `rootDir: backend` and the health check set to `/ready`
   - the `eventra-db` PostgreSQL database
3. Environment variables:
   - `DATABASE_URL` is wired from the database automatically.
   - `JWT_SECRET_KEY` is generated by Render.
   - `ENVIRONMENT=production` is set by the blueprint.
   - Set `FRONTEND_ORIGINS` yourself, to your frontend's URL.
4. Deploy. The container runs `alembic upgrade head`, then Uvicorn on Render's `$PORT`.
5. Verify the deployment:
   ```bash
   python scripts/smoke_test.py https://<your-service>.onrender.com
   ```
   Then open `https://<your-service>.onrender.com/docs`.

### Railway / Fly.io / any Docker host

Build `backend/Dockerfile` and provide these environment variables: `DATABASE_URL`, `JWT_SECRET_KEY` (at least 32 random characters), `ENVIRONMENT=production` and `FRONTEND_ORIGINS`. If TLS is terminated by a proxy, also set `FORWARDED_ALLOW_IPS`. Expose `$PORT`, which defaults to 8000, and point the health check at `/ready`.

In production mode, startup fails if `JWT_SECRET_KEY` is the development default or shorter than 32 characters, or if CORS is set to `*`.

## Security measures

**Passwords**
- Hashed with Argon2id and rehashed automatically when the hashing parameters change.
- Strength rules: 8–128 characters, with lowercase, uppercase, a digit and a symbol.
- An unknown email takes the same time to reject as a wrong password, so accounts cannot be discovered by timing.

**Tokens**
- **Access JWTs**: the algorithm is pinned (blocking `alg=none` and algorithm confusion), and issuer, `exp`, `iat` and `type` are required. Claims are minimal: no email or name.
- **Refresh tokens**: 384-bit random values stored only as SHA-256 hashes. They rotate on every use. Re-using an old token revokes its whole family. Logout and password changes revoke all of them.

**Authorization**
- Enforced by reusable dependencies (`CurrentUser`, `CurrentAttendee`, `CurrentOrganizer`) plus ownership checks in the services.
- The role is read from the database, not trusted from the token.
- Requests for other users' bookings return 404 rather than 403, so booking IDs cannot be probed.

**Input**
- Every schema uses `extra="forbid"`, so fields like `role`, `organizer_id`, `tickets_sold` or `total_price` cannot be smuggled in. Prices are always computed on the server.
- Quantities are strict integers.
- All SQL goes through parameterized ORM statements. The search term is a bound parameter.

**Responses**
- Responses never contain password hashes, tokens (except at login and refresh), organizer emails on public endpoints, stack traces or SQL.
- Validation errors drop the submitted values, so passwords are never echoed back.

**HTTP**
- CORS is limited to the configured origins.
- Security headers: `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, and `Cache-Control: no-store` on API routes. HSTS is added in production.
- Every response carries a request ID.

**Logging**: logs record the method, path, status and duration. They never include query strings, headers, passwords or tokens. SQL echo is off.

**Secrets**: kept in environment variables only. `.env` is git-ignored, and `.env.example` contains placeholders only.

## Known limitations

- **The rate limiter is in-process.** Each Uvicorn worker or replica counts separately, so the effective limit is 5 × the number of workers. For strict limits across several instances, move it to Redis. The interface is a single `hit(key)` method.
- **No payments.** `REFUND_PENDING` and `REFUNDED` record what is owed, and no money is moved.
- **Access tokens are not revocable before expiry**, which is at most 30 minutes. Deactivated accounts are still blocked immediately, because every request checks the user row.
- **The status sweep runs inside every web worker.** It is idempotent and harmless, but a dedicated scheduler would be cleaner at scale.
- **One booking per request.** There is no "hold seats, then pay" flow and no seat selection.
- **Free hosting tier.** The live instance sleeps when idle (slow first request), and Render's free PostgreSQL has a limited lifetime.
