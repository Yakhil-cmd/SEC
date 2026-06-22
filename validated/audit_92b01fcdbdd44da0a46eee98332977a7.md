### Title
Missing Staleness Check on ICP/XDR Conversion Rate Allows Excess Cycles Minting - (File: rs/nns/cmc/src/main.rs)

### Summary

The Cycles Minting Canister (CMC) converts ICP to cycles using a cached `icp_xdr_conversion_rate`. The function `tokens_to_cycles` reads only the numeric rate value and never checks the rate's `timestamp_seconds` for freshness. If the Exchange Rate Canister (XRC) becomes unavailable and the ICP price drops significantly, any unprivileged caller of `notify_top_up`, `notify_mint_cycles`, or `notify_create_canister` can mint more cycles per ICP than the current market rate warrants, causing the protocol to issue cycles at a discount.

### Finding Description

`tokens_to_cycles` in `rs/nns/cmc/src/main.rs` is the single conversion point used by all three public minting entry-points:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);   // timestamp_seconds is silently dropped
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles { ... }.to_cycles(amount)),
            None => Err(...),   // only None is rejected; a stale rate is accepted
        }
    })
}
``` [1](#0-0) 

The stored `IcpXdrConversionRate` carries a `timestamp_seconds` field, but it is never compared against the current time before the rate is used for minting. [2](#0-1) 

The rate is refreshed by a heartbeat-driven loop that calls the XRC every `REFRESH_RATE_INTERVAL_SECONDS` (5 minutes). [3](#0-2) 

When the XRC call fails, the CMC retries every minute but **keeps the old rate in state unchanged** — there is no circuit-breaker that rejects minting when the rate has not been refreshed for, say, one hour. [4](#0-3) 

A second staleness window arises from the `Disabled` state: when a governance proposal arrives with reason `DivergedRate`, automatic XRC polling is halted entirely. [5](#0-4) 

During either window the cached rate can diverge arbitrarily from the live market price, yet `tokens_to_cycles` will accept it without complaint.

### Impact Explanation

If the ICP market price falls while the CMC holds a stale (higher) rate, every caller of `notify_top_up` / `notify_mint_cycles` / `notify_create_canister` receives more cycles per ICP than the current market rate justifies. Cycles are backed by burned ICP; issuing excess cycles is inflationary and represents a direct resource-accounting loss for the protocol. The magnitude scales with both the staleness duration and the size of the ICP price move. [6](#0-5) 

### Likelihood Explanation

The XRC is a system canister and is generally available, but the heartbeat path can stall for legitimate reasons (XRC errors, canister upgrades, the `Disabled` state). The `Disabled` state in particular can persist until a new governance proposal re-enables polling, a window that could span hours. During any such window an unprivileged user can call `notify_top_up` with no special privileges; the only external precondition is that the ICP price has moved downward while the rate was frozen. [7](#0-6) 

### Recommendation

In `tokens_to_cycles`, compare `rate.timestamp_seconds` against `now_seconds()` and return an error (or a retriable error) when the age exceeds a defined threshold (e.g., 30 minutes):

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        match state.icp_xdr_conversion_rate.as_ref() {
            Some(rate) => {
                let age = now_seconds().saturating_sub(rate.timestamp_seconds);
                if age > MAX_RATE_AGE_SECONDS {
                    return Err(NotifyError::Other {
                        error_code: NotifyErrorCode::Internal as u64,
                        error_message: format!("ICP/XDR rate is stale ({age}s old)"),
                    });
                }
                Ok(TokensToCycles {
                    xdr_permyriad_per_icp: rate.xdr_permyriad_per_icp,
                    cycles_per_xdr: state.cycles_per_xdr,
                }.to_cycles(amount))
            }
            None => Err(...),
        }
    })
}
```

The threshold should be chosen to be larger than the normal refresh interval (5 min) but small enough to bound the worst-case price divergence.

### Proof of Concept

1. The XRC becomes unavailable (e.g., due to repeated errors or the `Disabled` state being set by a governance proposal). The CMC's cached `icp_xdr_conversion_rate` freezes at, say, `xdr_permyriad_per_icp = 100_000` (10 XDR/ICP).
2. Over the next several hours the ICP market price drops 20 % to 8 XDR/ICP, but the CMC still holds the stale rate.
3. An attacker transfers 1 ICP to the CMC subaccount for their canister and calls `notify_top_up`.
4. `tokens_to_cycles` reads `xdr_permyriad_per_icp = 100_000` without checking `timestamp_seconds`, computes cycles as if ICP is worth 10 XDR, and mints ~25 % more cycles than the current market rate (10 XDR vs 8 XDR) would justify.
5. The attacker repeats until the rate is refreshed, extracting excess cycles proportional to the price gap and the ICP deposited. [1](#0-0) [8](#0-7)

### Citations

**File:** rs/nns/cmc/src/main.rs (L217-219)
```rust
    /// How many XDR 1 ICP is worth, along with a timestamp.
    pub icp_xdr_conversion_rate: Option<IcpXdrConversionRate>,

```

**File:** rs/nns/cmc/src/main.rs (L1139-1162)
```rust
#[update]
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

**File:** rs/nns/cmc/src/main.rs (L1985-2011)
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
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L149-161)
```rust
                Err(error) => match error {
                    UpdateExchangeRateError::UpdateAlreadyInProgress => {}
                    UpdateExchangeRateError::Disabled => {}
                    UpdateExchangeRateError::NotReadyToGetRate(_) => {}
                    UpdateExchangeRateError::FailedToRetrieveRate(_)
                    | UpdateExchangeRateError::FailedToSetRate(_)
                    | UpdateExchangeRateError::InvalidRate(_) => {
                        state.update_exchange_rate_canister_state.replace(
                            UpdateExchangeRateState::get_rate_at_next_minute(
                                self.current_minute_in_seconds,
                            ),
                        );
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L291-300)
```rust
pub fn set_update_exchange_rate_state(
    safe_state: &'static LocalKey<RefCell<Option<State>>>,
    maybe_reason: &Option<UpdateIcpXdrConversionRatePayloadReason>,
    rate_timestamp_seconds: u64,
) {
    if let Some(reason) = maybe_reason {
        mutate_state(safe_state, |state| {
            let current_update_exchange_rate_state = state
                .update_exchange_rate_canister_state
                .unwrap_or_default();
```
