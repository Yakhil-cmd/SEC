### Title
Permanent Halt of ICRC-1 Index-NG Sync Timer via Unrecoverable `ic_cdk::trap` in `process_balance_changes` - (File: rs/ledger_suite/icrc1/index-ng/src/main.rs)

### Summary
The ICRC-1 index-ng canister's periodic block-sync loop (`build_index`) uses a one-shot timer that reschedules itself only upon successful completion. Inside the sync path, `process_balance_changes` calls `debit` and `credit`, which invoke `ic_cdk::trap` on arithmetic errors (underflow, overflow, or missing fee). Because `ic_cdk::trap` is a hard abort that bypasses Rust's `Result` error-handling, the trap propagates up through the entire timer callback, preventing the timer from ever being rescheduled. The index canister permanently stops syncing. The code itself acknowledges one of these stuck states with the message: `"bug: index is stuck because block with index {block_index} doesn't contain a fee and no fee has been recorded before"`.

### Finding Description

The sync loop entry point is `build_index()`, a one-shot async timer callback:

```
build_index()
  └─ fetch_blocks_via_get_blocks() / fetch_blocks_via_icrc3()
       └─ append_blocks()
            └─ append_block()
                 └─ process_balance_changes()   ← hard ic_cdk::trap() here
```

`build_index` reschedules itself via `set_build_index_timer` only at the end of a successful run. If any call in the chain above traps, the reschedule never happens and the index permanently stops.

`process_balance_changes` contains multiple hard-trap paths:

1. **Approve block with no fee and no prior fee recorded** (self-acknowledged stuck state): [1](#0-0) 

2. **Debit underflow** — traps if the index's tracked balance for an account is less than the debit amount: [2](#0-1) 

3. **Credit overflow** — traps if adding to a balance overflows the token type: [3](#0-2) 

4. **Transfer with no fee field** — traps if a Transfer block has neither `fee` nor `effective_fee`: [4](#0-3) 

5. **Token amount overflow in Burn/Transfer** — traps if `amount + fee` overflows: [5](#0-4) 

The timer is a one-shot timer that reschedules itself only on success: [6](#0-5) 

`set_build_index_timer` uses `ic_cdk_timers::set_timer` (one-shot), not `set_timer_interval`: [7](#0-6) 

The trap propagates through `append_block`, which calls `process_balance_changes` without any trap guard: [8](#0-7) 

**Recovery path is also blocked**: Upgrading the canister via `post_upgrade` restarts the timer, but the next invocation immediately encounters the same problematic block at the same index and traps again. There is no in-canister admin function to skip a block or restart the timer independently of the sync path. This is directly analogous to the dForce pattern where the recovery function (`_setInterestRateModel`) also invokes the failing guard (`settleInterest`).

The ICP index canister (`rs/ledger_suite/icp/index/src/main.rs`) has the same pattern — `debit` calls `ic_cdk::trap` on underflow and `clear_build_index_timer` is the only escape, but only for `Result`-returning errors, not hard traps: [9](#0-8) 

### Impact Explanation

When the trap fires, the ICRC-1 index-ng canister permanently stops syncing blocks from the ledger. All balance queries (`icrc1_balance_of` via the index), transaction history queries, and `list_subaccounts` return stale or incorrect data indefinitely. Any DeFi application, wallet, or protocol relying on the index for balance information receives wrong answers with no indication of staleness. The ledger itself continues to function, but the index — which is the primary read-path for many integrations — is permanently broken. Recovery requires a governance proposal to upgrade the canister with patched code, and even then the same block will be re-encountered unless the fix handles the error gracefully.

### Likelihood Explanation

The Approve-with-no-fee case is explicitly acknowledged in the code comments as a known historical ledger bug affecting mainnet blocks. Any index-ng canister deployed against a ledger that has such a block as its first Approve transaction (before any Transfer has set `last_fee`) will trap on first encounter. The debit-underflow and credit-overflow paths are less likely on a correctly functioning ledger but become reachable if any discrepancy exists between the ledger's canonical state and the index's tracked balances. The trap paths are reachable by any unprivileged user who can cause the ledger to produce blocks (i.e., any token holder calling `transfer` or `approve`).

### Recommendation

1. Change `process_balance_changes` to return `Result<(), SyncError>` instead of calling `ic_cdk::trap`. Propagate the error up through `append_block` and `append_blocks`.
2. In `build_index`, treat a non-retriable `SyncError` from block processing as a permanent stop condition with a clear log message, rather than a hard trap that kills the timer silently.
3. Add an admin-callable `restart_sync` update method that calls `set_build_index_timer` directly, so the controller can restart the sync without a full canister upgrade.
4. For the Approve-with-no-fee case specifically, treat a missing fee as a non-fatal condition (use `Tokens::zero()` as a fallback) rather than trapping.

### Proof of Concept

1. Deploy an ICRC-1 ledger that has (or will produce) an Approve block where both `fee` and `effective_fee` are `None`, and this is the first block the index processes.
2. Deploy an ICRC-1 index-ng canister pointing at that ledger.
3. Wait for the index timer to fire and call `build_index`.
4. `build_index` → `fetch_blocks_via_get_blocks` → `append_blocks` → `append_block` → `process_balance_changes` → `ic_cdk::trap("bug: index is stuck because block with index 0 doesn't contain a fee and no fee has been recorded before")`.
5. The timer callback traps. `set_build_index_timer` is never called. The index permanently stops syncing.
6. Query `status` on the index: `num_blocks_synced` remains at 0 forever while the ledger chain grows.
7. Upgrade the canister: `post_upgrade` calls `set_build_index_timer`, restarting the timer. The next tick fires, encounters block 0 again, and traps again. The index is permanently stuck in a trap-on-every-invocation loop until the code is patched.

### Citations

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L722-741)
```rust
    match num_indexed {
        Ok(num_indexed) => {
            let wait_time = compute_wait_time(num_indexed);
            mutate_state(|state| {
                state.last_wait_time = wait_time;
            });
            log!(P1, "Indexed: {} waiting : {:?}", num_indexed, wait_time);
            set_build_index_timer(wait_time);
        }
        Err(error) => {
            log!(P0, "{}", error.message);
            ic_cdk::eprintln!("{}", error.message);
            if error.retriable {
                let wait_time = with_state(|state| state.last_wait_time);
                set_build_index_timer(wait_time);
            } else {
                log!(P0, "Stopping the indexing timer.");
                ic_cdk::eprintln!("Stopping the indexing timer.");
            }
        }
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L846-850)
```rust
fn set_build_index_timer(after: Duration) {
    ic_cdk_timers::set_timer(after, async {
        let _ = build_index().await;
    });
}
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L938-941)
```rust
        // change the balance of the involved accounts
        process_balance_changes(block_index, &decoded_block);

        Ok(())
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L1032-1035)
```rust
                    amount_with_fee = amount.checked_add(&fee).unwrap_or_else(|| {
                        trap(format!(
                            "token amount overflow while indexing block {block_index}"
                        ))
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L1067-1070)
```rust
                let fee = block.effective_fee.or(fee).unwrap_or_else(|| {
                    ic_cdk::trap(format!(
                        "Block {block_index} is of type Transfer but has no fee or effective fee!"
                    ))
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L1104-1106)
```rust
                        None => ic_cdk::trap(format!(
                            "bug: index is stuck because block with index {block_index} doesn't contain a fee and no fee has been recorded before"
                        )),
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L1139-1144)
```rust
fn debit(block_index: BlockIndex64, account: Account, amount: Tokens) {
    change_balance(account, |balance| {
        balance.checked_sub(&amount).unwrap_or_else(|| {
            ic_cdk::trap(format!("Block {block_index} caused an underflow for account {account} when calculating balance {balance} - amount {amount}"));
        })
    })
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L1147-1152)
```rust
fn credit(block_index: BlockIndex64, account: Account, amount: Tokens) {
    change_balance(account, |balance| {
        balance.checked_add(&amount).unwrap_or_else(|| {
            ic_cdk::trap(format!("Block {block_index} caused an overflow for account {account} when calculating balance {balance} + amount {amount}"))
        })
    });
```

**File:** rs/ledger_suite/icp/index/src/main.rs (L510-519)
```rust
fn debit(block_index: BlockIndex, account_identifier: AccountIdentifier, amount: u64) {
    change_balance(account_identifier, |balance| {
        if balance < amount {
            ic_cdk::trap(format!(
                "Block {block_index} caused an overflow for account_identifier {account_identifier} when calculating balance {balance} + amount {amount}"
            ))
        }
        balance - amount
    });
}
```
