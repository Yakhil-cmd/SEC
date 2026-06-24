### Title
Stale ICP/XDR Exchange Rate Used Without Freshness Check in Cycles Minting Operations - (File: rs/nns/cmc/src/main.rs)

### Summary

The Cycles Minting Canister (CMC) converts ICP to cycles using a cached `icp_xdr_conversion_rate` in `tokens_to_cycles()`. This function reads the stored rate directly from state without checking whether the rate's `timestamp_seconds` is within an acceptable freshness window relative to the current canister time. If the Exchange Rate Canister (XRC) stops updating (or the CMC's heartbeat-driven update loop fails persistently), the CMC will continue minting cycles at an arbitrarily stale rate for an unbounded period. Any unprivileged user can call `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` to exploit this.

### Finding Description

The `tokens_to_cycles()` function in `rs/nns/cmc/src/main.rs` reads `state.icp_xdr_conversion_rate` and extracts only `xdr_permyriad_per_icp`, completely ignoring the `timestamp_seconds` field of the stored rate:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);  // timestamp_seconds never checked
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles { ... }.to_cycles(amount)),
            None => Err(...),
        }
    })
}
```

The CMC refreshes this rate via a heartbeat-driven call to the XRC every 5 minutes (`REFRESH_RATE_INTERVAL_SECONDS = 5 * ONE_MINUTE_SECONDS`). However, if the XRC becomes unavailable or the CMC's update loop fails persistently, the cached rate can become arbitrarily old. The only guard against using a stale rate is the `do_set_icp_xdr_conversion_rate()` function, which only enforces that a new rate's timestamp must be strictly greater than the current one — it does not enforce any maximum age on the rate already in state at the time of a minting operation.

The `validate_exchange_rate()` function called in `update_exchange_rate()` only checks the number of data sources (`base_asset_num_received_rates`, `quote_asset_num_received_rates`), not the age of the rate.

The three public update endpoints that call `tokens_to_cycles()` are:
- `notify_top_up` — callable by any principal
- `notify_create_canister` — callable by any principal
- `notify_mint_cycles` — callable by any principal

### Impact Explanation

If the ICP/XDR rate becomes stale (e.g., XRC is down for hours or days), the CMC continues to mint cycles at the last known rate. If ICP's real market price has dropped significantly since the last update, users can send ICP to the CMC and receive cycles computed at the old (higher) ICP/XDR rate, effectively getting more cycles per ICP than the current market rate justifies. This drains the protocol's cycle-minting capacity relative to the real cost of ICP. Conversely, if ICP's price has risen, users receive fewer cycles than they should — but the attacker-exploitable direction is the former (stale high rate). The impact is a **cycles/resource accounting bug**: cycles are minted at an incorrect rate, causing economic loss to the IC protocol (cycles are underpriced relative to real ICP cost).

### Likelihood Explanation

The XRC is a live canister dependency. Any sustained XRC outage, subnet issue, or CMC heartbeat failure lasting more than a few minutes leaves the rate stale. The CMC's own comment acknowledges this: `REFRESH_RATE_INTERVAL_SECONDS` is the intended refresh cadence, but there is no enforcement that the stored rate is fresh at the point of use. The attack requires no privileged access — any user with ICP can call `notify_top_up` or `notify_mint_cycles`. The window of exploitation grows with the duration of the XRC outage.

### Recommendation

In `tokens_to_cycles()`, compare `rate.timestamp_seconds` against the current canister time and reject the conversion if the rate is older than a defined maximum staleness threshold (e.g., `REFRESH_RATE_INTERVAL_SECONDS * 2` or a configurable bound). Return a `NotifyError` indicating the rate is stale, so callers can retry later when a fresh rate is available.

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let rate = state.icp_xdr_conversion_rate.as_ref();
        match rate {
            Some(rate) => {
                let age = now_seconds().saturating_sub(rate.timestamp_seconds);
                if age > MAX_RATE_AGE_SECONDS {
                    return Err(NotifyError::Other {
                        error_code: NotifyErrorCode::Internal as u64,
                        error_message: format!("ICP/XDR rate is stale ({age}s old)"),
                    });
                }
                Ok(TokensToCycles {
                    xdr_permyriad_per_icp: rate.xdr_permyriad_per_icp,
                    cycles_per_xdr: state.cycles_per_xdr,
                }.to_cycles(amount))
            }
            None => Err(...),
        }
    })
}
```

### Proof of Concept

1. The XRC canister becomes unavailable (e.g., subnet issue, canister trap loop). The CMC's heartbeat calls `update_exchange_rate()`, which calls `xrc_client.get_icp_to_xdr_exchange_rate(None)`, receives an error, and schedules a retry at the next minute. This continues indefinitely without updating `state.icp_xdr_conversion_rate`.

2. The real ICP market price drops significantly (e.g., from 10 XDR/ICP to 5 XDR/ICP), but the CMC's cached rate still reflects 10 XDR/ICP.

3. An unprivileged attacker sends ICP to the CMC's subaccount and calls `notify_top_up` (or `notify_mint_cycles`).

4. `process_top_up` calls `tokens_to_cycles()`, which reads `state.icp_xdr_conversion_rate.xdr_permyriad_per_icp` — the stale 10 XDR/ICP value — and mints cycles accordingly.

5. The attacker receives ~2x the cycles they should receive at the current market rate, effectively getting cycles at half price. The protocol absorbs the loss.

**Root cause code path:** [1](#0-0) 

The `timestamp_seconds` field of `icp_xdr_conversion_rate` is never compared to `now_seconds()` before the rate is used for minting. [2](#0-1) 

The 5-minute refresh interval is only a scheduling target; there is no enforcement at the point of use. [3](#0-2) 

`notify_top_up` is callable by any unprivileged principal, making this reachable without any special access. [4](#0-3) 

`validate_exchange_rate()` checks only source counts, not rate age — so a stale rate passes validation and is stored.

### Citations

**File:** rs/nns/cmc/src/main.rs (L1140-1145)
```rust
async fn notify_top_up(
    NotifyTopUp {
        block_index,
        canister_id,
    }: NotifyTopUp,
) -> Result<Cycles, NotifyError> {
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```
