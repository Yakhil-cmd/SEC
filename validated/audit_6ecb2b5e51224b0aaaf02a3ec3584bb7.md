Audit Report

## Title
Stale ICP/XDR Exchange Rate Used Without Age Validation in Cycle Minting - (File: rs/nns/cmc/src/main.rs)

## Summary
The `tokens_to_cycles` function in the Cycles Minting Canister reads `state.icp_xdr_conversion_rate` and uses it unconditionally without checking the age of the stored rate. When the Exchange Rate Canister (XRC) is temporarily unavailable, the CMC retains the last known rate indefinitely and continues serving cycle conversions at that frozen price. An unprivileged user can exploit a stale (inflated) rate by purchasing ICP at the depressed market price and converting it to cycles at the outdated rate, minting more cycles than the ICP is worth at current market value.

## Finding Description
`tokens_to_cycles` (rs/nns/cmc/src/main.rs, L1900–1923) extracts only `xdr_permyriad_per_icp` from the stored `IcpXdrConversionRate` and ignores `timestamp_seconds` entirely:

```rust
let xdr_permyriad_per_icp = state
    .icp_xdr_conversion_rate
    .as_ref()
    .map(|rate| rate.xdr_permyriad_per_icp);
```

`process_top_up` (L1985–2012) calls `tokens_to_cycles` directly with no pre-check on rate freshness. The rate is refreshed every `REFRESH_RATE_INTERVAL_SECONDS` (5 minutes, `exchange_rate_canister.rs` L15–16) via `update_exchange_rate`. When the XRC call fails, the error path at `exchange_rate_canister.rs` L270–274 returns `Err(UpdateExchangeRateError::FailedToRetrieveRate(...))` without touching the stored rate, leaving it frozen. On failure, `schedule_next_attempt` reschedules a retry at the next minute (L153–160), so the CMC keeps retrying but keeps serving conversions at the stale rate between attempts.

`do_set_icp_xdr_conversion_rate` (main.rs L1022–1030) only enforces that a new rate must have a strictly greater timestamp than the current one; it imposes no upper bound on how old the current rate may be when used for conversions. The circuit breaker (`UpdateExchangeRateState::Disabled`, `exchange_rate_canister.rs` L311–314) is only triggered by a governance proposal with `DivergedRate` reason — it is not triggered automatically by rate staleness. `validate_exchange_rate` (`exchange_rate_canister_client.rs` L110–129) only checks source counts, not rate age.

The exploit path: attacker sends ICP to the CMC subaccount → calls `notify_top_up` → `process_top_up` calls `tokens_to_cycles` → cycles are computed using the frozen stale rate → `deposit_cycles` enforces only the hourly `base_cycles_limit`, not rate freshness → cycles are deposited and ICP is burned.

## Impact Explanation
This is illegal minting of cycles: the protocol mints cycles in excess of the real economic value of the ICP burned, inflating the cycle supply. The per-hour rate limiter (`base_cycles_limit`) bounds the rate of exploitation but does not eliminate it — over a sustained XRC outage (hours to days, which is a documented failure mode), the cumulative over-minting can be substantial. This matches the allowed impact: "Theft, permanent loss, illegal minting, or protocol insolvency involving exorbitant ICP/Cycles." Severity is High given the rate limiter constraint; it could reach Critical during a prolonged outage with a large ICP price drop.

## Likelihood Explanation
XRC failures are a documented and explicitly tested scenario (the codebase has tests for `CryptoBaseAssetNotFound`, `StablecoinRateTooFewRates`, and insufficient source counts). The entry point (`notify_top_up`) requires no special privileges — any user with an ICP ledger account can call it. ICP price can move materially in minutes. The attacker needs only to observe that the CMC rate has not moved while the market price has dropped, which is publicly observable on-chain via the `cmc_icp_xdr_conversion_rate_timestamp_seconds` metric.

## Recommendation
1. **Add a staleness guard in `tokens_to_cycles`**: compare `rate.timestamp_seconds` against `env.now_timestamp_seconds()` and return an error (e.g., `NotifyError::Other` with a descriptive message) if the rate is older than a configurable threshold (e.g., 30 minutes).
2. **Automatic circuit breaker**: if the rate has not been successfully refreshed within `N` minutes, automatically set `UpdateExchangeRateState::Disabled` (or a new `RateTooStale` state) to pause cycle minting without requiring a governance proposal.
3. **Alert on rate age**: the existing `cmc_icp_xdr_conversion_rate_timestamp_seconds` metric already exposes the rate timestamp; add a monitoring alert so operators are notified before the rate becomes dangerously stale.

## Proof of Concept
1. Deploy a local replica with the CMC and a mock XRC that initially returns a valid rate (`xdr_permyriad_per_icp = 50_000`, i.e., 5 XDR/ICP).
2. Switch the mock XRC to return `ExchangeRateError::CryptoBaseAssetNotFound` for all subsequent calls. The CMC's stored rate freezes at 50,000.
3. Advance the replica clock by 60+ minutes (beyond any reasonable staleness threshold).
4. Fund a test principal with 100 ICP at the "current market price" of 4 XDR/ICP (40% below the frozen rate).
5. Call `notify_top_up` with those 100 ICP. Observe that `tokens_to_cycles` computes cycles using `xdr_permyriad_per_icp = 50_000` (5 XDR/ICP) rather than the current 4 XDR/ICP.
6. Assert that the cycles received correspond to 500 XDR worth of cycles, not 400 XDR — a 25% over-mint, bounded only by `base_cycles_limit` per hour.

This can be implemented as a deterministic PocketIC integration test using the existing `MockExchangeRateCanisterClient` infrastructure already present in `rs/nns/cmc/src/exchange_rate_canister.rs`.