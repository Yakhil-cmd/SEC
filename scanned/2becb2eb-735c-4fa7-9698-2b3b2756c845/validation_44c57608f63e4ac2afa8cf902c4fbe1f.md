Let me look at the key code paths more carefully to trace the exact behavior for coinbase inputs.

Based on my full read of `rs/bitcoin/checker/src/main.rs`, `rs/bitcoin/checker/src/fetch.rs`, and `rs/bitcoin/checker/src/state.rs`, here is the complete analysis:

---

### Title
Missing Coinbase Input Guard Causes Permanent `TransientInternalError` for Coinbase UTXOs — (`rs/bitcoin/checker/src/fetch.rs`, `rs/bitcoin/checker/src/state.rs`)

### Summary

The Bitcoin checker canister has no special handling for coinbase inputs (`previous_output.txid = [0;32]`, `vout = 0xFFFFFFFF`). When a coinbase transaction is submitted for checking, the checker attempts to fetch the non-existent "input transaction" with `txid=[0;32]` via HTTP outcall. Every provider returns a non-200 response (404), which is classified as a transient error and stored as `FetchTxStatus::Error`. On every subsequent retry the checker cycles to the next provider, which also returns 404. The check never resolves to `Passed` or `Failed`, permanently blocking ckBTC minting for any UTXO created directly by a coinbase transaction deposited to the minter's address.

### Finding Description

**Step 1 — No coinbase guard in input extraction.**

`TransactionCheckData::from_transaction` blindly maps every `TxIn` to a `PreviousOutput`, including the coinbase sentinel: [1](#0-0) 

There is no `if input.previous_output == OutPoint::null() { continue }` guard.

**Step 2 — `check_fetched` tries to fetch `txid=[0;32]`.**

For every input whose address is still `None`, `check_fetched` calls `try_fetch_tx(input.txid)`. For a coinbase transaction that input is `[0;32]`: [2](#0-1) 

**Step 3 — HTTP provider returns non-200; classified as transient.**

In `http_get_tx`, any non-200 status becomes `HttpGetTxError::Rejected { code: 2 /*SYS_TRANSIENT*/, … }`: [3](#0-2) 

**Step 4 — Error stored in cache; `into_response` returns `TransientInternalError`.**

`fetch_tx` stores `FetchTxStatus::Error` for `txid=[0;32]`, and `into_response` maps `Rejected` to `CheckTransactionRetriable::TransientInternalError`: [4](#0-3) 

**Step 5 — Retry logic cycles providers indefinitely.**

On every subsequent call, `try_fetch_tx` sees `FetchTxStatus::Error` and advances to the next provider — but all providers return 404 for `txid=[0;32]`: [5](#0-4) 

The state for `txid=[0;32]` oscillates through `FetchTxStatus::Error(provider=P1) → P2 → … → P1 → …` forever. The original coinbase txid remains `Fetched` but its single input address is never resolved, so `check_for_blocked_input_addresses` always returns `MissingInputAddresses`, and the outer call always re-enters the fetch loop. [6](#0-5) 

### Impact Explanation

Any coinbase transaction output deposited directly to the ckBTC minter's Bitcoin address can never be minted as ckBTC. The checker returns `TransientInternalError` (→ minter surfaces `TemporarilyUnavailable`) on every call, indefinitely. The deposit is permanently frozen without any irrecoverable error that would allow the minter to quarantine or reject it.

### Likelihood Explanation

Requires a miner to direct coinbase output(s) to the ckBTC minter's deposit address — unusual but entirely valid on-chain behavior requiring no privileged access. After Bitcoin's 100-block coinbase maturity, the UTXO becomes spendable and the minter's UTXO scanner will surface it, triggering the check. No admin key, governance vote, or threshold corruption is needed.

### Recommendation

In `TransactionCheckData::from_transaction` (or at the top of `check_fetched`), detect and skip coinbase inputs:

```rust
// Bitcoin coinbase sentinel: OutPoint::null()
if input.previous_output.txid == bitcoin::Txid::all_zeros()
    && input.previous_output.vout == 0xFFFF_FFFF
{
    // Coinbase input has no spendable predecessor; treat address as resolved (None / skip).
    continue;
}
```

Alternatively, use `tx.is_coinbase()` before building `TransactionCheckData` and short-circuit to `Passed` (coinbase outputs have no taint history to check).

### Proof of Concept

1. Configure a mock `FetchEnv` whose `http_get_tx` returns `HttpGetTxError::Rejected { code: 2, message: "404".into() }` for `txid=[0;32]` and returns a valid coinbase transaction for any other txid.
2. Call `check_transaction_inputs(coinbase_txid)`.
3. Observe `CheckTransactionRetriable::TransientInternalError` on the first call.
4. Call again up to `MAX_CHECK_TRANSACTION_RETRY` times.
5. Assert the response is never `Passed` or `Failed` — it is always `TransientInternalError` or `Pending`, confirming the invariant is permanently violated.

### Citations

**File:** rs/bitcoin/checker/src/state.rs (L92-99)
```rust
        let inputs = tx
            .input
            .iter()
            .map(|input| PreviousOutput {
                txid: Txid::from(*(input.previous_output.txid.as_ref() as &[u8; 32])),
                vout: input.previous_output.vout,
            })
            .collect();
```

**File:** rs/bitcoin/checker/src/fetch.rs (L22-33)
```rust
    pub(crate) fn into_response(self, txid: Txid) -> CheckTransactionResponse {
        let txid = txid.as_ref().to_vec();
        match self {
            HttpGetTxError::Rejected { message, .. } => {
                CheckTransactionRetriable::TransientInternalError(message).into()
            }
            HttpGetTxError::ResponseTooLarge => {
                (CheckTransactionIrrecoverableError::ResponseTooLarge { txid }).into()
            }
            _ => CheckTransactionRetriable::TransientInternalError(self.to_string()).into(),
        }
    }
```

**File:** rs/bitcoin/checker/src/fetch.rs (L85-91)
```rust
            Some(FetchTxStatus::Error(err)) => (
                // An FetchTxStatus error can be retried with another provider
                err.provider.next(),
                // The next provider can use the same max_response_bytes
                err.max_response_bytes,
            ),
            Some(FetchTxStatus::Fetched(fetched)) => return TryFetchResult::Fetched(fetched),
```

**File:** rs/bitcoin/checker/src/fetch.rs (L196-203)
```rust
        for (index, input) in fetched.tx.inputs.iter().enumerate() {
            if fetched.input_addresses[index].is_none() {
                use TryFetchResult::*;
                match self.try_fetch_tx(input.txid) {
                    ToFetch(do_fetch) => {
                        jobs.push((index, input.txid, input.vout));
                        futures.push(do_fetch)
                    }
```

**File:** rs/bitcoin/checker/src/fetch.rs (L306-309)
```rust
pub fn check_for_blocked_input_addresses(fetched: &FetchedTx) -> Result<(), CheckTxInputsError> {
    if fetched.input_addresses.iter().any(|x| x.is_none()) {
        return Err(CheckTxInputsError::MissingInputAddresses);
    }
```

**File:** rs/bitcoin/checker/src/main.rs (L426-432)
```rust
                if response.status != 200_u32 {
                    // All non-200 status are treated as transient errors
                    return Err(HttpGetTxError::Rejected {
                        code: 2, //SYS_TRANSIENT
                        message: format!("HTTP call {} received code {}", url, response.status),
                    });
                }
```
