### Title
Missing Staleness Check on ICP/XDR Exchange Rate Allows Stale Rate to Mint Incorrect Cycles — (File: rs/nns/cmc/src/main.rs)

---

### Summary

The `tokens_to_cycles` function in the Cycles Minting Canister (CMC) converts ICP to cycles using the stored `icp_xdr_conversion_rate` without ever checking the rate's age. The `validate_exchange_rate` helper only validates source counts, not the rate's timestamp or standard deviation. If the Exchange Rate Canister (XRC) is unavailable for an extended period, the CMC silently continues using a stale rate, causing users to receive incorrect cycle amounts for the ICP they burn — an exact analog of the stale-oracle fund-loss pattern in the external report.

---

### Finding Description

**`tokens_to_cycles` ignores `timestamp_seconds`**

In `rs/nns/cmc/src/main.rs`, the function that converts ICP to cycles reads only `xdr_permyriad_per_icp` from the stored rate and never inspects `timestamp_seconds`:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);   // timestamp_seconds silently dropped
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles {
                xdr_permyriad_per_icp,
                cycles_per_xdr: state.cycles_per_xdr,
            }
            .to_cycles(amount)),
            ...
        }
    })
}
``` [1](#0-0) 

Every public cycle-minting entry point — `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles` — funnels through this function. [2](#0-1) [3](#0-2) 

**`validate_exchange_rate` checks only source counts**

The shared validation helper accepts any rate that has ≥ 4 ICP sources and ≥ 4 CXDR sources. It does not check the rate's `timestamp`, nor the `standard_deviation` field that the XRC populates in `ExchangeRateMetadata`:

```rust
pub fn validate_exchange_rate(exchange_rate: &ExchangeRate) -> Result<(), ValidateExchangeRateError> {
    if exchange_rate.metadata.base_asset_num_received_rates < MINIMUM_ICP_SOURCES { ... }
    if exchange_rate.metadata.quote_asset_num_received_rates < MINIMUM_CXDR_SOURCES { ... }
    Ok(())   // timestamp and standard_deviation never inspected
}
``` [4](#0-3) 

**Heartbeat updates every 5 minutes; no maximum-age guard**

The CMC heartbeat calls `update_exchange_rate` every 5 minutes and retries after 1 minute on failure. There is no upper bound on how stale the stored rate may become before it is refused for use: [5](#0-4) [6](#0-5) 

`do_set_icp_xdr_conversion_rate` only rejects a new rate whose timestamp is not strictly greater than the current one — it does not reject a rate that is hours old: [7](#0-6) 

---

### Impact Explanation

When the XRC is unavailable (canister upgrade, network partition, or bug), the CMC's stored `icp_xdr_conversion_rate` becomes stale. All three public minting endpoints continue to accept ICP and burn it at the stale rate:

- **ICP price rises while rate is stale**: users receive fewer cycles than the current market rate warrants. The ICP is burned and the user has no recourse.
- **ICP price falls while rate is stale**: the protocol mints more cycles than warranted, causing uncontrolled cycle inflation.

The `IcpXdrConversionRate` struct carries `timestamp_seconds` precisely so consumers can detect staleness, but `tokens_to_cycles` never reads it. [8](#0-7) 

---

### Likelihood Explanation

The XRC is a live canister subject to upgrades, subnet migrations, and transient HTTP-outcall failures. The CMC retries every minute on failure but has no circuit-breaker that halts minting after a configurable staleness threshold. In a volatile market, a 1–2 hour XRC outage can produce a 5–10 % price discrepancy. Any unprivileged user who observes the divergence between the on-chain stale rate and the real market price can exploit it by timing their `notify_top_up` / `notify_mint_cycles` call accordingly.

---

### Recommendation

1. In `tokens_to_cycles`, compare `icp_xdr_conversion_rate.timestamp_seconds` against `now_seconds()`. If the rate is older than a configurable threshold (e.g., 1 hour), return a retriable `NotifyError` so the user can retry once the rate is refreshed.
2. Extend `validate_exchange_rate` to reject rates whose `standard_deviation` exceeds a protocol-defined maximum, analogous to a deviation-threshold guard. [9](#0-8) 

---

### Proof of Concept

1. The XRC canister is temporarily unavailable (e.g., during a routine upgrade).
2. The CMC heartbeat retries every minute but cannot refresh the rate; `icp_xdr_conversion_rate.timestamp_seconds` grows stale.
3. ICP market price rises 8 % during the outage.
4. An unprivileged user calls `notify_top_up` with 10 ICP.
5. `tokens_to_cycles` reads the stale `xdr_permyriad_per_icp` (8 % below current market) and mints cycles accordingly.
6. The user receives ~8 % fewer cycles than the current market rate warrants; the 10 ICP is burned via `burn_and_log` with no refund path.
7. Conversely, if ICP price fell 8 %, the same call mints 8 % more cycles than warranted, inflating the cycle supply. [10](#0-9) [11](#0-10)

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

**File:** rs/nns/cmc/src/main.rs (L2397-2401)
```rust
#[heartbeat]
async fn canister_heartbeat() {
    if with_state(|state| state.exchange_rate_canister_id.is_some()) {
        update_exchange_rate().await
    }
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

**File:** rs/nns/cmc/src/lib.rs (L488-497)
```rust
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
