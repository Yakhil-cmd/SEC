### Title
Unchecked Signed-to-Unsigned Cast Produces Phantom Large Token Amount - (File: rs/rosetta-api/icp/src/models/amount.rs)

### Summary
In the ICP Rosetta API, `ledgeramount_from_amount` converts an `i128` amount to `Tokens` via an unchecked `as u64` cast. The upstream validator `from_amount` only checks that `val.abs()` fits in `u64` — it does not reject negative values. A negative `i128` cast to `u64` in Rust wraps two's-complement, producing a phantom astronomically large token amount, directly mirroring the `uint256(-amount)` overflow in the reference report.

### Finding Description

`from_amount` parses a user-supplied string into `i128`, validates only that the absolute value fits in `u64`, and returns the signed value unchanged: [1](#0-0) 

```rust
let val: i128 = value.parse()...;
let _ = u64::try_from(val.abs()).map_err(|_| "Amount does not fit in u64"...)?;
Ok(val)   // val can be negative — no sign check
```

`ledgeramount_from_amount` then blindly casts the returned `i128` to `u64`: [2](#0-1) 

```rust
pub fn ledgeramount_from_amount(amount: &Amount, token_name: &str) -> Result<Tokens, String> {
    let inner = from_amount(amount, token_name)?;
    Ok(Tokens::from_e8s(inner as u64))   // inner can be negative → wraps
}
```

In Rust, `as` casts from signed to unsigned are defined as two's-complement truncation. For example:

```
(-1000_i128) as u64 == 18_446_744_073_709_550_616
```

This creates a `Tokens` object representing ≈184 billion ICP from a user-supplied `-1000` string.

The same pattern appears in `State::transaction` for the debit branch: [3](#0-2) 

```rust
self.debit = Some(AccountTokens {
    account,
    tokens: Tokens::from_e8s((-amount) as u64),
});
```

If `amount == i128::MIN`, then `-amount` overflows `i128` (undefined behavior in debug builds, wraps in release), and the subsequent `as u64` cast produces a wrong value.

### Impact Explanation

An unprivileged caller of the ICP Rosetta API's `/construction/parse` or `/construction/payloads` endpoints can supply a negative `value` string in the `Amount` field. The Rosetta API will construct a `Tokens` object with a phantom large e8s value. Downstream logic that trusts this constructed amount — including balance checks, fee calculations, and transaction submission — will operate on a corrupted token quantity. While the ICP ledger canister itself will reject a submitted transaction that exceeds the sender's balance, the Rosetta API layer will silently produce and propagate the malformed amount, potentially causing incorrect balance reporting, failed-but-misleading construction responses, and confusion in exchange integrations that rely on the Rosetta API as a trusted intermediary.

### Likelihood Explanation

The Rosetta API is a publicly reachable HTTP service. No authentication is required to call `/construction/parse`. The attacker only needs to supply a JSON body with a negative `value` string (e.g., `"-1000"`) in the `Amount` object. The `from_amount` validator explicitly accepts negative values (it is designed to represent debits), so the negative input passes validation and reaches the unsafe cast. Likelihood is **high** for any deployment of the ICP Rosetta API.

### Recommendation

In `ledgeramount_from_amount`, add an explicit non-negativity check before the cast:

```rust
pub fn ledgeramount_from_amount(amount: &Amount, token_name: &str) -> Result<Tokens, String> {
    let inner = from_amount(amount, token_name)?;
    if inner < 0 {
        return Err(format!("Amount must be non-negative, got {inner}"));
    }
    Ok(Tokens::from_e8s(inner as u64))
}
```

In `State::transaction`, guard against `i128::MIN` before negation:

```rust
let abs_amount = amount.checked_neg()
    .ok_or_else(|| ApiError::InvalidTransaction(false, "Amount overflow".into()))?;
tokens: Tokens::from_e8s(abs_amount as u64),
```

### Proof of Concept

1. Start the ICP Rosetta API pointed at any ICP node.
2. POST to `/construction/parse` with:
```json
{
  "network_identifier": { ... },
  "signed": false,
  "transaction": "...",
  "operations": [{
    "operation_identifier": {"index": 0},
    "type": "TRANSACTION",
    "account": { "address": "..." },
    "amount": { "value": "-1000", "currency": {"symbol":"ICP","decimals":8} }
  }]
}
```
3. The call reaches `ledgeramount_from_amount` → `from_amount` returns `-1000_i128` → `(-1000_i128) as u64 == 18_446_744_073_709_550_616` → `Tokens::from_e8s(18_446_744_073_709_550_616)` is constructed and used in subsequent logic. [4](#0-3) [5](#0-4)

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

**File:** rs/rosetta-api/icp/src/convert/state.rs (L106-129)
```rust
    pub fn transaction(
        &mut self,
        account: icp_ledger::AccountIdentifier,
        amount: i128,
    ) -> Result<(), ApiError> {
        if amount > 0 || self.debit.is_some() && amount == 0 {
            if self.credit.is_some() {
                self.flush()?;
            }
            self.credit = Some(AccountTokens {
                account,
                tokens: Tokens::from_e8s(amount as u64),
            });
        } else {
            if self.debit.is_some() {
                self.flush()?;
            }
            self.debit = Some(AccountTokens {
                account,
                tokens: Tokens::from_e8s((-amount) as u64),
            });
        }
        Ok(())
    }
```
