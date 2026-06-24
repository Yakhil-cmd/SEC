Audit Report

## Title
Stale ICP/XDR Conversion Rate Used for Cycle Minting Without Freshness Check - (File: rs/nns/cmc/src/main.rs)

## Summary
The `tokens_to_cycles` function in the Cycles Minting Canister reads `state.icp_xdr_conversion_rate` and uses only `xdr_permyriad_per_icp` for conversion, never comparing `timestamp_seconds` against the current canister time. If the Exchange Rate Canister (XRC) becomes consistently unavailable, the CMC silently continues minting cycles at an arbitrarily stale rate. Any unprivileged ICP holder can exploit the divergence between the cached rate and the real market price to receive more cycles per ICP than the current market justifies.

## Finding Description
`tokens_to_cycles` at `rs/nns/cmc/src/main.rs` L1900–1923 extracts only `xdr_permyriad_per_icp` from the stored `IcpXdrConversionRate`, ignoring `timestamp_seconds` entirely:

```rust
let xdr_permyriad_per_icp = state
    .icp_xdr_conversion_rate
    .as_ref()
    .map(|rate| rate.xdr_permyriad_per_icp);
```

The only guard is a `None` check; no age comparison is performed. The `IcpXdrConversionRate` struct (`rs/nns/cmc/src/lib.rs` L488–497) carries `timestamp_seconds` precisely for this purpose, but it is never consulted at the point of use.

`validate_exchange_rate` in `rs/nervous_system/clients/src/exchange_rate_canister_client.rs` L111–129 only validates source counts (`base_asset_num_received_rates`, `quote_asset_num_received_rates`); it performs no freshness check on `ExchangeRate.timestamp`.

`do_set_icp_xdr_conversion_rate` (`rs/nns/cmc/src/main.rs` L1009–1040) only enforces that a new rate must have a strictly greater timestamp than the current one — it does not enforce that the current stored rate is recent.

The heartbeat calls `update_exchange_rate` every 5 minutes on success and retries every minute on failure (`rs/nns/cmc/src/exchange_rate_canister.rs` L15–16, L53–60). If the XRC is consistently unavailable (e.g., canister upgrade, trapped state, sustained messaging failures), the CMC keeps scheduling retries but never invalidates or rejects the stale cached rate. All three user-facing minting paths — `process_top_up` (L1985–1991), `process_create_canister` (L1925–1932), and `process_mint_cycles` — call `tokens_to_cycles` as their first step and proceed to mint cycles using whatever rate is cached.

The `DivergedRate` governance mechanism (`rs/nns/cmc/src/exchange_rate_canister.rs` L311–315) can disable automatic updates, but it requires an NNS proposal to be submitted and voted on, introducing a multi-hour human-intervention window during which the stale rate remains in use.

## Impact Explanation
During a window of XRC unavailability coinciding with a significant ICP price drop, any ICP holder can call `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` and receive cycles computed from the stale (inflated) rate. The conversion is a direct linear function of `xdr_permyriad_per_icp` (`rs/nns/cmc/src/lib.rs` L358–366): a rate 2× the real price yields 2× the cycles for the same ICP. This constitutes unauthorized minting of cycles — cycles are created without corresponding economic backing — which is a concrete ledger-conservation violation. This matches the allowed High impact: "Significant XRC, NNS, or infrastructure security impact with concrete user or protocol harm."

## Likelihood Explanation
The XRC is a system canister on the NNS subnet. Scenarios where it is specifically unavailable while the CMC continues running include: XRC canister upgrade (minutes of unavailability), a bug causing the XRC to trap on requests, or sustained inter-canister messaging failures. The CMC's cached rate is publicly queryable via `get_icp_xdr_conversion_rate`. A sophisticated attacker can monitor the divergence between the CMC's cached rate and the real ICP market price (e.g., via CEX feeds), then time `notify_top_up` calls to exploit the gap. No privileged access is required; any ICP holder can trigger the minting endpoints. The attack is repeatable for as long as the stale rate persists.

## Recommendation
In `tokens_to_cycles`, compare `rate.timestamp_seconds` against `ic_cdk::api::time() / 1_000_000_000` and return a `NotifyError` (e.g., `NotifyError::Other` with a descriptive message) when the rate age exceeds a defined maximum (e.g., 1–2 hours). Extend `validate_exchange_rate` or add a separate freshness check that enforces a maximum age on `ExchangeRate.timestamp` before it is stored via `do_set_icp_xdr_conversion_rate`. This ensures that even if the XRC is unavailable for an extended period, the CMC refuses to mint cycles rather than minting at a potentially arbitrarily stale rate.

## Proof of Concept
1. Deploy a local replica with the CMC and a mock XRC that returns a rate of `100_000` permyriad (10 XDR/ICP).
2. After the CMC stores the rate, disable the mock XRC (simulate unavailability by making it trap).
3. Advance the replica clock by 6 hours; confirm via `get_icp_xdr_conversion_rate` that the CMC still reports `100_000` permyriad.
4. Simulate a market price drop to `50_000` permyriad (5 XDR/ICP) by what a fresh XRC call would return.
5. Send 1 ICP to the CMC subaccount and call `notify_top_up`.
6. Observe that `tokens_to_cycles` computes cycles using `100_000` permyriad — 2× the current market rate — with no error returned.
7. Confirm no freshness check exists in `tokens_to_cycles` (L1900–1923) or `validate_exchange_rate` (L111–129) by code inspection; the only guard is the `None` check on the rate.