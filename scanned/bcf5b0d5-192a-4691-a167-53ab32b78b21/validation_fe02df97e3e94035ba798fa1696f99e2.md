### Title
Unchecked ICP Burn Return Value in CMC Allows ICP Supply Inflation After Cycles Minting - (File: rs/nns/cmc/src/main.rs)

---

### Summary

The Cycles Minting Canister (CMC) intentionally discards the result of the ICP burn operation (`burn_and_log`) after cycles have already been minted, topped up, or a canister has been created. If the ICP ledger call fails at that point, cycles are delivered to the caller but the corresponding ICP is never destroyed, permanently inflating the ICP supply.

---

### Finding Description

In `rs/nns/cmc/src/main.rs`, three notification handlers — `process_mint_cycles`, `process_top_up`, and `process_create_canister` — follow the same pattern:

1. Perform the valuable operation (mint cycles to cycles ledger, deposit cycles to a canister, or create a canister).
2. Call `burn_and_log` to destroy the ICP held in the CMC's subaccount.

`burn_and_log` is declared `async fn burn_and_log(...) -> ()`. It issues a `send_pb` call to the ICP ledger to transfer the ICP to the minting account (burning it), but the `CallResult<BlockIndex>` is matched only for logging:

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

The function returns `()` regardless of success or failure. The callers proceed to return `Ok(...)` to the user unconditionally:

```rust
// process_mint_cycles
Ok(deposit_result) => {
    burn_and_log(sub, amount).await;   // result silently dropped
    Ok(NotifyMintCyclesSuccess { ... })
}

// process_top_up
Ok(()) => {
    burn_and_log(sub, amount).await;   // result silently dropped
    Ok(cycles)
}

// process_create_canister
Ok(canister_id) => {
    burn_and_log(sub, amount).await;   // result silently dropped
    Ok(canister_id)
}
```

The code comment explicitly acknowledges this:

> "Burning doesn't return errors - we don't want to reject the transaction notification because then it could be retried."

The design rationale is to prevent double-minting on retry, but the consequence is that a failed burn leaves ICP permanently stranded in the CMC's subaccount while cycles have already been issued.

---

### Impact Explanation

**Vulnerability class: Ledger conservation bug / chain-fusion mint/burn accounting bug.**

When `burn_and_log` fails:
- Cycles (or a canister) have already been delivered to the caller.
- The ICP that was sent to the CMC's subaccount is never burned (never sent to the minting account).
- The ICP stays in the CMC's subaccount with no recovery path: the notification is recorded as `NotifiedMint(Ok(...))` / `NotifiedTopUp(Ok(...))` / `NotifiedCreateCanister(Ok(...))`, so the user cannot retry, and there is no mechanism to drain the stuck ICP.
- The circulating ICP supply is higher than the protocol invariant requires (every cycle minted must correspond to burned ICP).

This is the direct IC analog of the ERC20 `transferFrom` unchecked-return-value class: a transfer/burn call whose failure is silently swallowed, allowing the downstream operation to complete without the corresponding ledger debit being confirmed.

---

### Likelihood Explanation

The ICP ledger and the cycles ledger are independent canisters. A window exists whenever the ICP ledger is temporarily unavailable (e.g., during an NNS-governed upgrade of the ICP ledger canister) while the cycles ledger remains live. During that window:

- `fetch_transaction` queries the ICP ledger via `block_pb` — if the ledger is mid-upgrade this call will fail and the notification will be rejected before cycles are minted, so no harm occurs.
- However, if the ledger becomes unavailable *after* `fetch_transaction` returns but *before* `burn_and_log` completes (i.e., the ledger upgrade starts between the two async await points), `do_mint_cycles` / `deposit_cycles` succeeds against the cycles ledger, and then `burn_and_log`'s `send_pb` call to the ICP ledger is rejected.

NNS upgrade proposals are public and their execution timing is observable on-chain, making this window predictable. An unprivileged caller who times a `notify_mint_cycles` call to straddle a ledger upgrade can reliably trigger the condition. The caller still pays ICP (it is locked in the CMC subaccount), so there is no "free cycles" benefit to the individual caller, but the ICP supply invariant is violated for the ecosystem.

---

### Recommendation

1. **Return a `Result` from `burn_and_log`** and treat a burn failure as a transient error that clears the `blocks_notified` entry, allowing the user to retry — but only after the cycles/canister delivery is rolled back or the cycles ledger deposit is reversed first. This requires a two-phase commit or a compensating transaction.

2. **Alternatively**, record the failed burn in persistent state and retry it asynchronously on a heartbeat/timer, so that the ICP is eventually burned even if the ledger was temporarily unavailable at notification time.

3. **At minimum**, emit a certified metric or stable-memory counter for unburned ICP amounts so that operators can detect and audit conservation violations.

---

### Proof of Concept

**Trigger path (unprivileged ingress caller):**

1. Caller sends ICP to `AccountIdentifier::new(CMC_ID, Subaccount::from(&caller()))` via `icrc1_transfer` on the ICP ledger. Block index `B` is recorded.
2. An NNS upgrade proposal for the ICP ledger is submitted and its execution is imminent (observable on-chain).
3. Caller submits `notify_mint_cycles({ block_index: B, to_subaccount: None, deposit_memo: None })` to the CMC, timed so that the ICP ledger upgrade fires between the `fetch_transaction` await and the `burn_and_log` await.
4. `fetch_transaction` succeeds (ledger still live), `do_mint_cycles` succeeds (cycles ledger unaffected), `burn_and_log`'s `send_pb` call is rejected (ICP ledger now upgrading).
5. CMC records `NotificationStatus::NotifiedMint(Ok(...))` and returns success to the caller.
6. Caller has received cycles; ICP remains in CMC subaccount; ICP supply is inflated by `amount`.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1925-1956)
```rust
async fn process_create_canister(
    controller: PrincipalId,
    from: AccountIdentifier,
    amount: Tokens,
    subnet_selection: Option<SubnetSelection>,
    settings: Option<CanisterSettings>,
) -> Result<CanisterId, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;

    let sub = Subaccount::from(&controller);

    print(format!(
        "Creating canister with controller {controller} with {cycles} cycles.",
    ));

    // Create the canister. If this fails, refund. Either way,
    // return a result so that the notification cannot be retried.
    // If refund fails, we allow to retry.
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

**File:** rs/nns/cmc/src/main.rs (L1958-1983)
```rust
async fn process_mint_cycles(
    to_account: Account,
    amount: Tokens,
    deposit_memo: Option<Vec<u8>>,
    from: AccountIdentifier,
    sub: Subaccount,
) -> NotifyMintCyclesResult {
    let cycles = tokens_to_cycles(amount)?;
    match do_mint_cycles(to_account, cycles, deposit_memo).await {
        Ok(deposit_result) => {
            burn_and_log(sub, amount).await;
            Ok(NotifyMintCyclesSuccess {
                block_index: deposit_result.block_index,
                minted: cycles.into(),
                balance: deposit_result.balance,
            })
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, MINT_CYCLES_REFUND_FEE).await?;
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
