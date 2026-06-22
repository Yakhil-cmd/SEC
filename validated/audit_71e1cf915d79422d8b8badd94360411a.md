The actual code is available. Let me verify the exact arithmetic claim before rendering a verdict.

The arithmetic claim is exactly correct. Let me trace it precisely:

**Step 1 — `from_amount` with `value = '-18446744073709551615'`:**
- `val = -18446744073709551615_i128` (parses fine; i128 range is ≈ ±1.7×10³⁸)
- `val.abs() = 18446744073709551615_i128` = `u64::MAX`
- `u64::try_from(u64::MAX as i128)` → `Ok(...)` — **the guard passes**
- Returns `Ok(-18446744073709551615_i128)`

**Step 2 — `ledgeramount_from_amount` cast:**
- `inner as u64` where `inner = -18446744073709551615_i128`
- Two's-complement lower-64-bit truncation: `2¹²⁸ − (2⁶⁴−1)` mod `2⁶⁴` = **1**
- Returns `Tokens::from_e8s(1)`

The bug is real. `from_amount` is intentionally signed (it is also used for debit legs of transfers), but `ledgeramount_from_amount` is supposed to produce a non-negative `Tokens` value and never checks the sign before the `as u64` cast.

---

### Title
Silent negative-amount wrap in `ledgeramount_from_amount` produces `Tokens::from_e8s(1)` for Disburse operations — (`rs/rosetta-api/icp/src/models/amount.rs`)

### Summary
`ledgeramount_from_amount` delegates to `from_amount`, which validates only that `val.abs()` fits in `u64` but returns the original signed value. The subsequent `inner as u64` cast silently wraps `-18446744073709551615_i128` to `1`, so the Rosetta construction pipeline produces a governance Disburse payload for 1 e8s instead of returning an error.

### Finding Description
`from_amount` is a shared helper used for both signed transfer legs (where negative values are legitimate) and for `ledgeramount_from_amount`, which is supposed to produce a strictly non-negative `Tokens` amount. [1](#0-0) 

The guard on line 35 checks `val.abs()` fits in `u64`, which is true for `-18446744073709551615` (its absolute value is exactly `u64::MAX`). The function then returns the negative `i128` unchanged. [2](#0-1) 

`ledgeramount_from_amount` performs `inner as u64` with no sign check. Rust's semantics for `i128 as u64` truncate to the low 64 bits, wrapping `-18446744073709551615` to `1`.

This value is then used directly in the Disburse construction path: [3](#0-2) 

### Impact Explanation
The Rosetta construction server silently substitutes the caller-supplied large negative amount with `Tokens::from_e8s(1)` and constructs a governance Disburse payload for 1 e8s. No error is returned to the caller. Any client or service that relies on the Rosetta server to validate amounts before signing will sign and submit a transaction for the wrong amount. The impact is bounded to the neuron owner's own funds (the caller must own or have hotkey access to the neuron and must sign the resulting payload), so cross-user fund theft is not possible. The concrete harm is incorrect transaction construction leading to an unintended 1 e8s disburse instead of a validation error.

### Likelihood Explanation
The path is fully reachable via an unauthenticated HTTP POST to `/construction/payloads` with a crafted Disburse operation. No privileged role is required. The specific value `-18446744073709551615` is the only i128 value that passes the `val.abs()` ≤ `u64::MAX` guard while being negative and wrapping to a non-zero result, making it a targeted but mechanically simple input.

### Recommendation
Add an explicit sign check in `ledgeramount_from_amount` before the cast:

```rust
pub fn ledgeramount_from_amount(amount: &Amount, token_name: &str) -> Result<Tokens, String> {
    let inner = from_amount(amount, token_name)?;
    if inner < 0 {
        return Err(format!("Amount must be non-negative, got {inner}"));
    }
    Ok(Tokens::from_e8s(inner as u64))
}
```

Alternatively, `from_amount` could be split into a signed variant (for transfer debit legs) and an unsigned variant (for Disburse/Stake amounts), eliminating the shared-but-differently-constrained interface.

### Proof of Concept
```rust
#[test]
fn ledgeramount_from_amount_rejects_negative_u64_max() {
    use crate::models::{Amount, Currency};
    use ic_ledger_core::tokens::DECIMAL_PLACES;

    let amount = Amount {
        value: "-18446744073709551615".to_string(),
        currency: Currency::new("ICP".into(), DECIMAL_PLACES),
        metadata: None,
    };
    // Currently returns Ok(Tokens::from_e8s(1)) — should return Err
    let result = ledgeramount_from_amount(&amount, "ICP");
    assert!(result.is_err(), "expected Err, got {:?}", result);
}
```

### Citations

**File:** rs/rosetta-api/icp/src/models/amount.rs (L34-36)
```rust
            let _ =
                u64::try_from(val.abs()).map_err(|_| "Amount does not fit in u64".to_string())?;
            Ok(val)
```

**File:** rs/rosetta-api/icp/src/models/amount.rs (L42-44)
```rust
pub fn ledgeramount_from_amount(amount: &Amount, token_name: &str) -> Result<Tokens, String> {
    let inner = from_amount(amount, token_name)?;
    Ok(Tokens::from_e8s(inner as u64))
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
