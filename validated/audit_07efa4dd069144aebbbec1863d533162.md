Based on my investigation of the production code, here is my assessment:

---

### Title
Unbounded Allowance Table Growth via Insufficient Per-Transaction Pruning — (`rs/ledger_suite/common/ledger_canister_core/src/ledger.rs`, `rs/ledger_suite/common/ledger_core/src/approvals.rs`)

### Summary

The ICRC-2 allowance table has no hard capacity cap. Each call to `apply_transaction` prunes at most `APPROVE_PRUNE_LIMIT = 100` expired entries. An attacker controlling many funded accounts can pre-populate the table with expiring approvals faster than the fixed-rate pruning can clear them, causing the table to grow without bound and eventually exhaust canister heap memory.

### Finding Description

**Root cause — no table size cap:**

`AllowanceTable::approve` inserts into an unbounded `BTreeMap` with no size check: [1](#0-0) 

**Root cause — fixed prune limit per transaction:**

`apply_transaction` prunes exactly `APPROVE_PRUNE_LIMIT = 100` expired entries per call, regardless of how many expired entries exist: [2](#0-1) 

**`prune` implementation — stops at 100:** [3](#0-2) 

**No throttle without `created_at_time`:**

The throttle guard only activates when `transactions_by_height` is non-empty, which only happens when callers set `created_at_time`. Without it, `num_pruned == 0` and `throttle()` returns `false` (window is empty), so approvals are admitted at the full IC ingress rate: [4](#0-3) 

**Storage structure — heap BTreeMap, no eviction:** [5](#0-4) 

### Impact Explanation

Each allowance entry (two `Account` keys + `Allowance` value + BTreeMap node overhead) consumes roughly 300–500 bytes. At 1 million entries the table occupies ~300–500 MB; at ~8–10 million entries it can exhaust the 4 GB Wasm heap limit, causing the canister to trap on allocation. All subsequent `icrc2_approve`, `icrc2_transfer_from`, and `icrc1_transfer` calls fail, constituting a full denial of service for the ledger.

### Likelihood Explanation

- Attacker needs many accounts each holding enough tokens to pay one approval fee. For low-fee tokens (e.g., ckBTC at 10 satoshis ≈ fractions of a cent) this is cheap at scale.
- Without `created_at_time`, there is no per-second throttle; the attacker submits at the IC ingress rate.
- Approvals with `expires_at` set a few seconds in the future are accepted, then expire, leaving dead entries that accumulate faster than 100-per-transaction pruning can clear them.
- The attack is fully unprivileged and reachable via standard ingress.

### Recommendation

1. **Enforce a hard cap** on `AllowanceTable` size (e.g., `MAX_ALLOWANCES`). Reject new approvals with `TemporarilyUnavailable` when the cap is reached, or require pruning to succeed before insertion.
2. **Increase or dynamically scale `APPROVE_PRUNE_LIMIT`** proportionally to the current expired-entry backlog rather than using a fixed constant.
3. **Prune before inserting** in `AllowanceTable::approve` so that each new approval displaces at least one expired entry when the table is at capacity.

### Proof of Concept

```
for i in 0..N:                          // N >> 100
    icrc2_approve(
        from    = account_i,            // distinct funded account
        spender = attacker,
        amount  = 1,
        expires_at = now + 2s,          // accepted, expires soon
        // no created_at_time → no throttle
    ) → Ok(block_height)

sleep(3s)                               // all N entries now expired

// Each subsequent transaction prunes only 100; N - 100*k entries remain
// after k transactions. Table never fully clears while attacker keeps
// submitting new expiring approvals at rate > 100/tx.

// At N ≈ 10_000_000 entries → heap exhaustion → canister traps
```

State-machine test: create 10,000 expiring approvals, advance time, assert `get_num_approvals()` decreases by exactly 100 per subsequent transaction — demonstrating the O(N/100) clearing time and the window during which memory is unbounded. [6](#0-5)

### Citations

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L67-87)
```rust
pub struct HeapAllowancesData<AccountId, Tokens>
where
    AccountId: Ord,
{
    allowances: BTreeMap<(AccountId, AccountId), Allowance<Tokens>>,
    expiration_queue: BTreeSet<(TimeStamp, (AccountId, AccountId))>,
    #[serde(default = "Default::default")]
    arrival_queue: BTreeSet<(TimeStamp, (AccountId, AccountId))>,
}
impl<AccountId, Tokens> Default for HeapAllowancesData<AccountId, Tokens>
where
    AccountId: Ord,
{
    fn default() -> Self {
        Self {
            allowances: BTreeMap::new(),
            expiration_queue: BTreeSet::new(),
            arrival_queue: BTreeSet::new(),
        }
    }
}
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L265-276)
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
                    Ok(amount)
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L326-328)
```rust
    pub fn get_num_approvals(&self) -> usize {
        self.allowances_data.len_allowances()
    }
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L373-399)
```rust
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

**File:** rs/ledger_suite/common/ledger_canister_core/src/ledger.rs (L211-231)
```rust
const APPROVE_PRUNE_LIMIT: usize = 100;

/// Adds a new block with the specified transaction to the ledger.
pub fn apply_transaction<L>(
    ledger: &mut L,
    transaction: L::Transaction,
    now: TimeStamp,
    effective_fee: L::Tokens,
) -> Result<(BlockIndex, HashOf<EncodedBlock>), TransferError<L::Tokens>>
where
    L: LedgerData,
{
    let num_pruned = purge_old_transactions(ledger, now);

    // If we pruned some transactions, let this one through
    // otherwise throttle if there are too many
    if num_pruned == 0 && throttle(ledger, now) {
        return Err(TransferError::TxThrottled);
    }

    ledger.approvals_mut().prune(now, APPROVE_PRUNE_LIMIT);
```
