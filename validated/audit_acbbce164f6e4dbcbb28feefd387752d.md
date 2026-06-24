Audit Report

## Title
Missing Staleness Check on Cached ICP/XDR Rate Enables Cycles Over-Minting During XRC Outage - (File: rs/nns/cmc/src/main.rs)

## Summary
`tokens_to_cycles` in `rs/nns/cmc/src/main.rs` reads `state.icp_xdr_conversion_rate` and uses it directly without checking whether the rate's `timestamp_seconds` is stale relative to the current canister time. If the Exchange Rate Canister (XRC) is unavailable, the CMC silently continues minting cycles at an arbitrarily old rate. When ICP price has fallen during an XRC outage, the stale-high cached rate causes the CMC to over-mint cycles for every ICP→cycles conversion, extracting value from the protocol.

## Finding Description
`tokens_to_cycles` is the single conversion function called by all three public minting endpoints. It extracts only `xdr_permyriad_per_icp` from the cached rate, discarding `timestamp_seconds` entirely:

```rust
// rs/nns/cmc/src/main.rs L1900-1922
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);  // timestamp_seconds silently dropped
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles { ... }.to_cycles(amount)),
            None => Err(...)
        }
    })
}
``` [1](#0-0) 

The `icp_xdr_conversion_rate` field carries a `timestamp_seconds` recording when the rate was fetched: [2](#0-1) 

`REFRESH_RATE_INTERVAL_SECONDS` (5 minutes) governs only when to call the XRC — it is never enforced at the point of use: [3](#0-2) 

`do_set_icp_xdr_conversion_rate` enforces only monotonicity (new timestamp must be strictly greater), not that the current cached rate is fresh enough to be used: [4](#0-3) 

When the XRC call fails, `update_exchange_rate` returns `FailedToRetrieveRate` and the cached rate is left unchanged indefinitely. On failure, the next attempt is scheduled one minute later, but the cached rate is never invalidated: [5](#0-4) 

All three public minting paths call `tokens_to_cycles` with no staleness guard: [6](#0-5) [7](#0-6) [8](#0-7) 

**Note on impact direction:** The over-minting scenario occurs when ICP price has *fallen* since the last successful XRC fetch. In that case the stale cached rate is *higher* than the current market rate, so `tokens_to_cycles` mints more cycles per ICP than the current ICP value warrants. (The claim's impact section incorrectly states the direction as "ICP price risen / stale-low rate"; the correct direction is "ICP price fallen / stale-high rate." The code defect and exploit path are otherwise accurate.)

## Impact Explanation
**Vulnerability type: Illegal cycles minting / protocol accounting bug.**

When ICP price falls during an XRC outage, the stale-high cached rate causes the CMC to mint cycles at a rate exceeding the current ICP market value. Any unprivileged user with a valid ICP ledger transfer can call `notify_top_up` or `notify_mint_cycles` and receive excess cycles. The magnitude scales with (a) the duration of the XRC outage, (b) the degree of ICP price decline, and (c) the volume of minting transactions during the outage. This constitutes illegal minting of cycles at the protocol's expense, matching the "Theft, permanent loss, illegal minting, or protocol insolvency involving exorbitant ICP/Cycles" impact class. Severity is **High** given the realistic but constrained conditions required.

## Likelihood Explanation
The XRC is a system canister on the NNS subnet and is generally highly available. However, subnet upgrades, replica bugs, or XRC-specific bugs can cause multi-minute to multi-hour outages. On failure, the CMC retries every minute but never invalidates the cached rate. The `notify_top_up` / `notify_create_canister` / `notify_mint_cycles` endpoints are publicly callable by any principal with a valid ICP ledger transfer. An attacker can observe the stale rate timestamp via the public `get_icp_xdr_conversion_rate` query and compare it against the current ICP spot price to determine whether the stale rate is favorable. The exploit is opportunistic (requires an XRC outage coinciding with ICP price decline) but requires no special privileges and is repeatable for the duration of the outage.

## Recommendation
In `tokens_to_cycles`, compare `rate.timestamp_seconds` against the current canister time and reject if the age exceeds a configured maximum (e.g., `REFRESH_RATE_INTERVAL_SECONDS * 2` or a dedicated `MAX_RATE_AGE_SECONDS` constant):

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let now = ic_cdk::api::time() / 1_000_000_000; // nanoseconds to seconds
        match state.icp_xdr_conversion_rate.as_ref() {
            Some(rate) if now.saturating_sub(rate.timestamp_seconds) <= MAX_RATE_AGE_SECONDS => {
                Ok(TokensToCycles {
                    xdr_permyriad_per_icp: rate.xdr_permyriad_per_icp,
                    cycles_per_xdr: state.cycles_per_xdr,
                }.to_cycles(amount))
            }
            Some(_) => Err(NotifyError::Other {
                error_code: NotifyErrorCode::Internal as u64,
                error_message: "ICP/XDR conversion rate is stale; please retry later".to_string(),
            }),
            None => Err(NotifyError::Other { ... }),
        }
    })
}
```

Additionally, expose a metric for `icp_xdr_conversion_rate_age_seconds` so operators can alert on stale rates before they affect minting.

## Proof of Concept
1. Query the current cached rate timestamp via the public query:
   ```
   dfx canister call rkp4c-7iaaa-aaaaa-aaaca-cai get_icp_xdr_conversion_rate
   ```
   Note `timestamp_seconds = T` and `xdr_permyriad_per_icp = R`.

2. Wait for or observe a period where the XRC is unavailable (e.g., NNS subnet upgrade). The CMC heartbeat will log `FailedToRetrieveRate` but the rate at timestamp `T` remains in state.

3. Observe that ICP spot price has *fallen* significantly below the rate recorded at `T` (i.e., the cached rate is now stale-high relative to current market). The CMC's public query will still return the old rate `R`.

4. Transfer ICP to the CMC's top-up subaccount for any canister:
   ```
   dfx ledger transfer <CMC_subaccount> --memo 1347768404 --amount <N>
   ```

5. Call `notify_top_up` with the resulting block index. The CMC calls `tokens_to_cycles`, which reads the stale-high rate `R` and mints cycles at the old (favorable) conversion, yielding more cycles than the current ICP price warrants.

6. A deterministic integration test can reproduce this by: (a) setting the CMC's cached rate to a high value, (b) advancing the mock clock beyond `REFRESH_RATE_INTERVAL_SECONDS * 2` without a successful XRC update, and (c) asserting that `tokens_to_cycles` still succeeds and returns the inflated cycle count rather than returning a staleness error.

### Citations

**File:** rs/nns/cmc/src/main.rs (L217-218)
```rust
    /// How many XDR 1 ICP is worth, along with a timestamp.
    pub icp_xdr_conversion_rate: Option<IcpXdrConversionRate>,
```

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

**File:** rs/nns/cmc/src/main.rs (L1932-1932)
```rust
    let cycles = tokens_to_cycles(amount)?;
```

**File:** rs/nns/cmc/src/main.rs (L1965-1965)
```rust
    let cycles = tokens_to_cycles(amount)?;
```

**File:** rs/nns/cmc/src/main.rs (L1991-1991)
```rust
    let cycles = tokens_to_cycles(amount)?;
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
