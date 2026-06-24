### Title
ckBTC Minter `estimate_withdrawal_fee` Panics Instead of Returning Error for Oversized Amounts, and `get_minter_info` Omits Maximum Withdrawal Limit - (File: `rs/bitcoin/ckbtc/minter/src/main.rs`)

---

### Summary

The ckBTC minter exposes `get_minter_info()` which only advertises `retrieve_btc_min_amount` but never a maximum withdrawal amount, while the actual `retrieve_btc` operation has a hard implicit maximum (bounded by available UTXOs and `max_num_inputs_in_transaction`). Additionally, the `estimate_withdrawal_fee` query endpoint **panics/traps** when the requested amount exceeds the minter's capacity, instead of returning a structured error — unlike the analogous ckDOGE minter which correctly returns `EstimateWithdrawalFeeError::AmountTooHigh`. This mirrors the ERC-4626 `maxDeposit()` violation: a limit-query surface that does not reflect actual operational limits, causing integration failures for callers.

---

### Finding Description

**Root cause 1 — `estimate_withdrawal_fee` panics on oversized amounts:**

In `rs/bitcoin/ckbtc/minter/src/main.rs` lines 219–248, the `estimate_withdrawal_fee` query endpoint matches on `BuildTxError` results and calls `panic!` for two distinct over-limit conditions:

```rust
Err(BuildTxError::NotEnoughFunds) => {
    panic!("ERROR: withdrawal amount is too large for the minter")
}
Err(BuildTxError::InvalidTransaction(
    e @ InvalidTransactionError::TooManyInputs { .. },
)) => panic!(
    "ERROR: the minter cannot currently process such a large withdrawal amount ..."
),
``` [1](#0-0) 

This causes the query call to **trap** (IC canister panic = reject with `CANISTER_ERROR`) rather than returning a structured error to the caller. The ckDOGE minter, which shares the same underlying fee estimation logic, correctly handles both cases by returning `EstimateWithdrawalFeeError::AmountTooHigh`:

```rust
.map_err(|e| match e {
    BuildTxError::NotEnoughFunds
    | BuildTxError::InvalidTransaction(InvalidTransactionError::TooManyInputs { .. }) => {
        EstimateWithdrawalFeeError::AmountTooHigh
    }
    ...
})
``` [2](#0-1) 

**Root cause 2 — `get_minter_info` omits maximum withdrawal amount:**

`get_minter_info()` exposes only `retrieve_btc_min_amount` (the minimum), with no corresponding maximum:

```rust
fn get_minter_info() -> MinterInfo {
    read_state(|s| MinterInfo {
        check_fee: s.check_fee,
        min_confirmations: s.min_confirmations,
        retrieve_btc_min_amount: s.fee_based_retrieve_btc_min_amount,
        deposit_btc_min_amount: Some(s.effective_deposit_min_btc_amount()),
    })
}
``` [3](#0-2) 

The `MinterInfo` struct definition confirms no maximum field exists:

```rust
pub struct MinterInfo {
    pub min_confirmations: u32,
    pub retrieve_btc_min_amount: u64,
    pub check_fee: u64,
    pub deposit_btc_min_amount: Option<u64>,
}
``` [4](#0-3) 

Yet the actual `retrieve_btc` operation has a hard implicit maximum: if the required UTXOs exceed `max_num_inputs_in_transaction`, the request is accepted, burned, and then **reimbursed** after the fact as `InvalidTransaction(TooManyInputs)`. This is confirmed by the test `should_cancel_and_reimburse_large_withdrawal`: [5](#0-4) 

The `max_num_inputs_in_transaction` is a runtime-configurable state field: [6](#0-5) 

---

### Impact Explanation

1. **Query trap for oversized amounts**: Any unprivileged caller (ingress query, canister query) who calls `estimate_withdrawal_fee` with an amount that exceeds the minter's current UTXO capacity receives a canister trap/rejection instead of a structured `AmountTooHigh` error. Integrators cannot distinguish this from a bug or canister crash.

2. **No discoverable maximum**: Because `get_minter_info()` only exposes the minimum, integrators have no standard way to determine the maximum valid withdrawal amount. The only way to discover it is to call `estimate_withdrawal_fee` — which panics for amounts above the limit. This breaks the "query before act" integration pattern.

3. **Post-hoc reimbursement loss**: A user who submits `retrieve_btc` or `retrieve_btc_with_approval` with an amount requiring too many UTXOs will have their ckBTC burned and then reimbursed minus a reimbursement fee (`BitcoinFeeEstimator::COST_OF_ONE_BILLION_CYCLES`), causing a real financial loss with no prior warning. [7](#0-6) 

---

### Likelihood Explanation

Any integrator or wallet that:
- Calls `estimate_withdrawal_fee` to validate an amount before submitting `retrieve_btc`, or
- Reads `get_minter_info()` to determine valid withdrawal ranges

will encounter this. The ckBTC minter is a production mainnet canister with significant TVL. The `max_num_inputs_in_transaction` limit is dynamic and operator-configurable, meaning the effective maximum withdrawal amount changes over time without any public signal. Likelihood is **medium-high** for integrators and **low** for casual users who stay within typical amounts.

---

### Recommendation

1. **Fix `estimate_withdrawal_fee`** to return a structured error instead of panicking, matching the ckDOGE minter's pattern:

```rust
Err(BuildTxError::NotEnoughFunds) | Err(BuildTxError::InvalidTransaction(
    InvalidTransactionError::TooManyInputs { .. }
)) => Err(WithdrawalFeeError::AmountTooHigh),
```

2. **Add a `retrieve_btc_max_amount` field to `MinterInfo`** computed from the current available UTXOs and `max_num_inputs_in_transaction`, so callers can discover the effective maximum without trial-and-error.

---

### Proof of Concept

**Step 1**: Call `estimate_withdrawal_fee` with an amount larger than the minter's total available UTXO value (or requiring more than `max_num_inputs_in_transaction` UTXOs):

```
dfx canister call ckbtc_minter estimate_withdrawal_fee '(record { amount = opt (99_999_999_999_999 : nat64) })'
```

**Expected (ckDOGE behavior)**: `Err(AmountTooHigh)`
**Actual (ckBTC behavior)**: Canister trap — `"ERROR: withdrawal amount is too large for the minter"` [8](#0-7) 

**Step 2**: Call `get_minter_info()` — observe that only `retrieve_btc_min_amount` is returned, with no maximum:

```
dfx canister call ckbtc_minter get_minter_info '()'
// Returns: { retrieve_btc_min_amount = 100_000; ... }
// No maximum field present.
``` [3](#0-2) 

**Step 3**: Submit `retrieve_btc_with_approval` with an amount requiring more than `DEFAULT_MAX_NUM_INPUTS_IN_TRANSACTION` UTXOs — the request is accepted, ckBTC is burned, and then reimbursed minus fees, as demonstrated by the existing test: [5](#0-4)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L219-248)
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
```

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L251-259)
```rust
#[query]
fn get_minter_info() -> MinterInfo {
    read_state(|s| MinterInfo {
        check_fee: s.check_fee,
        min_confirmations: s.min_confirmations,
        retrieve_btc_min_amount: s.fee_based_retrieve_btc_min_amount,
        deposit_btc_min_amount: Some(s.effective_deposit_min_btc_amount()),
    })
}
```

**File:** rs/dogecoin/ckdoge/minter/src/main.rs (L116-127)
```rust
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
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L83-91)
```rust
#[derive(Debug, CandidType, Deserialize, Serialize)]
pub struct MinterInfo {
    pub min_confirmations: u32,
    pub retrieve_btc_min_amount: u64,
    // Serialize to the old name to be backward compatible in Candid.
    #[serde(rename = "kyt_fee")]
    pub check_fee: u64,
    pub deposit_btc_min_amount: Option<u64>,
}
```

**File:** rs/bitcoin/ckbtc/minter/tests/tests.rs (L3108-3173)
```rust
#[test]
fn should_cancel_and_reimburse_large_withdrawal() {
    let ckbtc = CkBtcSetup::new();
    let user = Principal::from(ckbtc.caller);
    let subaccount: Option<[u8; 32]> = Some([1; 32]);
    let user_account = Account {
        owner: user,
        subaccount,
    };

    // Step 1: deposit enough small UTXOs to exceed the max inputs limit.
    // We need at least max + 1 UTXOs for the withdrawal to trigger TooManyInputs,
    // plus a small buffer so there are leftover UTXOs in the set.
    const MAX_INPUTS: usize = ic_ckbtc_minter::state::DEFAULT_MAX_NUM_INPUTS_IN_TRANSACTION;
    const NUM_UTXOS: usize = MAX_INPUTS + 100;
    let deposit_value = 100_000_u64;
    let _deposited_utxos =
        ckbtc.deposit_utxos_with_value(user_account, &[deposit_value; NUM_UTXOS]);
    let balance_after_deposit = ckbtc.balance_of(user_account);
    assert_eq!(
        balance_after_deposit,
        Nat::from(NUM_UTXOS as u64 * (deposit_value - CHECK_FEE))
    );

    let withdrawal_amount = (MAX_INPUTS as u64 + 1) * deposit_value;
    ckbtc.approve_minter(user, withdrawal_amount, subaccount);
    let balance_before_withdrawal = ckbtc.balance_of(user_account);

    let RetrieveBtcOk { block_index } = ckbtc
        .retrieve_btc_with_approval(
            WITHDRAWAL_ADDRESS.to_string(),
            withdrawal_amount,
            subaccount,
        )
        .expect("retrieve_btc failed");

    let balance_after_withdrawal = ckbtc.balance_of(user_account);
    assert_eq!(
        balance_after_withdrawal,
        balance_before_withdrawal.clone() - Nat::from(withdrawal_amount)
    );

    assert_eq!(
        ckbtc.retrieve_btc_status_v2(block_index),
        RetrieveBtcStatusV2::Pending
    );

    ckbtc.env.advance_time(MAX_TIME_IN_QUEUE);

    let mempool = ckbtc.mempool();
    assert_eq!(
        mempool.len(),
        0,
        "no transaction should appear when being reimbursed"
    );

    let reimbursement_block_index = block_index + 1;
    let reimbursement_amount = withdrawal_amount - BitcoinFeeEstimator::COST_OF_ONE_BILLION_CYCLES;

    assert_matches!(
        ckbtc.retrieve_btc_status_v2(block_index),
        RetrieveBtcStatusV2::Reimbursed(reimbursement) if
        reimbursement.account == user_account &&
        reimbursement.amount == reimbursement_amount &&
        reimbursement.mint_block_index == reimbursement_block_index
    );
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L483-484)
```rust
    /// The maximum number of input UTXOs allowed in a transaction.
    pub max_num_inputs_in_transaction: usize,
```
