### Title
IC HTTPS Outcall Response Size Limit Causes Permanent ckBTC Deposit Denial for Large Bitcoin Transactions - (File: `rs/bitcoin/checker/lib/lib.rs`)

### Summary
The IC's HTTPS outcall response size limit (hard-capped at 400 kB in the btc_checker canister, with a 2 MiB absolute subnet ceiling) imposes a fundamental, unrecoverable limitation on Bitcoin transaction verification. When a depositor sends BTC via a transaction exceeding 400 kB (e.g., a large Taproot transaction), the btc_checker canister returns `CheckTransactionIrrecoverableError::ResponseTooLarge`. The ckBTC minter's `check_utxo` function propagates this as `UpdateBalanceError::GenericError` without suspending or quarantining the UTXO. Because the UTXO is never moved to a suspended state, every subsequent `update_balance` call re-encounters the same UTXO and fails identically, permanently blocking ckBTC minting for that deposit with no recovery path short of a canister upgrade.

### Finding Description
The btc_checker canister fetches Bitcoin transactions via HTTPS outcalls to verify them for the ckBTC minter. The fetch logic in `rs/bitcoin/checker/src/fetch.rs` first attempts a 4 kB buffer, then retries with a 400 kB buffer (`RETRY_MAX_RESPONSE_BYTES`). If the transaction still exceeds 400 kB, the status is set to `FetchTxStatus::Error` and `HttpGetTxError::ResponseTooLarge` is returned, which is converted to `CheckTransactionIrrecoverableError::ResponseTooLarge`.

The library comment in `rs/bitcoin/checker/lib/lib.rs` explicitly acknowledges this limitation:

> "Taproot transactions could be as big as full block size (4MiB). Currently a subnet's maximum response size is only 2MiB. Transactions bigger than 2MiB are very rare, and we can't handle them."

The retry ceiling is 400 kB, not 2 MiB, so transactions between 400 kB and 2 MiB are also unhandled.

In `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`, the `check_utxo` function handles `CheckTransactionStatus::Error` by returning `Err(UpdateBalanceError::GenericError { error_code: KytError, ... })`. The caller `update_balance` propagates this error via `?` at line 302, without suspending or quarantining the UTXO. The `SuspendedReason` enum only has `ValueTooSmall` and `Quarantined` variants — there is no variant for "transaction too large to verify." The UTXO therefore remains in the "new UTXOs" pool indefinitely, causing every future `update_balance` call to re-attempt and re-fail with the same `GenericError`.

**Exact code path:**
1. User deposits BTC via a large transaction (> 400 kB).
2. User calls `update_balance` → `check_utxo` → `runtime.check_transaction(...)`.
3. btc_checker's `fetch_tx` tries 4 kB, then 400 kB buffer; both fail with `ResponseTooLarge`.
4. btc_checker returns `CheckTransactionIrrecoverableError::ResponseTooLarge`.
5. `check_utxo` returns `Err(UpdateBalanceError::GenericError { error_code: KytError })`.
6. `update_balance` propagates the error; UTXO is not suspended.
7. Every subsequent `update_balance` call repeats steps 2–6 indefinitely.

### Impact Explanation
A legitimate depositor who sends BTC via a large Bitcoin transaction (e.g., a Taproot transaction with many inputs/outputs exceeding 400 kB) permanently loses the ability to mint ckBTC for that deposit. The BTC is locked in the deposit address with no recovery path through the normal ckBTC flow. This is a direct analog to the tBTC issue: a depositor acting in good faith loses their deposit due to a protocol-level resource constraint, not any malicious action on their part.

### Likelihood Explanation
Taproot transactions can reach the full Bitcoin block size (4 MiB). Standard non-Taproot transactions can also approach 400 kB with many inputs. As Taproot adoption grows and consolidation transactions become larger, the probability of a depositor triggering this path increases. The trigger requires no special privileges — any user calling `update_balance` after depositing via a large transaction will hit this path.

### Recommendation
1. Add a `ResponseTooLarge` variant to `SuspendedReason` (or a dedicated `IrrecoverableError` reason) so that UTXOs whose transactions cannot be fetched due to size limits are suspended rather than causing permanent `GenericError` failures on every `update_balance` call.
2. Increase `RETRY_MAX_RESPONSE_BYTES` toward the 2 MiB subnet limit to handle transactions between 400 kB and 2 MiB.
3. Expose a UI/API warning to depositors when their transaction size may exceed the verifiable limit before they submit to the Bitcoin network.
4. Document and benchmark the exact transaction size thresholds that trigger `ResponseTooLarge` so depositors can be informed.

### Proof of Concept

**Step 1 — Root cause (size ceiling):** [1](#0-0) 

**Step 2 — Fetch logic: irrecoverable error on second `ResponseTooLarge`:** [2](#0-1) 

**Step 3 — `ResponseTooLarge` mapped to `CheckTransactionIrrecoverableError`:** [3](#0-2) 

**Step 4 — `check_utxo` returns `GenericError` without suspending the UTXO:** [4](#0-3) 

**Step 5 — `update_balance` propagates the error via `?`, UTXO never suspended:** [5](#0-4) 

**Step 6 — `SuspendedReason` has no variant for size-limit failures, so UTXO stays in the processable pool forever:** [6](#0-5)

### Citations

**File:** rs/bitcoin/checker/lib/lib.rs (L12-25)
```rust
// The max_response_bytes is initially set to 4kB, and then
// increased to 400kB if the initial size isn't enough.
// - The maximum size of a standard non-taproot transaction is 400k vBytes.
// - Taproot transactions could be as big as full block size (4MiB).
// - Currently a subnet's maximum response size is only 2MiB.
// - Transaction size between 400kB and 2MiB are also uncommon, we could
//   handle them in the future if required.
// - Transactions bigger than 2MiB are very rare, and we can't handle them.

/// Initial max response bytes is 4kB
pub const INITIAL_MAX_RESPONSE_BYTES: u32 = 4 * 1024;

/// Retry max response bytes is 400kB
pub const RETRY_MAX_RESPONSE_BYTES: u32 = 400 * 1024;
```

**File:** rs/bitcoin/checker/src/fetch.rs (L28-29)
```rust
            HttpGetTxError::ResponseTooLarge => {
                (CheckTransactionIrrecoverableError::ResponseTooLarge { txid }).into()
```

**File:** rs/bitcoin/checker/src/fetch.rs (L148-169)
```rust
            Err(HttpGetTxError::ResponseTooLarge)
                if max_response_bytes < RETRY_MAX_RESPONSE_BYTES =>
            {
                state::set_fetch_status(
                    txid,
                    FetchTxStatus::PendingRetry {
                        max_response_bytes: RETRY_MAX_RESPONSE_BYTES,
                    },
                );
                Ok(FetchResult::RetryWithBiggerBuffer)
            }
            Err(err) => {
                state::set_fetch_status(
                    txid,
                    FetchTxStatus::Error(FetchTxStatusError {
                        provider,
                        max_response_bytes,
                        error: err.clone(),
                    }),
                );
                Ok(FetchResult::Error(err))
            }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L302-302)
```rust
        let status = check_utxo(&utxo, &args, runtime).await?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L442-448)
```rust
            CheckTransactionResponse::Unknown(CheckTransactionStatus::Error(error)) => {
                log!(Priority::Debug, "Bitcoin checker error: {:?}", error);
                return Err(UpdateBalanceError::GenericError {
                    error_code: ErrorCode::KytError as u64,
                    error_message: format!("Bitcoin checker error: {error:?}"),
                });
            }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1911-1917)
```rust
#[derive(Clone, Copy, Eq, PartialEq, Debug, CandidType, Serialize, Deserialize)]
pub enum SuspendedReason {
    /// UTXO whose value is too small to pay the Bitcoin check fee.
    ValueTooSmall,
    /// UTXO that the Bitcoin checker considered tainted.
    Quarantined,
}
```
