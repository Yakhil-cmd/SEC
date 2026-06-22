### Title
Silent Ledger Burn Failure in CMC Allows Cycles Minting Without ICP Conservation - (File: rs/nns/cmc/src/main.rs)

---

### Summary

The Cycles Minting Canister (CMC) contains a `burn_and_log` function that intentionally swallows ledger transfer errors. After successfully minting cycles or creating a canister, the CMC calls `burn_and_log` to destroy the corresponding ICP. If the ICP ledger rejects or fails this burn call, the error is only printed — the notification is still marked as permanently successful, cycles remain minted, and the ICP is never burned. This is the direct IC analog of the ERC20 unhandled-return-value pattern: a token transfer call whose failure is silently ignored while downstream state proceeds as if it succeeded.

---

### Finding Description

In `rs/nns/cmc/src/main.rs`, the function `burn_and_log` is called from three code paths after cycles have already been deposited or a canister has been created:

- `process_top_up` → `burn_and_log` (line 2001)
- `process_create_canister` → `burn_and_log` (line 1945)
- `process_mint_cycles` → `burn_and_log` (line 1968) [1](#0-0) 

The function makes a protobuf ledger call to burn ICP from the CMC's subaccount: [2](#0-1) 

On error, it only logs the failure and returns `()` — no error is propagated to the caller. The comment at line 2015 explicitly documents this design: *"Burning doesn't return errors - we don't want to reject the transaction notification because then it could be retried."*

The three callers proceed identically regardless of whether `burn_and_log` succeeds: [3](#0-2) [4](#0-3) 

After `burn_and_log` returns, the notification block is recorded as permanently processed (`NotifiedTopUp`, `NotifiedCreateCanister`, or `NotifiedMint`) in `blocks_notified`, preventing any retry: [5](#0-4) 

---

### Impact Explanation

**Vulnerability class: ledger conservation bug.**

If the ICP ledger rejects the burn call (e.g., due to a transient `TemporarilyUnavailable` response, a reject from the replica, or any other call-level error), the following invariant is violated:

> *Every unit of cycles minted by the CMC must correspond to an equal-value ICP burn on the ledger.*

Cycles are minted and the notification is permanently sealed as successful, but the ICP remains unburned in the CMC's subaccount. The ICP is not accessible to the user (it is locked in a CMC-controlled subaccount keyed by `canister_id` or `controller`), but it is also never destroyed. The total ICP supply is therefore higher than it should be relative to the cycles outstanding, breaking the 1:1 backing invariant of the ICP↔cycles exchange.

Because the notification is sealed, there is no recovery path: the same block index cannot be re-submitted, and the CMC has no mechanism to retry failed burns.

---

### Likelihood Explanation

The trigger is a failed inter-canister call to the ICP ledger from the CMC. This can occur due to:

- A transient `TemporarilyUnavailable` response from the ledger (the ledger explicitly exposes this error variant in its interface)
- A replica-level reject (e.g., the ledger canister is being upgraded at the moment of the call)
- Any other call-level error returned by `call_protobuf`

The entry path is fully unprivileged: any principal can call `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` after sending ICP to the CMC's subaccount. The attacker does not need to cause the ledger failure deliberately — it can occur naturally during ledger upgrades or under load. The window is narrow (the burn call must fail after cycles are already deposited), but the condition is reachable without any privileged access.

---

### Recommendation

Propagate the burn error instead of swallowing it, or implement a persistent retry queue for failed burns. One approach:

1. If `burn_and_log` fails, record the pending burn in stable state (subaccount + amount).
2. A periodic heartbeat retries all pending burns.
3. Only seal the notification as permanently successful once the burn is confirmed.

Alternatively, if the current "seal-on-success" semantics must be preserved for idempotency, the failed burn should be recorded in an auditable on-chain log (not just a `print`) and a governance-controlled recovery mechanism should exist to retry or account for the unburned ICP.

---

### Proof of Concept

1. User sends 10 ICP to CMC's top-up subaccount for canister `C`.
2. User calls `notify_top_up { block_index: B, canister_id: C }`.
3. CMC fetches the transaction, verifies it, and calls `deposit_cycles` — cycles are deposited to `C`.
4. CMC calls `burn_and_log(sub, amount)`.
5. At this moment, the ICP ledger returns a transient error (e.g., during a ledger upgrade window).
6. `burn_and_log` logs the error and returns `()`.
7. CMC records `NotifiedTopUp(Ok(cycles))` in `blocks_notified`.
8. The 10 ICP remain unburned in the CMC's subaccount; canister `C` has received the cycles.
9. The block index `B` is permanently sealed — `notify_top_up` with the same `B` returns the cached `Ok` result without re-attempting the burn. [6](#0-5) [1](#0-0)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1214-1222)
```rust
            with_state_mut(|state| {
                state.blocks_notified.insert(
                    block_index,
                    NotificationStatus::NotifiedTopUp(result.clone()),
                );
                if is_transient_error(&result) {
                    state.blocks_notified.remove(&block_index);
                }
            });
```

**File:** rs/nns/cmc/src/main.rs (L1943-1956)
```rust
    match do_create_canister(controller, cycles, subnet_selection, settings).await {
        Ok(canister_id) => {
            burn_and_log(sub, amount).await;
            Ok(canister_id)
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, CREATE_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err,
                block_index: refund_block,
            })
        }
    }
}
```

**File:** rs/nns/cmc/src/main.rs (L1985-2012)
```rust
async fn process_top_up(
    canister_id: CanisterId,
    from: AccountIdentifier,
    amount: Tokens,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;

    let sub = Subaccount::from(&canister_id);

    print(format!(
        "Topping up canister {canister_id} by {cycles} cycles."
    ));

    match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
        Ok(()) => {
            burn_and_log(sub, amount).await;
            Ok(cycles)
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, TOP_UP_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err.to_string(),
                block_index: refund_block,
            })
        }
    }
}
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
