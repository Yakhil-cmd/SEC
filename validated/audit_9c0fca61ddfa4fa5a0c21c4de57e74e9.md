### Title
Unbounded Expired Allowances Enable Instruction Exhaustion in `get_allowances` / `icrc103_get_allowances` Query - (File: `rs/ledger_suite/icrc1/ledger/src/lib.rs`, `rs/ledger_suite/icp/ledger/src/lib.rs`)

### Summary
The `get_allowances` (ICP ledger) and `icrc103_get_allowances` (ICRC-1 ledger) query endpoints iterate over all stored allowances for an account, silently skipping expired entries with `continue` without counting them toward the `max_results` cap. Because there is no per-account cap on the number of allowances, an unprivileged caller can create an unbounded number of allowances with short expiration times. After they expire, any query for that account must traverse every expired entry before the loop can terminate, potentially exhausting the IC query instruction limit and permanently denying service for that account's allowance listing.

### Finding Description
Both ledger implementations share the same structural flaw. In the ICRC-1 ledger:

```rust
// rs/ledger_suite/icrc1/ledger/src/lib.rs:1194-1221
ALLOWANCES_MEMORY.with_borrow(|allowances| {
    for (account_spender, storable_allowance) in
        allowances.range(start_account_spender.clone()..)
    {
        ...
        if result.len() >= max_results as usize {
            break;                          // only breaks on collected results
        }
        if account_spender.account.owner != from.owner {
            break;
        }
        if let Some(expires_at) = storable_allowance.expires_at
            && expires_at.as_nanos_since_unix_epoch() <= now
        {
            continue;                       // expired: skip, NOT counted toward max_results
        }
        result.push(...)
    }
});
``` [1](#0-0) 

The identical pattern exists in the ICP ledger: [2](#0-1) 

The loop breaks only when `result.len() >= max_results` (capped at 500 by `MAX_TAKE_ALLOWANCES`) or when the account owner changes. Expired entries are skipped with `continue` and do not advance the result counter. If an account holds N expired allowances and zero non-expired ones, the loop must traverse all N entries before it can exit. [3](#0-2) [4](#0-3) 

There is no per-account cap on the number of allowances. The `approve` function in the shared core imposes no such limit: [5](#0-4) 

The background pruning mechanism removes at most `APPROVE_PRUNE_LIMIT = 100` expired allowances per transaction, meaning a large backlog of expired allowances accumulates faster than it is cleaned up: [6](#0-5) 

### Impact Explanation
An attacker who creates N allowances with short expiration times from a single account causes every subsequent `icrc103_get_allowances` / `get_allowances` query for that account to iterate through all N expired stable-BTreeMap entries. Each stable-memory read costs significant instructions. Benchmarks show approximately 6.4 million instructions for 500 allowances (`icrc103_get_allowances` scope). Extrapolating, roughly 390,000 expired allowances would exhaust the IC's 5-billion-instruction query limit, causing the query to permanently fail for that account. The account's allowance listing becomes permanently unavailable until the expired entries are pruned by background transactions — a process that takes thousands of ledger transactions at 100 pruned per transaction. [7](#0-6) 

### Likelihood Explanation
The attack requires paying the ledger transfer fee for each approval. At the ICP ledger's default fee of 10,000 e8s (0.0001 ICP), creating 390,000 allowances costs approximately 39 ICP. For ICRC-1 tokens with lower fees (e.g., ckBTC, ckETH), the cost is proportionally lower. The attack is permissionless — any account holder can execute it — and the effect is persistent until background pruning catches up. The victim is any caller of the allowance-listing query for the targeted account, including wallets, DeFi protocols, and monitoring tools.

### Recommendation
1. **Add a per-account allowance cap** in the `approve` function, analogous to `MAX_NFT_APPROVALS` in the referenced report. Reject new approvals once an account reaches the cap.
2. **Count expired entries toward the iteration budget** inside `get_allowances` / `get_allowances_list`. Introduce a separate `max_scanned` limit (e.g., `max_results * 10`) and break the loop when that many entries have been examined, regardless of how many were expired.
3. **Increase the prune limit** `APPROVE_PRUNE_LIMIT` from 100 to a higher value so expired allowances are cleaned up faster.

### Proof of Concept
1. Obtain an account `A` on the ICRC-1 ledger (e.g., ckBTC) with sufficient balance.
2. Call `icrc2_approve` in a loop, each time with a distinct spender and `expires_at = ic_cdk::api::time() + 1` (1 nanosecond in the future), until N allowances are created (N ≈ 390,000 for full instruction exhaustion; smaller N causes proportional slowdown).
3. Wait one second for all allowances to expire.
4. Call `icrc103_get_allowances({ from_account: Some(A), prev_spender: None, take: None })`.
5. Observe that the query iterates through all N expired stable-BTreeMap entries before returning an empty result, exhausting the instruction budget and returning a replica-level error for large N.

The same attack applies to the ICP ledger's `get_allowances` endpoint using `icrc2_approve` followed by time passage. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L1194-1221)
```rust
    ALLOWANCES_MEMORY.with_borrow(|allowances| {
        for (account_spender, storable_allowance) in
            allowances.range(start_account_spender.clone()..)
        {
            if spender.is_some() && account_spender == start_account_spender {
                continue;
            }
            if result.len() >= max_results as usize {
                break;
            }
            if account_spender.account.owner != from.owner {
                break;
            }
            if let Some(expires_at) = storable_allowance.expires_at
                && expires_at.as_nanos_since_unix_epoch() <= now
            {
                continue;
            }
            result.push(Allowance103 {
                from_account: account_spender.account,
                to_spender: account_spender.spender,
                allowance: Nat::from(storable_allowance.amount),
                expires_at: storable_allowance
                    .expires_at
                    .map(|t| t.as_nanos_since_unix_epoch()),
            });
        }
    });
```

**File:** rs/ledger_suite/icp/ledger/src/lib.rs (L1657-1681)
```rust

```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1214-1231)
```rust
#[query]
fn icrc103_get_allowances(arg: GetAllowancesArgs) -> Result<Allowances, GetAllowancesError> {
    let from_account = arg.from_account.unwrap_or_else(|| Account {
        owner: ic_cdk::api::msg_caller(),
        subaccount: None,
    });
    let max_take_allowances = Access::with_ledger(|ledger| ledger.max_take_allowances());
    let max_results = arg
        .take
        .map(|take| take.0.to_u64().unwrap_or(max_take_allowances))
        .map(|take| std::cmp::min(take, max_take_allowances))
        .unwrap_or(max_take_allowances);
    Ok(get_allowances(
        from_account,
        arg.prev_spender,
        max_results,
        ic_cdk::api::time(),
    ))
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1561-1573)
```rust
fn get_allowances(arg: GetAllowancesArgs) -> Allowances {
    let max_take_allowances = Access::with_ledger(|ledger| ledger.max_take_allowances());
    let max_results = arg
        .take
        .map(|take| std::cmp::min(take, max_take_allowances))
        .unwrap_or(max_take_allowances);
    get_allowances_list(
        arg.from_account_id,
        arg.prev_spender_id,
        max_results,
        ic_cdk::api::time(),
    )
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

**File:** rs/ledger_suite/icrc1/ledger/canbench_results/canbench_u256.yml (L14-18)
```yaml
      icrc103_get_allowances:
        calls: 1
        instructions: 6379538
        heap_increase: 0
        stable_memory_increase: 0
```
