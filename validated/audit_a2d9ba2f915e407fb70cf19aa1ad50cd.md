Audit Report

## Title
No Staleness Guard on Stored ICP/XDR Rate at Point of Cycles Minting - (File: rs/nns/cmc/src/main.rs)

## Summary
The `tokens_to_cycles` function in the Cycles Minting Canister reads only `xdr_permyriad_per_icp` from the stored `icp_xdr_conversion_rate` and never inspects `rate.timestamp_seconds`. When the Exchange Rate Canister (XRC) fails to supply a fresh rate — due to insufficient exchange sources — the CMC retries every minute but leaves the stored rate unchanged indefinitely. Any unprivileged caller of `notify_top_up` or `notify_mint_cycles` during such a period receives cycles computed from an arbitrarily stale rate, enabling over-minting of cycles relative to the ICP burned.

## Finding Description

**Root cause — `tokens_to_cycles`** (`rs/nns/cmc/src/main.rs`, lines 1900–1923):

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp); // timestamp_seconds never read
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles { ... }.to_cycles(amount)),
            None => Err(...)
        }
    })
}
```

`rate.timestamp_seconds` is present in the stored `IcpXdrConversionRate` struct but is never compared against the current time. There is no maximum-age threshold.

**Validation gap — `validate_exchange_rate`** (`rs/nervous_system/clients/src/exchange_rate_canister_client.rs`, lines 110–129):

The function checks only that `base_asset_num_received_rates >= MINIMUM_ICP_SOURCES` and `quote_asset_num_received_rates >= MINIMUM_CXDR_SOURCES`. It performs no check on `exchange_rate.timestamp` relative to the current time. A rate that was collected hours or days ago passes validation as long as it originally had enough sources.

**Staleness accumulation path** (`rs/nns/cmc/src/exchange_rate_canister.rs`, lines 149–161):

When `update_exchange_rate` returns `FailedToRetrieveRate`, `FailedToSetRate`, or `InvalidRate`, `schedule_next_attempt` sets the next retry to one minute later — but `state.icp_xdr_conversion_rate` is left unchanged. There is no upper bound on how many consecutive failures can occur before minting is suspended. The `DivergedRate` circuit-breaker (`UpdateExchangeRateState::Disabled`) requires a governance proposal to activate, which takes days and is not automatic.

**Attacker-controlled entry path** (`rs/nns/cmc/src/main.rs`, lines 1958–1965 and 1985–1991):

`process_mint_cycles` and `process_top_up` both call `tokens_to_cycles(amount)?` directly. Both are reachable from the public update methods `notify_mint_cycles` and `notify_top_up`, which require no privileged role.

## Impact Explanation

When the XRC is unavailable and ICP price falls, every call to `notify_top_up` or `notify_mint_cycles` mints cycles at the stale (higher) rate. The ICP burned is worth less than the cycles created, constituting illegal minting — a direct conservation violation against the ICP/cycles economic model. At scale, this drains the economic backing of the cycles supply. This matches the allowed impact: **"Theft, permanent loss, illegal minting, or protocol insolvency involving exorbitant ICP/Cycles"** and **"Significant XRC/NNS infrastructure security impact with concrete user or protocol harm."** Severity: **High**, with potential to escalate to Critical depending on the magnitude of price divergence and volume exploited.

## Likelihood Explanation

Exchange APIs are regularly rate-limited during high-volatility periods — exactly when price staleness is most dangerous. The XRC requires at least 4 ICP sources and 4 CXDR sources; simultaneous unavailability of enough sources is a realistic condition. The CMC retries every minute but has no circuit-breaker that halts minting after a configurable maximum staleness window. The exploit requires no special privileges: any principal can send ICP to a CMC subaccount and call `notify_top_up`. The window of exposure is unbounded and the attack is repeatable for as long as the XRC remains unavailable.

## Recommendation

1. Add a maximum-age check inside `tokens_to_cycles` before using the stored rate:

```rust
const MAX_RATE_AGE_SECONDS: u64 = 3600; // e.g. 1 hour

fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let rate = state.icp_xdr_conversion_rate.as_ref().ok_or_else(|| {
            NotifyError::Other { error_code: NotifyErrorCode::Internal as u64,
                error_message: "No conversion rate found in CMC".to_string() }
        })?;
        let age = now_seconds().saturating_sub(rate.timestamp_seconds);
        if age > MAX_RATE_AGE_SECONDS {
            return Err(NotifyError::Other {
                error_code: NotifyErrorCode::Internal as u64,
                error_message: format!("ICP/XDR rate is stale ({age}s old); minting suspended"),
            });
        }
        Ok(TokensToCycles {
            xdr_permyriad_per_icp: rate.xdr_permyriad_per_icp,
            cycles_per_xdr: state.cycles_per_xdr,
        }.to_cycles(amount))
    })
}
```

2. Extend `validate_exchange_rate` to reject rates whose `exchange_rate.timestamp` is older than a configurable threshold relative to the current time.

3. Consider automatically triggering `UpdateExchangeRateState::Disabled` after a configurable number of consecutive XRC failures, rather than relying solely on a governance proposal.

## Proof of Concept

1. CMC has a stored rate of 50,000 XDR/ICP (ICP = $10).
2. XRC HTTPS outcalls to exchanges begin failing (fewer than 4 sources respond). CMC logs `FailedToRetrieveRate` every minute but `state.icp_xdr_conversion_rate` remains at 50,000 XDR/ICP.
3. ICP price falls to $5 (25,000 XDR/ICP). XRC remains unavailable.
4. Attacker sends 1 ICP to the CMC subaccount and calls `notify_top_up` (or `notify_mint_cycles`).
5. `tokens_to_cycles` reads `xdr_permyriad_per_icp = 50_000` — no timestamp check occurs.
6. Attacker receives cycles equivalent to $10 worth of ICP while only depositing $5 worth: a 2× over-mint.
7. The ICP is burned; the excess cycles are permanently in circulation.

A deterministic integration test can reproduce this by: (a) initializing CMC state with a rate timestamped in the past, (b) configuring a mock XRC client that always returns `CryptoBaseAssetNotFound`, (c) advancing the mock clock by several hours, and (d) calling `notify_top_up` and asserting that cycles minted exceed the correct market-rate equivalent.