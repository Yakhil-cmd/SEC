### Title
No-Staleness-Check on Stored ICP/XDR Rate Allows Over-Minting of Cycles During XRC Outage - (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) converts ICP to cycles using a stored `icp_xdr_conversion_rate` that is refreshed every five minutes via the Exchange Rate Canister (XRC). The `tokens_to_cycles` function reads this rate and uses it directly with no check on the rate's age. If the XRC canister becomes unavailable for an extended period, the CMC silently continues minting cycles at the last-known (potentially stale and inflated) ICP price, allowing any caller to obtain more cycles per ICP than the current market price warrants.

### Finding Description
`tokens_to_cycles` in `rs/nns/cmc/src/main.rs` reads `state.icp_xdr_conversion_rate` and extracts only `xdr_permyriad_per_icp`, with no check on `timestamp_seconds`:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);   // age never checked
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles { … }.to_cycles(amount)),
            None => Err(…),
        }
    })
}
``` [1](#0-0) 

The rate is refreshed by `update_exchange_rate` (called from `canister_heartbeat`) every `REFRESH_RATE_INTERVAL_SECONDS = 5 * ONE_MINUTE_SECONDS`. When the XRC call fails, the guard schedules a retry one minute later and returns an error — but the stored rate is **not invalidated or zeroed**: [2](#0-1) [3](#0-2) 

`validate_exchange_rate` only checks the number of data sources; it performs no age/staleness validation: [4](#0-3) 

`do_set_icp_xdr_conversion_rate` only rejects a rate whose timestamp is not strictly greater than the current one; it imposes no upper bound on how old the stored rate may be when it is later consumed: [5](#0-4) 

All three public minting endpoints — `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles` — funnel through `tokens_to_cycles` and therefore inherit the same absence of staleness enforcement: [6](#0-5) [7](#0-6) [8](#0-7) 

### Impact Explanation
If the XRC canister is unavailable for a sustained period (e.g., due to a bug, upgrade, or subnet issue) and the ICP market price falls materially during that window, every caller of `notify_top_up` / `notify_mint_cycles` / `notify_create_canister` receives cycles computed from the inflated stale rate. The CMC burns the ICP and mints cycles at the old price, permanently over-issuing cycles relative to the ICP value actually surrendered. Because cycles are the universal compute currency on the IC, systematic over-issuance dilutes the economic backing of all cycles in circulation and undermines the ICP-to-cycles peg that the CMC is designed to maintain.

The hourly `base_cycles_limit` rate-limiter provides a partial bound on total damage within any single hour, but it does not prevent the over-issuance from occurring across multiple hours of XRC unavailability. [9](#0-8) 

### Likelihood Explanation
The XRC canister is a system canister on the NNS subnet. Transient failures (upgrade windows, `StablecoinRateTooFewRates`, `CryptoBaseAssetNotFound`, etc.) are already observed in integration tests and are handled by scheduling a retry one minute later. A sequence of consecutive failures lasting more than a few minutes is plausible during XRC upgrades or during periods of low exchange-data availability. Any unprivileged principal holding ICP can call the minting endpoints at any time; no special role or key is required. The attacker does not need to cause the XRC failure — they only need to observe that the stored rate is stale and the ICP spot price has dropped, then submit a minting notification.

### Recommendation
1. **Add a staleness guard in `tokens_to_cycles`**: compare `rate.timestamp_seconds` against `ic_cdk::api::time() / 1_000_000_000` and return an error (or a conservative fallback rate) if the stored rate is older than a configurable threshold (e.g., 30 minutes).
2. **Expose the rate age in metrics**: emit a gauge for `now - icp_xdr_conversion_rate.timestamp_seconds` so operators can alert on stale rates before they affect minting.
3. **Consider a minimum-rate floor**: if the stored rate is stale, refuse minting rather than silently using an outdated price, consistent with the "last good price" pattern used in the Quill composite price feed.

### Proof of Concept
1. Deploy a local IC state machine with the NNS canisters.
2. Set the XRC mock to return `ExchangeRateError::StablecoinRateTooFewRates` for all requests (simulating a sustained XRC outage).
3. Record the last stored `icp_xdr_conversion_rate` (e.g., 50 XDR/ICP).
4. Advance time by 30+ minutes; confirm via `get_icp_xdr_conversion_rate` that the timestamp has not advanced.
5. Simulate an ICP market price drop to 10 XDR/ICP (the XRC would return this if it were healthy).
6. Call `notify_top_up` with 1 ICP. Observe that the CMC mints cycles at the stale 50 XDR/ICP rate — 5× more cycles than the current market price of ICP warrants.
7. Confirm that `tokens_to_cycles` never inspects `rate.timestamp_seconds` and that no error is returned. [10](#0-9) [11](#0-10)

### Citations

**File:** rs/nns/cmc/src/main.rs (L232-233)
```rust
    /// How many cycles are allowed to be minted in an hour.
    pub base_cycles_limit: Cycles,
```

**File:** rs/nns/cmc/src/main.rs (L1018-1033)
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

**File:** rs/nns/cmc/src/main.rs (L1925-1932)
```rust
async fn process_create_canister(
    controller: PrincipalId,
    from: AccountIdentifier,
    amount: Tokens,
    subnet_selection: Option<SubnetSelection>,
    settings: Option<CanisterSettings>,
) -> Result<CanisterId, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;
```

**File:** rs/nns/cmc/src/main.rs (L1958-1966)
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
```

**File:** rs/nns/cmc/src/main.rs (L1985-1992)
```rust
async fn process_top_up(
    canister_id: CanisterId,
    from: AccountIdentifier,
    amount: Tokens,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;

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
