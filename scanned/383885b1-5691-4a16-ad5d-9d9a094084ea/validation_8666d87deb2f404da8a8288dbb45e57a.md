### Title
Stale ICP/XDR Rate Used for Cycle Minting Without Staleness Check — (File: rs/nns/cmc/src/main.rs)

---

### Summary
The Cycles Minting Canister (CMC) converts ICP to cycles using a stored `icp_xdr_conversion_rate` that carries no maximum-age enforcement at the point of use. If the Exchange Rate Canister (XRC) becomes unavailable for any extended period, the CMC silently continues minting cycles at an arbitrarily old rate, allowing any unprivileged caller to exploit the price discrepancy.

---

### Finding Description

In `rs/nns/cmc/src/main.rs`, the function `tokens_to_cycles` (line 1900) reads `state.icp_xdr_conversion_rate` and uses it directly for all cycle-minting operations. The only guard is a `None` check — there is no check on the rate's `timestamp_seconds` relative to the current time:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);   // ← age never checked
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles { ... }.to_cycles(amount)),
            None => Err(...)
        }
    })
}
``` [1](#0-0) 

The rate is normally refreshed every five minutes via the heartbeat: [2](#0-1) 

However, the heartbeat update can stop for two reasons without any fallback rejection in `tokens_to_cycles`:

1. **XRC unavailability**: If the XRC canister is temporarily unreachable (e.g., during a subnet upgrade), `update_exchange_rate` returns `FailedToRetrieveRate` and the CMC simply logs the error and retries next minute — the stored rate is never invalidated. [3](#0-2) 

2. **`Disabled` state**: When the rate diverges significantly, `UpdateExchangeRateState::Disabled` is set, permanently halting XRC calls. The CMC then uses the last stored rate indefinitely. [4](#0-3) 

Additionally, the upstream `validate_exchange_rate` function only checks the number of data sources — it never validates the age of the returned rate: [5](#0-4) 

`do_set_icp_xdr_conversion_rate` only enforces monotonically increasing timestamps (new > current), not that the rate is recent relative to wall-clock time: [6](#0-5) 

---

### Impact Explanation

`tokens_to_cycles` is called by every cycle-minting path: `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles`. If the stored rate is hours or days old and the ICP/XDR market price has moved significantly:

- **ICP price drops** while rate is stale → callers receive more cycles per ICP than the current market rate warrants, effectively extracting value from the CMC's economic model.
- **ICP price rises** while rate is stale → callers receive fewer cycles, but the primary security concern is the over-minting direction.

Over-minting cycles is a **ledger conservation bug**: cycles are minted against ICP that was burned at an incorrect conversion rate, permanently inflating the cycle supply relative to the ICP burned. [7](#0-6) 

---

### Likelihood Explanation

The XRC is a system canister on the NNS subnet and is highly available under normal conditions. However:

- Subnet upgrades, replica bugs, or XRC canister upgrades can cause multi-minute to multi-hour gaps in rate updates.
- The `Disabled` state can be triggered automatically by divergence detection, causing indefinite staleness without any operator action.
- Any unprivileged user who monitors the CMC's certified rate timestamp (publicly readable via `get_icp_xdr_conversion_rate`) can detect when the rate has gone stale and time their `notify_top_up` calls accordingly.

Likelihood is **low-medium**: requires an observable window of XRC unavailability, but the entry path is fully unprivileged and the staleness is publicly detectable.

---

### Recommendation

Add a maximum-age guard inside `tokens_to_cycles` (or at the call sites) that rejects conversions when the stored rate's `timestamp_seconds` is older than a configurable threshold (e.g., `MAX_RATE_AGE_SECONDS = 3600`):

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let rate = state.icp_xdr_conversion_rate.as_ref().ok_or_else(|| ...)?;
        let age = now_seconds().saturating_sub(rate.timestamp_seconds);
        if age > MAX_RATE_AGE_SECONDS {
            return Err(NotifyError::Other {
                error_code: NotifyErrorCode::Internal as u64,
                error_message: format!("ICP/XDR rate is stale ({age}s old)"),
            });
        }
        Ok(TokensToCycles { xdr_permyriad_per_icp: rate.xdr_permyriad_per_icp, ... }.to_cycles(amount))
    })
}
```

Additionally, `validate_exchange_rate` should verify that the returned rate's timestamp is within an acceptable window of the current time before it is accepted into state. [5](#0-4) 

---

### Proof of Concept

1. Observer monitors the CMC's public `get_icp_xdr_conversion_rate` endpoint and notes the `timestamp_seconds` of the stored rate.
2. XRC canister becomes temporarily unavailable (e.g., during a planned upgrade of the XRC or NNS subnet).
3. CMC heartbeat fails repeatedly; `icp_xdr_conversion_rate.timestamp_seconds` stops advancing.
4. ICP market price drops 20% during the outage window.
5. Attacker calls `notify_top_up` (or `notify_mint_cycles`) with ICP.
6. `tokens_to_cycles` uses the stale (pre-drop) rate — no age check is performed.
7. Attacker receives ~25% more cycles than the current market rate warrants, with the excess cycles permanently minted against ICP burned at the wrong rate. [1](#0-0)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1022-1030)
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

**File:** rs/nns/cmc/src/main.rs (L2397-2402)
```rust
#[heartbeat]
async fn canister_heartbeat() {
    if with_state(|state| state.exchange_rate_canister_id.is_some()) {
        update_exchange_rate().await
    }
}
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L98-100)
```rust
        if current_call_state == UpdateExchangeRateState::Disabled {
            return Err(UpdateExchangeRateError::Disabled);
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

**File:** rs/nervous_system/clients/src/exchange_rate_canister_client.rs (L111-129)
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
}
```
