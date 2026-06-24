### Title
`estimate_withdrawal_fee` Query Endpoint Traps Instead of Returning Structured Error - (`File: rs/bitcoin/ckbtc/minter/src/main.rs`)

### Summary

The ckBTC minter's `estimate_withdrawal_fee` `#[query]` endpoint unconditionally panics (traps) under multiple reachable conditions instead of returning a structured error. Any unprivileged caller — including other canisters, dapp frontends, or boundary-node users — can trigger these traps by supplying ordinary, valid-looking inputs. The ckDOGE minter already corrected this exact pattern by returning `Result<WithdrawalFee, EstimateWithdrawalFeeError>`.

### Finding Description

`estimate_withdrawal_fee` in the ckBTC minter is a public `#[query]` endpoint: [1](#0-0) 

It traps in **four** distinct reachable paths:

**Path 1 — fee percentiles not yet initialised** (`last_median_fee_per_vbyte` is `None`): [2](#0-1) 

`last_median_fee_per_vbyte` is an `Option<FeeRate>` that starts as `None` and is only populated after the first successful `estimate_fee_per_vbyte` async task completes: [3](#0-2) [4](#0-3) 

Any call to `estimate_withdrawal_fee` before that background task has run — e.g., immediately after canister install or upgrade — hits `.expect(...)` and traps.

**Path 2 — withdrawal amount too large (`BuildTxError::NotEnoughFunds`)**: [5](#0-4) 

**Path 3 — withdrawal amount too small (`BuildTxError::DustOutput` / `BuildTxError::AmountTooLow`)**: [6](#0-5) 

**Path 4 — amount requires too many inputs (`BuildTxError::InvalidTransaction(TooManyInputs)`)**: [7](#0-6) 

The ckDOGE minter, which shares the same underlying library, already corrected this by returning `Result<WithdrawalFee, EstimateWithdrawalFeeError>` for all error arms: [8](#0-7) 

The `EstimateWithdrawalFeeError` type explicitly models both `AmountTooHigh` and `AmountTooLow` variants: [9](#0-8) 

The ckBTC minter's DID interface declares `estimate_withdrawal_fee` as returning a plain record (not a `Result`), so callers have no way to distinguish a legitimate "amount too large" response from a canister trap: [10](#0-9) 

### Impact Explanation

- **Composability break for canister callers**: Any canister that calls `estimate_withdrawal_fee` with an amount outside the minter's current UTXO capacity, or before fee percentiles are populated, receives a reject response with `CanisterCalledTrap` / `CanisterTrapped` error code. The calling canister cannot distinguish this from a bug in its own code and cannot recover gracefully.
- **DoS of fee-estimation UX**: Dapp frontends and wallets that probe fee estimates for a range of amounts (e.g., to show a fee slider) will receive traps for amounts that are too large or too small, breaking the user experience.
- **Early-lifecycle unavailability**: Immediately after a canister upgrade or fresh install, before the first `RefreshFeePercentiles` timer fires, every call to `estimate_withdrawal_fee` traps unconditionally regardless of the supplied amount.

### Likelihood Explanation

- The `NotEnoughFunds` path is reachable by any caller who supplies an amount larger than the minter's total available UTXO value — a normal condition when the minter has few UTXOs or is freshly deployed.
- The `AmountTooLow`/`DustOutput` path is reachable by any caller who supplies a small amount (e.g., 1 satoshi).
- The `last_median_fee_per_vbyte` `None` path is reachable during every canister upgrade window before the background timer fires.
- No special privilege is required; the endpoint is callable by any principal including anonymous.

### Recommendation

Mirror the ckDOGE minter's approach: change the return type of `estimate_withdrawal_fee` to `Result<WithdrawalFee, EstimateWithdrawalFeeError>`, define an `EstimateWithdrawalFeeError` enum covering `FeePercentilesMissing`, `AmountTooHigh`, and `AmountTooLow` variants, and replace all `panic!`/`.expect()` calls with `Err(...)` returns. Update the DID file accordingly.

### Proof of Concept

1. Deploy the ckBTC minter canister (or call it immediately after upgrade, before the timer fires).
2. Call `estimate_withdrawal_fee` with `record { amount = opt (1 : nat64) }` (dust amount).
3. Observe: the canister traps with `"BUG: withdrawal amount is too low ..."` instead of returning a structured error.

Alternatively, with a running minter:
1. Call `estimate_withdrawal_fee` with `record { amount = opt (999_999_999_999 : nat64) }` (amount exceeding all available UTXOs).
2. Observe: the canister traps with `"ERROR: withdrawal amount is too large for the minter"` instead of returning `Err(AmountTooHigh)`.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L219-249)
```rust
#[query]
fn estimate_withdrawal_fee(arg: EstimateFeeArg) -> WithdrawalFee {
    // This is a **query** endpoint, so mutating the state is not an issue
    // (even when called in replicated mode) since any change will be discarded.
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
    }) {
        Ok(fee) => fee,
        Err(BuildTxError::NotEnoughFunds) => {
            panic!("ERROR: withdrawal amount is too large for the minter")
        }
        Err(e @ BuildTxError::DustOutput { .. } | e @ BuildTxError::AmountTooLow) => panic!(
            "BUG: withdrawal amount is too low ({e:?}), but the withdrawal amount should be large enough to prevent this"
        ),
        Err(BuildTxError::InvalidTransaction(
            e @ InvalidTransactionError::TooManyInputs { .. },
        )) => panic!(
            "ERROR: the minter cannot currently process such a large withdrawal amount because it would require too many inputs ({e:?}), \
            resulting in the transaction being potentially non-standard"
        ),
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L544-545)
```rust
    /// The last median fee per vbyte computed from `last_fee_per_vbyte`.
    pub last_median_fee_per_vbyte: Option<FeeRate>,
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L244-249)
```rust
                    );
                    mutate_state(|s| {
                        s.last_fee_per_vbyte = fees;
                        s.last_median_fee_per_vbyte = Some(median_fee);
                        s.fee_based_retrieve_btc_min_amount = fee_based_retrieve_btc_min_amount;
                    });
```

**File:** rs/dogecoin/ckdoge/minter/src/main.rs (L98-128)
```rust
#[query]
fn estimate_withdrawal_fee(
    arg: EstimateFeeArg,
) -> Result<WithdrawalFee, EstimateWithdrawalFeeError> {
    // This is a **query** endpoint, so mutating the state is not an issue
    // (even when called in replicated mode) since any change will be discarded.
    ic_ckbtc_minter::state::mutate_state(|s| {
        let fee_estimator = DOGECOIN_CANISTER_RUNTIME.fee_estimator(s);
        let withdrawal_amount = arg.amount.unwrap_or(s.fee_based_retrieve_btc_min_amount);

        ic_ckdoge_minter::fees::estimate_retrieve_doge_fee(
            &mut s.available_utxos,
            withdrawal_amount,
            s.last_median_fee_per_vbyte
                .expect("Bitcoin current fee percentiles not retrieved yet."),
            s.max_num_inputs_in_transaction,
            &fee_estimator,
        )
        .map_err(|e| match e {
            BuildTxError::NotEnoughFunds
            | BuildTxError::InvalidTransaction(InvalidTransactionError::TooManyInputs { .. }) => {
                EstimateWithdrawalFeeError::AmountTooHigh
            }
            BuildTxError::AmountTooLow | BuildTxError::DustOutput { .. } => {
                EstimateWithdrawalFeeError::AmountTooLow {
                    min_amount: s.fee_based_retrieve_btc_min_amount,
                }
            }
        })
    })
}
```

**File:** rs/dogecoin/ckdoge/minter/src/candid_api.rs (L118-130)
```rust
#[derive(Copy, Clone, Eq, PartialEq, Debug, CandidType, Serialize, Deserialize)]
pub enum EstimateWithdrawalFeeError {
    /// The given withdrawal amount is too low to pay for the minter and transaction fee.
    AmountTooLow {
        /// The current minimum withdrawal amount.
        /// Its value may vary depending on the current transaction fees.
        min_amount: u64,
    },
    /// The current withdrawal amount is too high so that either the minter does not have enough
    /// funds to satisfy that request; or, it would use too many UTXOs so that the transaction may be
    /// non-standard.
    AmountTooHigh,
}
```

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L711-712)
```text
    /// retrieve_btc request based on the current status of the Bitcoin network.
    estimate_withdrawal_fee : (record { amount : opt nat64 }) -> (record { bitcoin_fee : nat64; minter_fee : nat64 }) query;
```
