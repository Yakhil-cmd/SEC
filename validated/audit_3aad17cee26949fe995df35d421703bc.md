### Title
Stale ICP/XDR Rate Used for Cycles Minting Without Freshness Check — (`File: rs/nns/cmc/src/main.rs`)

### Summary
The Cycles Minting Canister (CMC) converts ICP to cycles using a cached `icp_xdr_conversion_rate` that is updated every five minutes via heartbeat. The `tokens_to_cycles()` function, called by every public minting path (`notify_top_up`, `notify_create_canister`, `notify_mint_cycles`), reads the stored rate without checking its `timestamp_seconds` against the current time. If the Exchange Rate Canister (XRC) becomes unavailable for any period while the ICP market price drops, the CMC continues minting cycles at the stale (inflated) rate, allowing any unprivileged caller to obtain more cycles per ICP than the current market rate justifies.

### Finding Description

The CMC stores the ICP/XDR conversion rate in state and refreshes it via heartbeat at `REFRESH_RATE_INTERVAL_SECONDS = 5 * ONE_MINUTE_SECONDS`: [1](#0-0) 

When the XRC call fails (e.g., `StablecoinRateTooFewRates`, `NotEnoughCycles`, call rejection), the rate is **not updated** and the next attempt is deferred by one minute: [2](#0-1) 

The `tokens_to_cycles()` function, which is the sole conversion path for all three public minting endpoints, reads `state.icp_xdr_conversion_rate` and uses only `xdr_permyriad_per_icp` — the `timestamp_seconds` field is never compared against the current time: [3](#0-2) 

All three public minting paths call this function unconditionally: [4](#0-3) [5](#0-4) [6](#0-5) 

The heartbeat only fires when `exchange_rate_canister_id` is set: [7](#0-6) 

The `validate_exchange_rate()` check only validates source count, not timestamp age: [8](#0-7) 

### Impact Explanation

If the ICP market price drops significantly while the XRC is unavailable (or returning too-few-sources errors), the CMC continues minting cycles at the old, higher `xdr_permyriad_per_icp` rate. An unprivileged caller who sends ICP during this window receives more cycles than the current market rate justifies — effectively getting subsidized compute at the expense of the ICP burned. This is a **cycles conservation bug**: more cycles are minted per unit of ICP burned than the protocol intends, diluting the cycles economy. The hourly `base_cycles_limit` (150e15 cycles) bounds but does not eliminate the damage. [9](#0-8) 

### Likelihood Explanation

XRC failures are a documented and tested operational scenario (the test suite explicitly covers `StablecoinRateTooFewRates`, insufficient ICP/CXDR sources, and call errors). ICP price volatility is real. The combination — XRC unavailable for >5 minutes during a price drop — is a realistic event. Any user who monitors the CMC's `cmc_icp_xdr_conversion_rate_timestamp_seconds` metric can detect a stale rate and time their `notify_top_up` / `notify_mint_cycles` calls accordingly. No privileged access is required; the attack entry point is a standard public update call. [10](#0-9) 

### Recommendation

In `tokens_to_cycles()`, compare `rate.timestamp_seconds` against `now_seconds()` and reject (or warn) if the rate is older than a configurable maximum staleness threshold (e.g., 10–15 minutes). Alternatively, gate minting on a freshness invariant enforced at the point of use rather than only at the point of update.

### Proof of Concept

1. Observe `cmc_icp_xdr_conversion_rate_timestamp_seconds` metric showing a rate timestamp that is, say, 30 minutes old (XRC has been failing).
2. ICP market price has dropped 20% since that timestamp.
3. Call `notify_top_up` with a valid ICP ledger block. `tokens_to_cycles()` reads the stale `xdr_permyriad_per_icp` (e.g., 50,000 permyriad = 5 XDR/ICP) instead of the current ~4 XDR/ICP.
4. Caller receives `50_000 / 10_000 * cycles_per_xdr * amount` cycles — approximately 25% more cycles than the current market rate justifies.
5. Repeat up to the hourly `base_cycles_limit`. [11](#0-10) [12](#0-11)

### Citations

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

**File:** rs/nns/cmc/src/main.rs (L83-83)
```rust
const DEFAULT_CYCLES_LIMIT: u128 = 150e15 as u128;
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

**File:** rs/nns/cmc/src/main.rs (L1958-1965)
```rust
async fn process_mint_cycles(
    to_account: Account,
    amount: Tokens,
    deposit_memo: Option<Vec<u8>>,
    from: AccountIdentifier,
    sub: Subaccount,
) -> NotifyMintCyclesResult {
    let cycles = tokens_to_cycles(amount)?;
```

**File:** rs/nns/cmc/src/main.rs (L1985-1991)
```rust
async fn process_top_up(
    canister_id: CanisterId,
    from: AccountIdentifier,
    amount: Tokens,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;
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
