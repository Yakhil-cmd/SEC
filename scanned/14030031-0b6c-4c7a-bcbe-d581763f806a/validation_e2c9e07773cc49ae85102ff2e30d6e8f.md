### Title
Unbounded ICRC-2 Allowance Table Growth via Non-Expiring Approvals Enables Ledger DoS - (`rs/ledger_suite/common/ledger_core/src/approvals.rs`)

---

### Summary

The ICRC-2 allowance table in the IC ledger suite has no upper bound on the number of stored approvals and no mechanism to prune approvals that were created without an expiration (`expires_at: None`). An unprivileged user controlling many accounts can flood the table with permanent, non-expiring approvals, causing unbounded growth of the ledger canister's memory and eventually halting ledger operations.

---

### Finding Description

The `AllowanceTable` stores one entry per unique `(account, spender)` pair. The `prune` function, called on every transaction via `apply_transaction`, only removes entries that have an `expires_at` timestamp that is in the past. It operates exclusively on the `expiration_queue`, which only contains entries that were created with a non-`None` `expires_at`. [1](#0-0) 

The constant `APPROVE_PRUNE_LIMIT = 100` caps how many expired approvals are pruned per transaction. Crucially, approvals created with `expires_at: None` are **never inserted into the `expiration_queue`** and are therefore **never pruned**. [2](#0-1) 

The `prune` function confirms this: it only iterates over `first_expiry()` entries, so non-expiring approvals are invisible to it. [3](#0-2) 

The `HeapAllowancesData` backing the ICP ledger stores allowances in an unbounded `BTreeMap`: [4](#0-3) 

There is no global cap on the number of allowances, no per-account cap, and no mechanism to force-remove non-expiring approvals except by the approver themselves (by re-approving with amount zero).

---

### Impact Explanation

An attacker who controls N accounts, each holding enough tokens to pay the approval fee, can call `icrc2_approve` from each account to a fixed spender with `expires_at: None`. Each call inserts a permanent entry into the allowances table. These entries accumulate indefinitely. For the ICP ledger, the allowances live in heap memory; for ICRC-1 ledgers, in stable memory. In both cases, unbounded growth eventually exhausts available memory, causing the canister to trap on new state writes and halting all ledger operations (transfers, approvals, burns) for all users — a full DoS of the ledger.

---

### Likelihood Explanation

The attack is reachable by any unprivileged ingress sender. IC principals are derived from public keys and can be generated offline in unlimited quantities. The only cost is the transfer fee per approval (e.g., 0.0001 ICP on the ICP ledger). Each entry in `HeapAllowancesData` occupies roughly 150–300 bytes (two `AccountIdentifier` keys plus an `Allowance` struct). The ICP ledger canister heap is bounded; filling it requires a large but finite number of approvals. The attack is gradual and does not require any privileged role, governance majority, or threshold corruption.

---

### Recommendation

1. **Enforce a global or per-account cap** on the number of active allowances. Reject new approvals when the cap is reached.
2. **Require an expiration** (`expires_at`) for all new approvals, or charge a recurring storage fee for non-expiring approvals.
3. **Expose a permissionless cleanup endpoint** that allows anyone to remove approvals whose `amount` has been fully consumed or whose account balance is zero.
4. Alternatively, **prune by arrival order** (using the existing `arrival_queue` field) in addition to expiry order, so that the oldest non-expiring approvals can be evicted when the table grows too large.

---

### Proof of Concept

1. Attacker generates N keypairs offline, deriving N IC principals `P_1 … P_N`.
2. Attacker funds each account `P_i` with enough tokens to pay the approval fee (e.g., 0.0002 ICP: 0.0001 for the approval fee, 0.0001 buffer).
3. For each `P_i`, attacker calls `icrc2_approve` with `spender = P_attacker`, `amount = 1`, `expires_at = None`.
4. Each call succeeds and inserts a permanent entry `(P_i, P_attacker) -> Allowance { amount: 1, expires_at: None }` into the `allowances` BTreeMap.
5. The `prune(now, 100)` call inside `apply_transaction` finds nothing to prune (no entries in `expiration_queue`).
6. After sufficiently many calls, the ledger canister's heap (ICP ledger) or stable memory (ICRC-1 ledger) is exhausted, and subsequent update calls trap with an out-of-memory error, halting all ledger functionality. [5](#0-4) [1](#0-0)

### Citations

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

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L66-87)
```rust
#[derive(Debug, Deserialize, Serialize)]
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

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L232-276)
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
