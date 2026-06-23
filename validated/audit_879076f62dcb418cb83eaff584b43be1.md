The evidence is conclusive. Let me trace the exact path:

**Step 1 — Core error production** (`apply_transaction`): [1](#0-0) 
`TxApplyError::ExpiredApproval` maps to `CTE::ExpiredApproval`.

**Step 2 — Conversion gap** (`endpoints.rs`): [2](#0-1) 
`CTE::ExpiredApproval` returns `Err("Expired not implemented for TransferFromError")` — no fallback to `GenericError`.

**Step 3 — Trap site** (ICRC-1 ledger `main.rs`): [3](#0-2) 
`Err(err) => ic_cdk::trap(&err)` — the `Err` from the conversion directly triggers a canister trap.

**Step 4 — Same trap site** (ICP ledger `main.rs`): [4](#0-3) 

**Step 5 — `TransferFromError` has no `Expired` variant**: [5](#0-4) 
The enum has `GenericError` which could serve as a fallback, but the `TryFrom` impl does not use it.

The path is fully concrete and requires no privilege:

---

### Title
Unprivileged spender can trap the ICRC-2 ledger canister by calling `icrc2_transfer_from` with an expired allowance — (`rs/ledger_suite/icrc1/src/endpoints.rs`)

### Summary
The `TryFrom<EndpointsTransferError> for TransferFromError` implementation in `endpoints.rs` returns `Err(...)` for `CTE::ExpiredApproval`, and both the ICRC-1 and ICP ledger `icrc2_transfer_from` handlers unconditionally call `ic_cdk::trap` / `trap` on any `Err` from that conversion. Any unprivileged spender holding an expired allowance can trigger this path, causing the canister message to abort with a replica-level trap instead of returning a typed `TransferFromError`.

### Finding Description
In `rs/ledger_suite/icrc1/src/endpoints.rs` lines 132–134, the `TryFrom<EndpointsTransferError<Tokens>> for TransferFromError` match arm for `CTE::ExpiredApproval` returns `Err("Expired not implemented for TransferFromError")` rather than mapping to `TransferFromError::GenericError{...}` (which exists in the enum and is the correct ICRC-2 fallback). Both call sites — `rs/ledger_suite/icrc1/ledger/src/main.rs` lines 719–721 and `rs/ledger_suite/icp/ledger/src/main.rs` lines 875–877 — pass this `Err` string directly to `ic_cdk::trap` / `trap`, aborting the message.

The full reachable call chain:
```
icrc2_transfer_from (ingress)
  → execute_transfer / icrc1_send
  → apply_transaction
  → TxApplyError::ExpiredApproval
  → CTE::ExpiredApproval
  → convert_transfer_error → EndpointsTransferError(CTE::ExpiredApproval)
  → EndpointsTransferError::try_into::<TransferFromError>()
  → Err("Expired not implemented for TransferFromError")
  → ic_cdk::trap(&err)   ← canister message aborts
```

Preconditions are trivially achievable by any user: call `icrc2_approve` with `expires_at` set to any past timestamp, then call `icrc2_transfer_from` as the spender.

### Impact Explanation
Every `icrc2_transfer_from` call against an expired allowance produces a canister trap (`ErrorCode::CanisterCalledTrap`) instead of a clean `Err(TransferFromError::...)`. This:
- Violates the ICRC-2 specification, which requires a typed error response
- Causes callers (including inter-canister callers such as ckBTC minter, ckETH minter, NNS governance) to receive a reject code rather than a decodable `TransferFromError`, potentially breaking their error-handling logic
- Can be triggered repeatedly by any unprivileged user on any ledger with an expired allowance

### Likelihood Explanation
Expired allowances are a normal, expected ledger state. Any spender who set `expires_at` and then calls `icrc2_transfer_from` after expiry hits this path. No special setup, no admin access, no key material required.

### Recommendation
In `rs/ledger_suite/icrc1/src/endpoints.rs`, replace the `CTE::ExpiredApproval` arm in `TryFrom<EndpointsTransferError<Tokens>> for TransferFromError` with a mapping to `TransferFromError::GenericError`:

```rust
CTE::ExpiredApproval { ledger_time } => TFE::GenericError {
    error_code: Nat::from(/* ICRC-2 error code for expired approval */),
    message: format!("approval expired at ledger time {}", ledger_time.as_nanos_since_unix_epoch()),
},
```

Similarly handle `CTE::AllowanceChanged` and `CTE::SelfApproval` with appropriate `GenericError` mappings rather than returning `Err(...)`.

### Proof of Concept
State-machine test (pseudocode):
```rust
// 1. Mint tokens to `from`
// 2. Approve spender with expires_at = now - 1ns
icrc2_approve(from, ApproveArgs { spender, amount: 1000, expires_at: Some(past_time), .. });
// 3. Advance ledger time past expiry (already past)
// 4. Call transfer_from as spender — expect Err(TransferFromError::GenericError{..})
//    but actually get CanisterCalledTrap
let result = env.execute_ingress_as(spender, ledger_id, "icrc2_transfer_from", encode(args));
assert!(result.is_err()); // ErrorCode::CanisterCalledTrap, not Ok(Err(TransferFromError))
```

### Citations

**File:** rs/ledger_suite/common/ledger_canister_core/src/ledger.rs (L265-267)
```rust
            TxApplyError::ExpiredApproval { now } => {
                TransferError::ExpiredApproval { ledger_time: now }
            }
```

**File:** rs/ledger_suite/icrc1/src/endpoints.rs (L132-134)
```rust
            CTE::ExpiredApproval { .. } => {
                return Err("Expired not implemented for TransferFromError".to_string());
            }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L718-722)
```rust
    .map_err(|err| {
        let err: TransferFromError = match err.try_into() {
            Ok(err) => err,
            Err(err) => ic_cdk::trap(&err),
        };
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L874-878)
```rust
        .map_err(|err| {
            let err: TransferFromError = match TransferFromError::try_from(err) {
                Ok(err) => err,
                Err(err) => trap(&err),
            };
```

**File:** packages/icrc-ledger-types/src/icrc2/transfer_from.rs (L30-42)
```rust
pub enum TransferFromError {
    BadFee { expected_fee: Nat },
    BadBurn { min_burn_amount: Nat },
    // The [from] account does not hold enough funds for the transfer.
    InsufficientFunds { balance: Nat },
    // The caller exceeded its allowance.
    InsufficientAllowance { allowance: Nat },
    TooOld,
    CreatedInFuture { ledger_time: u64 },
    Duplicate { duplicate_of: Nat },
    TemporarilyUnavailable,
    GenericError { error_code: Nat, message: String },
}
```
