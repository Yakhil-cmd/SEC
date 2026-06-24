### Title
Stale ICP/XDR Exchange Rate Used Without Age Check in Cycles Minting Canister - (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) converts ICP tokens to cycles using a cached `icp_xdr_conversion_rate`. The `tokens_to_cycles` function and the `validate_exchange_rate` helper only check that a rate exists and has sufficient data sources — neither checks whether the rate's `timestamp_seconds` is within an acceptable age window. If the Exchange Rate Canister (XRC) is unavailable for an extended period, the CMC silently continues using an arbitrarily stale rate for all ICP-to-cycles conversions triggered by any unprivileged caller.

### Finding Description

`tokens_to_cycles` in `rs/nns/cmc/src/main.rs` reads `state.icp_xdr_conversion_rate` and uses it directly:

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

The only guard is a `None` check — `rate.timestamp_seconds` is never compared against the current time. The `validate_exchange_rate` function, which is the sole validation step applied to a freshly fetched rate before it is stored, only checks source counts: [2](#0-1) 

`do_set_icp_xdr_conversion_rate` only enforces that the incoming rate's timestamp is strictly greater than the stored one — it imposes no upper bound on how old the stored rate may become: [3](#0-2) 

The CMC heartbeat refreshes the rate every five minutes via `update_exchange_rate`, but if the XRC subnet is degraded or the XRC canister is temporarily unavailable, the heartbeat silently fails and the cached rate ages indefinitely: [4](#0-3) 

The comment in `exchange_rate_canister.rs` itself acknowledges the concept of staleness (`REFRESH_RATE_INTERVAL_SECONDS` — "If the rate is older than this value, the CMC should ask for a new rate"), but this constant only governs *when to fetch*, not *whether to reject a stale cached value at use time*: [5](#0-4) 

### Impact Explanation

`tokens_to_cycles` is called by `process_top_up` and `process_create_canister`, which are invoked by the publicly callable `notify_top_up` and `notify_create_canister` endpoints: [6](#0-5) [7](#0-6) 

Any unprivileged user can call these endpoints. If the ICP/XDR rate is stale:

- **Rate too high (ICP price fell):** callers receive more cycles than the current market rate warrants — a direct economic loss for the protocol (cycles are minted below cost).
- **Rate too low (ICP price rose):** callers receive fewer cycles than they paid for — a loss for users.

The CMC is the sole on-chain mechanism for minting cycles; a stale rate affects every ICP-to-cycles conversion until the rate is refreshed.

### Likelihood Explanation

The XRC is a system canister on a dedicated subnet. Temporary unavailability (subnet upgrade, transient network partition, XRC canister upgrade) is a realistic operational event. During such a window — which could last minutes to hours — the CMC continues accepting `notify_top_up` calls and minting cycles at the stale rate. No attacker capability beyond submitting a normal ingress message is required.

### Recommendation

In `tokens_to_cycles`, compare `rate.timestamp_seconds` against the current canister time and reject conversions if the rate is older than a defined maximum age (e.g., `MAX_RATE_AGE_SECONDS = 2 * REFRESH_RATE_INTERVAL_SECONDS`):

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let now = now_seconds();
        match state.icp_xdr_conversion_rate.as_ref() {
            Some(rate) if now.saturating_sub(rate.timestamp_seconds) <= MAX_RATE_AGE_SECONDS => {
                Ok(TokensToCycles {
                    xdr_permyriad_per_icp: rate.xdr_permyriad_per_icp,
                    cycles_per_xdr: state.cycles_per_xdr,
                }.to_cycles(amount))
            }
            Some(_) => Err(NotifyError::Other {
                error_code: NotifyErrorCode::Internal as u64,
                error_message: "ICP/XDR conversion rate is stale; retry later".to_string(),
            }),
            None => Err(...),
        }
    })
}
```

Analogously, add a `ValidateExchangeRateError::RateTooOld` variant to `validate_exchange_rate` so that freshly fetched rates whose `timestamp` is far in the past are also rejected before being stored. [8](#0-7) 

### Proof of Concept

1. The XRC subnet undergoes a rolling upgrade or transient outage lasting 30+ minutes.
2. During this window, the CMC heartbeat calls `update_exchange_rate`, which calls `xrc_client.get_icp_to_xdr_exchange_rate(None)` and receives an error; the cached rate is not updated.
3. The ICP market price moves 20% during the outage.
4. An attacker (or any user) calls `notify_top_up` with a `block_index` referencing a valid ICP transfer.
5. `tokens_to_cycles` reads the 30-minute-old `icp_xdr_conversion_rate` without any age check and mints cycles at the stale rate.
6. If ICP price fell 20%, the attacker receives ~25% more cycles than the current market rate warrants, at the protocol's expense. [1](#0-0) [9](#0-8)

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

**File:** rs/nns/cmc/src/main.rs (L1345-1356)
```rust
#[update]
#[allow(deprecated)]
async fn notify_create_canister(
    NotifyCreateCanister {
        block_index,
        controller,
        subnet_type,
        subnet_selection,
        settings,
    }: NotifyCreateCanister,
) -> Result<CanisterId, NotifyError> {
    authorize_caller_to_call_notify_create_canister_on_behalf_of_creator(caller(), controller)?;
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

**File:** rs/nervous_system/clients/src/exchange_rate_canister_client.rs (L86-108)
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
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
