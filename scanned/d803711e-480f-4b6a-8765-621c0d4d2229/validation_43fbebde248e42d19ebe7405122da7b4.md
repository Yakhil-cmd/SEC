### Title
Silent Expiry-Based Pruning of ICRC-2 Allowances Without ICRC-3 Log Entry — (File: `rs/ledger_suite/common/ledger_core/src/approvals.rs`)

---

### Summary

The ICRC-1/ICRC-2 ledger silently removes expired allowances as a garbage-collection side effect of every transaction, via `approvals_mut().prune(...)`, without emitting any ICRC-3 block. Off-chain clients reconstructing allowance state from the ICRC-3 log cannot observe when or why an allowance was zeroed by expiry, directly mirroring the `BeamBalanceStore` generation-cleanup gap.

---

### Finding Description

In `rs/ledger_suite/common/ledger_canister_core/src/ledger.rs`, the function `apply_transaction` is the single entry point for every ledger state change (transfer, burn, mint, approve). Before recording the caller's transaction as a new ICRC-3 block, it unconditionally calls:

```rust
ledger.approvals_mut().prune(now, APPROVE_PRUNE_LIMIT);
``` [1](#0-0) 

The `prune` implementation iterates the expiry queue and silently deletes every allowance whose `expires_at ≤ now`:

```rust
pub fn prune(&mut self, now: TimeStamp, limit: usize) -> usize {
    ...
    if let Some((_, (account, spender))) = table.allowances_data.pop_first_expiry() {
        let key = (account, spender);
        if let Some(allowance) = table.allowances_data.get_allowance(&key)
            && allowance.expires_at.unwrap_or_else(remote_future) <= now
        {
            table.allowances_data.remove_allowance(&key);
            pruned += 1;
        }
    }
    ...
}
``` [2](#0-1) 

No `record_event`, no block addition, and no ICRC-3 entry of any kind is produced for these removals. The ICRC-3 schema defines only four block types — `burn`, `mint`, `approve`, and `xfer` — with no block type for allowance expiry or pruning: [3](#0-2) 

The `approve` block records the original grant (including `expires_at`), but the moment the allowance is silently garbage-collected there is no corresponding block. The test `test_approve_pruning` confirms the pruning happens as a side effect of the next transaction, with no observable ICRC-3 artifact: [4](#0-3) 

This is structurally identical to the `BeamBalanceStore` finding: a garbage-collection path (`prune` ↔ `updateTo` generation cleanup) zeroes out state without emitting any event, leaving off-chain observers unable to determine when or why the value became zero.

---

### Impact Explanation

Any off-chain client — wallet, DEX, index canister, or audit tool — that reconstructs allowance state by replaying the ICRC-3 block log will see an `approve` block granting an allowance but will never see a corresponding block revoking it upon expiry. The client will believe the allowance is still live until it independently tracks the `expires_at` field and applies its own clock. Clients that do not do this (a common pattern for generic ICRC-3 indexers) will report a stale, non-zero allowance for an account pair whose on-chain allowance is already zero. A spender relying on such an index to decide whether to call `icrc2_transfer_from` will receive `InsufficientAllowance` with no prior warning from the log. For DeFi protocols that use ICRC-3 as their source of truth for allowance accounting, this creates a systematic discrepancy between the certified log and the live ledger state.

---

### Likelihood Explanation

The trigger is any call to `icrc1_transfer`, `icrc2_approve`, or `icrc2_transfer_from` by any unprivileged user. `apply_transaction` is the shared code path for all of these. On a live ledger with active users, pruning fires continuously. Any account that has ever set an expiring allowance is affected the moment the next unrelated transaction arrives after the expiry timestamp. No special privilege or coordination is required.

---

### Recommendation

Emit a dedicated ICRC-3 block (e.g., block type `"expire"` or a synthetic `"approve"` block with `amount = 0`) for each allowance removed by `prune`. This mirrors the recommendation in the original report: emit an event whenever the garbage-collection mechanism zeroes out state. At minimum, document in the ICRC-3 schema that allowance expiry is not logged and that clients must apply `expires_at` timestamps independently when reconstructing state from the block log.

---

### Proof of Concept

1. Alice calls `icrc2_approve` granting Bob an allowance of 1 000 tokens expiring in 1 hour. ICRC-3 block N is created with `op = "approve"`, `expires_at = T+3600s`.
2. Two hours pass. No transaction occurs.
3. Carol calls `icrc1_transfer` for an unrelated transfer. `apply_transaction` fires, calls `prune(now, 100)`.
4. Alice's allowance (`expires_at = T+3600s ≤ now`) is removed from `allowances_data` inside `prune`. [5](#0-4) 

5. Carol's transfer is recorded as ICRC-3 block N+1. No block is created for Alice's allowance removal.
6. An ICRC-3 indexer replaying blocks 0…N+1 still shows Alice→Bob allowance = 1 000 tokens.
7. Bob's application queries the indexer, believes the allowance is live, and calls `icrc2_transfer_from`. The ledger returns `InsufficientAllowance { allowance: 0 }`.
8. `icrc2_allowance` confirms the allowance is 0, but there is no ICRC-3 block that explains the transition from 1 000 to 0.

### Citations

**File:** rs/ledger_suite/common/ledger_canister_core/src/ledger.rs (L231-231)
```rust
    ledger.approvals_mut().prune(now, APPROVE_PRUNE_LIMIT);
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L373-398)
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
```

**File:** packages/icrc-ledger-types/src/icrc3/schema.rs (L36-56)
```rust
    let is_icrc2_approve = and(vec![
        icrc1_common.clone(),
        item("op", Required, is(Value::text("approve"))),
        item("from", Required, is_account()),
        item("spender", Required, is_account()),
        item("expected_allowance", Optional, is_amount.clone()),
        item("expires_at", Optional, is_timestamp.clone()),
    ]);
    let is_icrc2_transfer_from = and(vec![
        icrc1_common,
        item("op", Required, is(Value::text("xfer"))),
        item("from", Required, is_account()),
        item("to", Required, is_account()),
        item("spender", Optional, is_account()),
    ]);
    let is_icrc1_or_icrc2_transaction = or(vec![
        is_icrc1_burn,
        is_icrc1_mint,
        is_icrc2_approve,
        is_icrc2_transfer_from,
    ]);
```

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L2647-2698)
```rust
pub fn test_approve_pruning<T>(ledger_wasm: Vec<u8>, encode_init_args: fn(InitArgs) -> T)
where
    T: CandidType,
{
    let from = PrincipalId::new_user_test_id(1);
    let spender = PrincipalId::new_user_test_id(2);

    let from_sub_1 = Account {
        owner: from.0,
        subaccount: Some([1; 32]),
    };

    let (env, canister_id) = setup(
        ledger_wasm,
        encode_init_args,
        vec![(Account::from(from.0), 100_000), (from_sub_1, 100_000)],
    );

    let mut approve_args = default_approve_args(spender.0, 150_000);

    // Approval expiring 1 hour from now.
    let expiration =
        Some(system_time_to_nanos(env.time()) + Duration::from_secs(3600).as_nanos() as u64);
    approve_args.expires_at = expiration;
    let block_index =
        send_approval(&env, canister_id, from.0, &approve_args).expect("approval failed");
    assert_eq!(block_index, 2);
    let allowance = Account::get_allowance(&env, canister_id, from.0, spender.0);
    assert_eq!(allowance.allowance.0.to_u64().unwrap(), 150_000);
    assert_eq!(allowance.expires_at, expiration);
    assert_eq!(balance_of(&env, canister_id, from.0), 90_000);
    assert_eq!(balance_of(&env, canister_id, spender.0), 0);

    // Test expired approval pruning, advance time 2 hours.
    env.advance_time(Duration::from_secs(2 * 3600));
    let expiration =
        Some(system_time_to_nanos(env.time()) + Duration::from_secs(3600).as_nanos() as u64);
    approve_args.from_subaccount = Some([1; 32]);
    approve_args.expires_at = expiration;
    approve_args.amount = Nat::from(100_000_u32);
    let block_index =
        send_approval(&env, canister_id, from.0, &approve_args).expect("approval failed");
    assert_eq!(block_index, 3);
    let allowance = Account::get_allowance(&env, canister_id, from.0, spender.0);
    let allowance_sub_1 = Account::get_allowance(&env, canister_id, from_sub_1, spender.0);
    assert_eq!(allowance.allowance.0.to_u64().unwrap(), 0);
    assert_eq!(allowance.expires_at, None);
    assert_eq!(allowance_sub_1.allowance.0.to_u64().unwrap(), 100_000);
    assert_eq!(allowance_sub_1.expires_at, expiration);
    assert_eq!(balance_of(&env, canister_id, from.0), 90_000);
    assert_eq!(balance_of(&env, canister_id, from_sub_1), 90_000);
    assert_eq!(balance_of(&env, canister_id, spender.0), 0);
```
