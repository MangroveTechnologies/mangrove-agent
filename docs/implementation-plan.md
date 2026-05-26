# mangrove-agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build mangrove-agent — a local FastAPI + MCP service that wraps `mangroveai` and `mangrovemarkets` SDKs, runs autonomous trading strategies on cron jobs, and logs every evaluation and trade.

**Architecture:** Single-process FastAPI app serves REST (`/api/v1/agent/*`) and MCP (`/mcp`) on port 9080 (externally; container-internal is still 8080). SQLite for all state including the APScheduler jobstore. Wallet keys encrypted with Fernet, master key in OS Keychain. Strategy evaluation delegated entirely to `mangroveai.execution.evaluate()` — the agent never reimplements signal/risk logic. Single execution path (`order_executor`) for both cron-driven and user-initiated swaps.

**Tech Stack:** Python 3.10+, FastAPI, FastMCP, SQLite, APScheduler, cryptography (Fernet), keyring, mangroveai SDK, mangrovemarkets SDK, pytest.

**Scope:** EVM-only for live execution. XRPL stubbed (501). Solana skipped. Local deployment via Docker Compose; cloud is roadmap.

**Spec:** [docs/specification.md](specification.md)
**Architecture:** [docs/architecture.md](architecture.md)
**Requirements:** [docs/user-stories.md](user-stories.md)

**Deadline:** April 24, 2026 (Bots & Bytes workshop, Nashville).

---

## Phase 1 — Foundation & Scaffold Cleanup

Goal: turn the mangrove-agent template into a mangrove-agent shell. After this phase, the app starts, but no agent endpoints exist yet.

### Task 1.1 — Scaffold cleanup

**Agent:** backend-developer
**Files:**
- Delete: `server/src/api/routes/items.py`, `server/src/api/routes/notes.py`, `server/src/api/routes/echo.py`, `server/src/api/routes/docs.py` (template demo only — agent gets its own discovery), `server/db/init.sql`, `infra/terraform/`, `.github/workflows/deploy-cloudrun.yaml`
- Delete: `server/tests/test_items.py`, `server/tests/test_notes.py`, `server/tests/test_echo.py`, `server/tests/test_docs.py`
- Rename: `server/src/api/routes/easter_egg.py` → `server/src/api/routes/hello_mangrove.py`
- Rename: `server/tests/test_easter_egg.py` → `server/tests/test_hello_mangrove.py`
- Modify: `server/src/api/router.py` (remove deleted routes, register `hello_mangrove`)
- Modify: `server/src/app.py` (update OpenAPI tags, remove items/notes/echo/docs)
- Modify: `docker-compose.yml` (remove `--profile full` services: postgres, redis; remove `db/init.sql` mount)
- Modify: `CLAUDE.md` (remove references to removed modules; update file inventory)

- [ ] **Step 1:** delete the file list above. Verify with `git status`.
- [ ] **Step 2:** run `rg easter_egg server/` and `rg EASTER_EGG server/` — replace every occurrence with `hello_mangrove` / `HELLO_MANGROVE`.
- [ ] **Step 3:** edit `server/src/app.py` `_setup_x402()` — change route key from `"GET /api/x402/easter-egg"` to `"GET /api/x402/hello-mangrove"`, update description. Update `openapi_tags` to drop items/notes/echo, leave x402.
- [ ] **Step 4:** edit `server/src/api/router.py` — remove imports + includes for deleted routes; add `hello_mangrove` import + include under `x402_router`.
- [ ] **Step 5:** edit `docker-compose.yml` — delete `postgres` and `redis` services + `volumes` block.
- [ ] **Step 6:** run the existing test suite: `cd server && pytest`. Expect failures only for removed tests (now deleted) and any test that imported the removed routes — fix any collateral.
- [ ] **Step 7:** run `docker compose up --build`. Verify the container starts and `curl http://localhost:9080/health` returns 200.
- [ ] **Step 8:** commit: `chore(scaffold): rip template demo routes; rename easter_egg → hello_mangrove`.

**Acceptance:** Clean repo with x402 still functional via `hello_mangrove`. App starts. No dead code.

---

### Task 1.2 — Configuration keys

**Agent:** backend-developer
**Files:**
- Modify: `server/src/config/configuration-keys.json`
- Modify: `server/src/config/local-example-config.json`
- Modify: `server/src/config/dev-config.json`, `prod-config.json`, `test-config.json`
- Create: `server/src/config/local-config.json` (gitignored)
- Modify: `.gitignore` (ensure `local-config.json` is ignored)

- [ ] **Step 1:** rewrite `configuration-keys.json` to match `docs/specification.md` Configuration section — include both agent keys and x402 keys.
- [ ] **Step 2:** rewrite `local-example-config.json` with the agent + x402 example values from the spec.
- [ ] **Step 3:** copy `local-example-config.json` → `local-config.json`, fill in real `MANGROVE_API_KEY` (placeholder for the user to populate).
- [ ] **Step 4:** confirm `local-config.json` is in `.gitignore`.
- [ ] **Step 5:** update `dev-config.json`, `prod-config.json`, `test-config.json` with the same shape (placeholder values).
- [ ] **Step 6:** start the app — `config.py` should load successfully. Add any missing required key handling.
- [ ] **Step 7:** commit: `chore(config): add agent + x402 configuration keys`.

**Acceptance:** App starts and `app_config.MANGROVE_API_KEY`, `app_config.DB_PATH`, `app_config.API_KEY` all read correctly.

---

### Task 1.3 — Dependencies

**Agent:** backend-developer
**Files:**
- Modify: `server/requirements.txt`
- Modify: `server/Dockerfile` (no change expected — pip install already covers this, just verify)

- [ ] **Step 1:** add to `requirements.txt`:
  ```
  mangroveai>=0.1.0
  mangrovemarkets>=0.1.0
  apscheduler[sqlalchemy]>=3.10
  cryptography>=42
  keyring>=24
  ```
- [ ] **Step 2:** rebuild Docker image: `docker compose build`. Verify install succeeds.
- [ ] **Step 3:** import smoke test — start container, exec into it, run `python -c "import mangrove_ai, mangrovemarkets, apscheduler, cryptography, keyring; print('ok')"`.
- [ ] **Step 4:** commit: `chore(deps): add SDK + scheduler + crypto + keyring`.

**Acceptance:** All five libraries installable and importable in the container.

---

### Task 1.4 — Errors module

**Agent:** backend-developer
**Files:**
- Create: `server/src/shared/errors.py`
- Create: `server/tests/unit/test_errors.py`

- [ ] **Step 1:** define `class AgentError(Exception)` with `code: str`, `message: str`, `suggestion: str | None`, `http_status: int`, `correlation_id: str` (auto-generated UUID).
- [ ] **Step 2:** define subclasses for each error code in `docs/specification.md` Error Handling section: `AuthMissingApiKey`, `AuthInvalidApiKey`, `ValidationError`, `ConfirmationRequired`, `WalletNotFound`, `WalletAlreadyExists`, `StrategyNotFound`, `StrategyInvalidStatusTransition`, `StrategyInvalidComposition`, `StrategyNoViableCandidates`, `AllocationInsufficient`, `SdkError`, `SigningError`, `EvaluationError`, `SchedulerError`, `ChainNotSupportedInV1`, `InternalError`. Each has its `code` and `http_status` baked in.
- [ ] **Step 3:** add a FastAPI exception handler that converts `AgentError` to the standard response shape from the spec (`{error, code, message, suggestion, correlation_id}`).
- [ ] **Step 4:** write a unit test per error class: instantiate, assert code + http_status + serialization shape.
- [ ] **Step 5:** wire the handler into `app.py` lifespan/startup.
- [ ] **Step 6:** commit: `feat(errors): add AgentError hierarchy + FastAPI handler`.

**Acceptance:** Raising `WalletNotFound("0xabc")` from a route returns the spec-defined JSON with HTTP 404.

---

### Task 1.5 — SDK client singletons

**Agent:** backend-developer
**Files:**
- Create: `server/src/shared/clients/__init__.py`
- Create: `server/src/shared/clients/mangrove.py`
- Create: `server/tests/unit/test_clients.py`

- [ ] **Step 1:** in `mangrove.py`, define module-level singletons (lazy):
  ```python
  from functools import lru_cache
  from mangrove_ai import MangroveAI
  from mangrove_markets import MangroveMarkets
  from src.config import app_config

  @lru_cache(maxsize=1)
  def mangrove_ai_client() -> MangroveAI:
      return MangroveAI(api_key=app_config.MANGROVE_API_KEY)

  @lru_cache(maxsize=1)
  def mangrove_markets_client() -> MangroveMarkets:
      return MangroveMarkets(
          base_url=app_config.MANGROVEMARKETS_BASE_URL,
          api_key=app_config.MANGROVE_API_KEY,
      )
  ```
- [ ] **Step 2:** write unit test that calls each accessor twice, asserts the same instance comes back.
- [ ] **Step 3:** add a smoke test: call `mangrove_ai_client().status` (or any free SDK call) — confirm a real connection works against dev URL.
- [ ] **Step 4:** commit: `feat(clients): add Mangrove SDK singletons`.

**Acceptance:** Routes and services can `from src.shared.clients.mangrove import mangrove_ai_client` and call SDK methods.

---

### Task 1.6 — Structured logging

**Agent:** backend-developer
**Files:**
- Create: `server/src/shared/logging.py`
- Modify: `server/requirements.txt` (add `structlog>=24`)
- Modify: `server/src/app.py` (install logging config at startup)
- Create: `server/tests/unit/test_logging.py`

**Goal:** every log line is structured (JSON when `ENVIRONMENT != local`, pretty console when `local`), carries a `correlation_id`, and emits consistent event names so the user can grep/tail logs and see exactly what the agent is doing.

- [ ] **Step 1:** add `structlog>=24` to `requirements.txt`.
- [ ] **Step 2:** create `shared/logging.py`:
  - `configure(env: str) -> None` — wires structlog with processors: `add_log_level`, `TimeStamper(fmt='iso', utc=True)`, `add_correlation_id` (custom), and a renderer that's `ConsoleRenderer` when `env=="local"` else `JSONRenderer`.
  - `get_logger(name: str) -> BoundLogger`.
  - `with_correlation_id(cid: str)` context manager that binds the id into the thread/contextvar so every subsequent log line in that scope carries it.
- [ ] **Step 3:** add a FastAPI middleware that generates a `correlation_id` (UUID4) per request, binds it into the logging context, and returns it in the `X-Correlation-Id` response header. If the caller supplies `X-Correlation-Id`, use that instead.
- [ ] **Step 4:** define the canonical event names the rest of the codebase will use (add to module docstring so subagents reuse them):
  ```
  # lifecycle
  "app.startup", "app.shutdown", "db.migrated", "scheduler.started"
  # auth
  "auth.accepted", "auth.rejected"
  # wallet
  "wallet.created", "wallet.signed_tx"
  # strategy
  "strategy.created", "strategy.status_changed",
  "strategy.tick.started", "strategy.tick.completed", "strategy.tick.errored"
  # order execution
  "order.executing", "order.live.signed", "order.live.broadcast",
  "order.live.confirmed", "order.paper.simulated", "order.errored"
  # scheduler
  "scheduler.job.registered", "scheduler.job.cancelled", "scheduler.job.fired"
  # sdk
  "sdk.call.started", "sdk.call.completed", "sdk.call.errored"
  ```
  Every log emit uses one of these event names as the first positional arg, with structured fields as kwargs.
- [ ] **Step 5:** wire `configure(app_config.ENVIRONMENT)` into the FastAPI lifespan startup. Emit `app.startup` with the app version.
- [ ] **Step 6:** update `shared/errors.py` exception handler to log the error event (`AgentError` instances → `level=error`, event = error code lowercased) with the correlation_id.
- [ ] **Step 7:** unit tests:
  - `test_console_renderer_in_local` — log line is human-readable
  - `test_json_renderer_in_prod` — log line is valid JSON with required keys (`event`, `level`, `timestamp`, `correlation_id`)
  - `test_correlation_id_propagates` — bind an id, assert a downstream log carries it
  - `test_request_middleware_injects_id` — send a request, assert the response header echoes back and any log emitted during the request is tagged
- [ ] **Step 8:** commit: `feat(logging): structured JSON logging with correlation_id propagation`.

**Acceptance:** `docker compose logs -f hank` shows readable lines locally and valid JSON in non-local environments. Every request has a `correlation_id` that can be traced through logs and returned in response headers.

---

### Task 1.7 — SQLite layer + migrations

**Agent:** backend-developer
**Files:**
- Create: `server/src/shared/db/__init__.py` (already exists from template — verify)
- Create: `server/src/shared/db/sqlite.py`
- Create: `server/src/shared/db/migrations/001_initial.sql`
- Modify: `server/src/shared/db/exceptions.py` (already exists — extend if needed)
- Create: `server/tests/integration/test_sqlite.py`

- [ ] **Step 1:** write `001_initial.sql` containing every CREATE TABLE + CREATE INDEX statement from `docs/specification.md` SQLite Schema section: `wallets`, `strategies`, `allocations`, `evaluations`, `trades`, `positions`. (APScheduler creates its own tables.)
- [ ] **Step 2:** in `sqlite.py`, write `get_connection() -> sqlite3.Connection` that opens `app_config.DB_PATH` with `PRAGMA foreign_keys = ON`, `PRAGMA journal_mode = WAL`. Cache via `lru_cache`.
- [ ] **Step 3:** add `init_db()` that runs all unapplied migrations in order. Track applied migrations in a `_migrations` table.
- [ ] **Step 4:** call `init_db()` from FastAPI lifespan startup.
- [ ] **Step 5:** emit `db.migrated` log event with the list of applied migration filenames after `init_db()` completes.
- [ ] **Step 6:** integration test — point `DB_PATH` to a tmp file, call `init_db()`, then introspect each table via `PRAGMA table_info(<table>)` and assert columns match the spec.
- [ ] **Step 7:** commit: `feat(db): SQLite connection + initial schema migration`.

**Acceptance:** App startup creates `agent.db` with all 6 tables, logs `db.migrated`, and restarting the app does not re-run migrations.

---

## Phase 2 — Core Infrastructure

Goal: the agent can hold wallets and write to its log tables. After this phase, no API yet, but the building blocks are in place.

### Task 2.1 — wallet_manager

**Agent:** backend-developer
**Files:**
- Create: `server/src/shared/crypto/__init__.py`, `server/src/shared/crypto/fernet.py`
- Create: `server/src/services/wallet_manager.py`
- Create: `server/src/models/db_models.py` (start; will grow across phases)
- Create: `server/tests/unit/test_wallet_manager.py`

- [ ] **Step 1:** in `crypto/fernet.py`, implement `get_master_key() -> bytes` — try `keyring.get_password(KEYRING_SERVICE_NAME, "master")`; if absent, generate via `Fernet.generate_key()`, store in keychain. Fallback to `MASTER_KEY_ENV_FALLBACK` config value when keychain unavailable.
- [ ] **Step 2:** add `encrypt(plaintext: bytes) -> bytes` and `decrypt(ciphertext: bytes) -> bytes` using Fernet with the master key.
- [ ] **Step 3:** in `wallet_manager.py`, implement `create_wallet(chain, network, chain_id, label) -> WalletCreateResult`:
  - For chain=`xrpl`: raise `ChainNotSupportedInV1`.
  - For chain=`evm`: call `mangrove_markets_client().wallet.create(...)` to get address + secret, encrypt secret, INSERT into `wallets`.
  - Return the result with the seed phrase included exactly once + the security warning from the spec.
- [ ] **Step 4:** implement `list_wallets() -> list[WalletListItem]` (no secrets returned).
- [ ] **Step 5:** implement `sign(unsigned_tx: dict, wallet_address: str) -> str` — load encrypted seed, decrypt in memory only, sign via web3.py (`eth_account`), zero the secret bytes immediately, return the signed hex string. **The SDK never receives the private key. It only receives `signed_tx` strings for broadcast.**
- [ ] **Step 6:** unit tests:
  - `test_create_wallet_evm` — create, assert row exists, secret is encrypted, seed phrase returned.
  - `test_create_wallet_xrpl_raises` — assert `ChainNotSupportedInV1`.
  - `test_list_wallets_redacts_secret` — secret never appears in output.
  - `test_sign_round_trip` — encrypt, decrypt, sign known tx, assert signature is valid.
- [ ] **Step 7:** commit: `feat(wallet): wallet_manager with Fernet encryption + local signing`.

**Acceptance:** Wallets created via `wallet_manager.create_wallet()` survive restart, secrets are encrypted on disk, signing works without leaking the key.

---

### Task 2.2 — trade_log

**Agent:** backend-developer
**Files:**
- Create: `server/src/services/trade_log.py`
- Create: `server/src/models/domain.py` (`OrderIntent`, `Evaluation`, `Trade`, `Position`)
- Create: `server/tests/unit/test_trade_log.py`

- [ ] **Step 1:** define Pydantic domain models from `docs/specification.md` Data Models: `OrderIntent`, `Evaluation`, `Trade`, `Position`.
- [ ] **Step 2:** implement `log_evaluation(evaluation: Evaluation) -> str` — INSERT into `evaluations`, return id.
- [ ] **Step 3:** implement `log_trade(trade: Trade) -> str` — INSERT into `trades`, return id.
- [ ] **Step 4:** implement `update_position(position: Position) -> None` — UPSERT into `positions`.
- [ ] **Step 5:** implement query helpers: `list_evaluations(strategy_id, limit, offset)`, `list_trades(strategy_id, limit, offset)`, `list_all_trades(limit, mode_filter)`.
- [ ] **Step 6:** unit tests for each method against a tmp SQLite DB.
- [ ] **Step 7:** commit: `feat(logs): trade_log service with evaluation + trade + position writers`.

**Acceptance:** Every cron tick can be logged and queried back.

---

### Task 2.3 — allocation_service

**Agent:** backend-developer
**Files:**
- Create: `server/src/services/allocation_service.py`
- Create: `server/tests/unit/test_allocation_service.py`

- [ ] **Step 1:** implement `record_allocation(strategy_id, wallet_address, token_address, token_symbol, amount) -> Allocation` — validate wallet exists, validate amount > 0, INSERT into `allocations` with `active=1`.
- [ ] **Step 2:** implement `release_allocation(strategy_id) -> None` — UPDATE active allocations for the strategy: set `active=0`, `released_at=now`.
- [ ] **Step 3:** implement `get_active_allocation(strategy_id) -> Allocation | None`.
- [ ] **Step 4:** unit tests including the wallet-not-found case (raises `WalletNotFound`).
- [ ] **Step 5:** commit: `feat(allocations): per-strategy fund accounting service`.

**Acceptance:** Live strategy activation records an allocation; deactivation releases it.

---

### Task 2.4 — scheduler_service

**Agent:** backend-developer
**Files:**
- Create: `server/src/services/scheduler_service.py`
- Create: `server/tests/unit/test_scheduler_service.py`
- Create: `server/tests/integration/test_scheduler_nonblocking.py`

**Non-blocking semantics:** `BackgroundScheduler` runs jobs in a separate threadpool. HTTP requests (REST + MCP) must never wait on a cron tick. A tick firing must not delay `GET /status` or any other request. Tests enforce this property.

- [ ] **Step 1:** implement module-level `BackgroundScheduler` with `SQLAlchemyJobStore(url=f"sqlite:///{app_config.DB_PATH}")`, `executors={'default': ThreadPoolExecutor(max_workers=10)}`, `job_defaults={'coalesce': True, 'max_instances': 1, 'misfire_grace_time': 60}`. Lazy init.
- [ ] **Step 2:** add timeframe-to-cron mapping table from architecture doc (1m → `*/1 * * * *`, etc.).
- [ ] **Step 3:** implement `register_job(strategy_id, timeframe, callable_path) -> str` — adds a `CronTrigger` job named `eval-<strategy_id>`. Idempotent (replace existing). Emit `scheduler.job.registered` log with strategy_id + cron expression.
- [ ] **Step 4:** implement `cancel_job(strategy_id) -> None` (emit `scheduler.job.cancelled`) and `list_active_jobs() -> list[dict]` returning `{strategy_id, next_run_at, cron_expression, last_run_at}`.
- [ ] **Step 5:** add a scheduler event listener for `EVENT_JOB_EXECUTED` + `EVENT_JOB_ERROR` that emits `scheduler.job.fired` (success) or `scheduler.job.errored` (failure) with strategy_id + duration + exception info. This is how external observers (including the chat UI) know a tick actually fired.
- [ ] **Step 6:** wire scheduler `start()` into FastAPI lifespan (log `scheduler.started`); `shutdown(wait=False)` on app stop so app shutdown doesn't block on in-flight ticks.
- [ ] **Step 7:** unit tests:
  - register a job, list jobs, assert it's there
  - cancel, assert it's gone
  - register same strategy twice, assert no duplicates
  - registering emits the `scheduler.job.registered` log event
- [ ] **Step 8:** integration test `test_scheduler_nonblocking.py` — register a job whose callable sleeps 3 seconds, then immediately hit `GET /status` 10 times in a row within a 1-second window. Assert every request returns under 100 ms (i.e., the slow tick does not block the request path). Assert `scheduler.job.fired` log event appears after the tick's sleep completes.
- [ ] **Step 9:** commit: `feat(scheduler): non-blocking APScheduler wrapper with fire-event observability`.

**Acceptance:** Jobs persist across app restart; HTTP requests are unaffected by in-flight ticks; every tick fire emits a structured log event that external observers (chat UI, log tail) can see.

---

## Phase 3 — Strategy Pipeline

Goal: the autonomous strategy creation flow + cron evaluation work end-to-end against the SDK.

### Task 3.1 — candidate_generator

**Agent:** backend-developer
**Files:**
- Create: `server/src/services/candidate_generator.py`
- Create: `server/tests/unit/test_candidate_generator.py`

- [ ] **Step 1:** define a deterministic mapping table — goal keywords → signal categories. Use the categories returned by `mangroveai.signals`. Example seed mapping:
  ```python
  GOAL_TO_CATEGORIES = {
      "momentum": {"trigger": ["momentum", "trend"], "filter": ["volume", "trend"]},
      "mean_reversion": {"trigger": ["overbought_oversold"], "filter": ["volatility"]},
      "breakout": {"trigger": ["breakout"], "filter": ["volume"]},
      "trend": {"trigger": ["trend"], "filter": ["momentum", "volume"]},
  }
  ```
- [ ] **Step 2:** implement `parse_goal(goal: str) -> dict` — detect keywords case-insensitive; default to "momentum" if none match.
- [ ] **Step 3:** implement `generate(goal, asset, timeframe, n=7) -> list[StrategyCandidate]`:
  - Fetch signal catalog via `mangrove_ai_client().signals.list()`
  - Filter by category buckets from the parsed goal
  - For each of n candidates: random-pick 1 trigger, 0–2 filters for entry; 0–1 trigger + 0+ filters for exit
  - Use sensible default param values from the signal metadata
  - Deterministic with a seed (so backtests are reproducible)
- [ ] **Step 4:** unit tests:
  - `test_parse_goal` — known phrasing → expected categories
  - `test_generate_seeded` — same seed produces same candidates
  - `test_generate_respects_composition_rules` — entry has exactly 1 trigger, exit has 0–1
- [ ] **Step 5:** commit: `feat(candidates): deterministic goal-to-strategy candidate generator`.

**Acceptance:** Calling `generate("momentum on ETH", "ETH", "1h")` returns 7 well-formed candidates that pass `mangroveai`'s strategy schema.

---

### Task 3.2 — backtest_service

**Agent:** backend-developer
**Files:**
- Create: `server/src/services/backtest_service.py`
- Create: `server/tests/integration/test_backtest_service.py`

- [ ] **Step 1:** implement `quick_backtest_all(candidates, asset, timeframe, lookback_months) -> list[BacktestResult]`:
  - For each candidate, call `mangrove_ai_client().backtesting.run(mode="quick", ...)` with the candidate's strategy_json
  - Capture per-candidate metrics
- [ ] **Step 2:** implement `filter_and_rank(results) -> list[BacktestResult]`:
  - Drop any with `win_rate <= 0.51` or `total_trades < 10`
  - Sort surviving by `irr_annualized` descending
- [ ] **Step 3:** implement `full_backtest(strategy_json, lookback_months, start_date=None, end_date=None) -> BacktestResult`.
- [ ] **Step 4:** integration tests against the dev Mangrove env:
  - `test_quick_backtest_returns_metrics` — assert all expected metric fields populated
  - `test_filter_drops_low_win_rate`
  - `test_filter_drops_low_trade_count`
  - `test_rank_by_irr`
  - `test_full_backtest_returns_trades` — full mode includes trade history
- [ ] **Step 5:** commit: `feat(backtest): quick + full backtest orchestration with IRR ranking`.

**Acceptance:** Pipeline runs 7 candidates in <30s wall clock against dev env, returns ranked list.

---

### Task 3.3 — order_executor

**Agent:** backend-developer
**Files:**
- Create: `server/src/services/order_executor.py`
- Create: `server/src/shared/explorer.py` (chain_id → block explorer URL mapping)
- Create: `server/tests/unit/test_order_executor.py`
- Create: `server/tests/unit/test_explorer.py`
- Create: `server/tests/integration/test_order_executor_live.py`

**Signing boundary:** All signing happens client-side inside `wallet_manager.sign()`. The `mangrovemarkets` SDK is handed already-signed transaction strings for broadcast; it never sees the seed phrase, private key, or any plaintext secret material.

- [ ] **Step 1:** create `shared/explorer.py` with `explorer_url(chain_id: int, tx_hash: str) -> str | None`. Mapping:
  ```python
  EXPLORERS = {
      1:     "https://etherscan.io/tx/",
      8453:  "https://basescan.org/tx/",
      84532: "https://sepolia.basescan.org/tx/",
      42161: "https://arbiscan.io/tx/",
      137:   "https://polygonscan.com/tx/",
      10:    "https://optimistic.etherscan.io/tx/",
      56:    "https://bscscan.com/tx/",
      43114: "https://snowtrace.io/tx/",
      324:   "https://explorer.zksync.io/tx/",
      100:   "https://gnosisscan.io/tx/",
      59144: "https://lineascan.build/tx/",
  }
  ```
  Unit test: each chain_id maps to the right base URL + tx_hash.
- [ ] **Step 2:** define `execute_one(intent: OrderIntent, mode: Literal["paper", "live"], wallet_address: str | None = None) -> Trade`. For paper: skip to step 4. For live: continue.
- [ ] **Step 3 (live mode):** implement the full 6-step swap from the spec:
  1. `dex.get_quote(input_token, output_token, amount, chain_id)` → Quote
  2. `dex.approve_token(...)` → may return None (already approved)
  3. If approval returned: call `wallet_manager.sign(approval_tx, wallet_address)` (client-side sign, SDK never sees secret) → pass `signed_tx` to `dex.broadcast(signed_tx)` → poll `dex.tx_status` until confirmed
  4. `dex.prepare_swap(quote_id, wallet_address)` → UnsignedTx
  5. `wallet_manager.sign(swap_tx, wallet_address)` (client-side sign) → `dex.broadcast(signed_tx)` → poll `dex.tx_status`
  6. Build `Trade` with `mode="live"`, `tx_hash`, `explorer_url=explorer_url(chain_id, tx_hash)`, `approval_tx_hash`, fill amounts/prices/fees
- [ ] **Step 4 (paper mode):** fetch current price via `mangrove_ai_client().crypto_assets.get_market_data(intent.symbol)`, build a `Trade` with `mode="paper"`, `status="simulated"`, `tx_hash=None`, `explorer_url=None`, fill at mid/mark price.
- [ ] **Step 5:** call `trade_log.log_trade(trade)` and `trade_log.update_position(...)` based on intent type (enter/exit). Return the `Trade`.
- [ ] **Step 6:** add `execute_many(intents, mode, wallet_address) -> list[Trade]` — sequential loop, each wrapped in try/except so one failure doesn't stop the batch.
- [ ] **Step 7:** unit tests with mocked SDK:
  - `test_paper_simulates_at_mark_price` — no SDK swap calls made, no signing
  - `test_live_skips_approval_when_none` — no approval signed
  - `test_live_full_flow_with_approval` — both txs signed client-side; SDK receives only signed bytes
  - `test_live_trade_includes_explorer_url` — chain_id 8453 → url starts with `https://basescan.org/tx/`
  - `test_failure_in_one_does_not_block_others` (for execute_many)
  - `test_sdk_never_receives_plaintext_key` — inspect all mock SDK call args, assert no seed phrase or private key present
- [ ] **Step 8:** integration test against Base Sepolia (Chain ID 84532) with a funded testnet wallet — actually swap a small amount and verify the trade row includes the explorer URL.
- [ ] **Step 9:** commit: `feat(executor): single swap path with client-side signing and explorer URLs`.

**Acceptance:** Paper mode logs simulated trades; live mode (testnet) completes a real swap and writes a Trade row with the tx hash.

---

### Task 3.4 — strategy_service

**Agent:** backend-developer
**Files:**
- Create: `server/src/services/strategy_service.py`
- Create: `server/tests/integration/test_strategy_service.py`

- [ ] **Step 1:** implement `create_autonomous(req: StrategyCreateAutonomousRequest) -> StrategyDetail`:
  1. `candidate_generator.generate(...)` → list of candidates
  2. `backtest_service.quick_backtest_all(...)` → results
  3. `filter_and_rank(...)` → survivors
  4. If empty: raise `StrategyNoViableCandidates` with suggestion
  5. `backtest_service.full_backtest(winner)` → full metrics
  6. `mangrove_ai_client().strategies.create(winner)` → mangrove_id
  7. Cache locally in `strategies` table with `generation_report_json`
  8. Return `StrategyDetail`
- [ ] **Step 2:** implement `create_manual(req)` — validate composition (1 TRIGGER + 0+ FILTERs entry; 0–1 TRIGGER + 0+ FILTERs exit), call `mangrove_ai_client().strategies.create(...)`, cache locally.
- [ ] **Step 3:** implement `list_strategies(status_filter, limit, offset)` and `get_strategy(id)` — read from local cache.
- [ ] **Step 4:** implement `update_status(id, status, confirm, allocation) -> StrategyDetail`:
  - Validate transition per spec (`StrategyInvalidStatusTransition` if illegal)
  - `confirm=True` required for live activation or live deactivation
  - On `→ live`: validate allocation block, call `allocation_service.record_allocation()`, call `mangrove_ai_client().strategies.update_status()`, register cron job via `scheduler_service.register_job(strategy_id, timeframe, "src.services.strategy_service.tick")`
  - On `→ paper`: register cron, no allocation
  - On `→ inactive` or `→ archived`: cancel cron, release allocation if any
- [ ] **Step 5:** implement `tick(strategy_id) -> Evaluation` — the cron callback. **Runs inside the scheduler threadpool; must never block the request path.** Every tick emits structured logs so external observers can see the fire-and-result sequence:
  1. Generate a `tick_id` (UUID), bind correlation_id to it, emit `strategy.tick.started` with `strategy_id`, `tick_id`, `timeframe`.
  2. Load strategy from local cache to get the `mangrove_id` + mode (`paper` or `live`) + wallet_address (live only).
  3. Call `mangrove_ai_client().execution.evaluate(mangrove_id, persist=(mode == "live"))` — the SDK fetches its own market data and applies all signal evaluation, position sizing, and risk gates. Emit `sdk.call.started` / `sdk.call.completed` bracketing. The returned `EvaluateResult` contains any `OrderIntent[]` the strategy generated.
  4. If order_intents empty: persist evaluation with `status="ok"`, emit `strategy.tick.completed` with `order_count=0`, `duration_ms`.
  5. If order_intents present: dispatch to `order_executor.execute_many(intents, mode, wallet_address)`, persist evaluation with `sdk_response_json` verbatim, emit `strategy.tick.completed` with `order_count=N`, `duration_ms`.
  6. On any exception: persist evaluation with `status="error"`, emit `strategy.tick.errored` with `exception` + `duration_ms`. **Never let the exception propagate out of the tick callback** — that would crash the scheduler worker.

**Note on `persist`:** Mangrove's `evaluate(persist=True)` writes orders/positions/trades to Mangrove's own account records. For live mode we want that (keeps Mangrove's view consistent with ours). For paper mode we set `persist=False` so simulated runs don't pollute Mangrove's live history — the agent's local SQLite is the source of truth for paper trades.
- [ ] **Step 6:** integration tests:
  - `test_create_autonomous_happy_path` — produces a StrategyDetail with generation_report
  - `test_create_autonomous_no_viable_candidates` — raises 422
  - `test_status_transition_paper_to_live_requires_confirm`
  - `test_status_transition_to_live_registers_cron_and_allocation`
  - `test_tick_paper_mode_logs_simulated_trade`
- [ ] **Step 7:** commit: `feat(strategy): orchestration service for create, lifecycle, and cron tick`.

**Acceptance:** Calling `create_autonomous` produces a working strategy in Mangrove + local cache; activating to paper registers a cron that ticks and logs.

---

## Phase 4 — API Layer

Goal: every spec endpoint and MCP tool is wired up. After this phase, the agent is feature-complete from the user's perspective.

**Service-layer discipline (per architecture):** Tasks 4.4 and parts of 4.2/4.3 create **routes only** — no wrapper service modules. Routes for market data, on-chain, signals, KB, portfolio, and DEX read operations (venues, pairs, quote) import the SDK singletons from `shared/clients/mangrove.py` and call the SDK methods inline. The six pass-through service modules we decided against (`signal_service`, `market_data`, `on_chain`, `kb_service`, `dex_service`, `portfolio_service`) are **not** created in this phase or any other. If the plan ever tempts you to create one, stop — routes call the SDK directly.

### Task 4.1 — Discovery routes

**Agent:** backend-developer
**Files:**
- Create: `server/src/api/routes/discovery.py`
- Modify: `server/src/api/router.py` (mount under `/api/v1/agent`)
- Create: `server/tests/integration/test_discovery_routes.py`

- [ ] **Step 1:** create `GET /tools` — return MCP tool catalog (placeholder; will be auto-populated once MCP tools are registered in 4.7).
- [ ] **Step 2:** create `GET /status` — return `{version, wallets_count, strategies: {…}, active_cron_jobs, db_path, uptime_seconds}`.
- [ ] **Step 3:** ensure `/health` already works (template provides it).
- [ ] **Step 4:** integration tests for each endpoint.
- [ ] **Step 5:** commit: `feat(api): discovery routes (status, tools)`.

**Acceptance:** `curl http://localhost:9080/api/v1/agent/status` returns the spec-defined shape.

---

### Task 4.2 — Wallet routes

**Agent:** backend-developer
**Files:**
- Create: `server/src/api/routes/wallet.py`
- Create: `server/src/models/requests.py`, `server/src/models/responses.py` (start; will grow)
- Create: `server/tests/integration/test_wallet_routes.py`

- [ ] **Step 1:** define request/response Pydantic models for wallet endpoints from the spec.
- [ ] **Step 2:** implement routes:
  - `POST /wallet/create` → `wallet_manager.create_wallet(...)`
  - `GET /wallet/list` → `wallet_manager.list_wallets()`
  - `GET /wallet/{address}/balances?chain_id` → `mangrove_markets_client().dex.balances(chain_id, address)` directly
  - `GET /wallet/{address}/portfolio?chain_id` → `mangrove_markets_client().portfolio.value/pnl/tokens/defi(...)` directly, aggregate
  - `GET /wallet/{address}/history?limit` → `mangrove_markets_client().portfolio.history(...)` directly
- [ ] **Step 3:** wire auth via the existing middleware (auth required on all wallet endpoints).
- [ ] **Step 4:** integration tests for create + list happy paths and `WalletNotFound` error.
- [ ] **Step 5:** commit: `feat(api): wallet routes`.

**Acceptance:** Full wallet workflow works via REST.

---

### Task 4.3 — DEX routes

**Agent:** backend-developer
**Files:**
- Create: `server/src/api/routes/dex.py`
- Create: `server/tests/integration/test_dex_routes.py`

- [ ] **Step 1:** routes that pass through to SDK directly:
  - `GET /dex/venues` → `mangrove_markets_client().dex.supported_venues()`
  - `GET /dex/pairs?venue_id` → `mangrove_markets_client().dex.supported_pairs(venue_id)`
  - `POST /dex/quote` → `mangrove_markets_client().dex.get_quote(...)`
- [ ] **Step 2:** `POST /dex/swap`:
  - Require `confirm=True` else raise `ConfirmationRequired`
  - Build `OrderIntent` from request body
  - Call `order_executor.execute_one(intent, mode="live", wallet_address=req.wallet_address)`
  - Return `SwapResult` populated from the returned `Trade` — **including `tx_hash`, `approval_tx_hash` (nullable), and `explorer_url`** so the user can click through to the block explorer
  - All signing stays inside `wallet_manager.sign()` on the client side; the SDK receives only signed tx bytes
- [ ] **Step 3:** **Spec sync:** the `SwapResult` response shape gains three fields — `tx_hash`, `approval_tx_hash` (nullable), `explorer_url` (nullable for paper). Update `docs/specification.md` `POST /dex/swap` response shape accordingly as part of this task, in the same commit.
- [ ] **Step 4:** integration tests including the `confirm=False` rejection path and an assertion that the response includes `explorer_url` for live swaps.
- [ ] **Step 5:** commit: `feat(api): DEX routes (venues, pairs, quote, swap with explorer URL)`.

**Acceptance:** End-to-end swap (testnet) via `POST /dex/swap` works. Response includes a clickable block explorer URL.

---

### Task 4.4 — Pass-through routes (market, on-chain, signals, KB)

**Agent:** backend-developer
**Files:**
- Create: `server/src/api/routes/market.py`
- Create: `server/src/api/routes/on_chain.py`
- Create: `server/src/api/routes/signals.py`
- Create: `server/src/api/routes/kb.py`
- Create: `server/tests/integration/test_passthrough_routes.py`

- [ ] **Step 1:** market routes — all delegate to `mangrove_ai_client().crypto_assets.*`:
  - `GET /market/ohlcv?symbol&timeframe&lookback_days`
  - `GET /market/data?symbol`
  - `GET /market/trending`
  - `GET /market/global`
- [ ] **Step 2:** on-chain routes — delegate to `mangrove_ai_client().on_chain.*`:
  - `GET /on-chain/smart-money?symbol&chain`
  - `GET /on-chain/whale-activity?symbol&hours_back`
  - `GET /on-chain/token-holders/{symbol}`
- [ ] **Step 3:** signals routes — delegate to `mangrove_ai_client().signals.*`:
  - `GET /signals?category&search&limit`
  - `GET /signals/{name}`
- [ ] **Step 4:** KB routes — delegate to `mangrove_ai_client().kb.*`:
  - `GET /kb/search?q&limit`
  - `GET /kb/glossary/{term}`
- [ ] **Step 5:** one integration test per route that just confirms the SDK call succeeds and the response is well-formed (mock the SDK; we're testing the wiring, not the SDK).
- [ ] **Step 6:** commit: `feat(api): market + on-chain + signals + KB routes`.

**Acceptance:** All pass-through endpoints reachable via REST.

---

### Task 4.5 — Strategy routes

**Agent:** backend-developer
**Files:**
- Create: `server/src/api/routes/strategies.py`
- Create: `server/tests/integration/test_strategy_routes.py`

- [ ] **Step 1:** define request/response models for all strategy endpoints from the spec.
- [ ] **Step 2:** implement routes (all delegate to `strategy_service`):
  - `POST /strategies/autonomous`
  - `POST /strategies/manual`
  - `GET /strategies?status&limit&offset`
  - `GET /strategies/{id}`
  - `PATCH /strategies/{id}/status` — single source of truth for lifecycle (incl. allocation in body for live)
  - `POST /strategies/{id}/backtest` — `{mode, lookback_months, start_date?, end_date?}`
  - `POST /strategies/{id}/evaluate` — manual tick (debugging)
- [ ] **Step 3:** integration tests including the autonomous happy path, the no-viable-candidates 422, and the live-without-confirm 400.
- [ ] **Step 4:** commit: `feat(api): strategy routes (CRUD, lifecycle, backtest, evaluate)`.

**Acceptance:** Full strategy lifecycle reachable via REST.

---

### Task 4.6 — Logs routes

**Agent:** backend-developer
**Files:**
- Create: `server/src/api/routes/logs.py`
- Create: `server/tests/integration/test_logs_routes.py`

- [ ] **Step 1:** implement routes (all delegate to `trade_log`):
  - `GET /strategies/{id}/evaluations?limit&offset`
  - `GET /strategies/{id}/trades?limit&offset`
  - `GET /trades?limit&strategy_id&mode`
- [ ] **Step 2:** integration tests against a seeded SQLite.
- [ ] **Step 3:** commit: `feat(api): log routes`.

**Acceptance:** Audit trail queryable via REST.

---

### Task 4.7 — MCP tool registration

**Agent:** backend-developer
**Files:**
- Modify: `server/src/mcp/tools.py`
- Modify: `server/src/mcp/registry.py` (if helpers needed)
- Create: `server/tests/integration/test_mcp_tools.py`

- [ ] **Step 1:** for every REST route in 4.1–4.6, register a matching MCP tool. Tool names from `docs/specification.md` MCP Tools table — plain `verb_resource` form, no project prefix.
- [ ] **Step 2:** core 22 tools first (see spec); nice-to-haves last. Each tool calls the same service function the REST route does — never duplicate logic.
- [ ] **Step 3:** ensure `GET /api/v1/agent/tools` returns the now-populated catalog from the registry.
- [ ] **Step 4:** integration test — start the app, connect via FastMCP test client, list tools, call `status` and assert response.
- [ ] **Step 5:** commit: `feat(mcp): register all agent tools mirroring REST routes`.

**Acceptance:** Claude Code with `.mcp.json` pointing to the agent can list and call all tools.

---

## Phase 5 — Verification

Goal: prove the full system works end-to-end.

### Task 5.1 — Endpoint smoke test

**Agent:** test-engineer
**Files:**
- Create: `server/tests/e2e/test_smoke.py`

- [ ] **Step 1:** parametrized test that hits every REST endpoint with valid input and asserts 2xx + a basic response shape. Use a fixture that sets `ENVIRONMENT=test` and a tmp DB.
- [ ] **Step 2:** also invoke each MCP tool via test client.
- [ ] **Step 3:** run `pytest server/tests/e2e/test_smoke.py` — must pass.
- [ ] **Step 4:** commit: `test(e2e): smoke test for all endpoints + MCP tools`.

**Acceptance:** Every endpoint returns the expected status code on a happy-path call.

---

### Task 5.2 — E2E paper trading lifecycle (non-blocking observation)

**Agent:** test-engineer
**Files:**
- Create: `server/tests/e2e/test_paper_lifecycle.py`

**Observation principle:** the test must NOT `time.sleep()` past a cron interval. It must stay responsive while the cron fires in the background, exactly the way a user chatting with the agent stays responsive. The test polls for tick evidence via HTTP at short intervals, simulating a real user checking in periodically.

- [ ] **Step 1:** test scenario:
  1. Create EVM wallet via `POST /wallet/create`.
  2. Create autonomous strategy: `POST /strategies/autonomous` with `{goal: "momentum", asset: "ETH", timeframe: "1m"}`.
  3. Activate to `paper` via `PATCH /strategies/{id}/status` with `{status: "paper"}`.
  4. Record `t_activation` timestamp.
  5. **Non-blocking poll loop** (no `sleep()` past one tick interval):
     - Every 5 seconds, in parallel with other non-blocking work:
       - `GET /status` — assert it returns in < 100 ms (proves the cron isn't blocking the request path).
       - `GET /strategies/{id}/evaluations?limit=1` — check if an evaluation with `timestamp > t_activation` exists.
     - Stop polling when an evaluation is found OR when 90 seconds elapsed (timeout failure).
  6. Assert the found evaluation has `status="ok"`. If `order_intents` is non-empty, assert corresponding `trades` rows with `mode="paper"`, `status="simulated"`, `tx_hash=None`.
  7. Also verify via log tail: capture stdout during the test window and assert `strategy.tick.started` + `strategy.tick.completed` events appear at least once for this `strategy_id`.
  8. Deactivate: `PATCH /strategies/{id}/status` with `{status: "inactive"}`. Assert `GET /status` shows `active_cron_jobs` decremented.
- [ ] **Step 2:** uses the dev Mangrove env. Skip if `SKIP_E2E=1`.
- [ ] **Step 3:** commit: `test(e2e): paper lifecycle with non-blocking tick observation`.

**Acceptance:** The full lifecycle runs end-to-end. The test itself proves the agent stays responsive to `/status` requests while a tick fires in the background. Log events prove the tick actually executed.

---

### Task 5.3 — E2E live swap on Base Sepolia testnet

**Agent:** test-engineer
**Files:**
- Create: `server/tests/e2e/test_live_swap_testnet.py`
- Create: `docs/testing-testnet.md` (funding + faucet runbook)

**Chain:** Base Sepolia (Chain ID `84532`). Explorer: `https://sepolia.basescan.org/`.

**Faucets (document in `docs/testing-testnet.md`):**
- Sepolia ETH faucet: `https://www.alchemy.com/faucets/base-sepolia`
- USDC on Base Sepolia (test token): deploy locally via the template's fixtures OR use Circle's testnet faucet `https://faucet.circle.com/` (select Base Sepolia)
- Expected funded balance before test: ~0.01 Sepolia ETH for gas + ~5 test USDC for swap input

- [ ] **Step 1:** write `docs/testing-testnet.md` with: chain info (name, ID, RPC URL, explorer), faucet links, step-by-step funding runbook (create wallet via `POST /wallet/create` with `chain=evm, network=testnet, chain_id=84532`, copy address shown once, visit both faucets, wait for confirmations, run test).
- [ ] **Step 2:** test scenario in `test_live_swap_testnet.py`:
  1. Fixture reads `BASE_SEPOLIA_WALLET_ADDRESS` env var (the pre-funded wallet address, created in advance and persisted in the agent's DB).
  2. `POST /dex/swap` with: `input_token=<USDC on Sepolia>`, `output_token=<WETH on Sepolia>`, `amount=1.0` (1 test USDC), `chain_id=84532`, `wallet_address=<fixture>`, `confirm=true`.
  3. Assert response includes `tx_hash`, `status="confirmed"`, `explorer_url` starting with `https://sepolia.basescan.org/tx/`.
  4. Fetch the URL with `httpx` (lightweight sanity check the explorer page exists — HTTP 200) — does not assert page contents.
  5. Query `/trades?limit=1` — assert the row matches (tx_hash, mode=live, status=confirmed, explorer_url populated).
- [ ] **Step 3:** Skip test if `SKIP_E2E=1` or `BASE_SEPOLIA_WALLET_ADDRESS` not set.
- [ ] **Step 4:** commit: `test(e2e): live DEX swap on Base Sepolia testnet with explorer verification`.

**Acceptance:** Test passes against Base Sepolia. The explorer URL returned by the agent actually resolves to the transaction page. **This task must pass before Task 5.4 runs on real mainnet.**

---

### Task 5.4 — E2E live swap on Base mainnet (real funds)

**Agent:** test-engineer
**Files:**
- Create: `server/tests/e2e/test_live_swap_mainnet.py`
- Create: `docs/testing-mainnet.md` (pre-flight checklist)

**Chain:** Base mainnet (Chain ID `8453`). Explorer: `https://basescan.org/`.

**Pre-flight (all must be true before running this test):**
- Task 5.3 Base Sepolia test passes cleanly
- `ENABLE_MAINNET_TEST=1` set explicitly (opt-in — default is skip)
- `BASE_MAINNET_WALLET_ADDRESS` set to a wallet with ≥ $2 USDC + ≥ $1 worth of ETH for gas
- User has verified the wallet address in the agent's DB matches their expectation

**Budget for this test:** ≤ $1 USDC swap size. Gas ≤ $0.50. Total exposure ≤ $1.50 per run.

- [ ] **Step 1:** write `docs/testing-mainnet.md` with the pre-flight checklist above, "this spends real money" warning in bold, recovery plan if the tx gets stuck (revoke approval via `https://revoke.cash`), explorer link format.
- [ ] **Step 2:** test scenario:
  1. Fail-fast if `ENABLE_MAINNET_TEST != "1"` or `BASE_MAINNET_WALLET_ADDRESS` unset.
  2. `POST /dex/swap` with: `input_token=<USDC mainnet>`, `output_token=<WETH mainnet>`, `amount=1.0`, `chain_id=8453`, `wallet_address=<fixture>`, `confirm=true`, `slippage=0.5`.
  3. Assert `tx_hash` present, `status="confirmed"`, `explorer_url` starts with `https://basescan.org/tx/`.
  4. Fetch the explorer URL — HTTP 200.
  5. Query `/trades` — assert the row has the real tx_hash and USD-denominated fills.
- [ ] **Step 3:** Document the run in `docs/testing-mainnet.md` — paste the resulting explorer URL as "last verified mainnet swap" so future runs have a reference.
- [ ] **Step 4:** commit: `test(e2e): live DEX swap on Base mainnet (opt-in, $1 budget)`.

**Acceptance:** One successful real-money swap on Base mainnet. Trade row persisted. Explorer URL resolves. Task 5.3 must pass first.

---

## Phase 6 — Workshop Polish

Goal: anyone cloning the repo on workshop day can `docker compose up`, hand Claude Code an `.mcp.json`, and have a working trading bot.

### Task 6.1 — README + verification of the 5-minute quick start

**Agent:** backend-developer
**Files:**
- Modify: `README.md`
- Create: `.mcp.json.example`
- Create: `scripts/verify_quickstart.sh`
- Modify: `docs/configuration.md` (refresh to match the agent's actual config)

**Acceptance criterion for this task: a fresh clone on a clean machine produces a working agent in under 5 minutes, verified by a scripted cold run.**

- [ ] **Step 1:** rewrite `README.md` with a **numbered, copy-pasteable quick start** (everything between the numbered steps must be a literal command or config paste — no prose like "then edit your config"):
  1. `git clone <repo> && cd mangrove-agent`
  2. `cp server/src/config/local-example-config.json server/src/config/local-config.json`
  3. `sed -i '' 's/MANGROVE_API_KEY_PLACEHOLDER/<your key>/' server/src/config/local-config.json` (or manual edit — document both)
  4. `docker compose up -d --build`
  5. `curl http://localhost:9080/health` — expect 200
  6. `curl -H "X-API-Key: local-dev-key" http://localhost:9080/api/v1/agent/status` — expect JSON with `version`, `wallets_count: 0`
  7. `cp .mcp.json.example .mcp.json` in any Claude Code project to connect
- [ ] **Step 2:** explicit scope section in README: "v1 supports EVM chains only (Ethereum, Base, Arbitrum, Polygon, Optimism, BNB, Avalanche, zkSync, Gnosis, Linea). XRPL wallet creation returns a 501 in v1. Solana is not supported."
- [ ] **Step 3:** link to `docs/testing-testnet.md` (Base Sepolia runbook) and `docs/testing-mainnet.md` (real-funds pre-flight) from the README under a "Testing" section.
- [ ] **Step 4:** create `.mcp.json.example`:
  ```json
  {
    "mcpServers": {
      "mangrove-agent": {
        "transport": "http",
        "url": "http://localhost:9080/mcp",
        "headers": {
          "X-API-Key": "local-dev-key"
        }
      }
    }
  }
  ```
- [ ] **Step 5:** create `scripts/verify_quickstart.sh` — a bash script that runs every command from the README quick start against a fresh clone in a tempdir, captures timings, and fails if any step errors OR if total elapsed > 300 seconds. The script is the **executable form** of the README.
- [ ] **Step 6:** run `scripts/verify_quickstart.sh` yourself. If it fails or takes longer than 5 minutes, fix the README or the container startup path until it passes.
- [ ] **Step 7:** refresh `docs/configuration.md` to match the v1 config keys.
- [ ] **Step 8:** commit: `docs: README + verified quick start + .mcp.json example`.

**Acceptance:** `scripts/verify_quickstart.sh` exits 0 in under 300 seconds on a cold clone.

---

### Task 6.2 — Final docker-compose verify

**Agent:** devops-engineer
**Files:**
- Modify: `docker-compose.yml` if needed
- Modify: `server/Dockerfile` if needed

- [ ] **Step 1:** mount `./agent.db` as a volume so it survives container restarts.
- [ ] **Step 2:** ensure `local-config.json` is mounted from a host path (or built into the image at build time for the workshop demo).
- [ ] **Step 3:** end-to-end smoke: `docker compose down -v`, `docker compose up --build`, run smoke test from 5.1 against the running container.
- [ ] **Step 4:** commit: `chore(docker): persistent volume + config mount`.

**Acceptance:** `docker compose up` produces a working agent that survives restart with state intact.

---

### Task 6.3 — Code review pass

**Agent:** code-review
**Files:** all modified files since branch start

- [ ] **Step 1:** run code-review across the diff vs. main. Address findings in follow-up commits.
- [ ] **Step 2:** verify against `.claude/rules/code-style.md` (if present) and the repo conventions.
- [ ] **Step 3:** verify spec/architecture/plan traceability — every endpoint exists, every service exists, no extra cruft.
- [ ] **Step 4:** commit any review fixes.

**Acceptance:** No blocking findings; ready for merge.

---

## Summary

**Total: 24 tasks across 6 phases.**

| Phase | Tasks | Parallelizable? |
|-------|-------|-----------------|
| 1. Foundation & cleanup | 7 | Mostly sequential (1.1 → 1.2 → 1.3 → 1.4/1.5 parallel → 1.6 → 1.7) |
| 2. Core infrastructure | 4 | 2.2/2.3/2.4 parallel after 2.1 |
| 3. Strategy pipeline | 4 | 3.1/3.2 parallel after Phase 2; 3.3 needs 2.1; 3.4 needs all of Phase 3 |
| 4. API layer | 7 | 4.1–4.6 parallel after Phase 3; 4.7 last |
| 5. Verification | 4 | Sequential; 5.4 gated by 5.3 and `ENABLE_MAINNET_TEST=1` |
| 6. Polish | 3 | Sequential |

**Critical path (sequential):** 1.1 → 1.2 → 1.3 → 1.6 (logging) → 1.7 (SQLite) → 2.1 → 3.3 → 3.4 → 4.7 → 5.1 → 5.2 → 5.3 → 5.4 → 6.x.

**Agent allocation:**
- backend-developer: 17 tasks
- test-engineer: 4 tasks (5.1 smoke + 5.2 paper E2E + 5.3 Sepolia E2E + 5.4 mainnet E2E)
- devops-engineer: 1 task
- code-review: 1 task (final pass)
- diagram-agent: not needed (diagrams already approved in arch phase)

**Definition of done for v1:**
1. All 24 tasks complete.
2. Paper lifecycle E2E green (Task 5.2) — proves non-blocking scheduler + observable tick events.
3. Base Sepolia testnet live swap E2E green with explorer URL verified (Task 5.3).
4. Base mainnet real-funds swap verified once, tx hash pasted in `docs/testing-mainnet.md` (Task 5.4).
5. `docker compose up` produces a working agent (Task 6.2).
6. `scripts/verify_quickstart.sh` exits 0 in under 300 seconds on a cold clone (Task 6.1).
7. Logs are structured JSON in non-local environments, correlation_id flows through every request → tick → SDK call → log line.

**Architecture discipline reminders (flagged during plan audit):**
- All signing is **client-side** in `wallet_manager.sign()`. The `mangrovemarkets` SDK receives only signed transaction bytes; it never sees seed phrases or private keys. Every task touching the DEX swap flow (2.1, 3.3, 4.3) carries this note.
- **No pass-through service modules exist or should be created.** Routes for market/on-chain/signals/KB/portfolio/dex-read-ops call the SDK clients directly from `shared/clients/mangrove.py`. The 8 services that DO exist (wallet_manager, strategy_service, candidate_generator, backtest_service, order_executor, scheduler_service, trade_log, allocation_service) all add orchestration the SDK doesn't provide.
- **XRPL is stubbed (501)**, not implemented. Users creating an XRPL wallet get a clear error. README calls this out.
- **Mainnet testing is opt-in and capped at $1 per run** — pre-flight checklist in `docs/testing-mainnet.md`.
- **Scheduler ticks are non-blocking.** APScheduler's `BackgroundScheduler` runs ticks in a threadpool; HTTP requests (REST + MCP) are never delayed by in-flight ticks. Task 2.4 enforces this with an integration test that parallel-hits `/status` while a 3-second tick runs.
- **Every tick is observable via structured logs.** `strategy.tick.started`, `strategy.tick.completed`, `strategy.tick.errored`, and `scheduler.job.fired` events let a user tailing logs (or chatting with the agent) see ticks fire in real time without having to poll an endpoint.
