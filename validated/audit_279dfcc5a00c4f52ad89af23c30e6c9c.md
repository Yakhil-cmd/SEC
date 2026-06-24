Audit Report

## Title
Unbounded caller-controlled `limit` in `search_transactions` enables unauthenticated DoS via CPU/memory exhaustion — (`rs/rosetta-api/icrc1/src/data_api/services.rs`)

## Summary
The `/search/transactions` endpoint accepts a caller-supplied `limit` that is used directly as a SQLite `LIMIT` clause with no upper-bound enforcement. `MAX_TRANSACTIONS_PER_SEARCH_TRANSACTIONS_REQUEST` (10,000) is applied only as the default when `limit` is omitted, never as a cap on caller-supplied values. Any unauthenticated HTTP client can force the service to fetch, CBOR-deserialize, and in-memory-sort an arbitrarily large number of blocks in a single request, exhausting CPU and heap.

## Finding Description
`MAX_TRANSACTIONS_PER_SEARCH_TRANSACTIONS_REQUEST = 10_000` is declared as a constant: [1](#0-0) 

In `services::search_transactions`, the limit is resolved as: [2](#0-1) 

The `unwrap_or` supplies the constant only when the caller omits `limit`. When the caller provides any value — including `i64::MAX` — it passes through unchecked. There is no `.min(MAX_TRANSACTIONS_PER_SEARCH_TRANSACTIONS_REQUEST as i64)` call anywhere in the function. The resolved `limit` is forwarded verbatim to SQLite: [3](#0-2) 

Every returned row is CBOR-deserialized into a `Vec<BlockTransaction>`, then the entire vector is sorted in memory: [4](#0-3) 

The router registers `/search/transactions` with no authentication, rate-limiting, concurrency cap, or request timeout middleware — only metrics and tracing layers are applied: [5](#0-4) 

## Impact Explanation
This is a **High** severity finding matching the allowed impact: *"Application/platform-level DoS, crash, or subnet availability impact not based on raw volumetric DDoS."* The ICRC-1 Rosetta API is explicitly in-scope under financial integrations. A single Rosetta instance is the sole integration point for exchanges using ICRC-1 Rosetta. Crashing or saturating it disrupts all Rosetta API calls — block queries, construction endpoints, balance lookups — for all clients of that instance. Memory exhaustion can OOM-kill the process; CPU saturation blocks the Tokio async executor, timing out all concurrent requests.

## Likelihood Explanation
The endpoint is public HTTP with no authentication. Any client knowing a high-volume account (publicly observable on-chain) can trigger this with a single well-formed JSON POST. No special privileges, no victim interaction, and no rate limiting or concurrency cap is present in the router. The attack is trivially repeatable and can be parallelized with a handful of concurrent requests to amplify impact.

## Recommendation
Replace the bare `unwrap_or` with an explicit clamp so the constant acts as both default and ceiling:

```rust
let limit: u64 = request
    .limit
    .unwrap_or(MAX_TRANSACTIONS_PER_SEARCH_TRANSACTIONS_REQUEST as i64)
    .min(MAX_TRANSACTIONS_PER_SEARCH_TRANSACTIONS_REQUEST as i64)  // enforce cap
    .try_into()
    .map_err(|err| {
        Error::request_processing_error(&format!("Limit has to be a valid u64: {err}"))
    })?;
```

Additionally, consider adding `tower::limit::ConcurrencyLimitLayer` or a token-bucket middleware at the Axum router level, and a per-request timeout to cancel runaway SQLite scans.

## Proof of Concept
Send the following request (repeat 4–8 times concurrently against a Rosetta instance with a high-volume ledger account):

```
POST /search/transactions HTTP/1.1
Content-Type: application/json

{
  "network_identifier": {"blockchain":"Internet Computer","network":"<ledger_id>"},
  "account_identifier": {"address":"<high-volume-account>"},
  "limit": 2147483647
}
```

SQLite will scan and return every matching row up to `i64::MAX`; the process will deserialize and sort all of them in memory. Observe CPU pinned at 100% and heap growing until OOM or all other API calls time out.

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
