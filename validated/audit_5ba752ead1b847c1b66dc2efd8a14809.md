### Title
Negative-to-Unsigned Unsafe Cast in `ledgeramount_from_amount` Bypasses Rosetta API Amount Validation — (File: `rs/rosetta-api/icp/src/models/amount.rs`)

---

### Summary

The ICP Rosetta API's `from_amount` function accepts negative `i128` values (it only validates that `val.abs()` fits in `u64`). The downstream `ledgeramount_from_amount` function then casts the signed result directly to `u64` via `inner as u64`, silently wrapping any negative value into a huge unsigned integer. This is the exact same vulnerability class as the Uniswap report: a signed value bypasses a guard check because the guard only checks the absolute value, and the subsequent cast to unsigned silently reinterprets the bit pattern.

---

### Finding Description

In `rs/rosetta-api/icp/src/models/amount.rs`, `from_amount` parses the Rosetta `Amount.value` string as `i128` and validates only that `val.abs()` fits in `u64`:

```rust
let val: i128 = value.parse()...;
let _ = u64::try_from(val.abs()).map_err(|_| "Amount does not fit in u64"...)?;
Ok(val)   // ← negative values pass through
``` [1](#0-0) 

`ledgeramount_from_amount` then converts the returned `i128` to `Tokens` using an unchecked `as u64` cast:

```rust
pub fn ledgeramount_from_amount(amount: &Amount, token_name: &str) -> Result<Tokens, String> {
    let inner = from_amount(amount, token_name)?;
    Ok(Tokens::from_e8s(inner as u64))   // ← wraps negative to huge u64
}
``` [2](#0-1) 

For example, `inner = -1` produces `Tokens::from_e8s(u64::MAX)` (≈ 184 billion ICP). This `Tokens` value is then used directly in the `Disburse` operation handler:

```rust
let amount = if let Some(ref amount) = o.amount {
    Some(ledgeramount_from_amount(amount, token_name).map_err(...)?)
} else { None };
state.disburse(account, neuron_index, amount, recipient)?;
``` [3](#0-2) 

A second instance exists in the `Fee` operation handler, where a positive fee amount is negated before the same unsafe cast:

```rust
state.fee(account, Tokens::from_e8s((-amount) as u64))?;
``` [4](#0-3) 

Here, if `amount` is a positive value (e.g., `100`), then `(-100_i128) as u64 = u64::MAX - 99`, producing a nonsensical fee.

---

### Impact Explanation

An unprivileged Rosetta API caller submitting a `construction/payloads` request with a negative `Disburse` amount (e.g., `"-1"`) causes the Rosetta API to construct and submit a `manage_neuron::Disburse` command to the NNS governance canister with `amount.e8s = u64::MAX`. The governance canister explicitly does not enforce that the requested disburse amount is ≤ the neuron's cached stake:

> "Note that we don't enforce that 'amount' is actually smaller than or equal to the cached stake in the neuron." [5](#0-4) 

The governance canister proceeds to compute `disburse_amount_e8s` from the attacker-supplied `a.e8s` (the wrapped huge value), burns any pending neuron fees (Transfer 1, which mutates neuron state), then attempts Transfer 2 (the actual disburse) which the ICP ledger rejects due to insufficient funds. The comment in the governance code acknowledges this partial-execution risk:

> "Transfer 2 - Disburse to the chosen account. This may fail if the user told us to disburse more than they had in their account (but the burn still happened)." [6](#0-5) 

The concrete impact is: the Rosetta API's amount validation is completely bypassed for any negative input, allowing an attacker to submit governance disburse commands with arbitrary `u64` amounts. The neuron's fee-burn state mutation (Transfer 1) executes before the ledger rejects Transfer 2, meaning the neuron's `cached_neuron_stake_e8s` is decremented by `fees_amount_e8s` even on a failed disburse triggered by the malformed amount. [7](#0-6) 

---

### Likelihood Explanation

The Rosetta API is a publicly accessible HTTP service. Any unprivileged caller can submit a `construction/payloads` request with a negative amount string (e.g., `"-1"`). The `from_amount` function explicitly parses the value as `i128` and only rejects values whose absolute value exceeds `u64::MAX`, so any value in the range `[-u64::MAX, -1]` passes validation and wraps to a large positive `u64`. No authentication or special privilege is required to reach this code path.

---

### Recommendation

1. In `ledgeramount_from_amount`, reject negative values before casting:
   ```rust
   pub fn ledgeramount_from_amount(amount: &Amount, token_name: &str) -> Result<Tokens, String> {
       let inner = from_amount(amount, token_name)?;
       if inner < 0 {
           return Err(format!("Amount must be non-negative, got {inner}"));
       }
       Ok(Tokens::from_e8s(inner as u64))
   }
   ```
2. In the `Fee` handler, validate that `amount` is negative (as the Rosetta standard requires for fees) before negating and casting, and reject positive fee amounts explicitly.
3. Consider replacing all `as u64` casts on `i128` values with `u64::try_from(inner).map_err(...)` to make the conversion failure explicit and auditable.

---

### Proof of Concept

1. Start the ICP Rosetta API server.
2. Call `POST /construction/payloads` with a `Disburse` operation whose `amount.value` is `"-1"` and `amount.currency` is `{"symbol":"ICP","decimals":8}`.
3. `from_amount` parses `val = -1_i128`; `val.abs() = 1` fits in `u64` → validation passes, returns `Ok(-1)`.
4. `ledgeramount_from_amount` computes `-1_i128 as u64 = 18446744073709551615` (= `u64::MAX`).
5. The Rosetta API constructs `manage_neuron::Disburse { amount: Some(Amount { e8s: 18446744073709551615 }), ... }`.
6. This is submitted to the NNS governance canister as a valid ingress message.
7. The governance canister computes `disburse_amount_e8s ≈ u64::MAX - fees`, burns fees (mutating neuron state), then attempts to transfer `u64::MAX - fees` tokens — which the ICP ledger rejects. [8](#0-7) [3](#0-2) [9](#0-8)

### Citations

**File:** rs/rosetta-api/icp/src/models/amount.rs (L23-44)
```rust
pub fn from_amount(amount: &Amount, token_name: &str) -> Result<i128, String> {
    let cur = Currency::new(token_name.into(), DECIMAL_PLACES);
    match amount {
        Amount {
            value,
            currency,
            metadata: None,
        } if currency == &cur => {
            let val: i128 = value
                .parse()
                .map_err(|e| format!("Parsing amount failed: {e}"))?;
            let _ =
                u64::try_from(val.abs()).map_err(|_| "Amount does not fit in u64".to_string())?;
            Ok(val)
        }
        wrong => Err(format!("This value is not {token_name} {wrong:?}")),
    }
}

pub fn ledgeramount_from_amount(amount: &Amount, token_name: &str) -> Result<Tokens, String> {
    let inner = from_amount(amount, token_name)?;
    Ok(Tokens::from_e8s(inner as u64))
```

**File:** rs/rosetta-api/icp/src/convert.rs (L188-188)
```rust
                state.fee(account, Tokens::from_e8s((-amount) as u64))?;
```

**File:** rs/rosetta-api/icp/src/convert.rs (L258-270)
```rust
                let amount = if let Some(ref amount) = o.amount {
                    Some(ledgeramount_from_amount(amount, token_name).map_err(|e| {
                        let err_msg = format!(
                            "Disburse - Could not convert amount (value: {}, currency: {:?}): {e:?}",
                            amount.value, amount.currency
                        );
                        debug!("{}", err_msg);
                        ApiError::internal_error(err_msg)
                    })?)
                } else {
                    None
                };
                state.disburse(account, neuron_index, amount, recipient)?;
```

**File:** rs/nns/governance/src/governance.rs (L1929-1935)
```rust
    /// Note that we don't enforce that 'amount' is actually smaller
    /// than or equal to the cached stake in the neuron.
    /// This will allow a user to still disburse funds if:
    /// - Someone transferred more funds to the neuron's subaccount after the
    ///   the initial neuron claim that we didn't know about.
    /// - The transfer of funds previously failed for some reason (e.g. the
    ///   ledger was unavailable or broken).
```

**File:** rs/nns/governance/src/governance.rs (L2016-2027)
```rust
        let mut disburse_amount_e8s = disburse
            .amount
            .as_ref()
            .map_or(neuron_minted_stake_e8s, |a| {
                a.e8s.saturating_sub(fees_amount_e8s)
            });

        // Subtract the transaction fee from the amount to disburse since it'll
        // be deducted from the source (the neuron's) account.
        if disburse_amount_e8s > transaction_fee_e8s {
            disburse_amount_e8s -= transaction_fee_e8s
        }
```

**File:** rs/nns/governance/src/governance.rs (L2067-2076)
```rust
        self.with_neuron_mut(id, |neuron| {
            // Update the stake and the fees to reflect the burning above.
            if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
                neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
            } else {
                neuron.cached_neuron_stake_e8s = 0;
            }
            neuron.neuron_fees_e8s = 0;
        })
        .expect("Expected the parent neuron to exist");
```

**File:** rs/nns/governance/src/governance.rs (L2078-2080)
```rust
        // Transfer 2 - Disburse to the chosen account. This may fail if the
        // user told us to disburse more than they had in their account (but
        // the burn still happened).
```
