### Title
`estimate_withdrawal_fee` Mutates UTXO State Inside a `#[query]` Endpoint, Corrupting Minter State When Called via Inter-Canister Update - (`rs/bitcoin/ckbtc/minter/src/main.rs`)

---

### Summary

The ckBTC minter exposes `estimate_withdrawal_fee` as a `#[query]` endpoint. Internally it calls `mutate_state`, which destructively removes UTXOs from `available_utxos`. The inline comment claims this is safe "even when called in replicated mode since any change will be discarded." This claim is incorrect: on the Internet Computer, when a canister calls another canister's query method via an inter-canister call, the execution runs in **replicated (update) mode** and state mutations **are committed**. Any unprivileged canister can therefore permanently drain the minter's UTXO set without triggering any Bitcoin withdrawal.

---

### Finding Description

`estimate_withdrawal_fee` is annotated `#[query]` and contains the following code:

```rust
#[query]
fn estimate_withdrawal_fee(arg: EstimateFeeArg) -> WithdrawalFee {
    // This is a **query** endpoint, so mutating the state is not an issue
    // (even when called in replicated mode) since any change will be discarded.
    match mutate_state(|s| {
        ...
        ic_ckbtc_minter::estimate_retrieve_btc_fee(
            &mut s.available_utxos,   // <-- UTXOs are removed here
            ...
        )
    })
``` [1](#0-0) 

`estimate_retrieve_btc_fee` delegates to `queries::estimate_withdrawal_fee`, which calls `utxos_selection`. `utxos_selection` calls `greedy`, which **removes** UTXOs from `available_utxos` via `available_utxos.remove(...)`: [2](#0-1) [3](#0-2) 

The comment's assumption is wrong. IC query calls made **directly by a user** do discard state. But when **another canister** calls `estimate_withdrawal_fee` as an inter-canister call, the IC executes it in replicated (update) mode, and all state mutations are committed to the replicated state. The ckDoge minter correctly exposes its equivalent endpoint as an `#[update]` (evidenced by the test harness using `update_call`), confirming the ckBTC design is inconsistent: [4](#0-3) 

---

### Impact Explanation

An unprivileged canister can call `estimate_withdrawal_fee` repeatedly as an inter-canister update call. Each call permanently removes UTXOs from `s.available_utxos` without creating any Bitcoin transaction or burning any ckBTC. After enough calls:

1. The minter's `available_utxos` set is depleted.
2. Legitimate user withdrawal requests via `retrieve_btc` / `retrieve_btc_with_approval` fail with `NotEnoughFunds` even though the minter holds sufficient Bitcoin on-chain.
3. The minter's internal accounting diverges from the actual Bitcoin UTXO set, breaking the conservation invariant.
4. Recovery requires a manual upgrade to re-scan and re-populate the UTXO set.

This is a **chain-fusion mint/burn/replay bug** class: the minter's internal state no longer correctly reflects the on-chain Bitcoin UTXOs it controls, causing denial-of-service for all ckBTC withdrawals.

---

### Likelihood Explanation

The entry path requires only that an attacker deploy a canister and call `estimate_withdrawal_fee` in a loop via inter-canister calls. No privileged access, no key material, and no governance majority is needed. The endpoint is publicly callable (no `check_anonymous_caller` guard), and the IC's inter-canister call mechanism is standard and well-documented. The cost is only cycles for the calls.

---

### Recommendation

Change `estimate_withdrawal_fee` from `#[query]` to `#[update]`, consistent with the ckDoge minter's design. Alternatively, rewrite the function to use `read_state` with a cloned UTXO set for simulation, so no mutation of the live state occurs:

```rust
#[query]
fn estimate_withdrawal_fee(arg: EstimateFeeArg) -> WithdrawalFee {
    read_state(|s| {
        let mut utxos_clone = s.available_utxos.clone();
        ic_ckbtc_minter::estimate_retrieve_btc_fee(
            &mut utxos_clone,  // operate on a clone, not live state
            ...
        )
    })
}
```

---

### Proof of Concept

```rust
// Attacker canister
use ic_cdk::api::call::call;

#[update]
async fn drain_ckbtc_utxos(minter: Principal) {
    for _ in 0..1000 {
        let _: (WithdrawalFee,) = call(
            minter,
            "estimate_withdrawal_fee",
            (EstimateFeeArg { amount: Some(50_000) },),
        ).await.unwrap();
        // Each call removes UTXOs from minter's available_utxos
        // because inter-canister query calls run in replicated mode
    }
    // minter.available_utxos is now depleted;
    // all subsequent retrieve_btc calls will fail with NotEnoughFunds
}
```

The root cause is at: [5](#0-4)

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

**File:** rs/bitcoin/ckbtc/minter/src/queries.rs (L46-66)
```rust
pub fn estimate_withdrawal_fee<F: FeeEstimator>(
    available_utxos: &mut UtxoSet,
    withdrawal_amount: u64,
    median_fee_millisatoshi_per_vbyte: FeeRate,
    minter_address: BitcoinAddress,
    recipient_address: BitcoinAddress,
    max_num_inputs_in_transaction: usize,
    fee_estimator: &F,
) -> Result<WithdrawalFee, BuildTxError> {
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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1070-1099)
```rust
fn greedy(target: u64, available_utxos: &mut UtxoSet) -> Vec<Utxo> {
    #[cfg(feature = "canbench-rs")]
    let _scope = canbench_rs::bench_scope("greedy");

    let mut solution = vec![];
    let mut goal = target;
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

    debug_assert!(solution.is_empty() || solution.iter().map(|u| u.value).sum::<u64>() >= target);

    solution
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
