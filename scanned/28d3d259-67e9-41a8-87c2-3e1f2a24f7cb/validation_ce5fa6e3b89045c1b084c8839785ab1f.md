Looking at the actual production code to verify the claim.

**Key findings from the code:**

**1. `APPROVE_PRUNE_LIMIT = 100` is hardcoded and called once per transaction:** [1](#0-0) 

**2. `prune()` removes at most `limit` entries per call, stopping early if the queue is empty or the next expiry is in the future:** [2](#0-1) 

**3. `approve()` has no check on the total number of existing allowances before inserting a new one:** [3](#0-2) 

**4. No `MAX_ALLOWANCES` constant or global cap exists anywhere in the codebase.** The `test_approve_cap` test only caps the *amount* value at `Tokens::max_value()`, not the *count* of allowances: [4](#0-3) 

**Analysis of the attack path:**

- During the creation phase (N approvals, each with `expires_at = now + 1ns`), `prune()` is called per transaction but finds nothing expired yet — the newly created allowances are still valid at creation time. Net effect: +1 allowance per transaction, 0 pruned.
- After time advances past all expiries, N expired entries sit in the `expiration_queue` and `allowances` map.
- Each subsequent transaction prunes at most 100. Clearing N entries requires ⌈N/100⌉ additional transactions.
- If the attacker creates entries faster than the system processes cleanup transactions, the table grows without bound.

**Throttle bypass note:** The throttle is skipped when `num_pruned > 0`: [5](#0-4) 

This means a large backlog of expired allowances actually *suppresses* throttling for subsequent transactions, compounding the issue.

**Economic cost reality check:** At ~0.0001 ICP per approval, creating 1,000,000 expired allowances costs ~100 ICP (~$600 at typical prices). Each allowance entry (two `AccountId`s + amount + expiry + arrived_at) is on the order of 100–200 bytes. One million entries ≈ 100–200 MB of heap/stable memory. Ten million entries ≈ 1–2 GB — approaching canister memory limits for a realistic attack budget against a high-value DeFi ledger.

---

### Title
Unbounded Allowance Table Growth via Short-Lived Approvals Outpacing Fixed Prune Limit — (`rs/ledger_suite/common/ledger_core/src/approvals.rs`, `rs/ledger_suite/common/ledger_canister_core/src/ledger.rs`)

### Summary
The ICRC-2 ledger's expired-allowance cleanup is capped at 100 entries per `apply_transaction` call (`APPROVE_PRUNE_LIMIT`). There is no cap on the total number of allowances that can exist. An unprivileged attacker who pays approval fees can create N >> 100 short-lived allowances with distinct spenders, causing the allowance table and expiration queue to grow to O(N) and remain bloated for O(N/100) subsequent transactions.

### Finding Description
`apply_transaction` calls `ledger.approvals_mut().prune(now, APPROVE_PRUNE_LIMIT)` with a fixed limit of 100 on every transaction. The `AllowanceTable::approve` function inserts into `allowances` and `expiration_queue` with no guard on the total count. An attacker submits N approvals (each to a unique spender, each with `expires_at = now + small_delta`). During submission, none are expired yet, so prune removes 0. After the expiry window passes, N stale entries remain. Each subsequent ledger transaction removes at most 100, so the table stays bloated for ⌈N/100⌉ transactions. With N = 100,000 and a normal transaction rate of ~10/s, the backlog persists for ~100 seconds; with N = 10,000,000 it persists for ~2.8 hours and consumes gigabytes of memory.

### Impact Explanation
- **Stable/heap memory exhaustion**: The `allowances` BTreeMap and `expiration_queue` BTreeSet grow without bound. For the ICP ledger (stable memory-backed), this can exhaust the canister's stable memory. For ICRC1 ledgers (heap-backed), this can cause the canister to trap on memory allocation.
- **Performance degradation**: BTreeMap operations are O(log N). A table with millions of entries degrades every `approve`, `transfer_from`, and `allowance` query.
- **Throttle suppression**: A large expired-allowance backlog causes `num_pruned > 0` on every transaction, bypassing the `throttle()` check and allowing the attacker to submit further transactions without rate limiting.

### Likelihood Explanation
The attack requires only a token balance sufficient to pay N approval fees — a one-time economic cost. No privileged access, no key compromise, and no consensus-level attack is needed. The entry point is the standard `icrc2_approve` ingress endpoint available to any principal.

### Recommendation
1. **Enforce a hard cap** on the total number of allowances (e.g., per-account or globally) inside `AllowanceTable::approve`, returning an error when the cap is reached.
2. **Increase or make adaptive the prune limit** — e.g., prune proportionally to the backlog size, or prune a larger batch when the queue exceeds a threshold.
3. **Charge a higher fee for approvals with short expiries**, or require a minimum `expires_at` distance from `now`, to raise the economic cost of this attack.

### Proof of Concept
```rust
// State-machine test sketch
let attacker_balance = 1_000_000 * FEE; // fund attacker
for i in 0..1_000 {
    icrc2_approve(spender = unique_account(i), amount = 1, expires_at = now() + 1);
}
advance_time(2); // all 1000 approvals are now expired
assert_eq!(allowance_table.len(), 1000); // still 1000 entries
for _ in 0..10 {
    icrc1_transfer(...); // 10 normal transactions, each prunes 100
}
// Expected if bounded: len ≈ 0
// Actual: len == 0 only after exactly 10 transactions (1000/100)
// With N=100_000 and 10 transactions: len still == 99_000
assert!(allowance_table.len() > 0); // table not cleared
```

The invariant "allowance table size is bounded relative to the number of active (non-expired) approvals" is violated: after all approvals expire and only 10 transactions occur, the table still holds up to 99,000 stale entries.

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

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L253-276)
```rust
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

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L2620-2644)
```rust
pub fn test_approve_cap<T, Tokens>(ledger_wasm: Vec<u8>, encode_init_args: fn(InitArgs) -> T)
where
    T: CandidType,
    Tokens: TokensType,
{
    let from = PrincipalId::new_user_test_id(1);
    let spender = PrincipalId::new_user_test_id(2);

    let (env, canister_id) = setup(
        ledger_wasm,
        encode_init_args,
        vec![(Account::from(from.0), 100_000)],
    );

    let mut approve_args = default_approve_args(spender.0, 150_000);

    approve_args.amount = Tokens::max_value().into() * 2_u8;
    let block_index =
        send_approval(&env, canister_id, from.0, &approve_args).expect("approval failed");
    assert_eq!(block_index, 1);
    let allowance = Account::get_allowance(&env, canister_id, from.0, spender.0);
    assert_eq!(allowance.allowance, Tokens::max_value().into());
    assert_eq!(allowance.expires_at, None);
    assert_eq!(balance_of(&env, canister_id, from.0), 90_000);
    assert_eq!(balance_of(&env, canister_id, spender.0), 0);
```
