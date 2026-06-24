Audit Report

## Title
Unbounded Iteration Over Expired Allowances in `get_allowances` Violates `MAX_TAKE_ALLOWANCES` Cost Bound — (File: rs/ledger_suite/icrc1/ledger/src/lib.rs)

## Summary
In `get_allowances`, the `max_results` break guard fires at line 1201 before the expiry `continue` at line 1210. Expired entries are skipped without incrementing `result.len()`, so the loop must exhaust all expired entries for an account before accumulating enough non-expired ones to break. An attacker who pre-populates N expired allowances for a single account forces every subsequent `icrc103_get_allowances` query for that account to scan all N entries, with iteration cost growing linearly in N and unbounded by `MAX_TAKE_ALLOWANCES`. The identical ordering flaw exists in the ICP ledger's `get_allowances_list`.

## Finding Description
In `get_allowances` (`rs/ledger_suite/icrc1/ledger/src/lib.rs:1194–1222`), the loop body executes in this order:

1. Skip pagination start entry (line 1198–1199)
2. **Break if `result.len() >= max_results`** (line 1201–1202)
3. Break if past the `from` account owner (line 1204–1205)
4. **Continue (skip) if expired** (line 1207–1210) — does not increment `result.len()`
5. Push valid entry to result

Because step 4 fires after step 2, expired entries consume loop iterations without contributing to `result.len()`. The `max_results` guard never fires on expired entries. With N expired entries and M ≥ max_results non-expired entries, the loop performs N + max_results iterations instead of the intended max_results iterations.

The same flaw exists in `get_allowances_list` (`rs/ledger_suite/icp/ledger/src/lib.rs:657–680`): the `result.len() >= max_results` check at line 664 precedes the expiry `continue` at line 670.

The `approve` function rejects allowances already expired at submission time (`rs/ledger_suite/common/ledger_core/src/approvals.rs:247–248`), so the attacker must set `expires_at = now + ε`, wait for expiry, then query. The `prune` function is called with a bounded `limit` during `icrc2_approve` and does not eagerly clean up all expired entries, so expired allowances persist indefinitely.

## Impact Explanation
`icrc103_get_allowances` is a `#[query]` endpoint callable by any principal without authentication. IC query calls are bounded by a 5-billion-instruction limit. Each stable-memory B-tree range iteration costs on the order of thousands of instructions. An attacker who accumulates hundreds of thousands of expired allowances for a single account forces every query call for that account to consume a disproportionate instruction budget, degrading or trapping legitimate queries for that account. This is a non-volumetric, state-based query exhaustion: a single well-crafted state causes repeated query degradation without ongoing attacker participation. This matches the **High** impact class: "Application/platform-level DoS … not based on raw volumetric DDoS" against an in-scope financial integration (ICRC/ICP ledger).

## Likelihood Explanation
The attack requires N distinct spender accounts (achievable with N principals), paying a ledger fee per allowance (economically bounded but not prohibitive at scale), and waiting for allowances to expire (requires setting `expires_at` slightly in the future). Once the expired allowances are in stable memory, the attacker needs no further action — every query call for that account pays the cost. The attack is repeatable and persistent.

## Recommendation
Move the expiry check before the `max_results` check, or introduce a separate total-iterations counter that counts expired entries toward the loop budget:

```rust
// Option A: move expiry check before max_results check
for (account_spender, storable_allowance) in allowances.range(...) {
    if spender.is_some() && account_spender == start_account_spender { continue; }
    if account_spender.account.owner != from.owner { break; }
    if expired { continue; }  // <-- moved before max_results check
    if result.len() >= max_results as usize { break; }
    result.push(...);
}

// Option B: add a total scan limit independent of result count
let mut scanned = 0usize;
for ... {
    if scanned >= SCAN_LIMIT { break; }
    scanned += 1;
    ...
}
```

Apply the same fix to `get_allowances_list` in `rs/ledger_suite/icp/ledger/src/lib.rs`.

## Proof of Concept
1. Fund account `A` with sufficient tokens.
2. Create N allowances from `A` to N distinct spender accounts, each with `expires_at = now + 1_000_000_000` (1 second).
3. Wait 1 second for all allowances to expire.
4. Repeatedly call `icrc103_get_allowances({ from_account: A, prev_spender: None, take: None })`.
5. Measure instruction count via `ic_cdk::api::performance_counter(0)` or via replica metrics.
6. Assert instruction count grows linearly with N, far exceeding the `MAX_TAKE_ALLOWANCES` bound.
7. Compare against a baseline with 0 expired allowances and `MAX_TAKE_ALLOWANCES` non-expired ones — baseline uses ~500 iterations; attack case uses N + 500 iterations. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L1194-1222)
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
    result
```

**File:** rs/ledger_suite/icp/ledger/src/lib.rs (L657-680)
```rust
    ALLOWANCES_MEMORY.with_borrow(|allowances| {
        for ((from_account_id, to_spender_id), storable_allowance) in
            allowances.range((from, start_spender)..)
        {
            if spender.is_some() && start_spender == to_spender_id {
                continue;
            }
            if result.len() >= max_results as usize || from_account_id != from {
                break;
            }
            if let Some(expires_at) = storable_allowance.expires_at
                && expires_at.as_nanos_since_unix_epoch() <= now
            {
                continue;
            }
            result.push(Allowance103 {
                from_account_id,
                to_spender_id,
                allowance: storable_allowance.amount,
                expires_at: storable_allowance
                    .expires_at
                    .map(|t| t.as_nanos_since_unix_epoch()),
            });
        }
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L247-249)
```rust
            if expires_at.unwrap_or_else(remote_future) <= now {
                return Err(ApproveError::ExpiredApproval { now });
            }
```
