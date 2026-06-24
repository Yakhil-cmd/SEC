### Title
Silent Capping of Oversized Approval Amounts to `Tokens::max_value()` / `u64::MAX` in ICRC-1 and ICP Ledgers - (File: `rs/ledger_suite/icrc1/ledger/src/main.rs`, `rs/ledger_suite/icp/ledger/src/main.rs`)

---

### Summary
Both the ICRC-1 ledger and the ICP ledger silently cap user-submitted `icrc2_approve` amounts that exceed the internal token type's maximum to `Tokens::max_value()` (or `u64::MAX`) instead of returning an error. A user who submits an oversized `amount` (valid as `nat`, the wire type) pays the approval fee and unknowingly grants the spender an effectively unlimited allowance — more than they intended.

---

### Finding Description
The ICRC-2 `ApproveArgs.amount` field is typed as `nat` (arbitrary-precision natural number) in both the Candid interface and the Rust types. Internally, both ledgers store token amounts as `u64`. When the submitted `amount` exceeds `u64::MAX`, neither ledger rejects the call; instead they silently substitute the maximum representable value:

**ICRC-1 ledger** (`rs/ledger_suite/icrc1/ledger/src/main.rs`, line 840):
```rust
let amount = Tokens::try_from(arg.amount).unwrap_or_else(|_| Tokens::max_value());
``` [1](#0-0) 

**ICP ledger** (`rs/ledger_suite/icp/ledger/src/main.rs`, line 1350):
```rust
let allowance = Tokens::from_e8s(arg.amount.0.to_u64().unwrap_or(u64::MAX));
``` [2](#0-1) 

In both cases the approval fee is charged and the block is written to the ledger chain, but the stored allowance is `Tokens::max_value()` / `u64::MAX` rather than the value the caller supplied. The `expected_allowance` guard only helps if the caller explicitly sets it; when `expected_allowance` is `None` (the common case) there is no protection.

The `ApproveArgs` type accepted over the wire: [3](#0-2) 

The Candid interface confirms `amount : nat` is unbounded: [4](#0-3) 

---

### Impact Explanation
Any ingress sender or canister caller can submit `icrc2_approve` with `amount = u64::MAX + 1` (a perfectly valid `nat`). The ledger:
1. Charges the approval fee from the caller's balance.
2. Records a block with `amount = u64::MAX` (not the submitted value).
3. Sets the spender's allowance to `u64::MAX` — effectively unlimited relative to any realistic balance.

The caller may believe the transaction failed or that a bounded allowance was set, while the spender silently holds an unlimited allowance. This is the direct IC analog of the Bebop finding: an unjustified `type(uint).max`-equivalent approval is granted without the account owner's informed consent.

---

### Likelihood Explanation
Moderate. The `amount` field is `nat` and programmatic callers (DeFi aggregators, SNS swap canisters, wallet SDKs) routinely compute allowance amounts arithmetically. An off-by-one or unit-conversion error that produces a value just above `u64::MAX` silently becomes an unlimited approval. The ckBTC and ckETH withdrawal flows explicitly encourage users to approve "a large amount" to avoid repeated approvals, increasing the chance of oversized values being submitted.

---

### Recommendation
Return `ApproveError::GenericError` (or a new `AmountTooLarge` variant) when the submitted `amount` cannot be represented in the ledger's internal token type, instead of silently capping it. Apply the same fix to the `expected_allowance` path for consistency.

```rust
// ICRC-1 ledger fix
let amount = Tokens::try_from(arg.amount)
    .map_err(|_| ApproveError::GenericError {
        error_code: Nat::from(1u64),
        message: "amount exceeds maximum token value".to_string(),
    })?;
```

---

### Proof of Concept
An unprivileged ingress sender submits:

```
icrc2_approve(record {
  spender  = record { owner = principal "ATTACKER" };
  amount   = 18_446_744_073_709_551_616;   // u64::MAX + 1, valid nat
  fee      = null;
  expires_at = null;
  expected_allowance = null;
  ...
})
```

The ICRC-1 ledger executes `Tokens::try_from(18_446_744_073_709_551_616)` which fails the `u64` conversion, falls through `unwrap_or_else(|_| Tokens::max_value())`, and stores allowance = `u64::MAX = 18_446_744_073_709_551_615`. [1](#0-0) 

The block is committed, the fee is deducted, and `ATTACKER` now holds a `u64::MAX` allowance — confirmed by `icrc2_allowance`. The caller receives `Ok(block_index)` with no indication that the stored allowance differs from the requested amount.

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L840-840)
```rust
        let amount = Tokens::try_from(arg.amount).unwrap_or_else(|_| Tokens::max_value());
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1350-1350)
```rust
    let allowance = Tokens::from_e8s(arg.amount.0.to_u64().unwrap_or(u64::MAX));
```

**File:** packages/icrc-ledger-types/src/icrc2/approve.rs (L12-27)
```rust
pub struct ApproveArgs {
    #[serde(default)]
    pub from_subaccount: Option<Subaccount>,
    pub spender: Account,
    pub amount: Nat,
    #[serde(default)]
    pub expected_allowance: Option<Nat>,
    #[serde(default)]
    pub expires_at: Option<u64>,
    #[serde(default)]
    pub fee: Option<Nat>,
    #[serde(default)]
    pub memo: Option<Memo>,
    #[serde(default)]
    pub created_at_time: Option<u64>,
}
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L21-30)
```text
type ApproveArgs = record {
  fee : opt nat;
  memo : opt blob;
  from_subaccount : opt blob;
  created_at_time : opt Timestamp;
  amount : nat;
  expected_allowance : opt nat;
  expires_at : opt Timestamp;
  spender : Account
};
```
