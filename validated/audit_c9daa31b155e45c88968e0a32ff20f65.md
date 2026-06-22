### Title
Stale ICP/XDR Rate Used Without Age Validation in Cycles Minting Canister - (File: `rs/nns/cmc/src/main.rs`)

### Summary
The `tokens_to_cycles` function in the Cycles Minting Canister (CMC) converts ICP to cycles using the cached `icp_xdr_conversion_rate` without checking whether the rate's `timestamp_seconds` is within an acceptable staleness threshold. If the Exchange Rate Canister (XRC) becomes unavailable or the automatic update mechanism enters the `Disabled` state, the CMC will continue minting cycles at an arbitrarily old price indefinitely, causing users to receive incorrect cycle amounts.

### Finding Description
The CMC stores the ICP/XDR conversion rate in `state.icp_xdr_conversion_rate` and refreshes it via a heartbeat-driven call to the XRC every 5 minutes (`REFRESH_RATE_INTERVAL_SECONDS`). When a user triggers a cycle-minting operation (`notify_top_up`, `notify_create_canister`, `notify_mint_cycles`), the `tokens_to_cycles` function is called:

```rust
// rs/nns/cmc/src/main.rs:1900-1923
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);  // timestamp_seconds is ignored
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles {
                xdr_permyriad_per_icp,
                cycles_per_xdr: state.cycles_per_xdr,
            }
            .to_cycles(amount)),
            None => { /* error */ }
        }
    })
}
```

The function only checks whether the rate is `Some` or `None`. It never inspects `rate.timestamp_seconds` against the current time. The `IcpXdrConversionRate` struct carries a `timestamp_seconds` field specifically for this purpose, but it is silently discarded at the point of use. [1](#0-0) 

The `validate_exchange_rate` function called during rate ingestion only validates the number of data sources, not the age of the rate: [2](#0-1) 

The `do_set_icp_xdr_conversion_rate` function only rejects a new rate if its timestamp is not strictly greater than the current one — it does not enforce any maximum age: [3](#0-2) 

The update mechanism can also enter `UpdateExchangeRateState::Disabled` when a rate diverges, at which point no automatic refresh occurs at all: [4](#0-3) [5](#0-4) 

### Impact Explanation
Any user calling `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` on the CMC will receive cycles computed from a potentially arbitrarily old ICP/XDR rate. If the ICP price has moved significantly since the last successful rate update:

- **Price rose**: Users receive fewer cycles than the current market rate warrants (economic loss to users).
- **Price fell**: Users receive more cycles than the current market rate warrants (economic loss to the network/cycle economy).

The same stale rate is also consumed by the SNS token valuation path (`CmcBased30DayMovingAverageXdrsPerIcpClient` in `rs/sns/governance/token_valuation/src/lib.rs`), which uses `get_average_icp_xdr_conversion_rate` to enforce 7-day treasury transfer limits. A stale rate could cause those limits to be computed incorrectly, potentially allowing larger-than-intended SNS treasury disbursements. [6](#0-5) 

### Likelihood Explanation
The XRC is an external canister dependency. Extended XRC unavailability (network partition, canister upgrade, or persistent error responses) causes the CMC's heartbeat update to fail and retry every minute without ever refreshing the stored rate. The `UpdateExchangeRateState::Disabled` path is also reachable via a diverged rate, after which no automatic refresh occurs. In both cases, `tokens_to_cycles` continues using the last-known rate with no bound on its age. This is a realistic operational scenario, not a theoretical one.

### Recommendation
- **Short term**: Add a staleness guard in `tokens_to_cycles` that compares `rate.timestamp_seconds` against `now_seconds()` and returns an error (or falls back to a conservative rate) if the rate is older than an acceptable threshold (e.g., 2× `REFRESH_RATE_INTERVAL_SECONDS` or a configurable maximum age).
- **Long term**: Expose a metric or certified endpoint that surfaces the age of the stored rate, and implement a circuit-breaker that halts cycle minting when the rate exceeds a configurable maximum staleness, with a governance-controlled override to resume operations.

### Proof of Concept
1. The XRC canister becomes unavailable (e.g., subnet upgrade, persistent error).
2. The CMC heartbeat calls `update_exchange_rate`, which calls `xrc_client.get_icp_to_xdr_exchange_rate(None)`, receives an error, and schedules a retry in 1 minute.
3. This continues indefinitely; `state.icp_xdr_conversion_rate` retains its last-set value with its original `timestamp_seconds`.
4. A user calls `notify_top_up` with ICP. `tokens_to_cycles` reads `state.icp_xdr_conversion_rate`, ignores `timestamp_seconds`, and computes cycles from the stale rate.
5. If ICP/XDR has moved 20% since the last update, the user receives ~20% more or fewer cycles than the current market rate warrants. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1018-1030)
```rust
    if proposed_conversion_rate.xdr_permyriad_per_icp == 0 {
        return Err("Proposed conversion rate must be greater than 0".to_string());
    }

    mutate_state(safe_state, |state| {
        if let Some(current_conversion_rate) = state.icp_xdr_conversion_rate.as_ref()
            && proposed_conversion_rate.timestamp_seconds
                <= current_conversion_rate.timestamp_seconds
        {
            return Err(
                "Proposed conversion rate must have greater timestamp than current one".to_string(),
            );
        }
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

**File:** rs/nervous_system/clients/src/exchange_rate_canister_client.rs (L111-128)
```rust
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
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L98-100)
```rust
        if current_call_state == UpdateExchangeRateState::Disabled {
            return Err(UpdateExchangeRateError::Disabled);
        }
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L236-280)
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
}
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L435-458)
```rust
        async fn get(&mut self) -> Result<Decimal, ValuationError> {
            let (response,): (IcpXdrConversionRateCertifiedResponse,) =
                MyRuntime::call_with_cleanup(
                    CYCLES_MINTING_CANISTER_ID,
                    // This is not in the cmc.did file (yet).
                    "get_average_icp_xdr_conversion_rate",
                    ((),),
                )
                .await
                .map_err(|err| {
                    ValuationError::new_external(format!(
                        "Unable to determine XDRs per ICP, because the cycles minting canister \
                         did not reply to a get_average_icp_xdr_conversion_rate call: {err:?}",
                    ))
                })?;

            // No need to validate the cerificate in response, because query is not used in this
            // case (specifically, canister A in subnet X is calling (another) canister B in
            // (another) subnet Y).

            let xdr_per_icp =
                Decimal::from(response.data.xdr_permyriad_per_icp) * *UNITS_PER_PERMYRIAD;

            Ok(xdr_per_icp)
```
