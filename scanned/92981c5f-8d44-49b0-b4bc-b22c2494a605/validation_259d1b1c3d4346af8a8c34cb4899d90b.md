### Title
Missing Staleness Check on XRC-Returned ICP/XDR Rate Allows Stale Price Acceptance in CMC - (`rs/nns/cmc/src/exchange_rate_canister.rs`)

---

### Summary

The Cycles Minting Canister (CMC) fetches the ICP/XDR exchange rate from the Exchange Rate Canister (XRC) via `update_exchange_rate`, but the validation step (`validate_exchange_rate`) never checks whether the returned rate's `timestamp` is recent relative to the current canister time. A stale rate — one whose `exchange_rate.timestamp` is arbitrarily far in the past — is accepted and committed to state as long as it has enough sources and a timestamp strictly greater than the previously stored rate.

---

### Finding Description

In `rs/nns/cmc/src/exchange_rate_canister.rs`, the periodic heartbeat-driven function `update_exchange_rate` calls the XRC with no timestamp argument (requesting the "latest" rate) and then validates the response:

```rust
let call_xrc_result = xrc_client.get_icp_to_xdr_exchange_rate(None).await;
// ...
Ok(exchange_rate) => {
    validate_exchange_rate(&exchange_rate)
        .map_err(|error| UpdateExchangeRateError::InvalidRate(error.to_string()))?;
    let icp_xdr_conversion_rate = IcpXdrConversionRate::from(exchange_rate);
    // ...
    do_set_icp_xdr_conversion_rate(safe_state, env, icp_xdr_conversion_rate)
``` [1](#0-0) 

The `validate_exchange_rate` function in `rs/nervous_system/clients/src/exchange_rate_canister_client.rs` checks only source counts:

```rust
pub fn validate_exchange_rate(exchange_rate: &ExchangeRate) -> Result<(), ValidateExchangeRateError> {
    if exchange_rate.metadata.base_asset_num_received_rates < MINIMUM_ICP_SOURCES { ... }
    if exchange_rate.metadata.quote_asset_num_received_rates < MINIMUM_CXDR_SOURCES { ... }
    Ok(())
}
``` [2](#0-1) 

There is **no check** that `exchange_rate.timestamp` is within an acceptable window of `now_timestamp_seconds`. The only timestamp guard in `do_set_icp_xdr_conversion_rate` is a monotonicity check — the new rate's timestamp must be strictly greater than the currently stored rate's timestamp:

```rust
if let Some(current_conversion_rate) = state.icp_xdr_conversion_rate.as_ref()
    && proposed_conversion_rate.timestamp_seconds <= current_conversion_rate.timestamp_seconds
{
    return Err("Proposed conversion rate must have greater timestamp than current one".to_string());
}
``` [3](#0-2) 

This means a rate with `timestamp = now - 3600` (one hour stale) is accepted without any warning or rejection, as long as it is newer than the previously stored rate. The `REFRESH_RATE_INTERVAL_SECONDS = 5 * ONE_MINUTE_SECONDS` constant governs how often the CMC calls the XRC, not how stale the returned rate is allowed to be. [4](#0-3) 

The same gap exists in the NNS Governance canister's `fetch_and_validate_rate` for the current-day rate path: `validate_exchange_rate` is called but no wall-clock recency check is performed on the returned `exchange_rate.timestamp`. [5](#0-4) 

---

### Impact Explanation

The ICP/XDR rate stored in the CMC is used directly to price cycles: every `notify_top_up` and `notify_create_canister` call converts ICP to cycles using this rate. If a stale rate is committed — e.g., one reflecting an ICP price from an hour ago during a period of high volatility — users receive cycles at an incorrect price. Depending on the direction of price movement, this constitutes either a loss to the CMC (users receive too many cycles) or a loss to users (users receive too few cycles). The rate is also certified and served to query callers via the `IcpXdrConversionRateResponse`, so stale data propagates to all consumers of the certified rate. [6](#0-5) 

---

### Likelihood Explanation

The XRC fetches rates from external exchanges via HTTPS outcalls. If those exchanges are temporarily unavailable or return degraded data, the XRC may return a rate whose `timestamp` is significantly behind the current time. The CMC has no defense against this: it will accept and commit the stale rate. The XRC is a trusted canister, so an unprivileged attacker cannot directly inject an arbitrary rate, but the missing check is a defense-in-depth gap that activates whenever the XRC's upstream data sources degrade — a realistic operational scenario. Likelihood is **low-medium**: the XRC is generally reliable, but the missing check means any XRC data-quality regression propagates unchecked into cycles pricing.

---

### Recommendation

After receiving the rate from the XRC, compare `exchange_rate.timestamp` to `now_timestamp_seconds` and reject the rate if the difference exceeds a defined staleness threshold (e.g., `REFRESH_RATE_INTERVAL_SECONDS` or a small multiple thereof):

```rust
let age_seconds = now_timestamp_seconds.saturating_sub(exchange_rate.timestamp);
if age_seconds > MAX_ACCEPTABLE_RATE_AGE_SECONDS {
    return Err(UpdateExchangeRateError::InvalidRate(
        format!("Rate timestamp is too old: age={}s", age_seconds)
    ));
}
```

Add a corresponding variant to `ValidateExchangeRateError` and extend `validate_exchange_rate` to accept the current time, so the check is co-located with the existing source-count checks in `rs/nervous_system/clients/src/exchange_rate_canister_client.rs`. [7](#0-6) 

---

### Proof of Concept

1. The CMC heartbeat fires and calls `update_exchange_rate` at `now = T`.
2. The XRC, due to exchange API degradation, returns `ExchangeRate { timestamp: T - 3600, rate: R, metadata: { base_asset_num_received_rates: 4, quote_asset_num_received_rates: 4, ... } }`.
3. `validate_exchange_rate` passes (source counts ≥ 4).
4. `do_set_icp_xdr_conversion_rate` passes (timestamp `T - 3600` > previously stored timestamp).
5. The CMC stores and certifies the one-hour-stale rate `R`.
6. All subsequent `notify_top_up` calls price cycles using `R` rather than the current market rate, resulting in incorrect cycles issuance for the duration until the next successful fresh-rate update. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L245-275)
```rust
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
```

**File:** rs/nervous_system/clients/src/exchange_rate_canister_client.rs (L86-129)
```rust
/// Validation errors for an exchange rate returned by the XRC.
#[derive(Debug)]
pub enum ValidateExchangeRateError {
    NotEnoughIcpSources { received: usize, queried: usize },
    NotEnoughCxdrSources { received: usize, queried: usize },
}

impl std::fmt::Display for ValidateExchangeRateError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ValidateExchangeRateError::NotEnoughIcpSources { received, queried } => write!(
                f,
                "Not enough exchange sources for rate's ICP base asset. \
                 Expected: {MINIMUM_ICP_SOURCES} Received: {received} Queried: {queried}"
            ),
            ValidateExchangeRateError::NotEnoughCxdrSources { received, queried } => write!(
                f,
                "Not enough forex sources for rate's CXDR quote asset. \
                 Expected: {MINIMUM_CXDR_SOURCES} Received: {received} Queried: {queried}"
            ),
        }
    }
}

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

**File:** rs/nns/cmc/src/main.rs (L1018-1036)
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

        state.icp_xdr_conversion_rate = Some(proposed_conversion_rate.clone());
        update_recent_icp_xdr_rates(state, &proposed_conversion_rate);

        let witness_generator = convert_data_to_mixed_hash_tree(state);
        env.set_certified_data(&witness_generator.hash_tree().digest().0[..]);
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L265-309)
```rust
    async fn fetch_and_validate_rate(&self, timestamp: u64) -> Option<SampledPrice> {
        let exchange_rate = match self
            .xrc_client
            .get_icp_to_xdr_exchange_rate(Some(timestamp))
            .await
        {
            Ok(rate) => rate,
            Err(err) => {
                println!(
                    "{}UpdateIcpXdrRateRelatedData: XRC call failed: {}",
                    LOG_PREFIX, err
                );
                return None;
            }
        };

        if let Err(err) = validate_exchange_rate(&exchange_rate) {
            println!(
                "{}UpdateIcpXdrRateRelatedData: XRC rate failed validation: {}",
                LOG_PREFIX, err
            );
            return None;
        }

        // Verify that XRC returned a rate for the day we requested. If not, the rate
        // won't fill the expected slot and backfill would loop on the same day.
        if exchange_rate.timestamp != timestamp {
            println!(
                "{}UpdateIcpXdrRateRelatedData: requested timestamp {} but XRC returned {}; ignoring.",
                LOG_PREFIX, timestamp, exchange_rate.timestamp
            );
            return None;
        }

        let rate = SampledPrice::from(&exchange_rate);
        if rate.xdr_permyriad_per_icp == 0 {
            println!(
                "{}UpdateIcpXdrRateRelatedData: received zero XDR/ICP rate; ignoring.",
                LOG_PREFIX
            );
            return None;
        }

        Some(rate)
    }
```
