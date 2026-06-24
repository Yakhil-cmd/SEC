The code matches the claim exactly. The arithmetic is verified:

- `val.abs()` for `-18446744073709551615_i128` = `18446744073709551615` = `u64::MAX`, so `u64::try_from` succeeds and the guard passes. [1](#0-0) 
- `ledgeramount_from_amount` performs `inner as u64` with no sign check, wrapping `-18446744073709551615_i128` to `1u64`. [2](#0-1) 
- The result flows directly into the Disburse construction path. [3](#0-2) 

---

Audit Report

## Title
Silent negative-amount wrap in `ledgeramount_from_amount` produces `Tokens::from_e8s(1)` for Disburse operations — (`rs/rosetta-api/icp/src/models/amount.rs`)

## Summary
`from_amount` validates only that `val.abs()` fits in `u64`, allowing the value `-18446744073709551615` (whose absolute value is exactly `u64::MAX`) to pass the guard and return a negative `i128`. `ledgeramount_from_amount` then casts this directly to `u64` via `inner as u64`, which wraps to `1` by two's-complement truncation. The Rosetta construction pipeline silently produces a governance Disburse payload for 1 e8s instead of returning an error.

## Finding Description
`from_amount` (L23–40) parses the string value as `i128`, then checks `u64::try_from(val.abs())`. For `value = "-18446744073709551615"`, `val.abs()` equals `u64::MAX = 18446744073709551615`, so `u64::try_from` succeeds and the function returns `Ok(-18446744073709551615_i128)`. `ledgeramount_from_amount` (L42–44) calls `from_amount` and immediately casts the result with `inner as u64`. Rust's `as` cast for `i128 → u64` takes the low 64 bits: `(-18446744073709551615_i128) mod 2^64 = 2^64 − (2^64 − 1) = 1`. The resulting `Tokens::from_e8s(1)` is passed without error into `state.disburse(...)` at `convert.rs` L270. No sign check exists anywhere in this path.

## Impact Explanation
The Rosetta API is explicitly in scope under financial integrations. The bug causes the construction server to silently substitute a caller-supplied large negative amount with `Tokens::from_e8s(1)` and return a well-formed, signable Disburse payload. Any client or automated service relying on Rosetta to validate amounts before signing will receive no error and may sign and submit a transaction disbursing 1 e8s instead of the intended amount. Impact is bounded to the neuron owner's own funds (the caller must own or hold hotkey access to the neuron and must sign the payload), so cross-user theft is not possible. This constitutes a significant Rosetta API security impact with concrete user harm, fitting the **High** severity band.

## Likelihood Explanation
The path is reachable via an unauthenticated HTTP POST to `/construction/payloads` with a crafted Disburse operation containing `value: "-18446744073709551615"`. No privileged role is required to reach `ledgeramount_from_amount`. The specific input is the unique `i128` value that passes the `val.abs() ≤ u64::MAX` guard while being negative and wrapping to a non-zero result, making it a targeted but mechanically trivial input requiring no special infrastructure.

## Recommendation
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

Alternatively, split `from_amount` into a signed variant (for transfer debit legs) and an unsigned variant (for Disburse/Stake amounts) to eliminate the shared-but-differently-constrained interface.

## Proof of Concept
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
