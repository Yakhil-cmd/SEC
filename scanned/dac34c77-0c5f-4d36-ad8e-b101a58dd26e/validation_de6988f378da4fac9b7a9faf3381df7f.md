### Title
Stale Cached Fee Estimate in `eip_1559_transaction_price` Query Deviates from Actual Fee Charged in `withdraw_eth` - (`rs/ethereum/cketh/minter/src/main.rs`)

---

### Summary

The ckETH minter exposes `eip_1559_transaction_price` as a query endpoint that returns a **cached** gas fee estimate. Users and integrators call this to preview how much ETH they will receive from a `withdraw_eth` call. However, the actual fee deducted at transaction-creation time is computed by `lazy_refresh_gas_fee_estimate()`, which may fetch a **fresher, higher** estimate. When Ethereum gas prices rise between the query and the asynchronous transaction creation, users receive fewer ETH than the preview indicated — a direct analog to the ERC4626 `previewRedeem` / `redeem` deviation.

---

### Finding Description

**Query endpoint — `eip_1559_transaction_price`:**

The function reads directly from `s.last_transaction_price_estimate`, a cached field in minter state, and returns it without refreshing:

```rust
// rs/ethereum/cketh/minter/src/main.rs:190-197
match read_state(|s| s.last_transaction_price_estimate.clone()) {
    Some((ts, estimate)) => {
        let mut result = Eip1559TransactionPrice::from(estimate.to_price(gas_limit));
        result.timestamp = Some(ts);
        result
    }
    None => ic_cdk::trap("ERROR: last transaction price estimate is not available"),
}
``` [1](#0-0) 

The cache has a 60-second TTL enforced only in `lazy_refresh_gas_fee_estimate`, not in the query itself. The query can return an arbitrarily stale value if the cache was last refreshed just before the TTL window:

```rust
// rs/ethereum/cketh/minter/src/tx.rs:672-680
let now_ns = ic_cdk::api::time();
match read_state(|s| s.last_transaction_price_estimate.clone()) {
    Some((last_estimate_timestamp_ns, estimate))
        if now_ns < last_estimate_timestamp_ns.saturating_add(MAX_AGE_NS) =>
    {
        Some(estimate)
    }
    _ => do_refresh().await,
}
``` [2](#0-1) 

**Update path — actual fee at transaction creation:**

When `withdraw_eth` is called, the full `withdrawal_amount` is burned immediately. The actual ETH amount sent to the recipient is computed **later**, asynchronously, in `create_transactions_batch`, using a **fresh** call to `lazy_refresh_gas_fee_estimate()`:

```rust
// rs/ethereum/cketh/minter/src/withdraw.rs:249-264
fn create_transactions_batch(gas_fee_estimate: GasFeeEstimate) {
    for request in read_state(|s| {
        s.eth_transactions
            .withdrawal_requests_batch(WITHDRAWAL_REQUESTS_BATCH_SIZE)
    }) {
        ...
        match create_transaction(&request, nonce, gas_fee_estimate.clone(), gas_limit, ...) {
``` [3](#0-2) 

The transaction amount is `withdrawal_amount - max_transaction_fee` where `max_transaction_fee` uses the **fresh** gas fee estimate, not the cached one returned by the query:

```rust
// rs/ethereum/cketh/minter/src/state/transactions/mod.rs:1123-1134
let transaction_price = gas_fee_estimate.to_price(gas_limit);
let max_transaction_fee = transaction_price.max_transaction_fee();
let tx_amount = match request.withdrawal_amount.checked_sub(max_transaction_fee) {
    Some(tx_amount) => tx_amount,
    None => {
        return Err(CreateTransactionError::InsufficientTransactionFee { ... });
    }
};
``` [4](#0-3) 

The same pattern applies to `estimate_withdrawal_fee` in the ckBTC minter, which reads from the cached `s.last_median_fee_per_vbyte`:

```rust
// rs/bitcoin/ckbtc/minter/src/main.rs:223-233
match mutate_state(|s| {
    ...
    ic_ckbtc_minter::estimate_retrieve_btc_fee(
        &mut s.available_utxos,
        withdrawal_amount,
        s.last_median_fee_per_vbyte
            .expect("Bitcoin current fee percentiles not retrieved yet."),
        ...
    )
})
``` [5](#0-4) 

---

### Impact Explanation

A user or integrating canister calls `eip_1559_transaction_price` (query) to determine how much ETH they will receive from a `withdraw_eth(amount)` call. The query returns `max_transaction_fee = F_cached`. The user expects to receive `amount - F_cached` ETH. However, by the time the minter's timer fires and `create_transactions_batch` runs (which can be minutes later), the fresh gas fee estimate may be `F_fresh > F_cached`. The user then receives `amount - F_fresh` ETH — **fewer than the preview indicated**. The difference is not reimbursed (as documented: "Overcharged transaction fees are not reimbursed"). This constitutes a direct loss of funds relative to the previewed amount, affecting any user or canister that relies on `eip_1559_transaction_price` to make withdrawal decisions. [6](#0-5) 

---

### Likelihood Explanation

Ethereum gas prices fluctuate continuously. The minter processes withdrawals on a timer interval (`PROCESS_ETH_RETRIEVE_TRANSACTIONS_INTERVAL`), meaning there is always a non-trivial delay between when a user calls `withdraw_eth` and when the transaction is actually created. During periods of gas price spikes (e.g., NFT mints, DeFi events), the discrepancy between the cached query result and the actual fee can be substantial. Any external canister or frontend that uses `eip_1559_transaction_price` to compute expected output before calling `withdraw_eth` is affected. The 60-second cache TTL in `lazy_refresh_gas_fee_estimate` means the query can return data up to 60 seconds stale, and the transaction creation can happen minutes after the withdrawal request is queued. [7](#0-6) 

---

### Recommendation

1. **Document the deviation explicitly** in the `eip_1559_transaction_price` Candid interface and documentation: the returned `max_transaction_fee` is an upper-bound estimate based on cached data; the actual fee deducted may be higher if gas prices increase before transaction creation.

2. **Add a staleness warning** to the query response. The `timestamp` field already exists in `Eip1559TransactionPrice` — callers should be advised to reject estimates older than a threshold.

3. **Consider a conservative safety margin** in the estimate (e.g., multiply by 1.1×) so that `eip_1559_transaction_price` is guaranteed to be ≤ the actual fee charged, making the preview a lower bound on received ETH rather than an upper bound. [8](#0-7) 

---

### Proof of Concept

**Step 1**: User queries `eip_1559_transaction_price` at time T. Cache was last refreshed at T-55s. Returns `max_transaction_fee = 1_000_000_000_000_000` wei (1 milli-ETH). User expects to receive `withdrawal_amount - 1_000_000_000_000_000` ETH.

**Step 2**: User calls `withdraw_eth(withdrawal_amount, recipient)`. The full `withdrawal_amount` is burned from the ckETH ledger immediately.

**Step 3**: 5 seconds later, the cache expires. The minter's timer fires and calls `lazy_refresh_gas_fee_estimate()`, which fetches fresh fee history from Ethereum JSON-RPC providers. Gas prices have spiked; fresh estimate gives `max_transaction_fee = 2_000_000_000_000_000` wei.

**Step 4**: `create_transactions_batch` creates the transaction with `amount = withdrawal_amount - 2_000_000_000_000_000`. The user receives 1 milli-ETH less than the preview indicated. The difference is not reimbursed. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L190-197)
```rust
    match read_state(|s| s.last_transaction_price_estimate.clone()) {
        Some((ts, estimate)) => {
            let mut result = Eip1559TransactionPrice::from(estimate.to_price(gas_limit));
            result.timestamp = Some(ts);
            result
        }
        None => ic_cdk::trap("ERROR: last transaction price estimate is not available"),
    }
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L545-553)
```rust
async fn estimate_erc20_transaction_fee() -> Option<Wei> {
    lazy_refresh_gas_fee_estimate()
        .await
        .map(|gas_fee_estimate| {
            gas_fee_estimate
                .to_price(CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT)
                .max_transaction_fee()
        })
}
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L610-612)
```rust
pub async fn lazy_refresh_gas_fee_estimate() -> Option<GasFeeEstimate> {
    const MAX_AGE_NS: u64 = 60_000_000_000_u64; //60 seconds

```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L636-641)
```rust
        let gas_fee_estimate = match estimate_transaction_fee(&fee_history) {
            Ok(estimate) => {
                mutate_state(|s| {
                    s.last_transaction_price_estimate =
                        Some((ic_cdk::api::time(), estimate.clone()));
                });
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L672-680)
```rust
    let now_ns = ic_cdk::api::time();
    match read_state(|s| s.last_transaction_price_estimate.clone()) {
        Some((last_estimate_timestamp_ns, estimate))
            if now_ns < last_estimate_timestamp_ns.saturating_add(MAX_AGE_NS) =>
        {
            Some(estimate)
        }
        _ => do_refresh().await,
    }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L249-264)
```rust
fn create_transactions_batch(gas_fee_estimate: GasFeeEstimate) {
    for request in read_state(|s| {
        s.eth_transactions
            .withdrawal_requests_batch(WITHDRAWAL_REQUESTS_BATCH_SIZE)
    }) {
        log!(DEBUG, "[create_transactions_batch]: processing {request:?}",);
        let ethereum_network = read_state(State::ethereum_network);
        let nonce = read_state(|s| s.eth_transactions.next_transaction_nonce());
        let gas_limit = estimate_gas_limit(&request);
        match create_transaction(
            &request,
            nonce,
            gas_fee_estimate.clone(),
            gas_limit,
            ethereum_network,
        ) {
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1123-1134)
```rust
            let transaction_price = gas_fee_estimate.to_price(gas_limit);
            let max_transaction_fee = transaction_price.max_transaction_fee();
            let tx_amount = match request.withdrawal_amount.checked_sub(max_transaction_fee) {
                Some(tx_amount) => tx_amount,
                None => {
                    return Err(CreateTransactionError::InsufficientTransactionFee {
                        cketh_ledger_burn_index: request.ledger_burn_index,
                        allowed_max_transaction_fee: request.withdrawal_amount,
                        actual_max_transaction_fee: max_transaction_fee,
                    });
                }
            };
```

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L223-233)
```rust
    match mutate_state(|s| {
        let fee_estimator = IC_CANISTER_RUNTIME.fee_estimator(s);
        let withdrawal_amount = arg.amount.unwrap_or(s.fee_based_retrieve_btc_min_amount);
        ic_ckbtc_minter::estimate_retrieve_btc_fee(
            &mut s.available_utxos,
            withdrawal_amount,
            s.last_median_fee_per_vbyte
                .expect("Bitcoin current fee percentiles not retrieved yet."),
            s.max_num_inputs_in_transaction,
            &fee_estimator,
        )
```

**File:** rs/ethereum/cketh/docs/cketh.adoc (L200-214)
```text
Note that the transaction will be made at the cost of the beneficiary meaning that the resulting received amount will be less than the specified withdrawal amount.
The exact fee deducted depends on the dynamic Ethereum transaction fees used at the time the transaction was created.

In more detail, assume that a user calls `withdraw_eth` (after having approved the minter) to withdraw `withdraw_amount` (e.g. 1ckETH) to some address.
Then the minter is going to do the following

. Burn `withdraw_amount` on the ckETH ledger for the IC principal (the caller of `withdraw_eth`).
. Estimate the maximum current cost of a transaction on Ethereum, say `max_tx_fee_estimate`. This `max_tx_fee_estimate` is expected to be large enough to be valid for the few next blocks.
. Issue an Ethereum transaction (via threshold ECDSA) with the value `withdraw_amount - max_tx_fee_estimate`. This requires of course that `withdraw_amount >= max_tx_fee_estimate` and that's why we currently have a conservative minimum value for withdrawals of `30_000_000_000_000_000` wei. This ensures that the minter can always send the transaction to Ethereum if one or several resubmissions are needed if the Ethereum network is congested and fees are increasing rapidly (each resubmission requires an increase of at least 10% of the transaction fee).
. When the transaction is mined, the destination of the transaction will receive `withdraw_amount - max_tx_fee_estimate`. Since on Ethereum transactions are paid by the sender, the minter’s account will be charged with
+
----
(withdraw_amount - max_tx_fee_estimate) + actual_tx_fee == withdrawal_amount - (max_tx_fee_estimate - actual_tx_fee),
----
where `actual_tx_fee` represents the actual transaction fee (can be retrieved from the transaction receipt) and by construction `max_tx_fee_estimate - actual_tx_fee > 0`.
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L158-180)
```text
// Estimate price of an EIP-1559 transaction
// when converting ckETH to ETH or ckERC20 to ERC20, see
// https://eips.ethereum.org/EIPS/eip-1559
type Eip1559TransactionPrice = record {
    // Maximum amount of gas transaction is authorized to consume.
    gas_limit : nat;

    // Maximum amount of Wei per gas unit that the transaction is willing to pay in total.
    // This covers the base fee determined by the network and the `max_priority_fee_per_gas`.
    max_fee_per_gas : nat;

    // Maximum amount of Wei per gas unit that the transaction gives to miners
    // to incentivize them to include their transaction (priority fee).
    max_priority_fee_per_gas : nat;

    // Maximum amount of Wei that can be charged for the transaction,
    // computed as `max_fee_per_gas * gas_limit`
    max_transaction_fee : nat;

    // Timestamp of when the price was estimated.
    // Nanoseconds since the UNIX epoch.
    timestamp : opt nat64;
};
```
