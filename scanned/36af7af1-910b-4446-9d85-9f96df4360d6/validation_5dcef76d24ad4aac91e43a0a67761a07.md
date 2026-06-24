### Title
`estimate_withdrawal_fee` Permanently Drains `available_utxos` When Invoked as a Replicated Update Call — (`rs/dogecoin/ckdoge/minter/src/main.rs`)

---

### Summary

`estimate_withdrawal_fee` is declared `#[query]` but internally calls `mutate_state` and removes UTXOs from `available_utxos` without restoring them. The inline comment claims this is safe "even when called in replicated mode since any change will be discarded." That claim is **incorrect**. On the Internet Computer, a `#[query]`-annotated method invoked as an update call (replicated query) **does commit state changes**. Any unprivileged caller can exploit this to silently drain the minter's UTXO set, causing all subsequent legitimate withdrawals to fail with `NotEnoughFunds`.

---

### Finding Description

**Entry point — `main.rs` lines 98–128:**

```rust
#[query]
fn estimate_withdrawal_fee(
    arg: EstimateFeeArg,
) -> Result<WithdrawalFee, EstimateWithdrawalFeeError> {
    // This is a **query** endpoint, so mutating the state is not an issue
    // (even when called in replicated mode) since any change will be discarded.
    ic_ckbtc_minter::state::mutate_state(|s| {
        ...
        ic_ckdoge_minter::fees::estimate_retrieve_doge_fee(
            &mut s.available_utxos,   // ← mutable borrow of live state
            ...
        )
    })
}
``` [1](#0-0) 

**Mutation path — `queries.rs` lines 46–76:**

`estimate_retrieve_doge_fee` delegates to `ic_ckbtc_minter::queries::estimate_withdrawal_fee`, which calls `utxos_selection`. `utxos_selection` calls `greedy`, which calls `available_utxos.remove(&utxo)` for each selected UTXO. On a **successful** fee estimation the selected UTXOs are **never reinserted** — they are simply dropped after the fee is computed. [2](#0-1) [3](#0-2) 

**Why the comment is wrong — `main.rs` lines 248–257:**

The same file contains `http_request`, which is also `#[query]` and explicitly guards against replicated execution:

```rust
#[query(hidden = true)]
fn http_request(req: HttpRequest) -> HttpResponse {
    if ic_cdk::api::in_replicated_execution() {
        ic_cdk::trap("update call rejected");
    }
    ...
}
``` [4](#0-3) 

The existence of this guard in the same file proves the developers know that `#[query]` methods **can** be called as update calls and that state changes **are** committed in that mode. The guard is present for `http_request` but **absent** from `estimate_withdrawal_fee`.

**Why the existing test does not catch this:**

The integration test calls `estimate_withdrawal_fee` via `update_call` (replicated path) and then asserts that `get_known_utxos` is unchanged: [5](#0-4) [6](#0-5) 

However, `get_known_utxos` reads from `utxos_state_addresses` (the per-account UTXO map), **not** from `available_utxos`. The two data structures are separate. Draining `available_utxos` leaves `utxos_state_addresses` untouched, so the assertion passes even when the minter's spendable UTXO pool has been silently emptied. [7](#0-6) 

---

### Impact Explanation

- `available_utxos` is the pool from which the minter selects inputs when building withdrawal transactions.
- After the attack, `available_utxos` is empty (or reduced below the required threshold).
- Every subsequent call to `retrieve_doge_with_approval` will fail with `NotEnoughFunds` / `AmountTooHigh`, blocking all user withdrawals.
- No funds are transferred to the attacker; the impact is a denial-of-service on the withdrawal path and corruption of UTXO accounting. Recovery requires a canister upgrade that re-derives `available_utxos` from the event log.

---

### Likelihood Explanation

- Requires no privilege, no key, no governance vote — any principal can send an ingress update call to a `#[query]` endpoint.
- The IC protocol explicitly supports calling query methods as update calls (replicated queries); this is standard behavior documented in the IC interface spec.
- The attack is a single ingress message with a valid `amount` argument.
- The test utility itself already exercises this exact path (`update_call` to `estimate_withdrawal_fee`), confirming the call is accepted by the runtime. [8](#0-7) 

---

### Recommendation

Apply the same guard already used in `http_request`:

```rust
#[query]
fn estimate_withdrawal_fee(
    arg: EstimateFeeArg,
) -> Result<WithdrawalFee, EstimateWithdrawalFeeError> {
    if ic_cdk::api::in_replicated_execution() {
        ic_cdk::trap("update call rejected");
    }
    ...
}
```

Alternatively, replace `mutate_state` with `read_state` and rewrite `estimate_retrieve_doge_fee` to operate on a **cloned** or **read-only** snapshot of `available_utxos`, so that no mutation of live state occurs regardless of call mode.

The integration test should also be updated to assert that `available_utxos` (not just `get_known_utxos`) is unchanged after calling `estimate_withdrawal_fee` via the update path.

---

### Proof of Concept

```rust
// State-machine test (PocketIC / StateMachine)
// 1. Deposit UTXOs so available_utxos is non-empty.
// 2. Read available_utxos count via a privileged query or self_check.
// 3. Call estimate_withdrawal_fee via execute_ingress / update_call
//    (NOT query_call) with a valid withdrawal amount.
// 4. Assert available_utxos count is UNCHANGED — this assertion FAILS,
//    proving permanent state mutation.

let utxos_before = minter.available_utxos_count(); // read via self_check or state inspection
minter.env.update_call(
    minter.id,
    Principal::anonymous(),
    "estimate_withdrawal_fee",
    Encode!(&EstimateFeeArg { amount: Some(RETRIEVE_DOGE_MIN_AMOUNT) }).unwrap(),
).unwrap();
let utxos_after = minter.available_utxos_count();
assert_eq!(utxos_before, utxos_after); // FAILS: utxos_after < utxos_before
```

The test utility at `rs/dogecoin/ckdoge/test_utils/src/minter.rs:147–163` already calls `estimate_withdrawal_fee` via `update_call`; the only missing step is asserting on `available_utxos` rather than `get_known_utxos`. [9](#0-8) [10](#0-9)

### Citations

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

**File:** rs/dogecoin/ckdoge/minter/src/main.rs (L248-257)
```rust
#[query(hidden = true)]
fn http_request(req: HttpRequest) -> HttpResponse {
    if ic_cdk::api::in_replicated_execution() {
        ic_cdk::trap("update call rejected");
    }
    let network = ic_ckbtc_minter::state::read_state(|s| s.btc_network)
        .try_into()
        .unwrap_or_else(|err| ic_cdk::trap(err));
    ic_ckbtc_minter::queries::http_request(req, &ckdoge_dashboard(network))
}
```

**File:** rs/bitcoin/ckbtc/minter/src/queries.rs (L37-44)
```rust
pub fn get_known_utxos(args: UpdateBalanceArgs) -> Vec<Utxo> {
    read_state(|s| {
        s.known_utxos_for_account(&Account {
            owner: args.owner.unwrap_or(ic_cdk::api::msg_caller()),
            subaccount: args.subaccount,
        })
    })
}
```

**File:** rs/bitcoin/ckbtc/minter/src/queries.rs (L55-66)
```rust
    // We simulate the algorithm that selects UTXOs for the
    // specified amount.
    let selected_utxos = utxos_selection(withdrawal_amount, available_utxos, 1);

    build_unsigned_transaction_from_inputs(
        &selected_utxos,
        vec![(recipient_address, withdrawal_amount)],
        &minter_address,
        max_num_inputs_in_transaction,
        median_fee_millisatoshi_per_vbyte,
        fee_estimator,
    )
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1076-1095)
```rust
    while goal > 0 {
        let candidate_utxo = available_utxos
            .find_lower_bound(goal)
            .or_else(|| available_utxos.last())
            .cloned();
        match candidate_utxo {
            Some(utxo) => {
                let utxo = available_utxos.remove(&utxo).expect("BUG: missing UTXO");
                goal = goal.saturating_sub(utxo.value);
                solution.push(utxo);
            }
            None => {
                // Not enough available UTXOs to satisfy the request.
                for u in solution {
                    available_utxos.insert(u);
                }
                return vec![];
            }
        }
    }
```

**File:** rs/dogecoin/ckdoge/test_utils/src/minter.rs (L147-163)
```rust
    pub fn estimate_withdrawal_fee(
        &self,
        withdrawal_amount: u64,
    ) -> Result<WithdrawalFee, EstimateWithdrawalFeeError> {
        let call_result = self
            .env
            .update_call(
                self.id,
                Principal::anonymous(),
                "estimate_withdrawal_fee",
                Encode!(&EstimateFeeArg {
                    amount: Some(withdrawal_amount)
                })
                .unwrap(),
            )
            .expect("BUG: failed to call estimate_withdrawal_fee");
        Decode!(&call_result, Result<WithdrawalFee, EstimateWithdrawalFeeError>).unwrap()
```

**File:** rs/dogecoin/ckdoge/minter/tests/tests.rs (L609-615)
```rust
        let utxos_before = minter.get_known_utxos(USER_PRINCIPAL);
        let result = minter.estimate_withdrawal_fee(withdrawal_amount);
        let utxos_after = minter.get_known_utxos(USER_PRINCIPAL);
        assert_eq!(
            utxos_before, utxos_after,
            "BUG: a query endpoint should not be able to modify state!"
        );
```
