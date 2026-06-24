Audit Report

## Title
No Staleness Check on Stored ICP/XDR Rate Allows Over-Minting of Cycles During XRC Outage - (File: rs/nns/cmc/src/main.rs)

## Summary
The `tokens_to_cycles` function in the Cycles Minting Canister reads `state.icp_xdr_conversion_rate` and extracts only `xdr_permyriad_per_icp`, never inspecting `timestamp_seconds`. When the Exchange Rate Canister (XRC) is unavailable, the CMC retries every minute but leaves the stored rate intact and unmodified. Any unprivileged caller can therefore mint cycles at a stale, potentially inflated rate for the entire duration of the XRC outage, permanently over-issuing cycles relative to the ICP value surrendered.

## Finding Description
`tokens_to_cycles` at `rs/nns/cmc/src/main.rs` L1900–1923 maps over `state.icp_xdr_conversion_rate` to extract only `xdr_permyriad_per_icp`; `timestamp_seconds` is never read or compared against the current time:

```rust
let xdr_permyriad_per_icp = state
    .icp_xdr_conversion_rate
    .as_ref()
    .map(|rate| rate.xdr_permyriad_per_icp);   // timestamp_seconds ignored
```

The rate is refreshed by `update_exchange_rate` in `rs/nns/cmc/src/exchange_rate_canister.rs` every `REFRESH_RATE_INTERVAL_SECONDS = 5 * ONE_MINUTE_SECONDS` (L15–16). On any retrieval failure (`FailedToRetrieveRate`, `FailedToSetRate`, `InvalidRate`), `schedule_next_attempt` (L149–161) schedules a retry one minute later but **does not clear or invalidate `state.icp_xdr_conversion_rate`**. The stored rate therefore persists unchanged for the entire outage.

`do_set_icp_xdr_conversion_rate` (L1018–1033) only rejects a proposed rate whose timestamp is not strictly greater than the current one; it imposes no upper bound on how old the stored rate may be when later consumed. `validate_exchange_rate` (in `rs/nervous_system/clients/src/exchange_rate_canister_client.rs` L111–129) checks only the number of data sources, not the rate's age.

All three public minting entry points funnel through `tokens_to_cycles`:
- `process_create_canister` L1932
- `process_mint_cycles` L1965
- `process_top_up` L1991

The exploit path is: XRC becomes unavailable → CMC retries every minute, leaving the last stored rate in place → ICP market price falls → attacker calls `notify_top_up` / `notify_mint_cycles` / `notify_create_canister` with ICP → CMC mints cycles at the stale inflated rate → ICP is burned, excess cycles are permanently issued.

## Impact Explanation
The impact is illegal minting of cycles: cycles are issued at a rate that exceeds the ICP value actually surrendered. Because cycles are the universal compute currency on the IC, systematic over-issuance dilutes the economic backing of all cycles in circulation and undermines the ICP-to-cycles peg the CMC is designed to maintain. The hourly `base_cycles_limit` (L232–233) bounds per-hour damage but does not prevent accumulation across multiple hours of XRC unavailability. This matches the allowed High impact: "Significant XRC, NNS, or infrastructure security impact with concrete user or protocol harm," and depending on the magnitude of the price drop and outage duration, may reach the Critical threshold of illegal minting involving exorbitant cycles.

## Likelihood Explanation
The XRC is a system canister on the NNS subnet. Transient failures (`StablecoinRateTooFewRates`, `CryptoBaseAssetNotFound`, call errors) are already handled in the retry logic, confirming they are observed in practice. A sequence of consecutive failures lasting more than a few minutes is plausible during XRC upgrades or periods of low exchange-data availability. The attacker requires no special privilege — any principal holding ICP can call the minting endpoints. The attacker does not need to cause the XRC failure; they only need to observe that the stored rate is stale and the ICP spot price has dropped, then submit minting notifications. The attack is repeatable across multiple hours until the XRC recovers or NNS governance intervenes.

## Recommendation
1. **Add a staleness guard in `tokens_to_cycles`**: compare `rate.timestamp_seconds` against `ic_cdk::api::time() / 1_000_000_000` and return a `NotifyError` if the stored rate is older than a configurable threshold (e.g., 30 minutes).
2. **Invalidate the stored rate on sustained failure**: after N consecutive XRC failures, set `state.icp_xdr_conversion_rate = None` so that `tokens_to_cycles` returns the existing "No conversion rate found" error rather than silently using a stale value.
3. **Expose rate age in metrics**: emit a gauge for `now - icp_xdr_conversion_rate.timestamp_seconds` so operators can alert before the threshold is breached.

## Proof of Concept
1. Deploy a local IC state machine with NNS canisters (PocketIC or `ic-state-machine-tests`).
2. Configure the XRC mock to return `ExchangeRateError::StablecoinRateTooFewRates` for all requests.
3. Record the last stored `icp_xdr_conversion_rate` (e.g., 50 XDR/ICP) via `get_icp_xdr_conversion_rate`.
4. Advance the state machine clock by 30+ minutes; confirm via `get_icp_xdr_conversion_rate` that `timestamp_seconds` has not advanced.
5. Simulate an ICP market price drop to 10 XDR/ICP (the value the XRC would return if healthy).
6. Call `notify_top_up` with 1 ICP. Observe that the CMC mints cycles at the stale 50 XDR/ICP rate — 5× more cycles than the current market price warrants.
7. Assert that `tokens_to_cycles` never inspects `rate.timestamp_seconds` and returns `Ok` with the inflated cycle count.