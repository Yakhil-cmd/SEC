### Title
Lack of Caller-Controlled Minimum Cycles Output in CMC ICP-to-Cycles Conversion - (File: `rs/nns/cmc/src/main.rs`)

### Summary
The Cycles Minting Canister (CMC) converts ICP to cycles in a two-step flow: the user first sends ICP to a CMC subaccount, then calls `notify_top_up`, `notify_mint_cycles`, or `notify_create_canister`. The cycles amount received is computed at the time of the notify call using the current `icp_xdr_conversion_rate` stored in CMC state. Neither the argument structs nor the processing functions accept a caller-specified minimum cycles output. Because the ICP/XDR rate is updated periodically by the Exchange Rate Canister (every ~5 minutes), the rate at notify time may be materially lower than what the user observed when they sent the ICP, and the user has no on-chain mechanism to abort the conversion if the rate has moved unfavorably.

### Finding Description
The two-step ICP-to-cycles flow in the CMC is:

1. User sends ICP to a CMC subaccount via the ICP ledger (committed on-chain).
2. User calls `notify_top_up` / `notify_mint_cycles` / `notify_create_canister`.

Inside `notify_top_up`, the call chain is:

```
notify_top_up → process_top_up → tokens_to_cycles(amount)
```

`tokens_to_cycles` reads `state.icp_xdr_conversion_rate` at execution time:

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
``` [1](#0-0) 

The argument structs carry no caller-specified minimum:

```rust
pub struct NotifyTopUp {
    pub block_index: BlockIndex,
    pub canister_id: CanisterId,
}

pub struct NotifyMintCyclesArg {
    pub block_index: BlockIndex,
    pub to_subaccount: Option<...>,
    pub deposit_memo: Option<Vec<u8>>,
}
``` [2](#0-1) [3](#0-2) 

Neither `process_top_up` nor `process_mint_cycles` checks any minimum cycles threshold before burning the ICP: [4](#0-3) [5](#0-4) 

The rate is updated by the Exchange Rate Canister on a ~5-minute cadence: [6](#0-5) 

The `TokensToCycles::to_cycles` conversion is a direct multiplication by the current rate with no floor check: [7](#0-6) 

### Impact Explanation
A user who sends ICP to the CMC and then calls `notify_top_up` or `notify_mint_cycles` may receive materially fewer cycles than they expected if the ICP/XDR rate drops between the two steps. Once `process_top_up` or `process_mint_cycles` succeeds, `burn_and_log` is called and the ICP is permanently burned — the user cannot recover the difference. For large ICP amounts (e.g., subnet rental top-ups, which can be hundreds of thousands of ICP), even a modest rate drop translates to a large cycles shortfall. Canister callers that programmatically call `notify_top_up` to ensure a minimum cycles balance are particularly exposed because they cannot predict the rate at execution time and have no on-chain abort condition.

### Likelihood Explanation
The ICP/XDR rate is updated every ~5 minutes by the Exchange Rate Canister. In normal market conditions the rate does not move dramatically in that window, but ICP is a volatile asset and intraday swings of several percent are common. For automated canister workflows that batch the ICP transfer and the notify call across separate rounds, or for users who pre-fund the CMC subaccount and call notify later, the exposure window is longer. No privileged access or oracle manipulation is required — ordinary market movement is sufficient to trigger the condition.

### Recommendation
**Short term:** Add an optional `min_cycles_out: Option<Cycles>` field to `NotifyTopUp`, `NotifyMintCyclesArg`, and `NotifyCreateCanister`. In `process_top_up` / `process_mint_cycles` / `process_create_canister`, after computing `cycles = tokens_to_cycles(amount)?`, check:

```rust
if let Some(min) = min_cycles_out {
    if cycles < min {
        let refund_block = refund_icp(...).await?;
        return Err(NotifyError::Refunded {
            reason: format!("cycles {} below caller minimum {}", cycles, min),
            block_index: refund_block,
        });
    }
}
```

**Long term:** Always allow callers to express their assumptions about variable conversion rates. Any two-step flow where the conversion rate can change between the commitment step (ICP transfer) and the execution step (notify call) should expose a caller-controlled bound on the output amount.

### Proof of Concept
1. Alice observes `get_icp_xdr_conversion_rate` returning `xdr_permyriad_per_icp = 50_000` (5 XDR/ICP).
2. Alice sends 100 ICP to her CMC subaccount, expecting ~500 trillion cycles.
3. Before Alice calls `notify_top_up`, the Exchange Rate Canister updates the rate to `xdr_permyriad_per_icp = 40_000` (4 XDR/ICP) — a 20% drop consistent with normal market volatility.
4. Alice calls `notify_top_up`. `tokens_to_cycles` reads the new rate and computes ~400 trillion cycles.
5. The ICP is burned. Alice receives 100 trillion fewer cycles than she planned for, with no recourse. No error is returned; the call succeeds.

The same scenario applies to `notify_mint_cycles` and `notify_create_canister` via `process_mint_cycles` and `process_create_canister` respectively. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1140-1162)
```rust
async fn notify_top_up(
    NotifyTopUp {
        block_index,
        canister_id,
    }: NotifyTopUp,
) -> Result<Cycles, NotifyError> {
    let caller = caller();

    let src_canister_principal = SUBNET_RENTAL_CANISTER_ID.get();
    let limiter_to_use =
        if caller == src_canister_principal && canister_id.get() == src_canister_principal {
            // caller and destination needs to be src_canister_principal to get alternate limiter
            CyclesMintingLimiterSelector::SubnetRentalLimit
        } else {
            CyclesMintingLimiterSelector::BaseLimit
        };

    let (amount, from) = fetch_transaction(
        block_index,
        Subaccount::from(&canister_id),
        MEMO_TOP_UP_CANISTER,
    )
    .await?;
```

**File:** rs/nns/cmc/src/main.rs (L1239-1262)
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
```

**File:** rs/nns/cmc/src/main.rs (L1900-1922)
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

**File:** rs/nns/cmc/src/lib.rs (L358-367)
```rust
impl TokensToCycles {
    pub fn to_cycles(&self, icpts: Tokens) -> Cycles {
        Cycles::new(
            icpts.get_e8s() as u128
                * self.xdr_permyriad_per_icp as u128
                * self.cycles_per_xdr.get()
                / (icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000),
        )
    }
}
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```
