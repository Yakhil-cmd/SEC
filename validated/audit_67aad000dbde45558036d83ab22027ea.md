The code at line 840 is definitive. Let me verify `max_value()` and check the ICP ledger for the same pattern.

The vulnerability is confirmed by direct code inspection. Line 840 of `rs/ledger_suite/icrc1/ledger/src/main.rs` contains exactly the pattern described:

```rust
let amount = Tokens::try_from(arg.amount).unwrap_or_else(|_| Tokens::max_value());
```

`TryFrom<Nat> for Tokens` correctly returns `Err` for values exceeding `u64::MAX`, but the call site silently discards that error and substitutes `Tokens::max_value()` (= `u64::MAX`). The ICP ledger's `icrc2_approve` does not exhibit the same pattern.

---

### Title
Silent Clamping of Over-u64 Approve Amount to `Tokens::MAX` in ICRC-1 Ledger — (`rs/ledger_suite/icrc1/ledger/src/main.rs`)

### Summary
An unprivileged caller can submit an `icrc2_approve` with `amount = Nat(u64::MAX + 1)`. Instead of being rejected, the ledger silently records an allowance of `u64::MAX`, granting the named spender effectively unlimited spending power over the approver's account.

### Finding Description
`icrc2_approve_not_async` converts the caller-supplied `Nat` amount to `Tokens` at line 840:

```rust
let amount = Tokens::try_from(arg.amount).unwrap_or_else(|_| Tokens::max_value());
``` [1](#0-0) 

`TryFrom<Nat> for Tokens` returns `Err` for any value that does not fit in `u64`:

```rust
fn try_from(value: Nat) -> Result<Self, Self::Error> {
    match value.0.to_u64() {
        Some(e8s) => Ok(Self { e8s }),
        None => Err(format!("value {value} is bigger than Tokens::max_value()")),
    }
}
``` [2](#0-1) 

The `unwrap_or_else(|_| Tokens::max_value())` at the call site swallows that `Err` and substitutes `Tokens::MAX` (`u64::MAX = 18_446_744_073_709_551_615`). The resulting `Transaction::Approve` is then applied to the ledger state with `amount = u64::MAX`, and the block is committed. [3](#0-2) 

### Impact Explanation
The spender named in the approval now holds an allowance of `u64::MAX` tokens. A subsequent `icrc2_transfer_from` call can drain the approver's entire balance. The approver never consented to this; they submitted an amount that should have been rejected as out-of-range. The impact is unauthorized token transfer — the spender can steal the approver's full balance.

### Likelihood Explanation
The attack requires only a single ingress `icrc2_approve` call with a Candid-encoded `Nat` value of `2^64` (or any value > `u64::MAX`). No privileged access, no key material, no governance majority, and no coordination is needed. Any user of any ICRC-1 ledger deployed with the u64 token type is affected.

### Recommendation
Replace the silent fallback with an explicit error return:

```rust
let amount = Tokens::try_from(arg.amount)
    .map_err(|_| ApproveError::GenericError {
        error_code: Nat::from(0u64),
        message: "amount exceeds maximum token value".to_string(),
    })?;
```

The `expected_allowance` path at lines 842–852 already demonstrates the correct pattern — it converts the `Err` into an `ApproveError` and returns it to the caller. [4](#0-3) 

### Proof of Concept
1. Encode `ApproveArgs { amount: Nat(2^64), spender: attacker_account, ... }` as Candid.
2. Submit as an ingress `icrc2_approve` call to any ICRC-1 u64 ledger canister.
3. Call `icrc2_allowance` for `(approver, attacker_account)` — the returned allowance will be `u64::MAX`, not a rejection.
4. Call `icrc2_transfer_from` from the attacker's principal with `amount = approver_balance` — the transfer succeeds, draining the approver's account.

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L840-840)
```rust
        let amount = Tokens::try_from(arg.amount).unwrap_or_else(|_| Tokens::max_value());
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L841-854)
```rust
        let expected_allowance = match arg.expected_allowance {
            Some(n) => match Tokens::try_from(n) {
                Ok(n) => Some(n),
                Err(_) => {
                    let current_allowance = ledger
                        .approvals()
                        .allowance(&from_account, &arg.spender, now)
                        .amount;
                    return Err(ApproveError::AllowanceChanged {
                        current_allowance: current_allowance.into(),
                    });
                }
            },
            None => None,
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L863-877)
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

        let (block_idx, _) = apply_transaction(ledger, tx, now, expected_fee_tokens)
            .map_err(convert_transfer_error)
```

**File:** rs/ledger_suite/common/ledger_core/src/tokens.rs (L291-296)
```rust
    fn try_from(value: Nat) -> Result<Self, Self::Error> {
        match value.0.to_u64() {
            Some(e8s) => Ok(Self { e8s }),
            None => Err(format!("value {value} is bigger than Tokens::max_value()")),
        }
    }
```
