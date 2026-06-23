### Title
Negative `i128` Amount Silently Cast to `u64` in Rosetta API Token Conversion - (File: `rs/rosetta-api/icp/src/models/amount.rs`)

### Summary
In the ICP Rosetta API, `ledgeramount_from_amount` calls `from_amount` which returns a signed `i128` that can be negative, then unconditionally casts it to `u64` via `inner as u64`. In Rust, casting a negative `i128` to `u64` wraps around (two's complement), producing an astronomically large token amount — a direct analog to the reported Solidity `int256 → uint256` cast bug.

### Finding Description

`from_amount` parses the Rosetta `Amount.value` string into an `i128` and only validates that `val.abs()` fits in `u64`, but it returns the signed value — which can be negative: [1](#0-0) 

`ledgeramount_from_amount` then calls `from_amount` and blindly casts the result to `u64`: [2](#0-1) 

In Rust, `(-100_i128) as u64` evaluates to `18446744073709551516` — a wrap-around to near `u64::MAX`. This produces a `Tokens` value of ~184 billion ICP, which is then used in ledger operations.

By contrast, the `State::transaction` method in `convert/state.rs` correctly handles the sign before casting: [3](#0-2) 

It checks `amount > 0` before casting to `u64` for credits, and uses `(-amount) as u64` for debits. `ledgeramount_from_amount` has no such guard.

### Impact Explanation

An unprivileged caller submitting a Rosetta `/construction/payloads` or `/construction/preprocess` request with a negative credit amount (e.g., `"-100"`) causes the Rosetta node to construct a ledger transfer for `18446744073709551516` e8s. The ICP ledger will reject the transaction due to insufficient balance, but the Rosetta node may surface a misleading error. More critically, if `ledgeramount_from_amount` is used in any path that does not subsequently validate the resulting `Tokens` value against the sender's balance before submission, the incorrect amount silently propagates through the construction pipeline, corrupting the unsigned transaction payload returned to the caller. Exchanges or wallets relying on the Rosetta API to construct transactions could receive a malformed payload without any explicit error.

### Likelihood Explanation

The Rosetta API is a public HTTP endpoint. Any unprivileged user can submit a JSON body with a negative `value` string in an `Amount` object. The `from_amount` function explicitly allows negative values (it is designed to represent debits), and `ledgeramount_from_amount` does not guard against them. No special privileges, keys, or majority corruption are required.

### Recommendation

Add a sign check in `ledgeramount_from_amount` before casting:

```rust
pub fn ledgeramount_from_amount(amount: &Amount, token_name: &str) -> Result<Tokens, String> {
    let inner = from_amount(amount, token_name)?;
    if inner < 0 {
        return Err(format!("Amount must be non-negative, got {inner}"));
    }
    Ok(Tokens::from_e8s(inner as u64))
}
```

Alternatively, use `u64::try_from(inner)` which returns an error for negative values, rather than the silent wrapping `as u64` cast.

### Proof of Concept

1. Submit a Rosetta `/construction/payloads` request with a credit operation containing `"value": "-100"`.
2. `from_amount` parses `-100_i128`, checks `u64::try_from(100_i128)` (passes), and returns `-100_i128`.
3. `ledgeramount_from_amount` computes `(-100_i128) as u64 = 18446744073709551516`.
4. `Tokens::from_e8s(18446744073709551516)` is used to construct the transfer.
5. The resulting unsigned transaction encodes a transfer of `184,467,440,737.09551516` ICP — a value that wraps silently with no error from the Rosetta node's construction layer. [2](#0-1) [4](#0-3)

### Citations

**File:** rs/rosetta-api/icp/src/models/amount.rs (L23-36)
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
```

**File:** rs/rosetta-api/icp/src/models/amount.rs (L42-44)
```rust
pub fn ledgeramount_from_amount(amount: &Amount, token_name: &str) -> Result<Tokens, String> {
    let inner = from_amount(amount, token_name)?;
    Ok(Tokens::from_e8s(inner as u64))
```

**File:** rs/rosetta-api/icp/src/convert/state.rs (L111-127)
```rust
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
```
