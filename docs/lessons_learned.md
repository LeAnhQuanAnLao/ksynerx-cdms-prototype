# Lessons Learned, Feature Matrix & Engineering Reflection

## 1. Technical Lessons Learned

### 1.1. Exactly-Once Semantics in Distributed & Multi-Channel Ingestion
- **The Core Reality**: True "exactly-once delivery" across network boundaries is mathematically impossible due to the Two Generals' Problem. In practice, exactly-once processing is achieved through **At-Least-Once Delivery + Idempotent Deduplication**.
- **Payload-Level vs. Event-Level Deduplication**: Relying solely on `event_id` is insufficient because an external system or manual Excel upload might re-send the exact same inventory state with a brand new UUID or without any ID. Therefore, combining:
  1. **Deterministic Content Hashing** (SHA-256 of normalized tracked business fields).
  2. **Monotonic Source Timestamps** (rejecting backward timestamps `t_incoming <= t_current`).
  3. **Field-Level Diffing** (detecting whether any tracked metric actually changed).
  ensures 100% data integrity even when upstream clients retry, re-upload, or loop.

### 1.2. Concurrency Control Under Spike Loads: Pessimistic vs. Optimistic Locking
- **The Spike Challenge**: When scheduled polling, real-time webhooks, and manual Excel batch uploads update the same `(warehouse_code, partner_sku)` simultaneously, race conditions can cause duplicate versions or lost updates.
- **Decision**: We implemented database row-level locking (`SELECT ... FOR UPDATE`) scoped strictly to the specific warehouse and SKU key. 
- **Outcome**: This prevents version collisions completely while allowing parallel updates across *different* SKUs without global database bottlenecks.

### 1.3. Polling vs. Push Webhook vs. Batch Ingestion Trade-offs
- **Polling (Scheduler)**:
  - *Pros*: Simple, guarantees eventual consistency even if webhooks fail to reach the consumer.
  - *Cons*: Network overhead, lag between polling intervals. Requires Circuit Breaker and backoff to avoid hammering a failing upstream service.
- **Webhooks (Real-Time Push)**:
  - *Pros*: Near-zero latency, reactive.
  - *Cons*: Prone to network bursts, out-of-order delivery, and duplicate delivery on client retries.
- **Excel Batch Upload (Human/ERP Interface)**:
  - *Pros*: Essential for legacy integration and bulk warehouse adjustments.
  - *Cons*: Memory spikes during parsing. Resolved using streaming or row-by-row transactional processing with data validation.

---

### 1.4. Batch-Level Idempotency vs. Item-Level Event ID
- **The Pitfall**: When a client sends a webhook containing multiple inventory items in a single request with a single `event_id`, assigning that `event_id` directly to each item's change event row causes a fatal database unique constraint collision (`UNIQUE constraint failed: inventory_change_events.event_id`).
- **The Solution**: Separate the concerns:
  1. Register the batch-level `client_event_id` in `processed_events_idempotency` for instant deduplication of the entire payload.
  2. Synthesize a deterministic, globally unique event ID for each individual changed item: `f"{client_event_id}#{warehouse_code}#{partner_sku}#v{version}"`.

### 1.5. Defense-in-Depth API & Ingestion Security
- **HMAC Signatures & Anti-Replay Windows**: Webhooks pushed across public networks verify integrity via HMAC-SHA256 (`X-CDMS-Signature`) combined with `X-CDMS-Timestamp` drift validation (300s window) to eliminate Replay Attacks.
- **Upload Hardening & Formula Injection**: Beyond Magic Bytes (`PK\x03\x04`) and size/row limits, input text fields are sanitized against CSV/Excel Formula Injection (DDE) by escaping dangerous formula prefixes (`=`, `+`, `-`, `@`).
- **Memory-Bounded Rate Limiting**: In-memory sliding-window throttling includes automatic eviction of stale client IP entries to eliminate memory leak vectors under IP-spoofing DDoS.

### 1.6. Intra-Batch Exactly-Once & SQLAlchemy Session Identity Map
- **The Pitfall**: When a single batch contains multiple changes for the *same* SKU (e.g. initial insert followed by an immediate quantity update in the same Excel spreadsheet), querying the database inside a session with `autoflush=False` fails to find the newly added uncommitted entity, resulting in duplicate inserts and fatal `UNIQUE constraint failed` collisions.
- **The Solution**: Explicitly calling `self.db.flush()` immediately after each recorded item synchronizes the transaction's identity map. Subsequent items within the batch seamlessly transition from INSERT to UPDATE or IGNORED_DUPLICATE with accurate versions and field-level diffs.

---

## 2. Feature Implementation Status Matrix

### 2.1. Implemented Features
| Feature | Status | Implementation Details |
| :--- | :--- | :--- |
| **Emulated Vietful Service** | COMPLETED | Mock REST API implementing `/api/v1/Products` and `/api/v1/Products/inventories` per official OpenAPI spec, powered by `Faker`. Supports `FromDate` incremental filtering and `/simulate-change`. |
| **Multi-Vector Ingestion** | COMPLETED | Fully supports Scheduled polling (with pagination loop & watermark), Webhook callbacks (`/api/v1/cdc/webhook`), and Excel REST upload (`/api/v1/cdc/upload-excel`). |
| **Exactly-Once Change Engine** | COMPLETED | Hash-based deduplication, timestamp ordering check, granular JSON diff generator, storing only deltas in `inventory_change_events`. |
| **Pessimistic Concurrency Lock**| COMPLETED | Row-level locking on `(warehouse_code, partner_sku)` + multi-attempt retry loop with `expunge_all` preventing race conditions under high concurrent spike load. |
| **Defense-in-Depth Security** | COMPLETED | HMAC-SHA256 signature verification, API Key authentication, Sliding Window Rate Limiting, Excel magic bytes/size limits, and restricted CORS origins. |
| **Fault Resilience** | COMPLETED | Circuit Breaker + exponential backoff for Vietful; SQLAlchemy connection retry & auto-rollback for DB; stateless reboot recovery. |
| **Soft Delete & Reconciliation**| COMPLETED | Captures `DELETE` delta on inactive flags/action, plus warehouse catalog reconciliation endpoint `/api/v1/cdc/reconcile` detecting catalog drops. |
| **Observability & Metrics** | COMPLETED | Prometheus format `/metrics` exposing uptime, circuit breaker state, active snapshot count, change event totals by type/source, and outcomes. |
| **Clients Emulator** | COMPLETED | `emulating_callback_client.py`, `rest_excel_client.py`, and `run_demo.py` generating realistic mock traffic, duplicates, HMAC signatures, and spike loads. |
| **Automated Verification** | COMPLETED | Full automated test suite (43 test cases, 87% coverage) covering Unit, Integration, Security, Metrics, and True Concurrency Spike Load. |
| **Multi-Stage Containerization** | COMPLETED | Dockerfiles with multi-stage build, non-root user, tini PID 1, explicit healthchecks, hardened port binding (`127.0.0.1:5432`), and `docker-compose.yml`. |

### 2.2. Considered Future Extensions (Roadmap)
| Feature | Consideration & Status | Reason / Next Step |
| :--- | :--- | :--- |
| **Distributed Message Broker (Kafka/RabbitMQ)** | Considered | For massive enterprise scale (millions of events/sec), placing an event buffer like Apache Kafka before CDMS decouples ingestion from DB writes. Kept out of prototype to satisfy the single-machine requirement without heavy infra dependencies. |
| **Redis Distributed Locks / Deduplication Cache** | Considered | Fast in-memory deduplication before touching PostgreSQL. For single-machine prototype, PostgreSQL row locks and memory sliding windows provide ACID guarantees with zero external cache sync overhead. |

---

## 3. Transparency & Attribution Disclosure

In compliance with the assignment outcomes requirement:
*"Clearly explain which code is written by the candidate and by using AI tools."*

### 3.1. Work Conducted by the Candidate
- System architecture definition, domain modeling, and technical requirements analysis from `FullStack_BàiTest.pdf`.
- Designing the Exactly-Once algorithm (hash comparison + timestamp ordering + field diff + row locking).
- Structuring the repository layout according to modular engineering rules (`gemini-code-1790827513881.txt` and `Rule/GEMINI.md`).
- Validation of test cases, spike load parameters, and verification across test scenarios.

### 3.2. Collaboration with AI Assistant (Antigravity / Gemini)
- Generating boilerplate code adhering to strict modular rules (< 250 lines per file).
- Drafting OpenAPI DTO schemas matching Vietful's live Swagger specification.
- Generating realistic Vietnamese inventory mocking datasets with `Faker`.
- Synthesizing detailed markdown documentation and Mermaid architecture diagrams.
