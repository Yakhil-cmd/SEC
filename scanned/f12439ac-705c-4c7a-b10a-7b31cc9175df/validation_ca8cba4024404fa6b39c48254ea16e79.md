### Title
Stale ICP/XDR Rate Used Without Freshness Check in Cycles Minting Canister - (`rs/nns/cmc/src/main.rs`)

### Summary

The Cycles Minting Canister (CMC) converts ICP to cycles using a cached `icp_xdr_conversion_rate` that is refreshed via heartbeat every 5 minutes by calling the Exchange Rate Canister (XRC). The `tokens_to_cycles` function uses this cached rate without any check on the age of the stored timestamp. If the XRC canister becomes unavailable for an extended period, the CMC silently continues minting cycles at the last known (potentially very stale) rate, enabling over-minting or under-minting of cycles relative to the true ICP market price.

### Finding Description

The CMC's `tokens_to_cycles` function reads `state.icp_xdr_conversion_rate` and uses it directly to compute cycles from ICP: [1](#0-0) 

The only guard is a `None` check — there is no check that `rate.timestamp_seconds` is within an acceptable age window. The rate is refreshed via heartbeat at a `REFRESH_RATE_INTERVAL_SECONDS` of 5 minutes: [2](#0-1) 

When the XRC call fails, the guard schedules a retry at the next minute but leaves the stored rate unchanged: [3](#0-2) 

The heartbeat only calls `update_exchange_rate` if `exchange_rate_canister_id` is set: [4](#0-3) 

If the XRC canister is stopped, its subnet is degraded, or its HTTP outcalls to external exchanges fail persistently, the CMC's stored rate becomes arbitrarily stale. All three public minting endpoints — `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles` — call `tokens_to_cycles` and will proceed with the stale rate: [5](#0-4) [6](#0-5) 

### Impact Explanation

If ICP's market price drops significantly while the XRC is offline, the CMC continues to mint cycles at the old (higher) ICP/XDR rate. Any user who sends ICP to the CMC during this window receives more cycles than the current market rate warrants — a direct cycles over-minting bug. Cycles are the IC's compute resource currency; over-minting dilutes the economic model and can be exploited by any user who observes the XRC is down and ICP price has fallen.

Conversely, if ICP price rises while the XRC is offline, users receive fewer cycles than they paid for, causing economic harm to users.

### Likelihood Explanation

The XRC canister depends on HTTP outcalls to external CEX/DEX APIs. These can fail due to rate limiting, exchange downtime, or network issues. The XRC canister itself can be stopped for upgrades. During any such period — which could last hours — the CMC's rate becomes stale. Any unprivileged user can call `notify_top_up` with a valid ICP ledger block index, making this reachable without any special privilege.

### Recommendation

In `tokens_to_cycles`, add a staleness check against `ic_cdk::api::time()`. If `rate.timestamp_seconds` is older than a configurable maximum age (e.g., 1 hour), reject the conversion and return an error rather than proceeding with a stale rate. This mirrors the Chainlink sequencer uptime pattern: do not execute financial operations when the price feed is known to be stale.

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let now = ic_cdk::api::time() / 1_000_000_000; // nanoseconds to seconds
        const MAX_RATE_AGE_SECONDS: u64 = 3600; // 1 hour
        match state.icp_xdr_conversion_rate.as_ref() {
            Some(rate) if now.saturating_sub(rate.timestamp_seconds) > MAX_RATE_AGE_SECONDS => {
                Err(NotifyError::Other {
                    error_code: NotifyErrorCode::Internal as u64,
                    error_message: "ICP/XDR conversion rate is stale, retry later".to_string(),
                })
            }
            Some(rate) => Ok(TokensToCycles {
                xdr_permyriad_per_icp: rate.xdr_permyriad_per_icp,
                cycles_per_xdr: state.cycles_per_xdr,
            }.to_cycles(amount)),
            None => Err(NotifyError::Other {
                error_code: NotifyErrorCode::Internal as u64,
                error_message: "No conversion rate found in CMC, notification aborted".to_string(),
            }),
        }
    })
}
```

### Proof of Concept

1. The XRC canister (`exchange_rate_canister_id`) becomes unavailable (e.g., its subnet stalls, or it is stopped for an upgrade).
2. The CMC heartbeat fires, calls `update_exchange_rate`, which calls `xrc_client.get_icp_to_xdr_exchange_rate(None)`, which fails. The error path schedules a retry but leaves `state.icp_xdr_conversion_rate` unchanged.
3. ICP market price drops 50% during the outage. The stored rate still reflects the pre-outage price.
4. An attacker sends ICP to the CMC's subaccount with `MEMO_TOP_UP_CANISTER` and calls `notify_top_up`.
5. `process_top_up` → `tokens_to_cycles` reads the stale rate and mints 2× the correct number of cycles.
6. The attacker receives cycles at the old (favorable) rate, effectively getting double the cycles per ICP compared to the true market rate. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1139-1145)
```rust
#[update]
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

**File:** rs/nns/cmc/src/main.rs (L1958-1965)
```rust
async fn process_mint_cycles(
    to_account: Account,
    amount: Tokens,
    deposit_memo: Option<Vec<u8>>,
    from: AccountIdentifier,
    sub: Subaccount,
) -> NotifyMintCyclesResult {
    let cycles = tokens_to_cycles(amount)?;
```

**File:** rs/nns/cmc/src/main.rs (L1985-1991)
```rust
async fn process_top_up(
    canister_id: CanisterId,
    from: AccountIdentifier,
    amount: Tokens,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;
```

**File:** rs/nns/cmc/src/main.rs (L2397-2401)
```rust
#[heartbeat]
async fn canister_heartbeat() {
    if with_state(|state| state.exchange_rate_canister_id.is_some()) {
        update_exchange_rate().await
    }
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L149-161)
```rust
                Err(error) => match error {
                    UpdateExchangeRateError::UpdateAlreadyInProgress => {}
                    UpdateExchangeRateError::Disabled => {}
                    UpdateExchangeRateError::NotReadyToGetRate(_) => {}
                    UpdateExchangeRateError::FailedToRetrieveRate(_)
                    | UpdateExchangeRateError::FailedToSetRate(_)
                    | UpdateExchangeRateError::InvalidRate(_) => {
                        state.update_exchange_rate_canister_state.replace(
                            UpdateExchangeRateState::get_rate_at_next_minute(
                                self.current_minute_in_seconds,
                            ),
                        );
                    }
```
