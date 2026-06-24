Audit Report

## Title
Stale ICP/XDR Conversion Rate Used Without Freshness Check in Cycles Minting - (File: `rs/nns/cmc/src/main.rs`)

## Summary
The `tokens_to_cycles` function in the Cycles Minting Canister reads the cached `icp_xdr_conversion_rate` and uses only `xdr_permyriad_per_icp`, never checking `timestamp_seconds`. If the Exchange Rate Canister (XRC) is unavailable for any sustained period, the cached rate grows stale and any unprivileged user can call `notify_top_up`, `notify_mint_cycles`, or `notify_create_canister` to mint cycles at an outdated favorable rate, receiving excess cycles per ICP relative to the current market price.

## Finding Description
`tokens_to_cycles` (L1899–1923) reads `state.icp_xdr_conversion_rate` and extracts only `xdr_permyriad_per_icp`:

```rust
let xdr_permyriad_per_icp = state
    .icp_xdr_conversion_rate
    .as_ref()
    .map(|rate| rate.xdr_permyriad_per_icp);
```

The `timestamp_seconds` field stored alongside the rate (L217–219) is never read. The rate is refreshed by a heartbeat-driven call to the XRC every `REFRESH_RATE_INTERVAL_SECONDS` (5 minutes, `exchange_rate_canister.rs` L15–16). On XRC call failure, the guard schedules a retry at the next minute (`exchange_rate_canister.rs` L153–161) but does **not** clear or age-gate the cached rate. A sustained XRC failure therefore directly translates to an unbounded staleness window, during which `tokens_to_cycles` continues to use the old rate. All three public mint paths call this function: `process_create_canister` (L1932), `process_mint_cycles` (L1965), and `process_top_up` (L1991). No existing guard in any of these paths checks the age of the cached rate before minting.

The `DivergedRate` governance path (`exchange_rate_canister.rs` L311–315) explicitly sets `UpdateExchangeRateState::Disabled`, which also leaves the cached rate in place indefinitely; however, triggering this path requires a governance proposal and is therefore a privileged action outside the unprivileged exploit path.

The primary exploit path requires only XRC unavailability (transient or sustained) combined with an ICP market price decline during the staleness window — both conditions that can occur without any attacker action on the protocol itself.

## Impact Explanation
When the real ICP market price drops while the CMC's cached rate remains at the old higher value, any user can send ICP to the CMC and call `notify_top_up` or `notify_mint_cycles` to receive more cycles per ICP than the current market rate warrants. Cycles are the IC's compute resource; excess cycles minted at a stale favorable rate represent a direct economic loss to the IC's cycle-pricing model. The per-hour damage is bounded by `DEFAULT_CYCLES_LIMIT` (150e15 cycles, L83), but accumulates for the full duration of the staleness window. This constitutes illegal minting of cycles and significant XRC/NNS infrastructure security impact with concrete protocol harm, fitting the **High** severity tier ($2,000–$10,000).

## Likelihood Explanation
The XRC is a DFINITY system canister on the NNS subnet. While extended unavailability is uncommon, transient failures (inter-canister call timeouts, XRC bugs, subnet congestion) can cause the rate to go un-refreshed for minutes to hours. The retry-at-next-minute logic means a sustained XRC failure directly translates to a growing staleness window. Any unprivileged user can observe the stale `timestamp_seconds` via the public `get_icp_xdr_conversion_rate` endpoint and time their minting calls accordingly. No special privileges, leaked keys, or governance access are required for the primary exploit path.

## Recommendation
In `tokens_to_cycles`, compare `rate.timestamp_seconds` against the current time (`ic_cdk::api::time() / 1_000_000_000`) and return a `NotifyError` if the rate is older than a configurable threshold (e.g., 30 minutes). This caps the exploitable staleness window regardless of how long the XRC remains unavailable. Optionally, expose the threshold as a canister configuration parameter so it can be adjusted via NNS proposal without a code upgrade.

## Proof of Concept
1. Monitor `get_icp_xdr_conversion_rate`; observe `timestamp_seconds` falling behind wall-clock time during an XRC outage.
2. Confirm ICP market price has dropped since that timestamp (stale rate is now higher than market).
3. Transfer ICP to the CMC subaccount with `MEMO_TOP_UP_CANISTER` or `MEMO_MINT_CYCLES`.
4. Call `notify_top_up` or `notify_mint_cycles` — `tokens_to_cycles` uses the stale `xdr_permyriad_per_icp` with no age check, minting cycles at the old favorable rate.
5. Repeat up to `DEFAULT_CYCLES_LIMIT` (150e15 cycles/hour) for the duration of the staleness window.
6. A deterministic integration test can reproduce this by: (a) initializing CMC state with a rate timestamped in the past, (b) disabling the XRC client mock to prevent refresh, (c) calling `notify_top_up` with a known ICP amount, and (d) asserting that cycles minted exceed what the current market rate would produce.