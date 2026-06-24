### Title
Stale ICP/XDR Exchange Rate Used Without Age Validation in Cycle Minting - (`rs/nns/cmc/src/main.rs`)

### Summary
The Cycles Minting Canister (CMC) converts ICP to cycles using a stored `icp_xdr_conversion_rate` that is never checked for staleness before use. When the Exchange Rate Canister (XRC) is temporarily unavailable, the stored rate can become arbitrarily old. Any unprivileged user calling `notify_top_up` or `notify_mint_cycles` during this window can exploit the stale (inflated) rate by purchasing ICP cheaply on the open market and converting it to cycles at the outdated price, minting more cycles than the ICP is worth at current market value.

### Finding Description

The `tokens_to_cycles` function in `rs/nns/cmc/src/main.rs` reads `state.icp_xdr_conversion_rate` and uses it unconditionally, with no check on the age of the stored rate:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles { ... }.to_cycles(amount)),
            None => Err(...),
        }
    })
}
``` [1](#0-0) 

The rate is updated periodically via `update_exchange_rate`, which calls the XRC every `REFRESH_RATE_INTERVAL_SECONDS` (5 minutes): [2](#0-1) 

When the XRC returns errors (e.g., `StablecoinRateTooFewRates`, `CryptoBaseAssetNotFound`), the update fails silently and the CMC retains the last known rate: [3](#0-2) 

The `validate_exchange_rate` function only checks that enough data sources responded — it does **not** validate the age of the rate: [4](#0-3) 

Similarly, `do_set_icp_xdr_conversion_rate` only enforces monotonically increasing timestamps (new rate must be newer than current), but imposes no upper bound on how old the current rate may be when it is used for conversions: [5](#0-4) 

The `IcpXdrConversionRate` struct carries a `timestamp_seconds` field that is never consulted at conversion time: [6](#0-5) 

The circuit breaker (`UpdateExchangeRateState::Disabled`) exists but requires a governance proposal with `DivergedRate` reason — it is not triggered automatically by rate staleness: [7](#0-6) 

### Impact Explanation

If ICP's market price drops significantly while the CMC's stored rate is stale (e.g., during an XRC outage), an attacker can:

1. Buy ICP cheaply on the open market at the depressed price.
2. Call `notify_top_up` or `notify_mint_cycles` to convert that ICP to cycles at the CMC's inflated stale rate.
3. Receive more cycles than the ICP is worth at current market value.

This is a **cycles/resource accounting bug**: the protocol mints cycles in excess of the real economic value of the ICP burned, inflating the cycle supply. The per-hour rate limiter (`base_cycles_limit`) provides partial mitigation but does not eliminate the issue — it only bounds the rate of exploitation, not the total exposure over a sustained XRC outage. [8](#0-7) 

### Likelihood Explanation

XRC failures are a documented and tested scenario (the codebase has explicit tests for `StablecoinRateTooFewRates`, `CryptoBaseAssetNotFound`, and insufficient source counts). During such failures the CMC retries after 1 minute but continues serving conversions at the stale rate. ICP price can move materially in minutes. The entry path (`notify_top_up`) is open to any unprivileged user with an ICP ledger account. No special access is required. [9](#0-8) 

### Recommendation

1. **Add a staleness guard in `tokens_to_cycles`**: compare `rate.timestamp_seconds` against `now_seconds()` and reject conversions (or return a specific error) if the rate is older than a configurable threshold (e.g., 30 minutes).
2. **Automatic circuit breaker**: if the rate has not been refreshed within `N` minutes, automatically pause cycle minting without requiring a governance proposal.
3. **Expose rate age in metrics**: the existing `cmc_icp_xdr_conversion_rate_timestamp_seconds` metric already exists; add an alert threshold so operators are notified before the rate becomes dangerously stale. [10](#0-9) 

### Proof of Concept

1. XRC begins returning `StablecoinRateTooFewRates` errors. The CMC's stored rate freezes at, say, `xdr_permyriad_per_icp = 50_000` (5 XDR/ICP).
2. ICP market price drops 20% to 4 XDR/ICP. The CMC rate remains at 5 XDR/ICP.
3. Attacker buys 100 ICP at market for 400 XDR worth of value.
4. Attacker calls `notify_top_up` with those 100 ICP. `tokens_to_cycles` computes cycles using the stale rate of 5 XDR/ICP, minting cycles equivalent to 500 XDR.
5. Attacker receives 25% more cycles than the ICP they burned is worth at current market price — a direct cycles inflation event bounded only by the hourly rate limiter. [11](#0-10)

### Citations

**File:** rs/nns/cmc/src/main.rs (L232-244)
```rust
    /// How many cycles are allowed to be minted in an hour.
    pub base_cycles_limit: Cycles,

    /// How many cycles are allowed to be minted by the Subnet Rental Canister in a month.
    pub subnet_rental_cycles_limit: Cycles,

    /// Maintain a count of how many cycles have been minted in the last hour.
    pub base_limiter: limiter::Limiter,

    /// Maintain a count of how many cycles have been minted by the Subnet Rental Canister
    /// in the last month.
    pub subnet_rental_canister_limiter: limiter::Limiter,

```

**File:** rs/nns/cmc/src/main.rs (L1022-1033)
```rust
    mutate_state(safe_state, |state| {
        if let Some(current_conversion_rate) = state.icp_xdr_conversion_rate.as_ref()
            && proposed_conversion_rate.timestamp_seconds
                <= current_conversion_rate.timestamp_seconds
        {
            return Err(
                "Proposed conversion rate must have greater timestamp than current one".to_string(),
            );
        }

        state.icp_xdr_conversion_rate = Some(proposed_conversion_rate.clone());
        update_recent_icp_xdr_rates(state, &proposed_conversion_rate);
```

**File:** rs/nns/cmc/src/main.rs (L1900-1923)
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

**File:** rs/nns/cmc/src/main.rs (L2493-2501)
```rust
        w.encode_gauge(
            "cmc_icp_xdr_conversion_rate_timestamp_seconds",
            state
                .icp_xdr_conversion_rate
                .as_ref()
                .unwrap()
                .timestamp_seconds as f64,
            "Timestamp of the last ICP/XDR conversion rate, in seconds since the Unix epoch.",
        )?;
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L311-315)
```rust
                UpdateIcpXdrConversionRatePayloadReason::DivergedRate => {
                    state
                        .update_exchange_rate_canister_state
                        .replace(UpdateExchangeRateState::Disabled);
                }
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L530-554)
```rust
    #[test]
    fn test_periodic_calls_the_xrc_and_call_fails() {
        thread_local! {
            static STATE: RefCell<Option<State>> = RefCell::new(Some(State::default()));
        }

        let env = TestExchangeRateCanisterEnvironment {
            now_timestamp_seconds: 1680044700,
            ..Default::default()
        };
        let xrc_client = MockExchangeRateCanisterClient::new(
            vec![Err(GetExchangeRateError::Xrc(
                ExchangeRateError::CryptoBaseAssetNotFound,
            ))]
            .into(),
        );
        let result = update_exchange_rate(&STATE, &env, &xrc_client)
            .now_or_never()
            .unwrap();

        assert!(
            matches!(result, Err(UpdateExchangeRateError::FailedToRetrieveRate(message)) if message == "The crypto base asset could not be found")
        );
        assert!(xrc_client.calls.lock().unwrap().is_empty());
    }
```

**File:** rs/nervous_system/clients/src/exchange_rate_canister_client.rs (L110-129)
```rust
/// Validates that an ICP/CXDR exchange rate has enough sources.
pub fn validate_exchange_rate(
    exchange_rate: &ExchangeRate,
) -> Result<(), ValidateExchangeRateError> {
    if exchange_rate.metadata.base_asset_num_received_rates < MINIMUM_ICP_SOURCES {
        return Err(ValidateExchangeRateError::NotEnoughIcpSources {
            received: exchange_rate.metadata.base_asset_num_received_rates,
            queried: exchange_rate.metadata.base_asset_num_queried_sources,
        });
    }

    if exchange_rate.metadata.quote_asset_num_received_rates < MINIMUM_CXDR_SOURCES {
        return Err(ValidateExchangeRateError::NotEnoughCxdrSources {
            received: exchange_rate.metadata.quote_asset_num_received_rates,
            queried: exchange_rate.metadata.quote_asset_num_queried_sources,
        });
    }

    Ok(())
}
```

**File:** rs/nns/cmc/src/lib.rs (L487-497)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Default, CandidType, Deserialize, Serialize)]
pub struct IcpXdrConversionRate {
    /// The time for which the market data was queried, expressed in UNIX epoch
    /// time in seconds.
    pub timestamp_seconds: u64,
    /// The number of 10,000ths of IMF SDR (currency code XDR) that corresponds
    /// to 1 ICP. This value reflects the current market price of one ICP
    /// token. In other words, this value specifies the ICP/XDR conversion
    /// rate to four decimal places.
    pub xdr_permyriad_per_icp: u64,
}
```
