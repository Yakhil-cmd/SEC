### Title
Unbounded Growth of Permanent Allowances in `ALLOWANCES_MEMORY` Stable Storage - (File: `rs/ledger_suite/common/ledger_core/src/approvals.rs`, `rs/ledger_suite/icp/ledger/src/lib.rs`, `rs/ledger_suite/icrc1/ledger/src/lib.rs`)

---

### Summary

The ICP and ICRC-1 ledger canisters allow any unprivileged user to create an unbounded number of permanent (no-expiration) allowance entries via `icrc2_approve`. The `prune()` function only removes **expired** allowances; allowances with `expires_at: None` are never automatically cleaned up and accumulate indefinitely in the `ALLOWANCES_MEMORY` stable `StableBTreeMap`. There is no per-account or global cap on the number of stored allowances. This is a direct analog to the `costableList` unbounded growth issue: just as every token transfer could inflate the `costableList` with empty addresses that were never removed, every `icrc2_approve` call with no expiration inflates the allowance stable map with entries that are never pruned.

---

### Finding Description

The `AllowanceTable::approve()` function in `rs/ledger_suite/common/ledger_core/src/approvals.rs` inserts an expiry entry into the expiration queue **only when `expires_at` is `Some`**:

```rust
if let Some(expires_at) = expires_at {
    table.allowances_data.insert_expiry(expires_at, key.clone());
}
table.allowances_data.set_allowance(key, Allowance { amount, expires_at, arrived_at: now });
```

When `expires_at` is `None`, the allowance is stored permanently with no entry in the expiration queue. The `prune()` function iterates only over the expiration queue:

```rust
pub fn prune(&mut self, now: TimeStamp, limit: usize) -> usize {
    ...
    match table.allowances_data.first_expiry() {
        Some((ts, _key)) => { if ts > now { return pruned; } }
        None => { return pruned; }  // stops immediately if queue is empty
    }
    ...
}
```

If all allowances are permanent (no expiration), `first_expiry()` returns `None` and `prune()` returns 0 immediately — it cannot remove any of them.

Both the ICP ledger (`rs/ledger_suite/icp/ledger/src/lib.rs`) and the ICRC-1 ledger (`rs/ledger_suite/icrc1/ledger/src/lib.rs`) back the allowance table with a `StableBTreeMap` (`ALLOWANCES_MEMORY`) that has no enforced size cap:

```rust
pub static ALLOWANCES_MEMORY: RefCell<StableBTreeMap<(AccountIdentifier, AccountIdentifier), StorableAllowance, VirtualMemory<DefaultMemoryImpl>>> = ...
```

There is no `max_allowances` constant or per-account limit anywhere in the ledger suite code. The only limit (`MAX_TAKE_ALLOWANCES = 500`) governs how many allowances are *returned* in a single query, not how many can be *stored*.

---

### Impact Explanation

An attacker can inflate `ALLOWANCES_MEMORY` without bound by calling `icrc2_approve` with `expires_at: None` from many accounts (or subaccounts — a single principal has 2^256 possible subaccounts) to many distinct spenders. Each call costs only the transfer fee (e.g., 10,000 e8s for ICP), but the resulting allowance entry persists in stable memory forever. As the stable BTreeMap grows, it consumes the ledger canister's stable memory allocation. If the stable memory is exhausted, the ledger canister can no longer process any transactions (transfers, approvals, burns), effectively halting the ledger. Even before exhaustion, a very large allowance table degrades the performance of every operation that touches stable memory.

**Vulnerability class**: cycles/resource accounting bug — unbounded canister stable memory growth triggered by unprivileged ingress.

---

### Likelihood Explanation

Medium. The attack requires paying the approval fee for each entry created, which imposes an economic cost. However:
- A single principal with many subaccounts can create entries at scale without needing many separate identities.
- The fee is small relative to the damage potential (halting the ICP or ICRC-1 ledger).
- The attack is silent and gradual — no single call is anomalous.
- There is no on-chain rate limiting or per-account allowance count cap.

---

### Recommendation

1. **Enforce a per-account cap** on the number of active allowances (e.g., reject `icrc2_approve` if the approver already has N active allowances). This directly mirrors the recommendation in the external report to limit the size of the inflatable list.
2. **Require an expiration** for new allowances, or charge a higher fee for permanent allowances to make the attack economically prohibitive.
3. **Add a global allowance count limit** with a configurable parameter, similar to how `MAX_TRANSACTIONS_IN_WINDOW` caps the transaction deduplication window.
4. **Implement a cleanup mechanism** analogous to the report's recommendation: a separate callable function that removes allowances whose approver account has a zero balance (since a zero-balance account cannot pay fees and is unlikely to legitimately need active allowances).

---

### Proof of Concept

1. Attacker controls principal `P` with many subaccounts `sub_0, sub_1, ..., sub_N`.
2. Attacker mints a small amount of tokens to each subaccount (enough to pay the approval fee).
3. For each subaccount `sub_i`, attacker calls `icrc2_approve` with `expires_at: None` and a distinct spender `S_j` for `j = 0..M`.
4. Each call succeeds and inserts a permanent entry into `ALLOWANCES_MEMORY`.
5. After `N × M` calls, `ALLOWANCES_MEMORY` contains `N × M` entries that `prune()` can never remove.
6. The attacker can then drain the token balance from each subaccount (via transfer), leaving zero-balance accounts with permanent allowances — exactly the empty-wallet-in-costableList scenario.
7. The stable memory of the ledger canister grows monotonically with no automatic reclamation path.

**Key code references:**

`AllowanceTable::approve` — no expiry inserted for permanent allowances: [1](#0-0) 

`AllowanceTable::prune` — only iterates the expiration queue, cannot touch permanent allowances: [2](#0-1) 

ICP ledger `ALLOWANCES_MEMORY` — unbounded stable BTreeMap: [3](#0-2) 

ICRC-1 ledger `ALLOWANCES_MEMORY` — unbounded stable BTreeMap: [4](#0-3) 

No global or per-account allowance count cap exists anywhere in the ledger suite: [5](#0-4)

### Citations

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L265-275)
```rust
                    if let Some(expires_at) = expires_at {
                        table.allowances_data.insert_expiry(expires_at, key.clone());
                    }
                    table.allowances_data.set_allowance(
                        key,
                        Allowance {
                            amount: amount.clone(),
                            expires_at,
                            arrived_at: now,
                        },
                    );
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L372-399)
```rust
    /// Prunes allowances that are expired, removes at most `limit` allowances.
    pub fn prune(&mut self, now: TimeStamp, limit: usize) -> usize {
        self.with_postconditions_check(|table| {
            let mut pruned = 0;
            for _ in 0..limit {
                match table.allowances_data.first_expiry() {
                    Some((ts, _key)) => {
                        if ts > now {
                            return pruned;
                        }
                    }
                    None => {
                        return pruned;
                    }
                }
                if let Some((_, (account, spender))) = table.allowances_data.pop_first_expiry() {
                    let key = (account, spender);
                    if let Some(allowance) = table.allowances_data.get_allowance(&key)
                        && allowance.expires_at.unwrap_or_else(remote_future) <= now
                    {
                        table.allowances_data.remove_allowance(&key);
                        pruned += 1;
                    }
                }
            }
            pruned
        })
    }
```

**File:** rs/ledger_suite/icp/ledger/src/lib.rs (L136-137)
```rust
    pub static ALLOWANCES_MEMORY: RefCell<StableBTreeMap<(AccountIdentifier, AccountIdentifier), StorableAllowance, VirtualMemory<DefaultMemoryImpl>>> =
        MEMORY_MANAGER.with(|memory_manager| RefCell::new(StableBTreeMap::init(memory_manager.borrow().get(ALLOWANCES_MEMORY_ID))));
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L74-74)
```rust
const MAX_TAKE_ALLOWANCES: u64 = 500;
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L526-527)
```rust
    pub static ALLOWANCES_MEMORY: RefCell<StableBTreeMap<AccountSpender, StorableAllowance, VirtualMemory<DefaultMemoryImpl>>> =
        MEMORY_MANAGER.with(|memory_manager| RefCell::new(StableBTreeMap::init(memory_manager.borrow().get(ALLOWANCES_MEMORY_ID))));
```
