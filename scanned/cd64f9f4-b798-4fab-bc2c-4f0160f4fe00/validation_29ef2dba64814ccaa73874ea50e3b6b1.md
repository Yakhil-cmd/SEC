### Title
Cycles Minting Canister Uses Manipulable Spot ICP/XDR Rate Instead of Available 30-Day Average for All Cycles Minting - (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) uses the instantaneous spot `icp_xdr_conversion_rate` (updated every 5 minutes from the XRC) to determine how many cycles to mint per ICP in `tokens_to_cycles`. A 30-day moving average (`average_icp_xdr_conversion_rate`) is computed, stored, and certified in the same canister state but is never used for cycles minting. An attacker who can temporarily inflate the ICP/XDR spot rate across the exchanges queried by the XRC can call `notify_top_up` / `notify_mint_cycles` at the inflated rate and extract excess cycles from the IC's cycles economy.

### Finding Description

`tokens_to_cycles` in `rs/nns/cmc/src/main.rs` reads exclusively from `state.icp_xdr_conversion_rate` — the most recent single-reading spot rate:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate   // ← spot rate, updated every 5 min
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        ...
        Ok(TokensToCycles { xdr_permyriad_per_icp, cycles_per_xdr: state.cycles_per_xdr }
            .to_cycles(amount))
    })
}
``` [1](#0-0) 

This function is called by every cycles-minting path reachable by an unprivileged ingress sender: `process_top_up` (via `notify_top_up`), `process_create_canister` (via `notify_create_canister`), and `process_mint_cycles` (via `notify_mint_cycles`). [2](#0-1) 

The CMC simultaneously maintains `average_icp_xdr_conversion_rate`, a 30-day moving average computed from `recent_icp_xdr_rates` via `compute_average_icp_xdr_rate_at_time`: [3](#0-2) [4](#0-3) 

This average is used by SNS governance for treasury valuation (via `get_average_icp_xdr_conversion_rate`) but is **never consulted** during cycles minting. [5](#0-4) 

The spot rate is sourced from the XRC canister, which aggregates prices from multiple exchanges and requires a minimum of 4 sources for both the ICP base asset and the CXDR quote asset: [6](#0-5) 

The CMC heartbeat polls the XRC every 5 minutes and stores the result directly as the operative minting rate: [7](#0-6) 

There is no deviation check between the incoming spot rate and the stored 30-day average before the spot rate is accepted and used for minting.

### Impact Explanation

An attacker who can temporarily inflate the ICP/XDR spot rate across the ≥4 exchanges queried by the XRC can call `notify_top_up` or `notify_mint_cycles` immediately after the CMC heartbeat stores the inflated rate. The CMC will mint cycles at the inflated rate, giving the attacker more cycles per ICP than the fair market rate. The excess cycles represent a direct extraction of value from the IC's cycles economy (cycles are backed by XDR-denominated compute resources). The hourly `base_cycles_limit` rate limiter bounds the per-hour damage but does not eliminate the attack surface, and the attacker can repeat the attack across multiple heartbeat windows.

### Likelihood Explanation

The attack requires temporarily moving the aggregated ICP/XDR rate across at least 4 exchanges simultaneously, which demands significant capital. However, the 5-minute heartbeat interval is publicly observable and predictable, giving the attacker a precise timing window. The attack is economically rational if the cost of market manipulation is less than the value of excess cycles obtained. This is the same class of attack described in the external report (spot price manipulation for guaranteed profit), and the IC's cycles economy provides a concrete financial incentive.

### Recommendation

Replace `state.icp_xdr_conversion_rate` with `state.average_icp_xdr_conversion_rate` in `tokens_to_cycles`. The 30-day moving average is already computed, certified, and stored in the same canister state; using it for minting would make the cycles price resistant to short-term spot manipulation, directly analogous to the TWAP recommendation in the external report. As a secondary defense, add a sanity check that rejects any incoming spot rate that deviates from the stored average by more than a configurable threshold (e.g., ±20%) before storing it as the operative rate.

### Proof of Concept

1. Monitor the CMC heartbeat schedule (fires every 5 minutes at :00, :05, :10, …).
2. In the ~30 seconds before the next heartbeat, execute coordinated large buy orders on the ≥4 exchanges queried by the XRC to temporarily inflate the ICP/XDR spot rate.
3. The XRC aggregates the inflated prices and returns the elevated rate to the CMC heartbeat call in `update_exchange_rate`.
4. The CMC stores the inflated rate in `state.icp_xdr_conversion_rate` via `do_set_icp_xdr_conversion_rate`.
5. Immediately call `notify_top_up` (or `notify_mint_cycles`) with ICP. The CMC calls `tokens_to_cycles`, which reads the inflated `icp_xdr_conversion_rate` and mints cycles at the elevated rate.
6. Unwind the market position. The attacker has received more cycles per ICP than the fair 30-day average rate, with the difference representing extracted value.

### Citations

**File:** rs/nns/cmc/src/main.rs (L220-227)
```rust
    /// The average ICP/XDR rate over `NUM_DAYS_FOR_ICP_XDR_AVERAGE` days. The
    /// timestamp is the UNIX epoch time in seconds at the start of the last
    /// considered day, which should correspond to midnight of the current
    /// day.
    pub average_icp_xdr_conversion_rate: Option<IcpXdrConversionRate>,

    /// The recent ICP/XDR rates used to compute the average rate.
    pub recent_icp_xdr_rates: Option<Vec<IcpXdrConversionRate>>,
```

**File:** rs/nns/cmc/src/main.rs (L949-975)
```rust
fn compute_average_icp_xdr_rate_at_time(
    recent_rates: &[IcpXdrConversionRate],
    time_s: u64,
) -> Option<IcpXdrConversionRate> {
    let day = time_s / 86_400;
    // Filter the rates based on valid days, i.e., days not before day
    // `day - NUM_ICP_XDR_RATES_FOR_AVERAGE` and not later than the given day.
    let filtered_rates: Vec<u64> = recent_rates
        .iter()
        .filter(|rate| {
            (rate.timestamp_seconds / 86_400) > day - (NUM_DAYS_FOR_ICP_XDR_AVERAGE as u64)
                && (rate.timestamp_seconds / 86_400) <= day
        })
        .map(|rate| rate.xdr_permyriad_per_icp)
        .collect();
    let size = filtered_rates.len() as u64;
    // If there are rates that meet the age requirement, compute the sum and compute
    // the average.
    if size > 0 {
        let sum: u64 = filtered_rates.into_iter().sum();
        Some(IcpXdrConversionRate {
            timestamp_seconds: day * 86_400,   // Start of the current day.
            xdr_permyriad_per_icp: sum / size, // The average of the valid data points.
        })
    } else {
        None
    }
```

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

**File:** rs/nervous_system/clients/src/exchange_rate_canister_client.rs (L13-17)
```rust
/// The minimum number of received sources to consider an ICP/CXDR rate's base asset valid.
pub const MINIMUM_ICP_SOURCES: usize = 4;

/// The minimum number of received sources to consider an ICP/CXDR rate's quote asset valid.
pub const MINIMUM_CXDR_SOURCES: usize = 4;
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
