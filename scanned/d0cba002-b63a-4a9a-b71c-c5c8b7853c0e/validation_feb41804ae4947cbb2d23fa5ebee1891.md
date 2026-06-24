### Title
ICP Ledger `icrc2_transfer_from` Only Checks Caller Against `can_send`, Not the `from` Account - (File: `rs/ledger_suite/icp/ledger/src/main.rs`)

---

### Summary

The ICP ledger's `icrc2_transfer_from` endpoint checks `can_send` only for the **caller** (the spender), not for the `from` account (the actual token owner). If a principal that fails `can_send` had previously granted an allowance to a spender, that spender can still drain the restricted principal's ICP tokens via `icrc2_transfer_from`, bypassing the ledger's transfer-restriction mechanism entirely.

---

### Finding Description

The ICP ledger enforces a `can_send` guard to restrict which principals may initiate token transfers. In `icrc1_transfer`, this is correct because the caller **is** the `from` account:

```rust
// rs/ledger_suite/icp/ledger/src/main.rs:807-843
#[update]
async fn icrc1_transfer(arg: TransferArg) -> Result<Nat, Icrc1TransferError> {
    if !LEDGER.read().unwrap().can_send(&PrincipalId::from(caller())) {
        trap("Caller cannot hold tokens on the ledger.");
    }
    let from_account = Account { owner: caller(), subaccount: arg.from_subaccount };
    // caller == from_account.owner, so the check is complete
```

However, in `icrc2_transfer_from`, the caller is the **spender**, not the token owner. The `from` account (`arg.from`) is never validated against `can_send`:

```rust
// rs/ledger_suite/icp/ledger/src/main.rs:845-882
#[update]
async fn icrc2_transfer_from(arg: TransferFromArgs) -> Result<Nat, TransferFromError> {
    if !LEDGER.read().unwrap().can_send(&PrincipalId::from(caller())) {
        // Only the SPENDER (caller) is checked — arg.from.owner is never checked
        trap("Caller cannot hold tokens on the ledger.");
    }
    let spender_account = Account { owner: caller(), subaccount: arg.spender_subaccount };
    Ok(Nat::from(
        icrc1_send(arg.memo, arg.amount, arg.fee,
            arg.from,  // <-- from account's principal is NEVER validated against can_send
            arg.to, Some(spender_account), arg.created_at_time,
        ).await ...
    ))
}
``` [1](#0-0) 

The `icrc1_transfer` path correctly conflates caller with `from`, so the single `can_send(caller())` check is sufficient there. [2](#0-1) 

The `can_send` function is defined in the ICP ledger library and gates whether a principal is permitted to hold and transfer ICP on the ledger. [3](#0-2) 

---

### Impact Explanation

Any principal that is restricted by `can_send` (e.g., a canister type that is not permitted to hold ICP, or a principal added to a deny list after the fact) but had previously issued an `icrc2_approve` to a spender retains a live allowance. A spender — who themselves passes `can_send` — can call `icrc2_transfer_from` with `from = <restricted_principal>` and successfully drain the restricted account's ICP balance. The restriction on the `from` account is completely bypassed.

This undermines the purpose of the `can_send` guard: it is supposed to prevent certain principals from moving ICP, but the approval-based transfer path circumvents it. In a scenario where a principal is restricted after a governance action (e.g., a canister is flagged), any pre-existing allowances remain exploitable.

---

### Likelihood Explanation

The attack requires two preconditions:
1. A principal that fails `can_send` must have previously called `icrc2_approve` to grant an allowance to a spender.
2. The spender must call `icrc2_transfer_from` before the allowance expires.

Both conditions are realistic: ICRC-2 approvals are a standard user action, and the window between a principal being restricted and all its allowances expiring can be arbitrarily long (approvals can be set without an expiry). An unprivileged ingress sender who holds a valid allowance from a restricted account can trigger this with a single `icrc2_transfer_from` call.

---

### Recommendation

Add a `can_send` check for `arg.from.owner` inside `icrc2_transfer_from`, mirroring the check already applied to the caller:

```rust
#[update]
async fn icrc2_transfer_from(arg: TransferFromArgs) -> Result<Nat, TransferFromError> {
    let ledger = LEDGER.read().unwrap();
    if !ledger.can_send(&PrincipalId::from(caller())) {
        trap("Caller cannot hold tokens on the ledger.");
    }
    // ADD: also validate the from account owner
    if !ledger.can_send(&PrincipalId::from(arg.from.owner)) {
        trap("The from account cannot hold tokens on the ledger.");
    }
    drop(ledger);
    // ... rest of function
}
``` [4](#0-3) 

---

### Proof of Concept

1. Principal **A** (a canister type that will later fail `can_send`) holds ICP on the ICP ledger.
2. **A** calls `icrc2_approve` granting spender **B** an allowance of N ICP with no expiry.
3. **A**'s principal is subsequently restricted (fails `can_send`). Any direct `icrc1_transfer` call from **A** is now rejected.
4. **B** (an unprivileged ingress sender who passes `can_send`) calls `icrc2_transfer_from` with `from = A's account`, `to = B's account`, `amount = N`.
5. The ledger checks `can_send(caller())` = `can_send(B)` → passes. It never checks `can_send(A)`.
6. The allowance is valid, the balance is sufficient, and the transfer succeeds — **A**'s restricted ICP is drained to **B**. [5](#0-4)

### Citations

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L807-843)
```rust
#[update]
async fn icrc1_transfer(
    arg: TransferArg,
) -> Result<Nat, icrc_ledger_types::icrc1::transfer::TransferError> {
    if !LEDGER
        .read()
        .unwrap()
        .can_send(&PrincipalId::from(caller()))
    {
        trap("Caller cannot hold tokens on the ledger.");
    }

    let from_account = Account {
        owner: caller(),
        subaccount: arg.from_subaccount,
    };
    Ok(Nat::from(
        icrc1_send(
            arg.memo,
            arg.amount,
            arg.fee,
            from_account,
            arg.to,
            None,
            arg.created_at_time,
        )
        .await
        .map_err(convert_transfer_error)
        .map_err(|err| {
            let err: Icrc1TransferError = match Icrc1TransferError::try_from(err) {
                Ok(err) => err,
                Err(err) => trap(&err),
            };
            err
        })?,
    ))
}
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L845-882)
```rust
#[update]
async fn icrc2_transfer_from(arg: TransferFromArgs) -> Result<Nat, TransferFromError> {
    if !LEDGER
        .read()
        .unwrap()
        .can_send(&PrincipalId::from(caller()))
    {
        trap("Caller cannot hold tokens on the ledger.");
    }

    if !LEDGER.read().unwrap().feature_flags.icrc2 {
        trap("ICRC-2 features are not enabled on the ledger.");
    }
    let spender_account = Account {
        owner: caller(),
        subaccount: arg.spender_subaccount,
    };
    Ok(Nat::from(
        icrc1_send(
            arg.memo,
            arg.amount,
            arg.fee,
            arg.from,
            arg.to,
            Some(spender_account),
            arg.created_at_time,
        )
        .await
        .map_err(convert_transfer_error)
        .map_err(|err| {
            let err: TransferFromError = match TransferFromError::try_from(err) {
                Ok(err) => err,
                Err(err) => trap(&err),
            };
            err
        })?,
    ))
}
```

**File:** rs/ledger_suite/icp/ledger/src/lib.rs (L1-36)
```rust
use ic_base_types::{CanisterId, PrincipalId};
use ic_cdk::{api::time, trap};
use ic_ledger_canister_core::archive::{Archive, ArchiveCanisterWasm};
use ic_ledger_canister_core::blockchain::{BlockDataContainer, Blockchain};
use ic_ledger_canister_core::ledger::{
    self as core_ledger, LedgerContext, LedgerData, TransactionInfo,
};
use ic_ledger_canister_core::runtime::CdkRuntime;
use ic_ledger_core::balances::BalancesStore;
use ic_ledger_core::{
    approvals::{Allowance, AllowanceTable, AllowancesData},
    balances::Balances,
    block::EncodedBlock,
    timestamp::TimeStamp,
};
use ic_ledger_core::{block::BlockIndex, tokens::Tokens};
use ic_ledger_hash_of::HashOf;
use ic_stable_structures::memory_manager::{MemoryId, MemoryManager, VirtualMemory};
use ic_stable_structures::{DefaultMemoryImpl, StableBTreeMap};
use ic_stable_structures::{Storable, storable::Bound};
use icp_ledger::{
    AccountIdentifier, Allowance as Allowance103, Allowances, Block, DEFAULT_TRANSFER_FEE,
    FeatureFlags, LedgerAllowances, LedgerBalances, MAX_TAKE_ALLOWANCES, Memo, Operation,
    PaymentError, Transaction, TransferError, TransferFee, UpgradeArgs,
};
use icrc_ledger_types::icrc1::account::Account;
use intmap::IntMap;
use lazy_static::lazy_static;
use minicbor::{Decode, Encode};
use serde::{Deserialize, Serialize};
use std::borrow::Cow;
use std::cell::{Cell, RefCell};
use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::ops::DerefMut;
use std::sync::{Arc, RwLock};
use std::time::Duration;
```
