### Title
Stale ICP/XDR Rate Used for Cycle Minting Without Freshness Check at Point of Use - (`rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) converts ICP to cycles using a stored `icp_xdr_conversion_rate` in `tokens_to_cycles()` with no check on how old that rate is. If the Exchange Rate Canister (XRC) becomes temporarily unavailable, the CMC continues to mint cycles at the last cached rate indefinitely. There is no maximum-age guard at the point of use, creating an exploitable window whenever the XRC is unreachable and the ICP market price has moved.

---

### Finding Description

The `tokens_to_cycles()` function in `rs/nns/cmc/src/main.rs` reads `state.icp_xdr_conversion_rate` and uses it directly for all cycle-minting operations:

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
            None => { /* error */ }
        }
    })
}
``` [1](#0-0) 

The only check performed is whether the rate is `Some`. The stored `timestamp_seconds` field of `IcpXdrConversionRate` is never compared against the current time at the point of use. [2](#0-1) 

The `validate_exchange_rate()` function, called when a new rate is received from the XRC, only validates that enough exchange sources responded — it does not check the age of the rate:

```rust
pub fn validate_exchange_rate(exchange_rate: &ExchangeRate) -> Result<(), ValidateExchangeRateError> {
    if exchange_rate.metadata.base_asset_num_received_rates < MINIMUM_ICP_SOURCES { ... }
    if exchange_rate.metadata.quote_asset_num_received_rates < MINIMUM_CXDR_SOURCES { ... }
    Ok(())
}
``` [3](#0-2) 

The `do_set_icp_xdr_conversion_rate()` function only enforces that a new rate has a strictly greater timestamp than the current one — it does not enforce a maximum age on the stored rate: [4](#0-3) 

The CMC's heartbeat calls `update_exchange_rate()` every 5 minutes (on success) or every 1 minute (on error) to refresh the rate from the XRC: [5](#0-4) 

If the XRC canister is unavailable, the CMC retries but continues to serve `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles` using the last cached rate with no bound on its age. [6](#0-5) 

---

### Impact Explanation

Cycles are priced in XDR terms: `cycles = ICP_amount × xdr_per_icp × cycles_per_xdr`. [7](#0-6) 

If the ICP market price drops while the XRC is unavailable, the stale `xdr_per_icp` value remains elevated. Any user calling `notify_top_up` or `notify_create_canister` during this window receives more cycles per ICP than the current market rate justifies, at the expense of the network's economic model. The longer the XRC is unavailable and the larger the price move, the greater the discrepancy. Since cycles are the computational currency of the entire IC, this directly undermines the invariant that 1 XDR ≈ 1 trillion cycles.

---

### Likelihood Explanation

The XRC canister is a real system canister that can be temporarily unavailable during routine upgrades, subnet maintenance windows, or when the external HTTP outcalls it depends on fail. The CMC has no mechanism to halt cycle minting when the stored rate is stale. An attacker who observes that the XRC is unreachable and that ICP price has dropped can immediately exploit the window by calling `notify_top_up` with ICP, receiving an above-market number of cycles. The entry path (`notify_top_up`, `notify_create_canister`) is fully open to any unprivileged principal. [8](#0-7) 

---

### Recommendation

Add a maximum-age guard inside `tokens_to_cycles()`. Before using the stored rate, compare `rate.timestamp_seconds` against the current canister time. If the rate is older than a defined threshold (e.g., 30 minutes), return an error rather than minting cycles at a potentially stale price. A constant analogous to `maxOracleFreshnessInSeconds` from the Chainlink pattern should be introduced.

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let rate = state.icp_xdr_conversion_rate.as_ref()
            .ok_or_else(|| /* no rate error */)?;

        let now = now_seconds();
        if now.saturating_sub(rate.timestamp_seconds) > MAX_RATE_AGE_SECONDS {
            return Err(NotifyError::Other {
                error_code: NotifyErrorCode::Internal as u64,
                error_message: "ICP/XDR conversion rate is stale".to_string(),
            });
        }
        // ... proceed with conversion
    })
}
```

---

### Proof of Concept

1. The XRC canister becomes temporarily unavailable (e.g., during a canister upgrade or subnet issue).
2. The CMC's heartbeat fails to update `icp_xdr_conversion_rate`; the last cached rate (e.g., `xdr_per_icp = 50_000`, meaning 5 XDR/ICP) remains in state.
3. The ICP market price drops 30% (actual rate should now be ~3.5 XDR/ICP, i.e., `xdr_per_icp ≈ 35_000`).
4. An attacker calls `notify_top_up` with 1 ICP.
5. `tokens_to_cycles()` uses the stale `xdr_per_icp = 50_000`, minting cycles equivalent to 5 XDR instead of 3.5 XDR — a ~43% overpayment in cycles.
6. The attacker repeats until the XRC recovers and the CMC updates its rate.

The attacker-controlled entry path is `notify_top_up` / `notify_create_canister` / `notify_mint_cycles`, all publicly callable by any principal. [1](#0-0) [9](#0-8)

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

**File:** rs/nns/cmc/src/main.rs (L1139-1146)
```rust
#[update]
async fn notify_top_up(
    NotifyTopUp {
        block_index,
        canister_id,
    }: NotifyTopUp,
) -> Result<Cycles, NotifyError> {
    let caller = caller();
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

**File:** rs/nns/cmc/src/lib.rs (L358-366)
```rust
impl TokensToCycles {
    pub fn to_cycles(&self, icpts: Tokens) -> Cycles {
        Cycles::new(
            icpts.get_e8s() as u128
                * self.xdr_permyriad_per_icp as u128
                * self.cycles_per_xdr.get()
                / (icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000),
        )
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
