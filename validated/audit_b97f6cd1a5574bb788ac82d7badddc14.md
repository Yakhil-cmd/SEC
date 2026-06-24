Audit Report

## Title
Stale ICP/XDR Conversion Rate Used for Cycles Minting Without Staleness Validation - (File: rs/nns/cmc/src/main.rs)

## Summary
The `tokens_to_cycles` function in the Cycles Minting Canister performs only a `None` check on `state.icp_xdr_conversion_rate` before computing cycles, never inspecting the rate's `timestamp_seconds`. If the Exchange Rate Canister (XRC) is persistently unavailable, the stored rate ages indefinitely, and any unprivileged user can call `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` to receive cycles computed from the stale rate, over-minting cycles relative to ICP's current market value.

## Finding Description
`tokens_to_cycles` at `rs/nns/cmc/src/main.rs` L1899–1923 extracts only `rate.xdr_permyriad_per_icp` from the stored `IcpXdrConversionRate`, discarding `timestamp_seconds` entirely:

```rust
let xdr_permyriad_per_icp = state
    .icp_xdr_conversion_rate
    .as_ref()
    .map(|rate| rate.xdr_permyriad_per_icp);   // timestamp_seconds silently dropped
```

The `IcpXdrConversionRate` struct at `rs/nns/cmc/src/lib.rs` L488–497 carries `timestamp_seconds` precisely to record when the market data was sampled, but no code path between the struct and the three public minting endpoints ever consults it.

The CMC refreshes the rate every `REFRESH_RATE_INTERVAL_SECONDS` (5 minutes) via heartbeat. On XRC failure it reschedules to the next minute (`exchange_rate_canister.rs` L153–161). There is no ceiling on how many consecutive failures are tolerated before minting is suspended — the retry loop runs indefinitely while the stored rate ages without bound.

A second staleness path exists via `DivergedRate`: `set_update_exchange_rate_state` at `exchange_rate_canister.rs` L311–315 sets `UpdateExchangeRateState::Disabled`, permanently halting automatic XRC polling until an `EnableAutomaticExchangeRateUpdates` governance proposal arrives. During this window the CMC continues to mint from the frozen rate with no expiry.

The conversion arithmetic at `rs/nns/cmc/src/lib.rs` L359–366 confirms that a higher `xdr_permyriad_per_icp` directly and linearly increases cycles output per ICP token. Existing guards are limited to the `None` branch; no maximum-age check exists anywhere in the CMC.

By contrast, the Governance canister's `should_refresh_xdr_rate` at `rs/nns/governance/src/governance.rs` L6336–6348 explicitly rejects rates older than one day, demonstrating that the pattern is known and intentionally applied elsewhere in the NNS — but was never applied to the CMC's minting path.

## Impact Explanation
Cycles are pegged to XDR value (1 XDR ≈ 1T cycles). A stale rate that overstates ICP's XDR value causes the CMC to mint more cycles per ICP than the current market justifies. This constitutes illegal minting: cycles are created in excess of the ICP value deposited, effectively subsidising compute resources from protocol reserves and diluting the value of cycles for all existing holders. The three affected public endpoints — `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles` — are callable by any principal without privilege. The impact maps to **High**: significant XRC/NNS security impact with concrete, repeatable protocol harm (cycles over-issuance proportional to the rate divergence and the volume of ICP converted during the outage window).

## Likelihood Explanation
The XRC is an IC-native canister. Transient errors (`StablecoinRateTooFewRates`, inter-subnet call failures) are explicitly exercised in `rs/nns/integration_tests/src/cycles_minting_canister_with_exchange_rate_canister.rs` L162–172, confirming the scenario is considered realistic by the development team. No privileged access is required to exploit the window: any user who observes the `cmc_icp_xdr_conversion_rate_timestamp_seconds` metric diverging from wall-clock time can time `notify_top_up` calls to maximise the discount. The exploit is repeatable for any number of ICP blocks until the XRC recovers.

## Recommendation
Add a maximum-age guard inside `tokens_to_cycles` (or a shared helper called by all three notify paths). Retrieve the current time via the canister environment, compute `now - icp_xdr_conversion_rate.timestamp_seconds`, and return a retriable `NotifyError` if the age exceeds a defined threshold (e.g., 1 hour). This mirrors the pattern already used in `should_refresh_xdr_rate` in the Governance canister (`rs/nns/governance/src/governance.rs` L6336–6348), which refuses to use a rate older than one day. The threshold for the CMC should be tighter given that the heartbeat targets a 5-minute refresh cadence.

## Proof of Concept
1. Deploy a local replica with the CMC and a mock XRC canister.
2. Set the mock XRC to return `ExchangeRateError::StablecoinRateTooFewRates` on every call (as done in the existing integration test at L162–172).
3. Advance the replica clock by several hours; confirm via `get_icp_xdr_conversion_rate` that `timestamp_seconds` is now hours in the past while the stored `xdr_permyriad_per_icp` remains at the pre-outage value.
4. Transfer 100 ICP to the CMC subaccount for a test canister and call `notify_top_up`.
5. Observe that `tokens_to_cycles` returns cycles computed from the stale rate (e.g., 100T cycles at 10 XDR/ICP) rather than the current market rate (e.g., 75T cycles at 7.5 XDR/ICP), confirming ~33% over-minting with no error or rejection.
6. Repeat step 4–5 to demonstrate the exploit is unbounded in volume.