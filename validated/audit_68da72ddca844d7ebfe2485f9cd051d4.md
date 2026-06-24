Audit Report

## Title
Stale ICP/XDR Exchange Rate Used Without Timestamp Freshness Check in Cycles Minting Canister - (File: rs/nns/cmc/src/main.rs)

## Summary
The `tokens_to_cycles` function in `rs/nns/cmc/src/main.rs` extracts only `xdr_permyriad_per_icp` from the cached `icp_xdr_conversion_rate`, silently discarding the `timestamp_seconds` field and performing no staleness check before minting cycles. When the Exchange Rate Canister (XRC) fails to deliver updates, the CMC continues minting cycles at the last cached rate indefinitely, allowing any unprivileged caller to over-mint cycles relative to the true ICP market value for the entire duration of the outage.

## Finding Description
`tokens_to_cycles` at L1900–1922 of `rs/nns/cmc/src/main.rs` reads `state.icp_xdr_conversion_rate`, maps it to only `xdr_permyriad_per_icp`, and uses that value unconditionally:

```rust
let xdr_permyriad_per_icp = state
    .icp_xdr_conversion_rate
    .as_ref()
    .map(|rate| rate.xdr_permyriad_per_icp);
```

The `timestamp_seconds` field of `IcpXdrConversionRate` (defined at L487–497 of `rs/nns/cmc/src/lib.rs`) is never read at the point of conversion. No constant such as `MAX_RATE_AGE_SECONDS` exists anywhere in `rs/nns/cmc/src/` — a grep for `stale`, `MAX_RATE_AGE`, `rate_age`, `freshness`, and `expir` returns zero matches.

The rate is refreshed by a periodic heartbeat calling `update_exchange_rate` in `rs/nns/cmc/src/exchange_rate_canister.rs`. When the XRC call fails, the error branch at L270–274 returns `UpdateExchangeRateError::FailedToRetrieveRate` and leaves `state.icp_xdr_conversion_rate` unchanged. The guard's `schedule_next_attempt` at L153–160 reschedules the next attempt for the following minute, but no expiry is placed on the cached rate.

`do_set_icp_xdr_conversion_rate` at L1022–1030 of `rs/nns/cmc/src/main.rs` enforces only monotonicity (new `timestamp_seconds` > current `timestamp_seconds`), not recency relative to wall-clock time. `validate_exchange_rate` at L111–128 of `rs/nervous_system/clients/src/exchange_rate_canister_client.rs` checks only the number of data sources, not the timestamp.

The stale rate flows into all three public minting paths: `process_create_canister` (L1932), `process_mint_cycles` (L1965), and `process_top_up` (L1991), all of which call `tokens_to_cycles` as their first step. `notify_top_up` at L1139–1145 is a public `#[update]` endpoint callable by any principal with a valid ICP ledger block.

## Impact Explanation
This constitutes illegal minting of cycles relative to true ICP/XDR value, matching the allowed impact class "Theft, permanent loss, illegal minting, or protocol insolvency involving exorbitant ICP/Cycles." The IC's economic invariant is that one trillion cycles always costs one XDR worth of ICP. A stale rate breaks this invariant for the entire duration of an XRC outage. If ICP's market price drops 50 % during a multi-hour outage, every caller receives twice the warranted cycles per ICP spent. The minting endpoints are fully unprivileged, the window of exploitation equals the outage duration, and the over-minting is unbounded in aggregate.

## Likelihood Explanation
The XRC aggregates prices via HTTP outcalls to external exchanges. Sustained exchange API failures, XRC canister bugs, or a temporary subnet outage are all realistic, non-adversarial failure modes that have occurred on the IC mainnet. No special privileges, governance access, or key material are required. Any principal holding ICP can call `notify_top_up` with a valid ledger block. The CMC heartbeat will log errors and retry every minute but will not block conversions, so the exploit window is the full duration of the XRC outage.

## Recommendation
In `tokens_to_cycles`, compare `rate.timestamp_seconds` against the current canister time (e.g., `ic_cdk::api::time() / 1_000_000_000`) and return a `NotifyError::Other` if the age exceeds an acceptable threshold (e.g., a small multiple of `REFRESH_RATE_INTERVAL_SECONDS`, such as 30 minutes). The `IcpXdrConversionRate` struct already carries `timestamp_seconds` for exactly this purpose. A constant `MAX_RATE_AGE_SECONDS` should be defined alongside `REFRESH_RATE_INTERVAL_SECONDS` in `rs/nns/cmc/src/exchange_rate_canister.rs` and imported into `main.rs`.

## Proof of Concept
1. Deploy a local replica with the CMC and a mock XRC that returns a fixed rate (e.g., 20 000 XDR permyriad per ICP).
2. Disable the mock XRC (simulate sustained HTTP-outcall failure). The CMC heartbeat logs `FailedToRetrieveRate` but the cached rate remains.
3. Advance the replica clock by several hours (PocketIC supports manual time advancement).
4. Reduce the mock XRC rate to 10 000 (simulating a 50 % ICP price drop), but keep the XRC unreachable so the CMC cannot fetch the new rate.
5. Call `notify_top_up` with a valid ICP ledger block. Observe that `tokens_to_cycles` returns cycles computed at the stale 20 000 rate — twice the correct amount.
6. Re-enable the mock XRC and confirm that after the next successful heartbeat, the rate updates and the over-minting stops.

This is reproducible as a deterministic PocketIC integration test without any mainnet interaction.