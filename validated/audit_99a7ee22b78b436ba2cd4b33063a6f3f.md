### Title
CMC ICP/XDR Rate Frozen During Volatility Causes Stale Cycles Pricing and Permanent Disable-Until-Governance - (`rs/nns/cmc/src/exchange_rate_canister.rs`)

### Summary

The Cycles Minting Canister (CMC) periodically fetches the ICP/XDR exchange rate from the Exchange Rate Canister (XRC) to price cycles minting. During high ICP price volatility, the XRC is likely to return `InconsistentRatesReceived` or fail minimum-source validation, causing the CMC to stop updating its rate. The CMC then uses a stale rate for all cycles minting operations. A compounding design flaw: when a `DivergedRate` NNS proposal is executed, the CMC enters `UpdateExchangeRateState::Disabled` permanently, and re-enabling requires a new NNS governance proposal that takes days to pass — the exact period when accurate pricing is most critical.

### Finding Description

The CMC's heartbeat calls `update_exchange_rate()` in `rs/nns/cmc/src/exchange_rate_canister.rs`, which calls the XRC and validates the result. Two failure paths exist:

**Path 1 — Persistent XRC failure during volatility:**
The `validate_exchange_rate()` function in `rs/nervous_system/clients/src/exchange_rate_canister_client.rs` requires at least `MINIMUM_ICP_SOURCES = 4` and `MINIMUM_CXDR_SOURCES = 4` received sources. During high volatility, the XRC itself may return `ExchangeRateError::InconsistentRatesReceived` (when collected rates from different exchanges deviate substantially), or individual exchange HTTP outcalls may fail, dropping below the minimum source count. Either causes `update_exchange_rate` to return `Err(UpdateExchangeRateError::FailedToRetrieveRate(...))` or `Err(UpdateExchangeRateError::InvalidRate(...))`. The guard then schedules a retry at the next minute, but if volatility persists, the CMC keeps failing and the stored `icp_xdr_conversion_rate` becomes increasingly stale.

**Path 2 — Permanent disable via `DivergedRate` proposal:**
`set_update_exchange_rate_state()` in `rs/nns/cmc/src/exchange_rate_canister.rs` handles the `DivergedRate` reason by unconditionally setting `UpdateExchangeRateState::Disabled`. Once disabled, `UpdateExchangeRateGuard::new()` immediately returns `Err(UpdateExchangeRateError::Disabled)` on every heartbeat, permanently halting all rate updates. The only recovery path is a new NNS proposal with `EnableAutomaticExchangeRateUpdates` reason — which requires days of voting.

All cycles minting operations (`notify_top_up`, `notify_create_canister`, `notify_mint_cycles`) call `tokens_to_cycles()` in `rs/nns/cmc/src/main.rs`, which reads `state.icp_xdr_conversion_rate` directly with no staleness check. If the rate is set (even to a value days old), it is used as-is.

### Impact Explanation

When the ICP/XDR rate is stale during a significant price move:
- If ICP price drops 20–30% and the rate is frozen at the pre-drop value, users receive more cycles per ICP than the protocol intends — a direct economic loss to the IC ecosystem.
- If ICP price rises and the rate is frozen at the pre-rise value, users receive fewer cycles per ICP than they should — a user-facing economic harm.
- The `DivergedRate` disable path means the CMC can be stuck with a stale rate for the entire duration of a governance vote (minimum several days), during which all cycles minting is priced incorrectly.
- This is most harmful precisely during the periods of highest volatility, when accurate pricing is most important.

### Likelihood Explanation

- The XRC's `InconsistentRatesReceived` error is explicitly designed to fire when collected rates deviate substantially — a condition that is likely during high ICP price volatility.
- The `DivergedRate` proposal mechanism is the documented response to rate divergence, meaning it is expected to be triggered in exactly these conditions.
- NNS governance proposals require a minimum voting period of days, making timely recovery structurally impossible during fast-moving market events.
- ICP price volatility of 20%+ within hours has occurred historically.

### Recommendation

1. Add a staleness check in `tokens_to_cycles()`: if `icp_xdr_conversion_rate.timestamp_seconds` is older than a configurable threshold (e.g., 30 minutes), reject the minting operation or apply a conservative fallback rate rather than silently using a stale value.
2. Instead of permanently disabling the XRC update loop on `DivergedRate`, implement a time-bounded backoff (e.g., retry after 10 minutes with exponential backoff) so the CMC self-heals when the XRC recovers.
3. Decouple the `DivergedRate` governance proposal from the permanent disable: the proposal can set a manual override rate while still allowing the XRC loop to continue attempting updates.

### Proof of Concept

**Step 1:** ICP price drops 25% within 30 minutes. The XRC begins returning `InconsistentRatesReceived` because CEX and DEX prices diverge during the move.

**Step 2:** The CMC heartbeat calls `update_exchange_rate()`. The XRC call returns `Err(GetExchangeRateError::Xrc(ExchangeRateError::InconsistentRatesReceived))`, causing `update_exchange_rate` to return `Err(UpdateExchangeRateError::FailedToRetrieveRate(...))`. [1](#0-0) 

**Step 3:** The guard schedules a retry at the next minute, but volatility persists. The CMC's `icp_xdr_conversion_rate` remains at the pre-drop value. [2](#0-1) 

**Step 4:** An NNS observer submits a `DivergedRate` proposal. When executed, `set_update_exchange_rate_state()` sets `UpdateExchangeRateState::Disabled`. [3](#0-2) 

**Step 5:** Every subsequent heartbeat hits the guard's early-exit check and returns immediately without updating the rate. [4](#0-3) 

**Step 6:** Users calling `notify_top_up` or `notify_mint_cycles` trigger `tokens_to_cycles()`, which reads the stale `icp_xdr_conversion_rate` with no staleness check and mints cycles at the pre-drop (inflated) ICP price — giving users ~33% more cycles per ICP than the current market rate warrants. [5](#0-4) 

**Step 7:** Recovery requires a new NNS proposal with `EnableAutomaticExchangeRateUpdates`. Until that proposal passes (days), all cycles minting continues at the stale rate. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L98-100)
```rust
        if current_call_state == UpdateExchangeRateState::Disabled {
            return Err(UpdateExchangeRateError::Disabled);
        }
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L259-275)
```rust
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
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L302-309)
```rust
                UpdateIcpXdrConversionRatePayloadReason::EnableAutomaticExchangeRateUpdates => {
                    if current_update_exchange_rate_state == UpdateExchangeRateState::Disabled {
                        state.update_exchange_rate_canister_state.replace(
                            UpdateExchangeRateState::get_rate_at_next_refresh_rate_interval(
                                rate_timestamp_seconds,
                            ),
                        );
                    }
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L311-315)
```rust
                UpdateIcpXdrConversionRatePayloadReason::DivergedRate => {
                    state
                        .update_exchange_rate_canister_state
                        .replace(UpdateExchangeRateState::Disabled);
                }
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

**File:** rs/nns/common/src/types.rs (L87-96)
```rust
pub enum UpdateIcpXdrConversionRatePayloadReason {
    /// The timestamp of the rate stored in the CMC is older than the execution interval.
    OldRate,
    /// The relative difference between the rate in the CMC and the rate the conversion rate provider retrieved exceeds
    /// a threshold defined by the conversion rate provider.
    DivergedRate,
    /// Used to restart the cycles minting canister automatic exchange rate update mechanism
    /// that calls the exchange rate canister.
    EnableAutomaticExchangeRateUpdates,
}
```
