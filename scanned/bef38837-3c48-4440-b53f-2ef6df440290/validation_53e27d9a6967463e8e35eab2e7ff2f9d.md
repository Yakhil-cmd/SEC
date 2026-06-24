### Title
Unchecked ICP Burn Return Value in CMC Allows Cycles Minting Without ICP Conservation - (File: rs/nns/cmc/src/main.rs)

### Summary
The `burn_and_log` function in the Cycles Minting Canister (CMC) silently discards ICP burn failures after cycles have already been minted or deposited. If the ICP ledger call fails transiently, cycles are fully credited to the recipient but the corresponding ICP is never burned, breaking the ICP/cycles conservation invariant.

### Finding Description
In `rs/nns/cmc/src/main.rs`, the function `burn_and_log` is responsible for burning ICP from the CMC's per-operation subaccount after a successful cycles operation. The function is explicitly designed to never propagate errors — it only logs them:

```rust
/// Attempt to burn the funds.
/// Burning doesn't return errors - we don't want to reject the transaction
/// notification because then it could be retried.
async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens) {
    ...
    let res: CallResult<BlockIndex> = call_protobuf(ledger_canister_id, "send_pb", send_args).await;
    match res {
        Ok(block) => print(format!("{msg} done in block {block}.")),
        Err((code, err)) => {
            let code = code as i32;
            print(format!("{msg} failed with code {code}: {err:?}"))
        }
    }
}
``` [1](#0-0) 

This function is called — with its return value (`()`) unconditionally discarded — in three production paths after the irreversible cycles operation has already succeeded:

- `process_top_up`: after `deposit_cycles` succeeds [2](#0-1) 
- `process_mint_cycles`: after `do_mint_cycles` succeeds [3](#0-2) 
- `process_create_canister`: after `do_create_canister` succeeds [4](#0-3) 

In all three cases, the notification block index is subsequently recorded as `NotifiedTopUp(Ok(...))`, `NotifiedMint(Ok(...))`, or `NotifiedCreateCanister(Ok(...))`, permanently preventing any retry. [5](#0-4) 

The analog to the original report is direct: just as `stakedToken.transferFrom(...)` and `fei().transferFrom(...)` returned `false` on failure without the caller checking, `burn_and_log` returns `()` on failure without the caller checking, and the protocol proceeds as if the burn succeeded.

### Impact Explanation
**Vulnerability class: Ledger conservation bug.**

When `burn_and_log` fails (e.g., due to a transient ICP ledger rejection or `TemporarilyUnavailable` response), the following state is reached:

- Cycles have been irreversibly deposited to the target canister or cycles-ledger account.
- The ICP in the CMC's per-operation subaccount is **not** burned.
- The notification is permanently marked as processed; it cannot be retried.
- The ICP remains stranded in the CMC subaccount indefinitely.

This breaks the fundamental economic invariant of the IC: every unit of cycles minted must correspond to a unit of ICP burned. The total ICP supply is not reduced, while the total cycles supply is increased, inflating cycles relative to ICP.

### Likelihood Explanation
**Low but non-zero.** The ICP ledger is a well-maintained system, but transient `TemporarilyUnavailable` or inter-canister call errors are a documented possibility on the IC. The CMC processes a high volume of `notify_top_up` and `notify_mint_cycles` calls. Over a long operational period, a transient ledger error during the burn step is plausible. No privileged access or attacker-controlled trigger is required — any unprivileged user who calls `notify_top_up` or `notify_mint_cycles` at a moment when the ICP ledger is transiently unavailable can trigger this condition.

### Recommendation
The `burn_and_log` function should propagate burn failures back to the caller. If the burn fails, the notification should either:
1. Not be marked as permanently processed (allow retry), or
2. Trigger a compensating action (e.g., refund the ICP to the sender rather than leaving it stranded).

At minimum, a failed burn should be recorded in a recoverable state so that an operator or governance proposal can re-attempt the burn, preserving the conservation invariant.

### Proof of Concept
1. User sends ICP to the CMC subaccount for canister `X` and calls `notify_top_up { block_index: B, canister_id: X }`.
2. CMC calls `fetch_transaction` — succeeds.
3. CMC calls `deposit_cycles(X, cycles, ...)` — succeeds; canister `X` now has cycles.
4. CMC calls `burn_and_log(sub, amount)` — the ICP ledger returns `TemporarilyUnavailable`; the error is logged and discarded.
5. CMC records `blocks_notified[B] = NotifiedTopUp(Ok(cycles))`.
6. User retries `notify_top_up` — CMC returns the cached `Ok(cycles)` immediately without re-attempting the burn.
7. **Result**: Canister `X` has cycles; ICP in CMC subaccount is never burned; ICP/cycles conservation invariant is violated.

### Citations

**File:** rs/nns/cmc/src/main.rs (L1212-1224)
```rust
            let result = process_top_up(canister_id, from, amount, limiter_to_use).await;

            with_state_mut(|state| {
                state.blocks_notified.insert(
                    block_index,
                    NotificationStatus::NotifiedTopUp(result.clone()),
                );
                if is_transient_error(&result) {
                    state.blocks_notified.remove(&block_index);
                }
            });

            result
```

**File:** rs/nns/cmc/src/main.rs (L1943-1946)
```rust
    match do_create_canister(controller, cycles, subnet_selection, settings).await {
        Ok(canister_id) => {
            burn_and_log(sub, amount).await;
            Ok(canister_id)
```

**File:** rs/nns/cmc/src/main.rs (L1966-1968)
```rust
    match do_mint_cycles(to_account, cycles, deposit_memo).await {
        Ok(deposit_result) => {
            burn_and_log(sub, amount).await;
```

**File:** rs/nns/cmc/src/main.rs (L1999-2002)
```rust
    match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
        Ok(()) => {
            burn_and_log(sub, amount).await;
            Ok(cycles)
```

**File:** rs/nns/cmc/src/main.rs (L2014-2049)
```rust
/// Attempt to burn the funds.
/// Burning doesn't return errors - we don't want to reject the transaction
/// notification because then it could be retried.
async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens) {
    let msg = format!("Burning of {amount} ICPTs from subaccount {from_subaccount}");
    let minting_account_id = with_state(|state| state.minting_account_id);
    if minting_account_id.is_none() {
        print(format!("{msg} failed: minting_account_id not set"));
        return;
    }
    let minting_account_id = minting_account_id.unwrap();
    let ledger_canister_id = with_state(|state| state.ledger_canister_id);

    if amount < DEFAULT_TRANSFER_FEE {
        print(format!("{msg}: amount too small ({amount})"));
        return;
    }

    let send_args = SendArgs {
        memo: Memo::default(),
        amount,
        fee: Tokens::ZERO,
        from_subaccount: Some(from_subaccount),
        to: minting_account_id,
        created_at_time: None,
    };
    let res: CallResult<BlockIndex> = call_protobuf(ledger_canister_id, "send_pb", send_args).await;

    match res {
        Ok(block) => print(format!("{msg} done in block {block}.")),
        Err((code, err)) => {
            let code = code as i32;
            print(format!("{msg} failed with code {code}: {err:?}"))
        }
    }
}
```
