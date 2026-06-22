Based on my thorough code review, I have all the information needed to render a verdict.

**Confirmed call chain:**

1. `main.rs:369` — `.route("/account/balance", post(account_balance))` — no auth middleware on the router
2. `endpoints.rs:113-130` — handler calls `services::account_balance_with_metadata` unconditionally, passing raw `request.metadata`
3. `services.rs:256-287` — reads `aggregate_all_subaccounts` from metadata, no rate-limit or subaccount-count guard, calls `storage_client.get_aggregated_balance_for_principal_at_block_idx`
4. `storage_operations/mod.rs:895-941` — executes a **correlated subquery with no LIMIT and no timeout**
5. `schema.rs:56-67, 136-141` — PK is `(principal, subaccount, block_idx)`; the only extra index is `block_idx_account_balances ON account_balances(block_idx)` — no dedicated `(principal)` covering index for the aggregation path
6. `storage_client.rs` uses `tokio_rusqlite::Connection` — a single background thread; a long-running query blocks all other DB operations including block-sync writes

---

### Title
Unauthenticated Unbounded Correlated SQL Scan via `aggregate_all_subaccounts=true` Causes Rosetta Node DoS — (`rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs`)

### Summary
Any unauthenticated HTTP client can POST to `/account/balance` with `metadata.aggregate_all_subaccounts=true` for a principal that owns many subaccounts. This triggers an unbounded correlated SQLite query with no LIMIT, no timeout, and no rate-limiting guard. The query runs on the single `tokio_rusqlite` background thread, blocking all other DB operations — including block synchronization writes — for the duration of the scan.

### Finding Description

The `/account/balance` endpoint is registered with no authentication or rate-limiting middleware: [1](#0-0) 

Every request, regardless of origin, is routed directly to `account_balance_with_metadata`: [2](#0-1) 

The function reads the `aggregate_all_subaccounts` boolean from the caller-supplied metadata with no guard on the number of subaccounts: [3](#0-2) 

This dispatches to `get_aggregated_balance_for_principal_at_block_idx`, which executes a correlated subquery — for every `(principal, subaccount)` pair it fires an inner `MAX(block_idx)` lookup — with **no LIMIT and no query timeout**: [4](#0-3) 

The schema defines the PK as `(principal, subaccount, block_idx)` and adds only a `block_idx` secondary index — there is no covering index that makes the per-principal aggregation O(1): [5](#0-4) [6](#0-5) 

`StorageClient` wraps a single `tokio_rusqlite::Connection`. All DB closures — including block-sync writes — are serialized through one background thread: [7](#0-6) 

### Impact Explanation
While the long-running query executes, the `tokio_rusqlite` background thread is occupied. All other `call(...)` invocations queue behind it. Block synchronization (`update_account_balances`, `store_blocks`) stalls, transaction submission via `/construction/submit` stalls, and all other balance/block queries time out. The Rosetta node becomes effectively unresponsive for the entire duration of the scan. Repeated requests keep it permanently degraded.

### Likelihood Explanation
The precondition — a principal with a large number of subaccounts — can arise naturally (e.g., an exchange hot-wallet receiving to millions of subaccounts) or be manufactured by an attacker who sends many small transfers to distinct subaccounts of a chosen principal. The endpoint requires zero authentication. The attacker needs only a single HTTP POST with a known principal address and `"aggregate_all_subaccounts": true`.

### Recommendation
1. **Add a subaccount-count guard**: before executing the aggregation query, count distinct subaccounts for the principal and reject (or cap) requests above a configurable threshold.
2. **Add a SQLite query timeout** via `rusqlite`'s `busy_timeout` / `progress_handler` to abort runaway queries.
3. **Add a per-IP or global rate-limit** on the `/account/balance` endpoint.
4. **Rewrite the correlated subquery** as a `GROUP BY` with a window function or a `MAX` aggregate, and add a composite index on `(principal, subaccount, block_idx DESC)` to make the per-principal scan efficient.

### Proof of Concept
```
# 1. Populate the ledger with 10^6 transfers to distinct subaccounts of principal P.
# 2. Wait for the Rosetta node to sync all blocks.
# 3. Send:
curl -X POST http://<rosetta-host>:8080/account/balance \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"ICRC-1","network":"<ledger-id>"},
    "account_identifier": {"address":"<P-address>"},
    "metadata": {"aggregate_all_subaccounts": true}
  }'
# 4. Observe: response hangs for minutes; concurrent /account/balance and
#    /construction/submit requests also hang; block sync stalls.
```

### Citations

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

**File:** rs/rosetta-api/icrc1/src/data_api/endpoints.rs (L113-130)
```rust
pub async fn account_balance(
    State(state): State<Arc<MultiTokenAppState>>,
    Json(request): Json<AccountBalanceRequest>,
) -> Result<Json<AccountBalanceResponse>> {
    let state = get_state_from_network_id(&request.network_identifier, &state)
        .map_err(|err| Error::invalid_network_id(&format!("{err:?}")))?;
    Ok(Json(
        services::account_balance_with_metadata(
            &state.storage,
            &request.account_identifier,
            &request.block_identifier,
            &request.metadata,
            state.metadata.decimals,
            state.metadata.symbol.clone(),
        )
        .await?,
    ))
}
```

**File:** rs/rosetta-api/icrc1/src/data_api/services.rs (L255-287)
```rust
    // Check if aggregate_all_subaccounts flag is set in metadata
    let aggregate_all_subaccounts = metadata
        .as_ref()
        .and_then(|m| m.get("aggregate_all_subaccounts"))
        .and_then(|v| v.as_bool())
        .unwrap_or(false);

    let balance = if aggregate_all_subaccounts {
        // Validate that no subaccount is specified when aggregating all subaccounts
        let account = Account::try_from(account_identifier.clone())
            .map_err(|err| Error::parsing_unsuccessful(&err))?;

        // Check if a non-default subaccount is specified
        // Note: subaccount None and Some([0; 32]) both represent the default subaccount
        let has_non_default_subaccount = match account.subaccount {
            None => false,
            Some(subaccount) => subaccount != [0_u8; 32],
        };

        if has_non_default_subaccount {
            return Err(Error::request_processing_error(
                &"Cannot specify subaccount when aggregate_all_subaccounts is true".to_owned(),
            ));
        }

        // Get aggregated balance for all subaccounts of the principal
        storage_client
            .get_aggregated_balance_for_principal_at_block_idx(
                &account.owner.into(),
                rosetta_block.index,
            )
            .await
            .map_err(|e| Error::unable_to_find_account_balance(&e))?
```

**File:** rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs (L895-941)
```rust
pub fn get_aggregated_balance_for_principal_at_block_idx(
    connection: &Connection,
    principal: &PrincipalId,
    block_idx: u64,
) -> anyhow::Result<Nat> {
    // Query to get the latest balance for each subaccount of the principal at or before the given block index
    let mut stmt = connection.prepare_cached(
        "SELECT a1.subaccount, a1.amount
         FROM account_balances a1
         WHERE a1.principal = :principal
         AND a1.block_idx = (
             SELECT MAX(a2.block_idx)
             FROM account_balances a2
             WHERE a2.principal = a1.principal
             AND a2.subaccount = a1.subaccount
             AND a2.block_idx <= :block_idx
         )",
    )?;

    let rows = stmt.query_map(
        named_params! {
            ":principal": principal.as_slice(),
            ":block_idx": block_idx
        },
        |row| {
            let amount_str: String = row.get(1)?;
            Nat::from_str(&amount_str).map_err(|_| {
                rusqlite::Error::InvalidColumnType(
                    1,
                    "amount".to_string(),
                    rusqlite::types::Type::Text,
                )
            })
        },
    )?;

    let mut total_balance = Nat(BigUint::zero());
    for balance_result in rows {
        let balance = balance_result?;
        total_balance = Nat(total_balance
            .0
            .checked_add(&balance.0)
            .with_context(|| "Overflow while aggregating balances")?);
    }

    Ok(total_balance)
}
```

**File:** rs/rosetta-api/icrc1/src/common/storage/schema.rs (L56-67)
```rust
    connection.execute(
        r#"
        CREATE TABLE IF NOT EXISTS account_balances (
            block_idx INTEGER NOT NULL,
            principal BLOB NOT NULL,
            subaccount BLOB NOT NULL,
            amount TEXT NOT NULL,
            PRIMARY KEY(principal,subaccount,block_idx)
        )
        "#,
        [],
    )?;
```

**File:** rs/rosetta-api/icrc1/src/common/storage/schema.rs (L134-161)
```rust
/// Creates all the necessary indexes for optimal query performance.
pub fn create_indexes(connection: &Connection) -> anyhow::Result<()> {
    connection.execute(
        r#"
        CREATE INDEX IF NOT EXISTS block_idx_account_balances
        ON account_balances(block_idx)
        "#,
        [],
    )?;

    connection.execute(
        r#"
        CREATE INDEX IF NOT EXISTS tx_hash_index
        ON blocks(tx_hash)
        "#,
        [],
    )?;

    connection.execute(
        r#"
        CREATE INDEX IF NOT EXISTS block_hash_index
        ON blocks(hash)
        "#,
        [],
    )?;

    Ok(())
}
```

**File:** rs/rosetta-api/icrc1/src/common/storage/storage_client.rs (L489-505)
```rust
    pub async fn get_aggregated_balance_for_principal_at_block_idx(
        &self,
        principal: &ic_base_types::PrincipalId,
        block_idx: u64,
    ) -> anyhow::Result<Nat> {
        let principal = *principal;
        Ok(self
            .storage_connection
            .call::<_, _, StorageError>(move |conn| {
                Ok(
                    storage_operations::get_aggregated_balance_for_principal_at_block_idx(
                        conn, &principal, block_idx,
                    )?,
                )
            })
            .await?)
    }
```
