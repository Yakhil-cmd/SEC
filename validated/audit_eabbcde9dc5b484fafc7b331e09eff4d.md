### Title
`check_utxo` Retry Loop Skips `Retriable` Responses, Causing Immediate Failure Instead of Retry - (File: `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`)

### Summary

The `check_utxo` function in the ckBTC minter contains a retry loop (`for i in 0..MAX_CHECK_TRANSACTION_RETRY`) that correctly retries on `NotEnoughCycles` but immediately returns an error on `CheckTransactionStatus::Retriable`, even though the Bitcoin checker canister's own interface explicitly documents `Retriable` as "the result is not available, but calls **can be retried**." This mirrors the Allora bug class exactly: an error condition is embedded inside a response variant, and the retry loop fails to act on it.

### Finding Description

In `check_utxo`, the retry loop handles four response arms:

```rust
for i in 0..MAX_CHECK_TRANSACTION_RETRY {
    match runtime.check_transaction(...).await...? {
        CheckTransactionResponse::Passed => return Ok(UtxoCheckStatus::Clean),
        CheckTransactionResponse::Failed(...) => return Ok(UtxoCheckStatus::Tainted),
        CheckTransactionResponse::Unknown(CheckTransactionStatus::NotEnoughCycles) => {
            continue;  // ✅ retries
        }
        CheckTransactionResponse::Unknown(CheckTransactionStatus::Retriable(status)) => {
            return Err(UpdateBalanceError::TemporarilyUnavailable(...));  // ❌ should continue
        }
        CheckTransactionResponse::Unknown(CheckTransactionStatus::Error(error)) => {
            return Err(...);  // irrecoverable, correct
        }
    }
}
``` [1](#0-0) 

The `Retriable` arm covers three sub-cases, all explicitly transient:

- `Pending` — the checker is already fetching the transaction data; caller should wait and retry
- `HighLoad` — the checker is under load; caller should retry
- `TransientInternalError(String)` — a transient HTTP error occurred fetching Bitcoin data; caller should retry [2](#0-1) [3](#0-2) 

The `NotEnoughCycles` arm correctly uses `continue` to loop. The `Retriable` arm, despite being semantically identical in terms of "try again," uses `return Err(...)` and exits the loop entirely.

### Impact Explanation

When the Bitcoin checker canister returns any `Retriable` response during a `update_balance` call, the ckBTC minter immediately propagates `UpdateBalanceError::TemporarilyUnavailable` to the caller. The user's UTXO is not processed and no ckBTC is minted. The user must re-invoke `update_balance` from scratch. Under sustained checker load (`HighLoad`) or while the checker is actively fetching transaction data (`Pending`), every `update_balance` call will fail at the first `Retriable` response, making ckBTC minting unreliable for all depositors during those windows. [4](#0-3) 

### Likelihood Explanation

The Bitcoin checker canister returns `Retriable` responses in normal operation: `Pending` is returned whenever the checker is mid-flight fetching Bitcoin transaction data via HTTP outcalls (a multi-round process), and `HighLoad` is returned when the fetch guard is exhausted. Both conditions are expected during normal ckBTC deposit activity. Any unprivileged user who deposits Bitcoin and calls `update_balance` while the checker is in one of these states will trigger the bug. No special privileges or adversarial setup are required. [5](#0-4) 

### Recommendation

Change the `Retriable` arm to `continue` the retry loop (matching the `NotEnoughCycles` arm), optionally with a sleep between retries. The loop already has a bounded iteration count (`MAX_CHECK_TRANSACTION_RETRY = 10`) to prevent infinite looping. [6](#0-5) 

```rust
CheckTransactionResponse::Unknown(CheckTransactionStatus::Retriable(status)) => {
    log!(
        Priority::Debug,
        "The Bitcoin checker canister is temporarily unavailable: {:?}, retrying...",
        status
    );
    continue; // was: return Err(...)
}
```

### Proof of Concept

1. User deposits BTC to their ckBTC address and calls `update_balance`.
2. The ckBTC minter calls `check_utxo`, which calls `runtime.check_transaction(...)` on the Bitcoin checker canister.
3. The Bitcoin checker is mid-flight fetching the transaction's input data and returns `CheckTransactionResponse::Unknown(CheckTransactionStatus::Retriable(CheckTransactionRetriable::Pending))`.
4. The `check_utxo` loop hits the `Retriable` arm at line 432 and executes `return Err(UpdateBalanceError::TemporarilyUnavailable(...))`.
5. The retry loop (which has 10 iterations budgeted) is exited after the very first call, without ever retrying.
6. The user receives a `TemporarilyUnavailable` error; no ckBTC is minted.

The existing unit test `should_call_check_transaction_again_when_cycles_not_enough` demonstrates that `NotEnoughCycles` correctly retries 3 times before succeeding, but no equivalent test exists for `Retriable`, confirming the gap. [7](#0-6)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L18-20)
```rust
// Max number of times of calling check_transaction with cycle payment, to avoid spending too
// many cycles.
const MAX_CHECK_TRANSACTION_RETRY: usize = 10;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L400-450)
```rust
    for i in 0..MAX_CHECK_TRANSACTION_RETRY {
        match runtime
            .check_transaction(
                btc_checker_principal,
                utxo,
                CHECK_TRANSACTION_CYCLES_REQUIRED,
            )
            .await
            .map_err(|call_err| {
                UpdateBalanceError::TemporarilyUnavailable(format!(
                    "Failed to call Bitcoin checker canister: {call_err}"
                ))
            })? {
            CheckTransactionResponse::Failed(addresses) => {
                log!(
                    Priority::Info,
                    "Discovered a tainted UTXO {} (due to input addresses {}) for update_balance({:?}) call",
                    DisplayOutpoint(&utxo.outpoint),
                    addresses.join(","),
                    args,
                );
                return Ok(UtxoCheckStatus::Tainted);
            }
            CheckTransactionResponse::Passed => return Ok(UtxoCheckStatus::Clean),
            CheckTransactionResponse::Unknown(CheckTransactionStatus::NotEnoughCycles) => {
                log!(
                    Priority::Debug,
                    "The Bitcoin checker canister requires more cycles, Remaining tries: {}",
                    MAX_CHECK_TRANSACTION_RETRY - i - 1
                );
                continue;
            }
            CheckTransactionResponse::Unknown(CheckTransactionStatus::Retriable(status)) => {
                log!(
                    Priority::Debug,
                    "The Bitcoin checker canister is temporarily unavailable: {:?}",
                    status
                );
                return Err(UpdateBalanceError::TemporarilyUnavailable(format!(
                    "The Bitcoin checker canister is temporarily unavailable: {status:?}"
                )));
            }
            CheckTransactionResponse::Unknown(CheckTransactionStatus::Error(error)) => {
                log!(Priority::Debug, "Bitcoin checker error: {:?}", error);
                return Err(UpdateBalanceError::GenericError {
                    error_code: ErrorCode::KytError as u64,
                    error_message: format!("Bitcoin checker error: {error:?}"),
                });
            }
        }
    }
```

**File:** rs/bitcoin/checker/lib/types.rs (L77-95)
```rust
#[derive(CandidType, Debug, Clone, Deserialize, Serialize)]
pub enum CheckTransactionStatus {
    /// Caller should call with a minimum of `CHECK_TRANSACTION_CYCLES_REQUIRED` cycles.
    NotEnoughCycles,
    /// The result is not available, but calls can be retried.
    Retriable(CheckTransactionRetriable),
    /// The result is unknown due to an irrecoverable error.
    Error(CheckTransactionIrrecoverableError),
}

#[derive(CandidType, Debug, Clone, Deserialize, Serialize)]
pub enum CheckTransactionRetriable {
    /// Work is already in progress, and the result is pending.
    Pending,
    /// The service is experience high load.
    HighLoad,
    /// There was a transient error fetching data.
    TransientInternalError(String),
}
```

**File:** rs/bitcoin/checker/btc_checker_canister.did (L31-47)
```text
type CheckTransactionStatus = variant {
    // Caller should call with a minimum of 40 billion cycles.
    NotEnoughCycles;
    // The result is not available, but calls can be retried.
    Retriable: CheckTransactionRetriable;
    /// The result is unknown due to an irrecoverable error.
    Error: CheckTransactionIrrecoverableError;
};

type CheckTransactionRetriable = variant {
    // Work is already in progress, and the result is pending.
    Pending;
    // The service is experience high load.
    HighLoad;
    // There was a transient error fetching data.
    TransientInternalError: text;
};
```

**File:** rs/bitcoin/checker/src/fetch.rs (L84-96)
```rust
            Some(FetchTxStatus::PendingOutcall) => return TryFetchResult::Pending,
            Some(FetchTxStatus::Error(err)) => (
                // An FetchTxStatus error can be retried with another provider
                err.provider.next(),
                // The next provider can use the same max_response_bytes
                err.max_response_bytes,
            ),
            Some(FetchTxStatus::Fetched(fetched)) => return TryFetchResult::Fetched(fetched),
        };
        let guard = match self.new_fetch_guard(txid) {
            Ok(guard) => guard,
            Err(_) => return TryFetchResult::HighLoad,
        };
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/tests.rs (L42-81)
```rust
    #[tokio::test]
    async fn should_call_check_transaction_again_when_cycles_not_enough() {
        init_state_with_ecdsa_public_key();
        let account = ledger_account();
        let mut runtime = MockCanisterRuntime::new();
        use_ckbtc_event_logger(&mut runtime);
        mock_increasing_time(&mut runtime, NOW, Duration::from_secs(1));
        let test_utxo = utxo();
        let amount = test_utxo.value - read_state(|s| s.check_fee);
        mock_derive_user_address(&mut runtime, account);
        mock_get_utxos_for_account(&mut runtime, account, vec![test_utxo.clone()]);
        // The expectation below also ensures check_transaction is called exactly 3 times
        expect_check_transaction_returning_responses(
            &mut runtime,
            test_utxo.clone(),
            vec![
                CheckTransactionResponse::Unknown(CheckTransactionStatus::NotEnoughCycles),
                CheckTransactionResponse::Unknown(CheckTransactionStatus::NotEnoughCycles),
                CheckTransactionResponse::Passed,
            ],
        );
        runtime
            .expect_mint_ckbtc()
            .times(1)
            .withf(move |amount_, account_, _memo| amount_ == &amount && account_ == &account)
            .return_const(Ok(amount));
        mock_schedule_now_process_logic(&mut runtime);

        let result = update_balance(
            UpdateBalanceArgs {
                owner: Some(account.owner),
                subaccount: account.subaccount,
            },
            &runtime,
        )
        .await;

        // Check if the mint is successful in the end.
        assert!(result.is_ok());
        assert_matches::assert_matches!(&result.unwrap()[0], UtxoStatus::Minted { utxo, .. } if *utxo == test_utxo);
```
