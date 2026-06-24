### Title
CMC `process_mint_cycles` / `process_top_up` / `process_create_canister` Silently Ignore `burn_and_log` Failure, Enabling Cycles Minting Without ICP Burn — (File: `rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) performs the value-dispensing action (minting cycles, topping up a canister, or creating a canister) **before** burning the backing ICP, and the return value of `burn_and_log` is silently discarded. If the ICP ledger call inside `burn_and_log` fails for any reason, the user receives cycles (or a new canister) while the ICP remains unburned in the CMC's subaccount. The notification is simultaneously recorded as fully successful, so the block index can never be re-notified. This is the direct IC analog of the Goldilend ordering bug: a resource-dispensing step executes before the corresponding resource-acquisition step is confirmed, breaking the conservation invariant between ICP supply and cycles supply.

---

### Finding Description

Three async helpers in `rs/nns/cmc/src/main.rs` share the same flawed ordering:

**`process_mint_cycles`** (lines 1958–1983):
```rust
match do_mint_cycles(to_account, cycles, deposit_memo).await {
    Ok(deposit_result) => {
        burn_and_log(sub, amount).await;   // ← result silently dropped
        Ok(NotifyMintCyclesSuccess { ... })
    }
    ...
}
```

**`process_top_up`** (lines 1985–2012):
```rust
match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
    Ok(()) => {
        burn_and_log(sub, amount).await;   // ← result silently dropped
        Ok(cycles)
    }
    ...
}
```

**`process_create_canister`** (lines 1925–1956):
```rust
match do_create_canister(controller, cycles, subnet_selection, settings).await {
    Ok(canister_id) => {
        burn_and_log(sub, amount).await;   // ← result silently dropped
        Ok(canister_id)
    }
    ...
}
```

In every case the pattern is:
1. **Dispense value** (`do_mint_cycles` / `deposit_cycles` / `do_create_canister`) — this is an inter-canister call that can succeed.
2. **Burn ICP** (`burn_and_log`) — this is a second inter-canister call whose `Result` is never inspected.
3. **Return `Ok`** unconditionally, regardless of whether step 2 succeeded.

The caller (`notify_mint_cycles`, `notify_top_up`, `notify_create_canister`) then records the block index as permanently processed:

```rust
state.blocks_notified.insert(
    block_index,
    NotificationStatus::NotifiedMint(result.clone()),
);
```

So even if `burn_and_log` fails, the block is sealed as `NotifiedMint(Ok(...))` and can never be re-notified. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

---

### Impact Explanation

**Ledger conservation break.** The ICP/cycles system relies on the invariant: every batch of cycles minted is backed by an equal-value ICP burn. If `burn_and_log` fails silently:

- Cycles are credited to the user on the cycles ledger (or a canister is created / topped up).
- The ICP remains in the CMC's subaccount — it is neither burned nor refunded.
- The notification block is permanently sealed as successful, so the ICP is permanently stranded.
- The total cycles supply increases without a corresponding decrease in ICP supply, violating the 1:1 backing guarantee.

Repeated occurrences (e.g., during a ledger upgrade window) compound the imbalance. The stranded ICP cannot be recovered by the user (no retry path) and is not automatically swept by the CMC.

---

### Likelihood Explanation

The ICP ledger is an inter-canister call target. It can transiently reject calls during:
- Canister upgrades (the ledger is briefly stopped).
- Subnet congestion causing call queue overflow.
- Any transient `TemporarilyUnavailable` ledger error.

An unprivileged user who sends ICP to the CMC's subaccount and calls `notify_mint_cycles` during such a window will trigger the bug without any special privilege. The window is narrow but recurs with every ledger upgrade. The attacker does not need to cause the unavailability — they only need to observe it (e.g., by monitoring the ledger's upgrade proposals on the NNS dashboard) and time their notification call accordingly.

---

### Recommendation

1. **Propagate the `burn_and_log` result.** Change `burn_and_log` to return a `Result` and propagate errors back to the caller. If the burn fails, the notification should not be sealed as successful.

2. **Burn before dispensing (preferred fix, matching the analog).** Burn the ICP first; only if the burn succeeds, proceed to mint cycles / create canister / top up. This eliminates the window entirely:

```rust
// Correct order:
burn_icp(sub, amount).await?;          // acquire first
do_mint_cycles(to_account, cycles, deposit_memo).await?;  // dispense second
```

3. **If dispensing-first is kept for UX reasons**, at minimum record the notification as failed (or transient) when `burn_and_log` fails, so the user can retry and the ICP is not permanently stranded.

---

### Proof of Concept

**Entry path (unprivileged ingress):**

1. User transfers `N` ICP to `AccountIdentifier(CMC_ID, Subaccount::from(&caller))` on the ICP ledger with `memo = MEMO_MINT_CYCLES`.
2. User calls `notify_mint_cycles { block_index, to_subaccount: None, deposit_memo: None }` on the CMC **during a window when the ICP ledger is temporarily unavailable** (e.g., mid-upgrade).
3. CMC executes `process_mint_cycles`:
   - `do_mint_cycles` succeeds → cycles are credited to the user on the cycles ledger.
   - `burn_and_log` fails (ledger rejects the call) → error is silently dropped.
   - CMC returns `Ok(NotifyMintCyclesSuccess { minted: cycles, ... })` to the user.
4. CMC records `NotificationStatus::NotifiedMint(Ok(...))` for the block index.
5. **Result:** User holds `cycles` worth of cycles. The `N` ICP sits unburned in the CMC's subaccount. The block index is permanently sealed; no refund or retry is possible. ICP supply and cycles supply are now out of balance by `N` ICP. [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1239-1317)
```rust
async fn notify_mint_cycles(
    NotifyMintCyclesArg {
        block_index,
        to_subaccount,
        deposit_memo,
    }: NotifyMintCyclesArg,
) -> NotifyMintCyclesResult {
    let subaccount = Subaccount::from(&caller());
    let to_account = Account {
        owner: caller().into(),
        subaccount: to_subaccount,
    };

    let deposit_memo_len = deposit_memo.as_ref().map_or(0, |memo| memo.len());
    if deposit_memo_len > MAX_MEMO_LENGTH {
        return Err(NotifyError::Other {
            error_code: NotifyErrorCode::DepositMemoTooLong as u64,
            error_message: format!(
                "Memo length {deposit_memo_len} exceeds the maximum length of {MAX_MEMO_LENGTH}"
            ),
        });
    }

    let (amount, from) = fetch_transaction(block_index, subaccount, MEMO_MINT_CYCLES).await?;

    let maybe_early_result = with_state_mut(|state| {
        state.purge_old_notifications(MAX_NOTIFY_HISTORY);

        if block_index <= state.last_purged_notification {
            return Some(Err(NotifyError::TransactionTooOld(
                state.last_purged_notification + 1,
            )));
        }

        match state.blocks_notified.entry(block_index) {
            Entry::Occupied(entry) => match entry.get() {
                NotificationStatus::Processing => Some(Err(NotifyError::Processing)),
                NotificationStatus::NotifiedMint(resp) => Some(resp.clone()),
                NotificationStatus::NotifiedCreateCanister(_) => {
                    Some(Err(NotifyError::InvalidTransaction(
                        "The same payment is already processed as a create canister request."
                            .into(),
                    )))
                }
                NotificationStatus::NotifiedTopUp(_) => Some(Err(NotifyError::InvalidTransaction(
                    "The same payment is already processed as a top up request.".into(),
                ))),
                NotificationStatus::NotMeaningfulMemo(_) => {
                    Some(Err(NotifyError::InvalidTransaction(
                        "The same payment is already processed as an automatic refund.".into(),
                    )))
                }
            },
            Entry::Vacant(entry) => {
                entry.insert(NotificationStatus::Processing);
                None
            }
        }
    });

    match maybe_early_result {
        Some(result) => result,
        None => {
            let result =
                process_mint_cycles(to_account, amount, deposit_memo, from, subaccount).await;

            with_state_mut(|state| {
                state.blocks_notified.insert(
                    block_index,
                    NotificationStatus::NotifiedMint(result.clone()),
                );
                if is_transient_error(&result) {
                    state.blocks_notified.remove(&block_index);
                }
            });

            result
        }
    }
```

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

**File:** rs/nns/cmc/src/main.rs (L2140-2172)
```rust
async fn do_mint_cycles(
    account: Account,
    cycles: Cycles,
    deposit_memo: Option<Vec<u8>>,
) -> Result<CyclesLedgerDepositResult, String> {
    let Some(cycles_ledger_canister_id) = with_state(|state| state.cycles_ledger_canister_id)
    else {
        return Err("No cycles ledger canister id configured.".to_string());
    };
    // Always use base cycles limit for minting cycles, since the Subnet Rental Canister
    // doesn't call endpoints using this function.
    ensure_balance(cycles, CyclesMintingLimiterSelector::BaseLimit)?;

    let arg = CyclesLedgerDepositArgs {
        to: account,
        memo: deposit_memo,
    };

    let result: CallResult<(CyclesLedgerDepositResult,)> = ic_cdk::api::call::call_with_payment128(
        cycles_ledger_canister_id.get().0,
        "deposit",
        (arg,),
        u128::from(cycles),
    )
    .await;

    result.map(|r| r.0).map_err(|(code, msg)| {
        format!(
            "Cycles ledger rejected deposit call with code {}: {:?}",
            code as i32, msg
        )
    })
}
```
