### Title
Wrong Minimum Deposit Threshold Used in `update_balance` Causes Valid UTXOs to Be Permanently Ignored - (File: `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`)

### Summary
The ckBTC minter's `update_balance` function reads the raw `deposit_btc_min_amount` field from state when filtering UTXOs, instead of using the computed `effective_deposit_min_btc_amount()` which correctly accounts for the `check_fee`. This means that when `deposit_btc_min_amount` is set to a value lower than `check_fee + 1`, UTXOs whose value falls in the gap `[deposit_btc_min_amount, check_fee]` pass the first filter but are then caught by the second filter (`utxo.value <= check_fee`) and permanently suspended as `ValueTooSmall`. The `get_minter_info` endpoint advertises the effective (correct) minimum via `effective_deposit_min_btc_amount()`, but the actual processing logic uses the raw, lower value — creating a discrepancy between what the minter reports as the minimum and what it actually enforces.

### Finding Description
In `update_balance`, the UTXO filtering logic reads two separate thresholds from state:

```rust
let (deposit_btc_min_amount, check_fee) =
    read_state(|s| (s.deposit_btc_min_amount, s.check_fee));
```

It then applies two sequential checks:

```rust
if utxo.value < deposit_btc_min_amount {
    // ignored: below configured minimum
} else if utxo.value <= check_fee {
    // ignored: value does not exceed check fee
}
```

The state also defines a helper method `effective_deposit_min_btc_amount()`:

```rust
pub fn effective_deposit_min_btc_amount(&self) -> u64 {
    self.deposit_btc_min_amount
        .max(self.check_fee.saturating_add(1))
}
```

This method is used by `get_minter_info` to report the effective minimum to callers:

```rust
deposit_btc_min_amount: Some(s.effective_deposit_min_btc_amount()),
```

The `UpgradeArgs` handler allows setting `deposit_btc_min_amount` to any value, including values below `check_fee`. The `validate_config` method only checks that `check_fee <= retrieve_btc_min_amount`, not that `deposit_btc_min_amount >= check_fee + 1`. This means a governance-controlled upgrade can set `deposit_btc_min_amount` to a value lower than `check_fee`.

When this happens, a UTXO with value exactly equal to `check_fee` (e.g., `utxo.value == check_fee`) will:
1. Pass the first check (`utxo.value >= deposit_btc_min_amount` since `deposit_btc_min_amount < check_fee`)
2. Fail the second check (`utxo.value <= check_fee`)
3. Be suspended as `ValueTooSmall` and re-evaluated only once per day

The `get_minter_info` endpoint would report `deposit_btc_min_amount = check_fee + 1` (the effective value), but the processing logic uses the raw `deposit_btc_min_amount`. This is the same class of bug as H-01: a parameter check uses the wrong (lower) value, causing valid operations to be incorrectly rejected.

The `ensure_reason_consistent_with_state` assertion in `suspend_utxo` confirms this is the intended invariant check:

```rust
SuspendedReason::ValueTooSmall => {
    assert!(utxo.value < self.deposit_btc_min_amount || utxo.value <= self.check_fee);
}
```

This assertion passes for `utxo.value == check_fee` even when `deposit_btc_min_amount < check_fee`, confirming the code path is reachable.

### Impact Explanation
Any user who deposits a UTXO with value exactly equal to `check_fee` (when `deposit_btc_min_amount` has been set below `check_fee`) will have their UTXO permanently suspended as `ValueTooSmall`. The UTXO is re-evaluated once per day but will always fail the same `utxo.value <= check_fee` check. The user's BTC is locked in the minter's deposit address with no ckBTC minted. The `get_minter_info` endpoint misleads users by reporting a higher effective minimum than what the processing logic actually uses, so users who check the advertised minimum before depositing may still have their UTXOs rejected.

**Impact: Medium** — User funds (BTC UTXOs) are permanently stuck; no ckBTC is minted. The BTC is not lost (it remains at the deposit address) but is inaccessible until the minter is upgraded to fix the threshold.

### Likelihood Explanation
**Likelihood: Medium** — This requires `deposit_btc_min_amount` to be set below `check_fee` via an upgrade. The `UpgradeArgs` handler does not prevent this. The mainnet upgrade proposal from January 2026 explicitly set `deposit_btc_min_amount = 300` while `check_fee` is `DEFAULT_CHECK_FEE`. If `deposit_btc_min_amount` is ever set to a value ≤ `check_fee`, any user depositing a UTXO with value in `[deposit_btc_min_amount, check_fee]` is affected. The test `test_min_deposit_amount` confirms that `get_minter_info` returns `check_fee + 1` even when `deposit_btc_min_amount` is set lower, but the processing path is not guarded by the same logic.

### Recommendation
Replace the raw `deposit_btc_min_amount` read in `update_balance` with `effective_deposit_min_btc_amount()`:

```diff
- let (deposit_btc_min_amount, check_fee) =
-     read_state(|s| (s.deposit_btc_min_amount, s.check_fee));
+ let (deposit_btc_min_amount, check_fee) =
+     read_state(|s| (s.effective_deposit_min_btc_amount(), s.check_fee));
```

This ensures the processing logic uses the same threshold that `get_minter_info` advertises, and the second `utxo.value <= check_fee` branch becomes unreachable (since `effective_deposit_min_btc_amount() >= check_fee + 1`).

### Proof of Concept

1. Deploy ckBTC minter with `check_fee = 2000` (the `DEFAULT_CHECK_FEE`).
2. Upgrade the minter with `deposit_btc_min_amount = Some(1000)` (below `check_fee`).
3. Verify `get_minter_info().deposit_btc_min_amount == Some(2001)` (effective value reported correctly).
4. Send a Bitcoin UTXO with value `2000` (exactly `check_fee`) to the user's deposit address.
5. Call `update_balance`.
6. Observe: the UTXO passes the first filter (`2000 >= 1000`) but fails the second (`2000 <= 2000`), returning `UtxoStatus::ValueTooSmall`.
7. The user receives no ckBTC despite the minter advertising a minimum of `2001` and the UTXO value being `2000`.

The root cause is confirmed at: [1](#0-0) 

The correct effective threshold is computed but only used for reporting: [2](#0-1) 

The `get_minter_info` endpoint uses the effective value: [3](#0-2) 

The upgrade handler allows setting `deposit_btc_min_amount` below `check_fee` without validation: [4](#0-3) 

The `validate_config` method does not enforce `deposit_btc_min_amount >= check_fee + 1`: [5](#0-4)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L272-293)
```rust
    let (deposit_btc_min_amount, check_fee) =
        read_state(|s| (s.deposit_btc_min_amount, s.check_fee));
    let mut utxo_statuses: Vec<UtxoStatus> = vec![];

    for utxo in processable_utxos {
        let ignored_reason = if utxo.value < deposit_btc_min_amount {
            Some(format!(
                "Ignored UTXO {} for account {caller_account} because UTXO value {} is lower than the minimum deposit amount {}",
                DisplayOutpoint(&utxo.outpoint),
                DisplayAmount(utxo.value),
                DisplayAmount(deposit_btc_min_amount)
            ))
        } else if utxo.value <= check_fee {
            Some(format!(
                "Ignored UTXO {} for account {caller_account} because UTXO value {} is not higher than the check fee {}",
                DisplayOutpoint(&utxo.outpoint),
                DisplayAmount(utxo.value),
                DisplayAmount(check_fee)
            ))
        } else {
            None
        };
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L705-706)
```rust
        if let Some(deposit_btc_min_amount) = deposit_btc_min_amount {
            self.deposit_btc_min_amount = deposit_btc_min_amount;
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L759-769)
```rust
    pub fn validate_config(&self) {
        if self.check_fee > self.retrieve_btc_min_amount {
            ic_cdk::trap("check_fee cannot be greater than retrieve_btc_min_amount");
        }
        if self.ecdsa_key_name.is_empty() {
            ic_cdk::trap("ecdsa_key_name is not set");
        }
        if self.btc_checker_principal.is_none() {
            ic_cdk::trap("Bitcoin checker principal is not set");
        }
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1831-1836)
```rust
    /// Compute the minimum BTC amount that can be deposited.
    /// UTXOs with a lower value will be ignored.
    pub fn effective_deposit_min_btc_amount(&self) -> u64 {
        self.deposit_btc_min_amount
            .max(self.check_fee.saturating_add(1))
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L252-258)
```rust
fn get_minter_info() -> MinterInfo {
    read_state(|s| MinterInfo {
        check_fee: s.check_fee,
        min_confirmations: s.min_confirmations,
        retrieve_btc_min_amount: s.fee_based_retrieve_btc_min_amount,
        deposit_btc_min_amount: Some(s.effective_deposit_min_btc_amount()),
    })
```
