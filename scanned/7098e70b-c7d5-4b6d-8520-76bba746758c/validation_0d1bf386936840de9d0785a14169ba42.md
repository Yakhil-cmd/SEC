The critical question is whether `fee=None` and `fee=Some(F)` produce different transaction hashes, bypassing deduplication. Let me trace the exact code path.

### Title
ICRC-2 Approve Deduplication Bypass via `fee=None` vs `fee=Some(F)` Hash Divergence — (`rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

An unprivileged caller can submit two `icrc2_approve` calls with identical semantic intent but differing only in whether `fee` is `None` or `Some(F)`. Because the `fee` field is included verbatim in the CBOR-serialized transaction hash, the two calls produce distinct hashes and both pass the deduplication guard, causing the approver's balance to be debited twice (2×F) for a single logical approve operation.

---

### Finding Description

**Step 1 — Fee validation is permissive for both variants.**

In `icrc2_approve_not_async`:

```rust
// rs/ledger_suite/icrc1/ledger/src/main.rs:859-861
if arg.fee.is_some() && arg.fee.as_ref() != Some(&expected_fee) {
    return Err(ApproveError::BadFee { expected_fee });
}
```

`fee=None` passes (the `is_some()` guard short-circuits). `fee=Some(F)` where `F == expected_fee` also passes. Both are valid inputs. [1](#0-0) 

**Step 2 — The `fee` field is mapped directly into the `Operation::Approve` struct.**

```rust
// rs/ledger_suite/icrc1/ledger/src/main.rs:863-874
let tx = Transaction {
    operation: Operation::Approve {
        ...
        fee: arg.fee.map(|_| expected_fee_tokens),  // None stays None; Some(_) becomes Some(F)
    },
    ...
};
```

`fee=None` → `Operation::Approve { fee: None }`. `fee=Some(F)` → `Operation::Approve { fee: Some(expected_fee_tokens) }`. These are structurally different values. [2](#0-1) 

**Step 3 — The hash is computed over the CBOR-serialized `FlattenedTransaction`, which omits `None` fields.**

```rust
// rs/ledger_suite/icrc1/src/lib.rs:116-118
#[serde(skip_serializing_if = "Option::is_none")]
fee: Option<Tokens>,
```

`fee=None` → the `fee` key is absent from the CBOR map → hash H₁. `fee=Some(F)` → the `fee` key is present with value F → hash H₂. H₁ ≠ H₂. [3](#0-2) 

The hash function itself: [4](#0-3) 

**Step 4 — Deduplication checks only the hash.**

```rust
// rs/ledger_suite/common/ledger_canister_core/src/ledger.rs:249-253
if let Some(block_height) = ledger.transactions_by_hash().get(&tx_hash) {
    return Err(TransferError::TxDuplicate { duplicate_of: *block_height });
}
```

Since H₁ ≠ H₂, the second call is not recognized as a duplicate and proceeds. [5](#0-4) 

**Step 5 — Both calls debit the fee.**

```rust
// rs/ledger_suite/icrc1/src/lib.rs:537-539
context.balances_mut().burn(from, fee.clone().unwrap_or(effective_fee.clone()))?;
```

For `fee=None`: burns `effective_fee` (= F). For `fee=Some(F)`: burns F. Both debit F. Net result: 2×F drained. [6](#0-5) 

---

### Impact Explanation

- **Double fee drain**: The approver loses 2×F instead of F for a single logical approve.
- **Allowance set twice**: The allowance is overwritten to the same value twice (no net difference on allowance state, but two blocks are recorded).
- **Scope**: Any ICRC-2 ledger deploying this code. No privileged access required — any token holder with sufficient balance can trigger this against themselves or be tricked into it.

---

### Likelihood Explanation

The attack is trivially executable by any caller: submit the same `ApproveArgs` twice, once with `fee=None` and once with `fee=Some(F)`, using the same `created_at_time`. Both calls succeed. The only precondition is having balance ≥ 2×F. This is locally testable in a state-machine test with no external dependencies.

---

### Recommendation

Normalize the `fee` field before computing the transaction hash. Specifically, in `icrc2_approve_not_async`, always set `fee: Some(expected_fee_tokens)` in the constructed `Operation::Approve`, regardless of whether `arg.fee` was `None` or `Some(F)`. This ensures both input variants produce the same transaction hash and the second call is correctly rejected as a duplicate.

---

### Proof of Concept

```rust
// Pseudocode state-machine test
let fee = ledger_fee(); // e.g., 10_000
let initial_balance = 3 * fee;
mint(approver, initial_balance);

let base_args = ApproveArgs {
    spender, amount, expires_at: None, expected_allowance: None,
    memo: None, created_at_time: Some(fixed_timestamp),
    from_subaccount: None,
};

// Call 1: fee=None
let r1 = icrc2_approve(approver, ApproveArgs { fee: None, ..base_args.clone() });
assert!(r1.is_ok());

// Call 2: fee=Some(F) — same created_at_time, should be deduplicated but isn't
let r2 = icrc2_approve(approver, ApproveArgs { fee: Some(Nat::from(fee)), ..base_args });
assert!(r2.is_ok()); // BUG: should return Err(Duplicate{...})

// Balance drained by 2*F instead of F
assert_eq!(balance_of(approver), initial_balance - 2 * fee);
```

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L859-861)
```rust
        if arg.fee.is_some() && arg.fee.as_ref() != Some(&expected_fee) {
            return Err(ApproveError::BadFee { expected_fee });
        }
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

**File:** rs/ledger_suite/icrc1/src/lib.rs (L116-118)
```rust
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    fee: Option<Tokens>,
```

**File:** rs/ledger_suite/icrc1/src/lib.rs (L432-445)
```rust
    fn hash(&self) -> HashOf<Self> {
        let mut cbor_bytes = vec![];
        ciborium::ser::into_writer(self, &mut cbor_bytes)
            .expect("bug: failed to encode a transaction");
        hash::hash_cbor(&cbor_bytes)
            .map(HashOf::new)
            .unwrap_or_else(|err| {
                panic!(
                    "bug: transaction CBOR {} is not hashable: {}",
                    hex::encode(&cbor_bytes),
                    err
                )
            })
    }
```

**File:** rs/ledger_suite/icrc1/src/lib.rs (L537-539)
```rust
                context
                    .balances_mut()
                    .burn(from, fee.clone().unwrap_or(effective_fee.clone()))?;
```

**File:** rs/ledger_suite/common/ledger_canister_core/src/ledger.rs (L249-253)
```rust
        if let Some(block_height) = ledger.transactions_by_hash().get(&tx_hash) {
            return Err(TransferError::TxDuplicate {
                duplicate_of: *block_height,
            });
        }
```
