### Title
Unbounded Permanent Allowance Storage Bloat via `icrc2_approve` with No Expiry - (`rs/ledger_suite/common/ledger_core/src/approvals.rs`)

---

### Summary

An unprivileged ledger user can bloat the ICP/ICRC-1 ledger canister's stable storage indefinitely by creating an unbounded number of permanent ICRC-2 allowances (with `expires_at: None`) at the cost of exactly one transfer fee per entry. The `AllowanceTable::prune()` function only removes **expired** allowances; permanent allowances are never reclaimed. There is no cap on the total number of allowances stored per account or globally.

---

### Finding Description

The `AllowanceTable::approve()` function in `rs/ledger_suite/common/ledger_core/src/approvals.rs` inserts a new allowance entry for each unique `(from, spender)` pair without enforcing any upper bound on the total number of stored allowances. [1](#0-0) 

When `expires_at` is `None`, no entry is added to the `expiration_queue`: [2](#0-1) 

The `prune()` function, called on every `apply_transaction`, iterates only over the `expiration_queue` (entries with a set expiry). Permanent allowances — those with `expires_at: None` — have no entry in the expiration queue and are therefore **never pruned**: [3](#0-2) 

This is confirmed by the postcondition invariant, which explicitly allows more allowances than expirations: [4](#0-3) 

`prune()` is invoked with a fixed `APPROVE_PRUNE_LIMIT` on every transaction, but this only ever touches the expiration queue: [5](#0-4) 

Both the ICP ledger and the ICRC-1 ledger back allowances with a `StableBTreeMap` in stable memory, meaning every inserted allowance permanently consumes stable storage until explicitly removed: [6](#0-5) [7](#0-6) 

The only way a permanent allowance is removed is if the approver explicitly sets `amount = 0` (revocation) or if `use_allowance` drains it to zero. An attacker has no incentive to do either.

---

### Impact Explanation

An attacker holding sufficient tokens can:

1. Call `icrc2_approve` with N distinct spender accounts (e.g., different subaccounts of a controlled principal), each with `expires_at: None`.
2. Each call costs exactly one transfer fee and succeeds unconditionally (subject to balance).
3. N permanent entries accumulate in `ALLOWANCES_MEMORY` (stable BTreeMap) and are never reclaimed.

The ledger canister pays for stable storage in cycles. As the allowance table grows without bound:
- The ledger canister's cycle reserves are drained by storage costs.
- Upgrade serialization/deserialization time grows, risking upgrade failures.
- All ledger operations that touch the allowance table (approve, transfer_from, prune) degrade in performance.
- In the extreme case, the ledger canister becomes unresponsive or unable to upgrade.

This directly mirrors the Phala bloat attack: the attacker's per-entry cost (one transfer fee) is far below the storage cost imposed on the shared canister.

---

### Likelihood Explanation

The attack is reachable by any unprivileged ingress sender who holds a token balance. For the ICP ledger, the transfer fee is 10,000 e8s ≈ $0.001 per allowance. Creating 1 million permanent allowances costs ~$1,000 USD and consumes significant stable storage. For ICRC-1 tokens deployed via the ledger suite orchestrator with lower configured fees, the cost per entry is proportionally lower, making the attack cheaper. The `icrc2_approve` endpoint is publicly callable with no rate limiting beyond the transfer fee itself. [8](#0-7) [9](#0-8) 

---

### Recommendation

1. **Enforce a maximum allowance count per approver account** (e.g., 1,000 entries). Reject `icrc2_approve` with `GenericError` when the limit is reached.
2. **Alternatively, require `expires_at` to be set** for all new allowances, ensuring the expiration queue covers all entries and `prune()` can reclaim them.
3. **Alternatively, charge a refundable storage deposit** per allowance entry (returned when the allowance is revoked or expires), making the attacker bear the full storage cost.

---

### Proof of Concept

```
1. Attacker controls principal P with balance = N * transfer_fee tokens.
2. For i in 1..=N:
     icrc2_approve({
       from_subaccount: None,
       spender: { owner: attacker_controlled_principal_i, subaccount: None },
       amount: 1,
       expires_at: None,   // permanent — never pruned
       fee: transfer_fee,
       ...
     })
3. Each call succeeds; AllowanceTable inserts (P, spender_i) → Allowance{amount:1, expires_at:None}
   into ALLOWANCES_MEMORY (StableBTreeMap).
4. prune() is called on every subsequent transaction but only iterates expiration_queue,
   which has zero entries for these allowances. Nothing is ever removed.
5. After N calls, the ledger's stable storage holds N permanent allowance entries.
   The ledger canister's cycle balance is drained by storage fees; all users are impacted.
``` [10](#0-9) [3](#0-2)

### Citations

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L199-206)
```rust
    fn check_postconditions(&self) {
        debug_assert!(
            self.allowances_data.len_expirations() <= self.allowances_data.len_allowances(),
            "expiration queue length ({}) larger than allowances length ({})",
            self.allowances_data.len_expirations(),
            self.allowances_data.len_allowances()
        );
    }
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L232-277)
```rust
    /// Changes the spender's allowance for the account to the specified amount and expiration.
    pub fn approve(
        &mut self,
        account: &AD::AccountId,
        spender: &AD::AccountId,
        amount: AD::Tokens,
        expires_at: Option<TimeStamp>,
        now: TimeStamp,
        expected_allowance: Option<AD::Tokens>,
    ) -> Result<AD::Tokens, ApproveError<AD::Tokens>> {
        self.with_postconditions_check(|table| {
            if account == spender {
                return Err(ApproveError::SelfApproval);
            }

            if expires_at.unwrap_or_else(remote_future) <= now {
                return Err(ApproveError::ExpiredApproval { now });
            }

            let key = (account.clone(), spender.clone());

            match table.allowances_data.get_allowance(&key) {
                None => {
                    if let Some(expected_allowance) = expected_allowance
                        && !expected_allowance.is_zero()
                    {
                        return Err(ApproveError::AllowanceChanged {
                            current_allowance: AD::Tokens::zero(),
                        });
                    }
                    if amount == AD::Tokens::zero() {
                        return Ok(amount);
                    }
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
                    Ok(amount)
                }
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

**File:** rs/ledger_suite/common/ledger_canister_core/src/ledger.rs (L231-231)
```rust
    ledger.approvals_mut().prune(now, APPROVE_PRUNE_LIMIT);
```

**File:** rs/ledger_suite/icp/ledger/src/lib.rs (L136-137)
```rust
    pub static ALLOWANCES_MEMORY: RefCell<StableBTreeMap<(AccountIdentifier, AccountIdentifier), StorableAllowance, VirtualMemory<DefaultMemoryImpl>>> =
        MEMORY_MANAGER.with(|memory_manager| RefCell::new(StableBTreeMap::init(memory_manager.borrow().get(ALLOWANCES_MEMORY_ID))));
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L526-527)
```rust
    pub static ALLOWANCES_MEMORY: RefCell<StableBTreeMap<AccountSpender, StorableAllowance, VirtualMemory<DefaultMemoryImpl>>> =
        MEMORY_MANAGER.with(|memory_manager| RefCell::new(StableBTreeMap::init(memory_manager.borrow().get(ALLOWANCES_MEMORY_ID))));
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L893-903)
```rust
#[update]
async fn icrc2_approve(arg: ApproveArgs) -> Result<Nat, ApproveError> {
    let block_idx = icrc2_approve_not_async(ic_cdk::api::msg_caller(), arg)?;

    // NB. we need to set the certified data before the first async call to make sure that the
    // blockchain state agrees with the certificate while archiving is in progress.
    ic_cdk::api::certified_data_set(Access::with_ledger(Ledger::root_hash));

    archive_blocks::<Access>(&LOG, MAX_MESSAGE_SIZE).await;
    Ok(Nat::from(block_idx))
}
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1418-1425)
```rust
#[update]
async fn icrc2_approve(arg: ApproveArgs) -> Result<Nat, ApproveError> {
    let block_index = icrc2_approve_not_async(caller(), arg, None)?;

    let max_msg_size = *MAX_MESSAGE_SIZE_BYTES.read().unwrap();
    archive_blocks::<Access>(DebugOutSink, max_msg_size as u64).await;
    Ok(block_index)
}
```
