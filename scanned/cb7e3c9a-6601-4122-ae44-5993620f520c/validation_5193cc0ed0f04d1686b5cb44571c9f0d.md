### Title
No Minimum Cycles Slippage Protection in ICP-to-Cycles Conversion — (`rs/nns/cmc/src/main.rs`)

### Summary

The Cycles Minting Canister (CMC) converts ICP to cycles via `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles`. None of these endpoints accept a caller-specified minimum cycles amount. The ICP/XDR conversion rate used at execution time is whatever the CMC's state holds at the moment the notification is processed — which can differ from the rate the user observed when they sent their ICP. There is no mechanism for a user to say "only proceed if I receive at least N cycles."

### Finding Description

The two-step ICP-to-cycles flow is:
1. User sends ICP to a CMC subaccount (ledger transfer, recorded on-chain).
2. User calls `notify_top_up` / `notify_create_canister` / `notify_mint_cycles` to trigger conversion.

Between steps 1 and 2, the ICP/XDR rate stored in CMC state can change. The CMC heartbeat calls the Exchange Rate Canister every ~5 minutes and updates `icp_xdr_conversion_rate` in state.

The conversion in `tokens_to_cycles` uses whatever rate is current at notification time:

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

None of the three public notify endpoints accept a `minimum_cycles` parameter:

- `NotifyTopUp` contains only `block_index` and `canister_id`.
- `NotifyCreateCanister` contains only `block_index`, `controller`, `subnet_selection`, and `settings`.
- `NotifyMintCyclesArg` contains only `block_index`, `to_subaccount`, and `deposit_memo`.

Once the ICP transfer is committed on-chain (step 1), the user **cannot cancel**. They must call notify and accept whatever rate the CMC applies. If the rate dropped between step 1 and step 2, the user receives fewer cycles than expected with no recourse — the ICP is burned and the cycles are deposited.

### Impact Explanation

**Impact: Medium.** A user who sent ICP expecting a certain number of cycles (e.g., to meet a canister's minimum cycle requirement) may receive fewer cycles than needed. The ICP is irrevocably burned. For `notify_create_canister`, if the resulting cycles fall below `CREATE_CANISTER_MIN_CYCLES` the call fails and the ICP is refunded minus a fee — but for `notify_top_up` and `notify_mint_cycles`, the cycles are deposited regardless of how low the rate has fallen, with no refund path. The user has no way to specify a floor.

### Likelihood Explanation

**Likelihood: Low.** The ICP/XDR rate is updated at most every 5 minutes via the Exchange Rate Canister heartbeat. Large rate swings within a single 5-minute window are uncommon. However, the two-step flow can span multiple heartbeat cycles if the user delays calling notify, making the window arbitrarily large. Any user who pre-funds a CMC subaccount and calls notify later (e.g., automated tooling, retries) is exposed.

### Recommendation

Add an optional `minimum_cycles: Option<u128>` field to `NotifyTopUp`, `NotifyMintCyclesArg`, and `NotifyCreateCanister`. After computing `cycles = tokens_to_cycles(amount)`, check:

```rust
if let Some(min) = minimum_cycles {
    if cycles < Cycles::new(min) {
        // refund ICP and return error
    }
}
```

This mirrors the standard slippage-protection pattern and lets callers bound their worst-case exchange rate.

### Proof of Concept

1. User queries `get_icp_xdr_conversion_rate` — rate is 10,000 XDR/ICP.
2. User sends 1 ICP to their CMC subaccount (step 1 of the flow).
3. CMC heartbeat fires; Exchange Rate Canister returns a new rate of 5,000 XDR/ICP; `do_set_icp_xdr_conversion_rate` updates state.
4. User calls `notify_top_up` with `{ block_index, canister_id }`.
5. `process_top_up` → `tokens_to_cycles` reads the new rate (5,000 XDR/ICP) and mints 50T cycles instead of the 100T the user expected.
6. ICP is burned; user has no recourse.

The attacker-controlled entry path is the public `notify_top_up` / `notify_mint_cycles` / `notify_create_canister` ingress endpoints, reachable by any unprivileged principal. The vulnerable step is `tokens_to_cycles` reading a rate the user cannot constrain. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1139-1145)
```rust
#[update]
async fn notify_top_up(
    NotifyTopUp {
        block_index,
        canister_id,
    }: NotifyTopUp,
) -> Result<Cycles, NotifyError> {
```

**File:** rs/nns/cmc/src/main.rs (L1239-1245)
```rust
async fn notify_mint_cycles(
    NotifyMintCyclesArg {
        block_index,
        to_subaccount,
        deposit_memo,
    }: NotifyMintCyclesArg,
) -> NotifyMintCyclesResult {
```

**File:** rs/nns/cmc/src/main.rs (L1347-1355)
```rust
async fn notify_create_canister(
    NotifyCreateCanister {
        block_index,
        controller,
        subnet_type,
        subnet_selection,
        settings,
    }: NotifyCreateCanister,
) -> Result<CanisterId, NotifyError> {
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

**File:** rs/nns/cmc/src/lib.rs (L125-153)
```rust
/// Argument taken by top up notification endpoint
#[derive(Clone, Eq, PartialEq, Hash, Debug, CandidType, Deserialize, Serialize)]
pub struct NotifyTopUp {
    pub block_index: BlockIndex,
    pub canister_id: CanisterId,
}

/// Argument taken by create canister notification endpoint
#[derive(Clone, Eq, PartialEq, Debug, CandidType, Deserialize)]
pub struct NotifyCreateCanister {
    pub block_index: BlockIndex,

    /// If this not set to the caller's PrincipalId, notify_create_canister
    /// returns Err.
    ///
    /// Thus, notify_create_canister cannot be called on behalf of another
    /// principal. This might be surprising, but it is intentional.
    ///
    /// If controllers is not set in settings, controllers will be just
    /// [controller]. (Without this "default" behavior, the controller of the
    /// canister would be the Cycles Minting Canister itself.)
    pub controller: PrincipalId,

    #[deprecated(note = "use subnet_selection instead")]
    pub subnet_type: Option<String>,
    pub subnet_selection: Option<SubnetSelection>,

    pub settings: Option<CanisterSettings>,
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L236-279)
```rust
pub async fn update_exchange_rate(
    safe_state: &'static LocalKey<RefCell<Option<State>>>,
    env: &impl Environment,
    xrc_client: &impl ExchangeRateCanisterClient,
) -> Result<(), UpdateExchangeRateError> {
    let now_timestamp_seconds = env.now_timestamp_seconds();
    let current_minute_seconds =
        round_down_to_multiple_of(now_timestamp_seconds, ONE_MINUTE_SECONDS);

    UpdateExchangeRateGuard::with_guard(safe_state, current_minute_seconds, async {
        let call_xrc_result = xrc_client.get_icp_to_xdr_exchange_rate(None).await;
        // Check if updating the rate via the exchange rate canister was disabled while retrieving the rate.
        // If it has, exit early.
        let is_updating_rate_disabled = read_state(safe_state, |state| {
            state
                .update_exchange_rate_canister_state
                .unwrap_or_default()
                == UpdateExchangeRateState::Disabled
        });
        if is_updating_rate_disabled {
            return Err(UpdateExchangeRateError::Disabled);
        }

        match call_xrc_result {
            Ok(exchange_rate) => {
                validate_exchange_rate(&exchange_rate)
                    .map_err(|error| UpdateExchangeRateError::InvalidRate(error.to_string()))?;
                let icp_xdr_conversion_rate = IcpXdrConversionRate::from(exchange_rate);
                if let Err(error) =
                    do_set_icp_xdr_conversion_rate(safe_state, env, icp_xdr_conversion_rate)
                {
                    return Err(UpdateExchangeRateError::FailedToSetRate(error));
                }
            }
            Err(error) => {
                return Err(UpdateExchangeRateError::FailedToRetrieveRate(
                    error.to_string(),
                ));
            }
        };

        Ok(())
    })
    .await
```
