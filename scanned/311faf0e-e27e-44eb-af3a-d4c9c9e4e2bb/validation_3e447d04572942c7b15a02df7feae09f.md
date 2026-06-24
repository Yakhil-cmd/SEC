### Title
Unchecked ICP Burn Return Value Allows Cycles Minting Without Token Destruction - (File: `rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) silently discards the result of the ICP burn operation after successfully minting or depositing cycles. If the burn call to the ICP ledger fails, cycles are permanently created without the corresponding ICP being destroyed, breaking the ICP/cycles conservation invariant. The notification is simultaneously marked as processed and cannot be retried, making the ICP permanently stranded in the CMC subaccount.

---

### Finding Description

In `rs/nns/cmc/src/main.rs`, two production async functions — `process_mint_cycles` and `process_top_up` — follow this pattern:

1. Deposit cycles to the target account (via `do_mint_cycles` or `deposit_cycles`).
2. Call `burn_and_log(sub, amount).await` to destroy the backing ICP.
3. Return success to the caller.

The `burn_and_log` function is explicitly designed to return `()` regardless of whether the underlying ledger call succeeds or fails: [1](#0-0) 

```rust
/// Attempt to burn the funds.
/// Burning doesn't return errors - we don't want to reject the transaction
/// notification because then it could be retried.
async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens) {
```

Inside `burn_and_log`, the ledger call result is only logged, never propagated: [2](#0-1) 

The callers discard the unit return: [3](#0-2) [4](#0-3) 

After `burn_and_log` returns (whether the burn succeeded or failed), the notification block index is written into `blocks_notified` as `NotifiedMint(Ok(...))` or `NotifiedTopUp(Ok(...))`: [5](#0-4) 

This permanently marks the block as processed. Any subsequent call with the same `block_index` returns the cached success result without re-attempting the burn.

---

### Impact Explanation

**Vulnerability class: Ledger conservation bug.**

If the ICP ledger rejects or fails the `send_pb` burn call (e.g., during a ledger upgrade window, a transient inter-canister reject, or an edge-case insufficient-balance condition in the CMC subaccount), the following state is committed:

- Cycles have been minted and credited to the user's cycles ledger account — irreversible.
- The ICP in the CMC subaccount is **not** burned — it remains there permanently.
- The notification is marked processed — it cannot be retried by the user.
- No error is surfaced to the caller; they receive a success response.

The net effect is that new cycles exist in the system without the corresponding ICP supply reduction. This inflates the effective cycles supply relative to the ICP backing, breaking the economic conservation invariant that underpins the ICP/cycles exchange rate enforced by the CMC.

---

### Likelihood Explanation

**Low-to-medium.** The ICP ledger is a co-located NNS canister. Inter-canister calls within a subnet are reliable under normal operation. However, the failure window is real:

- During ICP ledger upgrades, the canister is briefly stopped and will reject incoming calls with a `CanisterStopped` or `CanisterStopping` error.
- A user who times a `notify_top_up` or `notify_mint_cycles` call to complete its cycles-deposit step during a ledger upgrade window will trigger exactly this condition.
- The CMC and ICP ledger are both NNS canisters upgraded via NNS proposals, which are publicly observable on-chain, making the upgrade window predictable.

An unprivileged ingress sender with no special access can trigger this path by submitting a valid `notify_top_up` or `notify_mint_cycles` call at the right moment.

---

### Recommendation

`burn_and_log` should propagate its result. If the burn fails, `process_mint_cycles` and `process_top_up` should either:

1. **Retry the burn** before marking the notification as processed (preferred), or
2. **Mark the notification as a transient error** (remove it from `blocks_notified`) so the user can retry, accepting the risk of a second cycles-deposit attempt being deduplicated by the cycles ledger.

The current comment justification ("we don't want to reject the transaction notification because then it could be retried") conflates two separate concerns: the cycles deposit (which is idempotent via the cycles ledger's deduplication) and the ICP burn (which must succeed for conservation). These should be handled independently.

---

### Proof of Concept

1. Observer monitors the NNS governance feed for an upcoming ICP ledger upgrade proposal.
2. Attacker sends ICP to their CMC subaccount (`AccountIdentifier::new(CMC_ID, Some(Subaccount::from(&attacker_principal)))`).
3. Attacker submits `notify_mint_cycles` (or `notify_top_up`) timed so that `do_mint_cycles` completes (cycles deposited) just before the ledger upgrade stops the ledger canister.
4. `burn_and_log` calls `send_pb` on the ledger; the ledger is stopped and returns a reject.
5. `burn_and_log` logs the error and returns `()`.
6. `process_mint_cycles` returns `Ok(NotifyMintCyclesSuccess { ... })`.
7. The block index is stored as `NotifiedMint(Ok(...))`.
8. Attacker holds newly minted cycles; the ICP remains unburned in the CMC subaccount.
9. The ICP is permanently stranded — no mechanism exists to retry the burn for an already-processed notification.

### Citations

**File:** rs/nns/cmc/src/main.rs (L1304-1313)
```rust

            with_state_mut(|state| {
                state.blocks_notified.insert(
                    block_index,
                    NotificationStatus::NotifiedMint(result.clone()),
                );
                if is_transient_error(&result) {
                    state.blocks_notified.remove(&block_index);
                }
            });
```

**File:** rs/nns/cmc/src/main.rs (L1966-1974)
```rust
    match do_mint_cycles(to_account, cycles, deposit_memo).await {
        Ok(deposit_result) => {
            burn_and_log(sub, amount).await;
            Ok(NotifyMintCyclesSuccess {
                block_index: deposit_result.block_index,
                minted: cycles.into(),
                balance: deposit_result.balance,
            })
        }
```

**File:** rs/nns/cmc/src/main.rs (L1999-2002)
```rust
    match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
        Ok(()) => {
            burn_and_log(sub, amount).await;
            Ok(cycles)
```

**File:** rs/nns/cmc/src/main.rs (L2014-2017)
```rust
/// Attempt to burn the funds.
/// Burning doesn't return errors - we don't want to reject the transaction
/// notification because then it could be retried.
async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens) {
```

**File:** rs/nns/cmc/src/main.rs (L2040-2048)
```rust
    let res: CallResult<BlockIndex> = call_protobuf(ledger_canister_id, "send_pb", send_args).await;

    match res {
        Ok(block) => print(format!("{msg} done in block {block}.")),
        Err((code, err)) => {
            let code = code as i32;
            print(format!("{msg} failed with code {code}: {err:?}"))
        }
    }
```
