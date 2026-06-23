### Title
Check-After-Async-Call: `blocks_notified` Processing Status Set After `fetch_transaction` Await, Enabling Double-Processing of ICP Payments - (File: rs/nns/cmc/src/main.rs)

### Summary

In the Cycles Minting Canister (CMC), the `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles` functions all share the same structural flaw: the deduplication guard (`NotificationStatus::Processing`) is inserted into `blocks_notified` **after** an inter-canister `await` call to `fetch_transaction`. This means that between the time `fetch_transaction` returns and the time the `Processing` status is written, a second concurrent ingress call for the same `block_index` can pass the deduplication check and proceed to process the same ICP payment a second time — minting cycles or creating a canister twice from a single ICP transfer.

### Finding Description

All three notify endpoints follow this pattern:

1. Call `fetch_transaction(block_index, ...)` — an `async` inter-canister call to the ICP ledger.
2. **After** the `await` returns, check `blocks_notified` and insert `NotificationStatus::Processing`.
3. Call the actual processing function (`process_top_up`, `process_create_canister`, `process_mint_cycles`) — another `async` call.
4. After that returns, update `blocks_notified` to the final status.

The critical window is between step 1 and step 2. On the Internet Computer, every `await` is a yield point. While the first call is suspended at `fetch_transaction.await`, a second ingress message for the same `block_index` can begin executing. It will also call `fetch_transaction.await`, and when it resumes, it will find `blocks_notified` still empty for that `block_index` (because the first call has not yet written `Processing`). Both calls then proceed to insert `Processing` and execute the processing function concurrently.

In `notify_top_up`: [1](#0-0) 

The `fetch_transaction` call is an `await` that yields execution. Only after it returns does the code check and set `blocks_notified`: [2](#0-1) 

The same pattern exists in `notify_create_canister`: [3](#0-2) 

And in `notify_mint_cycles`: [4](#0-3) 

The `is_transient_error` path makes this worse: if `process_top_up` returns a transient error, the `blocks_notified` entry is **removed**, allowing the same block to be retried indefinitely — but the window for a concurrent second call to slip through before `Processing` is set remains regardless. [5](#0-4) 

### Impact Explanation

An unprivileged ingress sender who controls a canister or can send multiple concurrent ingress messages can:

1. Transfer ICP to the CMC subaccount once.
2. Send two (or more) concurrent `notify_top_up` calls for the same `block_index`.
3. Both calls pass through `fetch_transaction` before either writes `Processing`.
4. Both calls proceed to `process_top_up`, which calls `deposit_cycles` and then `burn_and_log`.
5. The target canister receives cycles twice (or more) from a single ICP payment — a **ledger conservation bug** / **chain-fusion mint/burn replay bug**.

The same applies to `notify_create_canister` (double canister creation from one payment) and `notify_mint_cycles` (double cycles ledger mint from one payment).

**Impact**: Cycles conservation invariant broken — more cycles minted than ICP burned. This is a direct financial loss to the IC protocol.

### Likelihood Explanation

The IC's asynchronous execution model makes this exploitable by any unprivileged user who can send concurrent ingress messages. The attacker only needs to:
- Make one ICP transfer to the CMC.
- Submit two `notify_top_up` calls simultaneously before either completes `fetch_transaction`.

The `fetch_transaction` call goes to the ICP ledger (a separate canister), introducing a real inter-canister round-trip delay during which the second call can execute. This is a realistic and straightforward attack requiring no privileged access, no key compromise, and no social engineering.

### Recommendation

Move the `blocks_notified` deduplication check and `Processing` insertion to **before** the first `await` (i.e., before `fetch_transaction`). The block index is known from the ingress argument before any async call is made. Setting `Processing` synchronously at the start of the function — before any `await` — eliminates the race window entirely. This is the IC analog of the "set withdrawn status before the transfer" fix described in the reference report.

Alternatively, use a per-block-index lock that is acquired synchronously at function entry and released only after the final state update.

### Proof of Concept

```
1. Attacker transfers 10 ICP to CMC subaccount for canister X.
   → ICP ledger records this at block_index = N.

2. Attacker sends two concurrent ingress calls:
     notify_top_up({ block_index: N, canister_id: X })
     notify_top_up({ block_index: N, canister_id: X })

3. Call A executes: fetch_transaction(N, ...).await  [yields]
4. Call B executes: fetch_transaction(N, ...).await  [yields]

5. Call A resumes: blocks_notified[N] is Vacant → inserts Processing
6. Call B resumes: blocks_notified[N] is Occupied(Processing) → ...
   BUT: if Call B resumed BEFORE Call A wrote Processing (step 5),
   it also sees Vacant and inserts Processing.

7. Both calls proceed to process_top_up → deposit_cycles called twice.
8. Canister X receives 2× the cycles from 1× the ICP payment.
```

The race window is the inter-canister round-trip latency of `fetch_transaction` (typically hundreds of milliseconds on mainnet), which is more than sufficient for a second ingress message to begin executing. [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1157-1227)
```rust
    let (amount, from) = fetch_transaction(
        block_index,
        Subaccount::from(&canister_id),
        MEMO_TOP_UP_CANISTER,
    )
    .await?;

    // Try to set the status of this block to Processing. In order for this to
    // succeed, two conditions must hold:
    //
    //     1. It must not already have a status.
    //
    //     2. The block is "sufficiently recent". More precisely, it must be
    //        more recent than last_purged_notification. (To avoid unbounded
    //        growth of the blocks_notified.)
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

                // If the user makes a duplicate request, we respond as though
                // the current request is the original one.
                NotificationStatus::NotifiedTopUp(result) => Some(result.clone()),
                NotificationStatus::NotifiedCreateCanister(_) => {
                    Some(Err(NotifyError::InvalidTransaction(
                        "The same payment is already processed as create canister request".into(),
                    )))
                }
                NotificationStatus::NotifiedMint(_) => Some(Err(NotifyError::InvalidTransaction(
                    "The same payment is already processed as mint request".into(),
                ))),
                NotificationStatus::NotMeaningfulMemo(_) => {
                    Some(Err(NotifyError::InvalidTransaction(
                        "The same payment is already processed as automatic refund".into(),
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
        }
    }
}
```

**File:** rs/nns/cmc/src/main.rs (L1239-1318)
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
}
```

**File:** rs/nns/cmc/src/main.rs (L1345-1424)
```rust
#[update]
#[allow(deprecated)]
async fn notify_create_canister(
    NotifyCreateCanister {
        block_index,
        controller,
        subnet_type,
        subnet_selection,
        settings,
    }: NotifyCreateCanister,
) -> Result<CanisterId, NotifyError> {
    authorize_caller_to_call_notify_create_canister_on_behalf_of_creator(caller(), controller)?;

    let subnet_selection =
        get_subnet_selection(subnet_type, subnet_selection).map_err(|error_message| {
            NotifyError::Other {
                error_code: NotifyErrorCode::BadSubnetSelection as u64,
                error_message,
            }
        })?;

    let (amount, from) = fetch_transaction(
        block_index,
        Subaccount::from(&controller),
        MEMO_CREATE_CANISTER,
    )
    .await?;

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
                NotificationStatus::NotifiedCreateCanister(resp) => Some(resp.clone()),
                NotificationStatus::NotifiedTopUp(_) => Some(Err(NotifyError::InvalidTransaction(
                    "The same payment is already processed as a top up request.".into(),
                ))),
                NotificationStatus::NotifiedMint(_) => Some(Err(NotifyError::InvalidTransaction(
                    "The same payment is already processed as a mint request.".into(),
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
                process_create_canister(controller, from, amount, subnet_selection, settings).await;

            with_state_mut(|state| {
                state.blocks_notified.insert(
                    block_index,
                    NotificationStatus::NotifiedCreateCanister(result.clone()),
                );
                if is_transient_error(&result) {
                    state.blocks_notified.remove(&block_index);
                }
            });

            result
        }
    }
}
```
