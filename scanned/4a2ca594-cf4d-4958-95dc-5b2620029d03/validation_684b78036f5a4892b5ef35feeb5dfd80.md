### Title
No ICP/XDR Rate Staleness Check in SNS Token Valuation — (File: `rs/sns/governance/token_valuation/src/lib.rs`)

### Summary
The `CmcBased30DayMovingAverageXdrsPerIcpClient::get()` function in the SNS governance token valuation module fetches the ICP/XDR conversion rate from the Cycles Minting Canister (CMC) and uses it directly without checking the `timestamp_seconds` field of the response. This rate gates `TransferSnsTreasuryFunds` and `MintSnsTokens` proposal execution. If the CMC's stored rate is stale, the treasury valuation-based access control can be bypassed or incorrectly applied.

### Finding Description
In `rs/sns/governance/token_valuation/src/lib.rs`, the production `XdrsPerIcpClient` implementation calls `get_average_icp_xdr_conversion_rate` on the CMC and immediately extracts `response.data.xdr_permyriad_per_icp` without inspecting `response.data.timestamp_seconds`:

```rust
let xdr_per_icp =
    Decimal::from(response.data.xdr_permyriad_per_icp) * *UNITS_PER_PERMYRIAD;
Ok(xdr_per_icp)
``` [1](#0-0) 

The `IcpXdrConversionRateCertifiedResponse` carries a `timestamp_seconds` field in its `data` payload that records when the market data was queried. [2](#0-1) 

The CMC updates its stored rate every 5 minutes via a heartbeat/timer that calls the Exchange Rate Canister (XRC). The XRC itself fetches prices from external exchanges via HTTP outcalls. If the XRC's HTTP outcalls fail (e.g., transient network issues, exchange downtime) or the XRC canister is temporarily unavailable, the CMC's stored rate is not refreshed. The CMC will continue to serve the last known rate — potentially hours or days old — with no indication of staleness beyond the `timestamp_seconds` field that callers are expected to check. [3](#0-2) 

The fetched rate flows into `try_get_balance_valuation_factors`, which computes the XDR value of the SNS treasury: [4](#0-3) 

This valuation is then used by `ProposalsAmountTotalUpperBound` to decide whether a `TransferSnsTreasuryFunds` or `MintSnsTokens` proposal is within the allowed limit. A stale (low) rate causes the treasury to appear smaller than it actually is, relaxing the cap and allowing proposals that should be blocked to proceed.

The code comment at the call site explicitly notes that certificate validation is skipped for inter-canister calls, but makes no mention of checking the rate's age: [5](#0-4) 

The same pattern appears in NNS governance's node-provider reward calculation, where `get_average_icp_xdr_conversion_rate` is called and the result used without a staleness check: [6](#0-5) 

### Impact Explanation
`TransferSnsTreasuryFunds` and `MintSnsTokens` proposals are subject to a valuation-based cap enforced at submission time. If the ICP/XDR rate used for valuation is stale and lower than the true market rate, the treasury's XDR value is underestimated. This causes the cap to be computed as a larger token amount than it should be, allowing proposals that exceed the intended limit to be submitted and executed. Concretely, an SNS treasury holding a large ICP balance could have funds drained beyond the intended 7-day window limit if the rate has not been refreshed during a period of XRC unavailability.

### Likelihood Explanation
The XRC canister fetches prices via HTTP outcalls to external exchanges. Transient failures (exchange downtime, network partitions, rate-limiting) cause the XRC to return errors, which in turn cause the CMC's heartbeat to skip the rate update. A sustained period of XRC unavailability (hours to days) is realistic without requiring any subnet-majority corruption. No privileged access or governance majority is needed; the stale-rate condition arises from ordinary infrastructure failures.

### Recommendation
After receiving the response from `get_average_icp_xdr_conversion_rate`, check that `response.data.timestamp_seconds` is within an acceptable freshness window before using the rate:

```rust
let now_seconds = /* ic_cdk::api::time() / 1_000_000_000 */;
let rate_age_seconds = now_seconds.saturating_sub(response.data.timestamp_seconds);
if rate_age_seconds > MAX_ACCEPTABLE_RATE_AGE_SECONDS {
    return Err(ValuationError::new_external(format!(
        "ICP/XDR rate is stale: age {} seconds exceeds limit {}",
        rate_age_seconds, MAX_ACCEPTABLE_RATE_AGE_SECONDS,
    )));
}
```

A reasonable threshold is 24–48 hours, consistent with the CMC's own 1-day staleness check used in NNS governance. [7](#0-6) 

### Proof of Concept
1. The XRC canister's HTTP outcalls to external exchanges begin failing (e.g., sustained exchange downtime or network partition — no privileged access required).
2. The CMC's heartbeat calls `get_icp_to_xdr_exchange_rate` on the XRC and receives errors; the CMC's stored `average_icp_xdr_conversion_rate` is not updated. Its `timestamp_seconds` remains at the last successful fetch.
3. ICP's market price rises significantly (e.g., 2×) during the outage period.
4. An SNS governance canister calls `try_get_icp_balance_valuation` → `new_standard_xdrs_per_icp_client::get()` → CMC's `get_average_icp_xdr_conversion_rate`.
5. The CMC returns the stale rate (half the true market value) with an old `timestamp_seconds`. The SNS governance code uses `response.data.xdr_permyriad_per_icp` directly without checking `response.data.timestamp_seconds`. [8](#0-7) 
6. The treasury's XDR valuation is computed at half the true value, placing it in a lower tier of `ProposalsAmountTotalUpperBound`. The per-proposal token cap is doubled relative to what it should be.
7. A `TransferSnsTreasuryFunds` proposal that would normally be blocked (treasury too large) is accepted and executed, draining funds beyond the intended limit.

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L141-191)
```rust
async fn try_get_balance_valuation_factors(
    account: Account,
    icrc1_client: &mut dyn Icrc1Client,
    icps_per_token_client: &mut dyn IcpsPerTokenClient,
    xdrs_per_icp_client: &mut dyn XdrsPerIcpClient,
) -> Result<ValuationFactors, ValuationError> {
    // Fetch the three ingredients:
    //
    //     1. balance
    //     2. token -> ICP
    //     3. ICP -> XDR
    //
    // No await here. Instead, we use join (right after this).
    let balance_of_request = icrc1_client.icrc1_balance_of(account);
    let icps_per_token_request = icps_per_token_client.get();
    let xdrs_per_icp_request = xdrs_per_icp_client.get();

    // Make all (3) requests (concurrently).
    let (balance_of_response, icps_per_token_response, xdrs_per_icp_response) = join!(
        balance_of_request,
        icps_per_token_request,
        xdrs_per_icp_request,
    );

    // Unwrap/forward errors to the caller.
    let balance_of_response = balance_of_response.map_err(|err| {
        ValuationError::new_external(format!("Unable to obtain balance from ledger: {err:?}"))
    })?;
    let icps_per_token_response = icps_per_token_response.map_err(|err| {
        ValuationError::new_external(format!("Unable to determine ICPs per token: {err:?}"))
    })?;
    let xdrs_per_icp_response = xdrs_per_icp_response.map_err(|err| {
        ValuationError::new_external(format!("Unable to obtain XDR per ICP: {err:?}"))
    })?;

    // Extract and interpret the data we actually care about from the (Ok) responses.
    let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
        ValuationError::new_arithmetic(format!(
            "Balance of {account:?} does not fit in u128: {err:?}"
        ))
    })?) / Decimal::from(E8);
    let icps_per_token = icps_per_token_response;
    let xdrs_per_icp = xdrs_per_icp_response;

    // Compose the fetched/interpretted data (i.e. multiply them) to construct the final result.
    Ok(ValuationFactors {
        tokens,
        icps_per_token,
        xdrs_per_icp,
    })
}
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L435-459)
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```

**File:** rs/nns/governance/src/governance.rs (L6336-6347)
```rust
    fn should_refresh_xdr_rate(&self) -> bool {
        let xdr_conversion_rate = &self.heap_data.xdr_conversion_rate;

        let now_seconds = self.env.now();

        let seconds_since_last_conversion_rate_refresh =
            now_seconds.saturating_sub(xdr_conversion_rate.timestamp_seconds);

        // Return `true` if more than 1 day has passed since the last `xdr_conversion_rate` was
        // updated. This assumes that `xdr_conversion_rate.timestamp_seconds` is rounded down to
        // the nearest day's beginning.
        seconds_since_last_conversion_rate_refresh > ONE_DAY_SECONDS
```

**File:** rs/nns/governance/src/governance.rs (L7668-7670)
```rust
        // The average (last 30 days) conversion rate from 10,000ths of an XDR to 1 ICP
        let icp_xdr_conversion_rate = self.get_average_icp_xdr_conversion_rate().await?.data;
        let avg_xdr_permyriad_per_icp = icp_xdr_conversion_rate.xdr_permyriad_per_icp;
```
