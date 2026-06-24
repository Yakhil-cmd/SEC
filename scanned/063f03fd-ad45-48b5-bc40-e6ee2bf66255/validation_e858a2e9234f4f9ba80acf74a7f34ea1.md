The code is confirmed. Let me now check the U256 `try_from` implementation and the `max_value` method to complete the analysis.

### Title
Silent Allowance Capping on Out-of-Range `amount` in `icrc2_approve_not_async` — (`rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

`icrc2_approve_not_async` silently caps any `amount` exceeding `Tokens::max_value()` to `Tokens::max_value()` instead of returning an error. An unprivileged caller can submit `amount = Nat(u64::MAX + 1)` (or any value above the token type's maximum), receive a successful `Ok(block_idx)` response, and have an allowance of `Tokens::max_value()` recorded on-chain — without any indication that the stored value differs from the submitted value.

---

### Finding Description

In `icrc2_approve_not_async`, the conversion of the caller-supplied `amount` is:

```rust
let amount = Tokens::try_from(arg.amount).unwrap_or_else(|_| Tokens::max_value());
``` [1](#0-0) 

`Tokens::try_from` for the u64 token type (`U64`, used by ckBTC and the ICP ICRC-1 ledger) calls `to_u64()` on the underlying `BigUint` and returns `Err` for any value exceeding `u64::MAX`:

```rust
fn try_from(value: Nat) -> Result<Self, Self::Error> {
    match value.0.to_u64() {
        Some(e8s) => Ok(Self { e8s }),
        None => Err(format!("value {value} is bigger than Tokens::max_value()")),
    }
}
``` [2](#0-1) 

When `try_from` returns `Err`, the `unwrap_or_else` silently substitutes `Tokens::max_value()` (`u64::MAX` e8s ≈ 1.84 × 10¹¹ tokens). The capped value is then written directly into the `Operation::Approve` block and stored as the allowance:

```rust
let tx = Transaction {
    operation: Operation::Approve {
        from: from_account,
        spender: arg.spender,
        amount,   // ← capped value, not the submitted value
        ...
    },
    ...
};
``` [3](#0-2) 

The same silent-cap pattern exists in the ICP ledger:

```rust
let allowance = Tokens::from_e8s(arg.amount.0.to_u64().unwrap_or(u64::MAX));
``` [4](#0-3) 

This is **inconsistent** with how `icrc_transfer` handles the same overflow: the transfer path explicitly returns `InsufficientFunds` when `amount > Tokens::max_value()`, reasoning that "no one can have so many tokens":

```rust
Err(_) => {
    // No one can have so many tokens
    let balance_tokens = ledger.balances().account_balance(&from_account);
    ...
    return Err(CoreTransferError::InsufficientFunds { balance: balance_tokens });
}
``` [5](#0-4) 

---

### Impact Explanation

An approver who submits `amount = Nat(u64::MAX + 1)` — expecting either an error or an "unlimited" semantic — instead silently grants an allowance of `u64::MAX` e8s. The call returns `Ok(block_idx)`, giving no indication that the stored allowance differs from the submitted value. A spender holding that allowance can immediately call `icrc2_transfer_from` and drain up to the approver's full balance. The approver has no on-chain signal that anything unexpected occurred; the block records only the capped value.

---

### Likelihood Explanation

The trigger requires the caller to submit a `Nat` larger than `u64::MAX`. This is realistic in several scenarios:

- A wallet or dApp UI that uses `u128::MAX` or `2^256` as a conventional "unlimited" sentinel.
- A buggy client that computes amounts in a different denomination (e.g., wei-scale) and overflows the u64 range.
- A malicious frontend that deliberately submits an oversized value to silently grant a max allowance.

The ICRC-2 standard defines `amount` as an unbounded `nat`, so callers have no type-level reason to expect the ledger to reject large values. The inconsistency with `icrc_transfer` (which returns an error) makes the behavior surprising and undocumented.

---

### Recommendation

Replace the silent cap with an explicit error return, consistent with the transfer path:

```rust
let amount = Tokens::try_from(arg.amount).map_err(|_| ApproveError::GenericError {
    error_code: Nat::from(0_u64),
    message: "amount exceeds the maximum token value".to_string(),
})?;
```

Apply the same fix to the ICP ledger at `rs/ledger_suite/icp/ledger/src/main.rs` line 1350.

---

### Proof of Concept

1. Deploy a u64-token ICRC-1 ledger (e.g., ckBTC configuration).
2. Mint any non-zero balance to `approver`.
3. Call `icrc2_approve` from `approver` with `amount = Nat(u64::MAX) + 1` and `fee = None`.
4. Assert the call returns `Ok(block_idx)` — it does.
5. Call `icrc2_allowance { account: approver, spender }`.
6. Assert `allowance == Nat(u64::MAX)` — it does, not the submitted value.
7. Call `icrc2_transfer_from` from `spender` with `amount = approver_balance`.
8. Assert the transfer succeeds and `approver`'s balance is zero.

The invariant "stored allowance equals submitted amount, or the call returns an error" is violated at step 4/6.

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L593-603)
```rust
        let amount = match Tokens::try_from(amount.clone()) {
            Ok(n) => n,
            Err(_) => {
                // No one can have so many tokens
                let balance_tokens = ledger.balances().account_balance(&from_account);
                let balance = Nat::from(balance_tokens);
                assert!(balance < amount);
                return Err(CoreTransferError::InsufficientFunds {
                    balance: balance_tokens,
                });
            }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L840-840)
```rust
        let amount = Tokens::try_from(arg.amount).unwrap_or_else(|_| Tokens::max_value());
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L863-874)
```rust
        let tx = Transaction {
            operation: Operation::Approve {
                from: from_account,
                spender: arg.spender,
                amount,
                expected_allowance,
                expires_at: arg.expires_at,
                fee: arg.fee.map(|_| expected_fee_tokens),
            },
            created_at_time: arg.created_at_time,
            memo: arg.memo,
        };
```

**File:** rs/ledger_suite/common/ledger_core/src/tokens.rs (L288-296)
```rust
impl TryFrom<Nat> for Tokens {
    type Error = String;

    fn try_from(value: Nat) -> Result<Self, Self::Error> {
        match value.0.to_u64() {
            Some(e8s) => Ok(Self { e8s }),
            None => Err(format!("value {value} is bigger than Tokens::max_value()")),
        }
    }
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1350-1350)
```rust
    let allowance = Tokens::from_e8s(arg.amount.0.to_u64().unwrap_or(u64::MAX));
```
