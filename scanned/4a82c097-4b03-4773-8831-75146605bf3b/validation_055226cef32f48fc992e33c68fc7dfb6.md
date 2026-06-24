### Title
Unbounded `limit` in `search_transactions` enables unauthenticated CPU/memory exhaustion — (`rs/rosetta-api/icrc1/src/data_api/services.rs`)

---

### Summary

The `search/transactions` endpoint accepts a caller-controlled `limit` that is passed directly to a SQLite `LIMIT` clause with no upper-bound enforcement. `MAX_TRANSACTIONS_PER_SEARCH_TRANSACTIONS_REQUEST` (10 000) is used only as the **default** when the caller omits `limit`; it is never applied as a cap on caller-supplied values. Any unauthenticated HTTP client can therefore force the service to fetch, CBOR-deserialize, and in-memory-sort an arbitrarily large number of blocks in a single request, and can repeat this indefinitely.

---

### Finding Description

`MAX_TRANSACTIONS_PER_SEARCH_TRANSACTIONS_REQUEST = 10_000` is declared in `constants.rs`: [1](#0-0) 

In `services::search_transactions` the limit is resolved as:

```rust
let limit: u64 = request
    .limit
    .unwrap_or(MAX_TRANSACTIONS_PER_SEARCH_TRANSACTIONS_REQUEST as i64)
    .try_into()...;
``` [2](#0-1) 

There is no `min(limit, MAX_TRANSACTIONS_PER_SEARCH_TRANSACTIONS_REQUEST)` call. The resolved `limit` is forwarded verbatim to SQLite:

```rust
command.push_str("LIMIT :limit ");
parameters.push((":limit".to_string(), rusqlite::types::Value::Integer(limit as i64)));
``` [3](#0-2) 

Every returned row is CBOR-deserialized and pushed into a `Vec`, then the entire vector is sorted: [4](#0-3) 

The endpoint is registered with no authentication or rate-limiting middleware: [5](#0-4) 

An attacker can POST `{"limit": 9223372036854775807, "account_identifier": "<high-volume account>"}` to `/search/transactions`. SQLite will scan and return every matching row; the process will deserialize and sort all of them in memory. Because the Rosetta process is single-instance and the Tokio runtime shares threads, a handful of concurrent such requests can saturate CPU and exhaust heap.

Even at the "intended" ceiling of 10 000, the constant is not enforced, so the attacker is not constrained to it.

---

### Impact Explanation

- **Availability**: Repeated large requests block the Tokio async executor, delaying or timing out all other Rosetta API calls (block queries, construction endpoints, etc.).
- **Memory**: Each `RosettaBlock` holds a deserialized CBOR blob; 10 000–millions of them held simultaneously can OOM the process.
- **Scope**: Constrained to a single Rosetta sidecar instance (Medium), but that instance is the sole integration point for exchanges using ICRC-1 Rosetta.

---

### Likelihood Explanation

- The endpoint is public HTTP with no authentication.
- Any client that knows a high-volume account (publicly observable on-chain) can trigger this.
- No rate limiting, no concurrency cap, no request timeout is visible in the router setup.
- The exploit requires only a single well-formed JSON POST.

---

### Recommendation

1. **Enforce the cap**: Replace the `unwrap_or` default with an explicit clamp:
   ```rust
   let limit: u64 = request
       .limit
       .unwrap_or(MAX_TRANSACTIONS_PER_SEARCH_TRANSACTIONS_REQUEST as i64)
       .min(MAX_TRANSACTIONS_PER_SEARCH_TRANSACTIONS_REQUEST as i64)  // add this
       .try_into()...;
   ```
2. **Add rate limiting** at the Axum router level (e.g., `tower::limit::ConcurrencyLimitLayer` or a token-bucket middleware).
3. **Add a request timeout** so runaway SQLite scans are cancelled.

---

### Proof of Concept

```
POST /search/transactions HTTP/1.1
Content-Type: application/json

{
  "network_identifier": {"blockchain":"Internet Computer","network":"<ledger_id>"},
  "account_identifier": {"address":"<high-volume-account>"},
  "limit": 2147483647
}
```

Repeat 4–8 times concurrently. Observe Rosetta process CPU pinned at 100 % and heap growing until OOM or all other API calls time out.

### Citations

**File:** rs/rosetta-api/icrc1/src/common/constants.rs (L18-18)
```rust
pub const MAX_TRANSACTIONS_PER_SEARCH_TRANSACTIONS_REQUEST: u64 = 10000;
```

**File:** rs/rosetta-api/icrc1/src/data_api/services.rs (L385-391)
```rust
    let limit: u64 = request
        .limit
        .unwrap_or(MAX_TRANSACTIONS_PER_SEARCH_TRANSACTIONS_REQUEST as i64)
        .try_into()
        .map_err(|err| {
            Error::request_processing_error(&format!("Limit has to be a valid u64: {err}"))
        })?;
```

**File:** rs/rosetta-api/icrc1/src/data_api/services.rs (L487-491)
```rust
    command.push_str("LIMIT :limit ");
    parameters.push((
        ":limit".to_string(),
        rusqlite::types::Value::Integer(limit as i64),
    ));
```

**File:** rs/rosetta-api/icrc1/src/data_api/services.rs (L499-517)
```rust
    for rosetta_block in rosetta_blocks.iter_mut() {
        transactions.push(BlockTransaction {
            block_identifier: rosetta_block.clone().get_block_identifier(),
            transaction: icrc1_rosetta_block_to_rosetta_core_transaction(
                rosetta_block.clone(),
                currency.clone(),
            )
            .map_err(|err| Error::parsing_unsuccessful(&err))?,
        })
    }

    transactions.iter_mut().for_each(|tx| {
        tx.transaction.operations.iter_mut().for_each(|op| {
            op.status = Some(STATUS_COMPLETED.to_string());
        })
    });

    // Sort the transactions by block index in descending order
    transactions.sort_by(|a, b| b.block_identifier.index.cmp(&a.block_identifier.index));
```

**File:** rs/rosetta-api/icrc1/src/main.rs (L361-391)
```rust
    let app = Router::new()
        .route("/ready", get(ready))
        .route("/health", get(health))
        .route("/call", post(call))
        .route("/network/list", post(network_list))
        .route("/network/options", post(network_options))
        .route("/network/status", post(network_status))
        .route("/block", post(block))
        .route("/account/balance", post(account_balance))
        .route("/block/transaction", post(block_transaction))
        .route("/search/transactions", post(search_transactions))
        .route("/mempool", post(mempool))
        .route("/mempool/transaction", post(mempool_transaction))
        .route("/construction/derive", post(construction_derive))
        .route("/construction/preprocess", post(construction_preprocess))
        .route("/construction/metadata", post(construction_metadata))
        .route("/construction/combine", post(construction_combine))
        .route("/construction/submit", post(construction_submit))
        .route("/construction/hash", post(construction_hash))
        .route("/construction/payloads", post(construction_payloads))
        .route("/construction/parse", post(construction_parse))
        // Apply the metrics middleware
        .layer(metrics_layer)
        // This layer creates a span for each http request and attaches
        // the request_id, HTTP Method and path to it.
        .layer(add_request_span())
        // This layer creates a new id for each request and puts it into the
        // request extensions. Note that it should be added after the
        // Trace layer.
        .layer(RequestIdLayer)
        .with_state(token_app_states.clone());
```
