### Title
No Minimum Cycles Output Protection in CMC ICP-to-Cycles Conversion - (`File: rs/nns/cmc/src/main.rs`)

### Summary
The Cycles Minting Canister (CMC) converts ICP to cycles via a two-step process: (1) transfer ICP to a CMC subaccount, then (2) call `notify_top_up` or `notify_mint_cycles`. Neither notification argument struct contains a `min_cycles_expected` field. The conversion uses the live `icp_xdr_conversion_rate` at execution time, which is updated every ~5 minutes by the exchange rate canister. A user who observes a favorable rate, transfers ICP, and then calls the notification after a rate drop receives fewer cycles than expected with no recourse, because the ICP is already committed to the CMC subaccount.

### Finding Description

The `NotifyTopUp` struct contains only `block_index` and `canister_id`:

```rust
pub struct NotifyTopUp {
    pub block_index: BlockIndex,
    pub canister_id: CanisterId,
}
```

The `NotifyMintCyclesArg` struct contains only `block_index`, `to_subaccount`, and `deposit_memo`:

```rust
pub struct NotifyMintCyclesArg {
    pub block_index: BlockIndex,
    pub to_subaccount: Option<icrc_ledger_types::icrc1::account::Subaccount>,
    pub deposit_memo: Option<Vec<u8>>,
}
```

Neither struct has a `min_cycles_expected` field. The conversion is performed by `tokens_to_cycles`, which reads the live `icp_xdr_conversion_rate` from state at execution time:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles {
                xdr_permyriad_per_icp,
                cycles_per_xdr: state.cycles_per_xdr,
            }
            .to_cycles(amount)),
            ...
        }
    })
}
```

This rate is refreshed automatically every `REFRESH_RATE_INTERVAL_SECONDS` (5 minutes) via the exchange rate canister heartbeat. The rate can also be updated by NNS governance proposals at any time. There is no check that the resulting cycles meet any user-specified minimum before the ICP is burned.

### Impact Explanation

A user who:
1. Observes a favorable ICP/XDR rate (e.g., 1 ICP = 100 XDR → 100T cycles)
2. Transfers ICP to the CMC subaccount
3. Calls `notify_top_up` or `notify_mint_cycles` after the rate has dropped (e.g., to 50 XDR → 50T cycles)

…receives half the expected cycles. The ICP is burned unconditionally once `process_top_up` or `process_mint_cycles` succeeds — there is no "rate too low" refund path. The user has no mechanism to abort the conversion if the rate is unfavorable at execution time.

### Likelihood Explanation

The ICP/XDR rate is updated every 5 minutes by the exchange rate canister. In volatile market conditions, the rate can move materially within the window between the ICP transfer (step 1) and the notification call (step 2). This is a normal operational condition, not a contrived attack. Any user performing the two-step flow is exposed to this slippage with no opt-out.

### Recommendation

Add an optional `min_cycles_expected` field to both `NotifyTopUp` and `NotifyMintCyclesArg`:

```rust
// rs/nns/cmc/src/lib.rs
pub struct NotifyTopUp {
    pub block_index: BlockIndex,
    pub canister_id: CanisterId,
    pub min_cycles_expected: Option<Cycles>, // NEW
}

pub struct NotifyMintCyclesArg {
    pub block_index: BlockIndex,
    pub to_subaccount: Option<icrc_ledger_types::icrc1::account::Subaccount>,
    pub deposit_memo: Option<Vec<u8>>,
    pub min_cycles_expected: Option<Cycles>, // NEW
}
```

In `process_top_up` and `process_mint_cycles`, after computing `cycles = tokens_to_cycles(amount)?`, check:

```rust
if let Some(min) = min_cycles_expected {
    if cycles < min {
        let refund_block = refund_icp(sub, from, amount, TOP_UP_CANISTER_REFUND_FEE).await?;
        return Err(NotifyError::Refunded {
            reason: format!("Cycles {} below minimum {}", cycles, min),
            block_index: refund_block,
        });
    }
}
```

### Proof of Concept

1. Alice queries `get_icp_xdr_conversion_rate` and sees 100 XDR/ICP → 100T cycles per ICP.
2. Alice transfers 1 ICP to `CMC_CANISTER_ID` subaccount derived from her principal.
3. Before Alice calls `notify_mint_cycles`, the exchange rate canister heartbeat fires and updates `icp_xdr_conversion_rate` to 50 XDR/ICP.
4. Alice calls `notify_mint_cycles` with `NotifyMintCyclesArg { block_index, to_subaccount: None, deposit_memo: None }`.
5. `tokens_to_cycles` reads the new rate (50 XDR/ICP) and returns 50T cycles.
6. `burn_and_log` burns Alice's 1 ICP unconditionally.
7. Alice receives 50T cycles instead of the 100T she expected — a 50% loss — with no recourse. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** rs/nns/cmc/src/lib.rs (L125-130)
```rust
/// Argument taken by top up notification endpoint
#[derive(Clone, Eq, PartialEq, Hash, Debug, CandidType, Deserialize, Serialize)]
pub struct NotifyTopUp {
    pub block_index: BlockIndex,
    pub canister_id: CanisterId,
}
```

**File:** rs/nns/cmc/src/lib.rs (L257-263)
```rust
/// Argument taken by `notify_mint_cycles` endpoint
#[derive(Clone, Eq, PartialEq, Hash, Debug, CandidType, Deserialize, Serialize)]
pub struct NotifyMintCyclesArg {
    pub block_index: BlockIndex,
    pub to_subaccount: Option<icrc_ledger_types::icrc1::account::Subaccount>,
    pub deposit_memo: Option<Vec<u8>>,
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

**File:** rs/nns/cmc/src/main.rs (L1899-1923)
```rust
// If conversion fails, log and return an error
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles {
                xdr_permyriad_per_icp,
                cycles_per_xdr: state.cycles_per_xdr,
            }
            .to_cycles(amount)),
            None => {
                let error_message =
                    "No conversion rate found in CMC, notification aborted".to_string();
                print(&error_message);
                Err(NotifyError::Other {
                    error_code: NotifyErrorCode::Internal as u64,
                    error_message,
                })
            }
        }
    })
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
