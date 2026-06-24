### Title
Zero-Balance Account Accumulation in ICRC-1 Index-NG Stable Memory — Unbounded Growth Enabling DoS on `list_subaccounts` - (File: rs/ledger_suite/icrc1/index-ng/src/main.rs)

---

### Summary

The `change_balance` function in the ICRC-1 Index-NG canister unconditionally inserts every updated balance — including zero — into the `ACCOUNT_DATA` stable BTreeMap. Unlike the ICP index canister and the core ledger's `BalancesStore`, it never removes zero-balance entries. An unprivileged caller can create arbitrarily many zero-balance entries by repeatedly receiving tokens into fresh subaccounts and then transferring them out, causing unbounded growth of the `ACCOUNT_DATA` map and degrading or blocking the `list_subaccounts` query endpoint.

---

### Finding Description

In `rs/ledger_suite/icrc1/index-ng/src/main.rs`, the `change_balance` helper always calls `account_data.insert(key, new_balance)` regardless of whether `new_balance` is zero:

```rust
// rs/ledger_suite/icrc1/index-ng/src/main.rs  lines 303-308
fn change_balance(account: Account, f: impl FnOnce(Tokens) -> Tokens) {
    let key = balance_key(account);
    let new_balance = f(get_balance(account));
    with_account_data(|account_data| account_data.insert(key, new_balance));
}
``` [1](#0-0) 

The `ACCOUNT_DATA` map is a `StableBTreeMap` keyed by `(AccountDataType, (Blob<29>, [u8; 32]))` — i.e., `(data_type, (principal_bytes, subaccount_bytes))`: [2](#0-1) 

This contrasts directly with the ICP index canister, which explicitly removes zero-balance entries:

```rust
// rs/ledger_suite/icp/index/src/main.rs  lines 241-252
fn change_balance(account_identifier: AccountIdentifier, f: impl FnOnce(u64) -> u64) {
    let key = balance_key(account_identifier);
    let new_balance = f(get_balance(account_identifier));
    if new_balance == 0 {
        with_account_identifier_data(|account_identifier_data| {
            account_identifier_data.remove(&key)
        });
    } else { ... }
}
``` [3](#0-2) 

The core ledger's `BalancesStore` implementation for `BTreeMap` also removes zero-balance entries on every update: [4](#0-3) 

The `list_subaccounts` endpoint (imported via `ListSubaccountsArgs` at line 15) range-scans the `ACCOUNT_DATA` map for a given principal. With many zero-balance entries for a principal, this scan grows proportionally, eventually hitting the per-query instruction limit. [5](#0-4) 

---

### Impact Explanation

An attacker who accumulates N zero-balance entries for a target principal causes the `list_subaccounts` query for that principal to iterate over all N entries. Because `StableBTreeMap` range scans consume instructions proportional to the number of entries visited, a sufficiently large N causes the query to trap with an instruction-limit exceeded error, permanently denying service for that principal's subaccount listing. Additionally, each zero-balance entry permanently occupies stable memory (the `ACCOUNT_DATA_MEMORY_ID` region), wasting canister resources for the lifetime of the index canister.

---

### Likelihood Explanation

The attack requires only the ability to send ICRC-1 transfers — available to any unprivileged principal on any ICRC-1 ledger that has deployed this index canister (e.g., SNS token indexes). The cost per zero-balance entry is two transfer fees (one to fund the subaccount, one to drain it). At typical ICRC-1 fees this is economically feasible at scale. No privileged access, governance majority, or threshold key is required.

---

### Recommendation

Modify `change_balance` to remove the map entry when the resulting balance is zero, mirroring the ICP index and core ledger behavior:

```rust
fn change_balance(account: Account, f: impl FnOnce(Tokens) -> Tokens) {
    let key = balance_key(account);
    let new_balance = f(get_balance(account));
    with_account_data(|account_data| {
        if new_balance.is_zero() {
            account_data.remove(&key);
        } else {
            account_data.insert(key, new_balance);
        }
    });
}
```

---

### Proof of Concept

1. Obtain a small amount of any ICRC-1 token whose index canister runs `index-ng`.
2. For each of N subaccounts `[i; 32]` (i = 0..N):
   a. Transfer `fee + 1` tokens to `Account { owner: attacker, subaccount: Some([i; 32]) }`.
   b. Transfer `1` token back out (paying the fee), leaving balance = 0.
3. After step 2, the `ACCOUNT_DATA` map contains N entries with value `Tokens::zero()` for the attacker's principal.
4. Call `list_subaccounts({ owner: attacker, start: None })` as a query. For large N the call traps with an instruction-limit error, confirming DoS.

The zero-balance entries are never removed by `change_balance`, so they persist indefinitely across upgrades of the index canister.

### Citations

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L12-16)
```rust
use ic_icrc1_index_ng::{
    DEFAULT_MAX_BLOCKS_PER_RESPONSE, FeeCollectorRanges, GetAccountTransactionsArgs,
    GetAccountTransactionsResponse, GetAccountTransactionsResult, GetBlocksMethod, IndexArg,
    InitArg, ListSubaccountsArgs, Log, LogEntry, Status, TransactionWithId, UpgradeArg,
};
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L78-79)
```rust
type AccountDataMapKey = (AccountDataType, (Blob<29>, [u8; 32]));
type AccountDataMap = StableBTreeMap<AccountDataMapKey, Tokens, VM>;
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L303-308)
```rust
/// A helper function to change the balance of an account.
fn change_balance(account: Account, f: impl FnOnce(Tokens) -> Tokens) {
    let key = balance_key(account);
    let new_balance = f(get_balance(account));
    with_account_data(|account_data| account_data.insert(key, new_balance));
}
```

**File:** rs/ledger_suite/icp/index/src/main.rs (L241-252)
```rust
fn change_balance(account_identifier: AccountIdentifier, f: impl FnOnce(u64) -> u64) {
    let key = balance_key(account_identifier);
    let new_balance = f(get_balance(account_identifier));
    if new_balance == 0 {
        with_account_identifier_data(|account_identifier_data| {
            account_identifier_data.remove(&key)
        });
    } else {
        with_account_identifier_data(|account_identifier_data| {
            account_identifier_data.insert(key, new_balance)
        });
    }
```

**File:** rs/ledger_suite/common/ledger_core/src/balances.rs (L38-55)
```rust
        match self.entry(k) {
            Entry::Occupied(mut entry) => {
                let new_v = f(Some(entry.get()))?;
                if !new_v.is_zero() {
                    *entry.get_mut() = new_v.clone();
                } else {
                    entry.remove_entry();
                }
                Ok(new_v)
            }
            Entry::Vacant(entry) => {
                let new_v = f(None)?;
                if !new_v.is_zero() {
                    entry.insert(new_v.clone());
                }
                Ok(new_v)
            }
        }
```
