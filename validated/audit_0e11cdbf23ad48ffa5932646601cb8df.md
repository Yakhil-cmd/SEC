Audit Report

## Title
Cycles Minting Canister Uses Stale ICP/XDR Rate Without Staleness Check at Point of Conversion - (`File: rs/nns/cmc/src/main.rs`)

## Summary
The CMC's `tokens_to_cycles` function reads the stored `icp_xdr_conversion_rate` and uses only `xdr_permyriad_per_icp` to compute cycles, never inspecting `timestamp_seconds` or comparing it against the current time. If the Exchange Rate Canister (XRC) becomes unavailable for a sustained period, the CMC will continue minting cycles at the last stored rate indefinitely, with no on-chain staleness guard at the point of conversion. Any unprivileged user holding ICP can exploit a price divergence during such a window to receive more cycles than the current market rate warrants, constituting illegal over-minting of cycles.

## Finding Description

**Root cause — `tokens_to_cycles` (rs/nns/cmc/src/main.rs L1900–1923):**
The function reads `state.icp_xdr_conversion_rate`, extracts only `xdr_permyriad_per_icp`, and returns `Ok(...)`. The only guard is a `None` check (rate never initialized). `timestamp_seconds` is present in the struct but is never read here. There is no comparison of the form `now - rate.timestamp_seconds < MAX_STALENESS_THRESHOLD`, and no such constant exists anywhere in `rs/nns/cmc/`.

**`validate_exchange_rate` (rs/nervous_system/clients/src/exchange_rate_canister_client.rs L111–129):**
Validates only that `base_asset_num_received_rates >= MINIMUM_ICP_SOURCES` and `quote_asset_num_received_rates >= MINIMUM_CXDR_SOURCES`. No timestamp or age check is performed.

**`do_set_icp_xdr_conversion_rate` (rs/nns/cmc/src/main.rs L1022–1033):**
Checks only that `proposed_conversion_rate.timestamp_seconds > current_conversion_rate.timestamp_seconds` (monotonicity). It does not compare the proposed rate's timestamp against the current wall-clock time, so a rate that is hours old can be stored and will remain in use indefinitely once the XRC stops responding.

**Retry logic confirms the gap (rs/nns/cmc/src/exchange_rate_canister.rs L149–161):**
On `FailedToRetrieveRate`, `FailedToSetRate`, or `InvalidRate`, the next attempt is scheduled one minute later. During the entire retry window — which is unbounded in duration — the stale rate remains the active conversion rate with no age bound enforced at the point of use.

**Callers pass the result directly to minting operations:**
`process_top_up` (L1991) and `process_mint_cycles` (L1965) both call `tokens_to_cycles(amount)?` and immediately pass the result to `deposit_cycles` / `do_mint_cycles` with no intervening staleness check.

## Impact Explanation

This matches the allowed High impact: *"Significant XRC, NNS, or infrastructure security impact with concrete user or protocol harm."* Cycles are the sole resource unit for computation on the IC. Over-minting cycles at a stale (inflated) ICP/XDR rate means the protocol issues more cycles per ICP burned than the current market rate warrants. This is a direct, on-chain economic loss to the protocol: burned ICP is permanently destroyed, but the excess cycles created represent value extracted from the cycle economy. The attack is repeatable for the entire duration of the XRC outage and is accessible to any unprivileged user with ICP.

## Likelihood Explanation

The XRC is a DFINITY-operated system canister that aggregates rates from multiple external sources. The CMC's own error-handling code explicitly handles `StablecoinRateTooFewRates`, `InconsistentRatesReceived`, `CryptoBaseAssetNotFound`, and call-level failures — confirming that sustained XRC unavailability is a known, realistic failure mode. The retry interval is one minute on failure, but there is no upper bound on how many retries can occur before the stale rate becomes dangerously old. ICP price can move 20–50% in hours during volatile market conditions. Any user with ICP can call the public `notify_top_up` or `notify_mint_cycles` endpoints without any special privilege.

## Recommendation

In `tokens_to_cycles`, after extracting `xdr_permyriad_per_icp`, also read `rate.timestamp_seconds` and compare it against the current canister time (available via the `Environment` abstraction already used throughout the CMC, e.g., `env.now_timestamp_seconds()`). Define a `MAX_RATE_STALENESS_SECONDS` constant (e.g., 30 minutes) and return a `NotifyError` if `now - rate.timestamp_seconds > MAX_RATE_STALENESS_SECONDS`. This single guard at the point of conversion is sufficient to prevent over-minting regardless of how long the XRC remains unavailable.

## Proof of Concept

1. Deploy a local replica with the CMC and a mock XRC that returns a valid rate once, then begins returning `StablecoinRateTooFewRates` on all subsequent calls.
2. Record the stored `icp_xdr_conversion_rate` (e.g., reflecting ICP at $10 → `xdr_permyriad_per_icp = X`).
3. Advance the replica clock by 2 hours (e.g., via PocketIC's `advance_time`). Confirm via `get_icp_xdr_conversion_rate` that the stored rate's `timestamp_seconds` is now 2 hours old.
4. Transfer ICP to the CMC subaccount for a test canister and call `notify_top_up`.
5. Observe that `tokens_to_cycles` succeeds and returns cycles computed from the 2-hour-old rate — no staleness error is returned.
6. Repeat with a rate that is 24 hours old; the result is identical, confirming the absence of any staleness bound.