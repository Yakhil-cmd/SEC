Audit Report

## Title
Stale ICP/XDR Conversion Rate Used in Cycles Minting Without Freshness Check — (File: `rs/nns/cmc/src/main.rs`)

## Summary
The Cycles Minting Canister stores the ICP/XDR conversion rate as `StateV2::icp_xdr_conversion_rate` and uses it in `tokens_to_cycles()` for all ICP-to-cycles conversions without ever comparing `rate.timestamp_seconds` against the current time. When the Exchange Rate Canister (XRC) is unavailable, the heartbeat-driven update fails silently and reschedules a retry, but the cached rate is never invalidated. Any unprivileged user can call the public minting endpoints during this window and receive cycles computed from an arbitrarily stale rate.

## Finding Description
`StateV2::icp_xdr_conversion_rate` is an `Option<IcpXdrConversionRate>` carrying both `xdr_permyriad_per_icp` and `timestamp_seconds`. [1](#0-0) 

The rate is refreshed via `canister_heartbeat()` → `update_exchange_rate()`, which calls the XRC at most once every `REFRESH_RATE_INTERVAL_SECONDS = 5 * ONE_MINUTE_SECONDS`. [2](#0-1) [3](#0-2) 

When the XRC call fails (`FailedToRetrieveRate`, `FailedToSetRate`, or `InvalidRate`), `schedule_next_attempt()` only reschedules the next attempt one minute later — it does **not** clear or flag `state.icp_xdr_conversion_rate`. [4](#0-3) 

`tokens_to_cycles()` reads `state.icp_xdr_conversion_rate` and checks only whether it is `Some`. It never reads `timestamp_seconds` and never compares it against the current time:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);  // timestamp_seconds ignored
        ...
    })
}
``` [5](#0-4) 

All three public minting paths call `tokens_to_cycles()` unconditionally: [6](#0-5) [7](#0-6) [8](#0-7) 

Additionally, `UpdateExchangeRateState::Disabled` (set by a governance `DivergedRate` proposal) permanently halts XRC polling while leaving the last cached rate in place indefinitely. [9](#0-8) [10](#0-9) 

`do_set_icp_xdr_conversion_rate()` only validates that the incoming rate has a strictly greater timestamp than the current one — it performs no check that the rate is recent relative to wall-clock time. [11](#0-10) 

`notify_top_up` is a public `#[update]` endpoint with no caller restriction and no rate-freshness guard. [12](#0-11) 

## Impact Explanation
If the ICP market price drops while the XRC is unavailable, the CMC continues minting cycles at the old, higher ICP/XDR rate. Every call to `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` during this window mints more cycles per ICP than the current market rate justifies. Cycles are the sole unit of computation cost on the IC and are backed by ICP at the prevailing market rate; the CMC is the sole authorized minter. Over-issuance of cycles constitutes illegal minting and direct protocol-level accounting loss. This matches the **High** impact class: "Significant XRC, NNS, or infrastructure security impact with concrete user or protocol harm." The `base_cycles_limit` constrains the per-hour damage but does not prevent the vulnerability. [13](#0-12) 

## Likelihood Explanation
The XRC is a system canister on the NNS subnet. Any transient failure — canister upgrade window, trap, inter-canister call timeout — causes the CMC heartbeat to log an error and schedule a retry without invalidating the cached rate. The `Disabled` path (triggered by a governance diverged-rate proposal) permanently stops updates. The three minting endpoints are publicly callable by any principal with no rate-freshness guard, making exploitation straightforward whenever the rate is stale and the ICP price has moved downward. No special privileges are required. [14](#0-13) 

## Recommendation
In `tokens_to_cycles()`, after reading `state.icp_xdr_conversion_rate`, compare `rate.timestamp_seconds` against `ic_cdk::api::time() / 1_000_000_000`. If the rate is older than a defined maximum age (e.g., 30 minutes, or a configurable `max_rate_age_seconds` stored in state), return a `NotifyError` indicating the rate is stale rather than proceeding with the conversion. This ensures that a prolonged XRC outage causes minting to halt rather than proceed at an incorrect rate. [5](#0-4) 

## Proof of Concept
1. The XRC becomes unavailable (e.g., canister upgrade or sustained call failure on the NNS subnet).
2. The CMC heartbeat fires, `update_exchange_rate()` is called, the XRC call returns an error, `schedule_next_attempt()` reschedules one minute later — `state.icp_xdr_conversion_rate` retains its last value (e.g., ICP = 10 XDR, set hours ago).
3. ICP market price drops to 5 XDR.
4. An attacker sends 1 ICP to the CMC subaccount for their canister with `MEMO_TOP_UP_CANISTER` and calls `notify_top_up` with the resulting block index.
5. `process_top_up` → `tokens_to_cycles` reads `xdr_permyriad_per_icp` from the stale cached rate (10 XDR/ICP) and mints cycles equivalent to 10 XDR worth of cycles.
6. The attacker receives approximately 2× the cycles they should receive at the current market rate, with no check preventing this.

A deterministic integration test can reproduce this by: (a) initializing CMC state with a known rate and timestamp, (b) advancing the mock clock past the staleness threshold without triggering a successful XRC update, and (c) asserting that `notify_top_up` succeeds and returns cycles computed from the stale rate rather than returning a staleness error. [15](#0-14)

### Citations

**File:** rs/nns/cmc/src/main.rs (L217-218)
```rust
    /// How many XDR 1 ICP is worth, along with a timestamp.
    pub icp_xdr_conversion_rate: Option<IcpXdrConversionRate>,
```

**File:** rs/nns/cmc/src/main.rs (L232-233)
```rust
    /// How many cycles are allowed to be minted in an hour.
    pub base_cycles_limit: Cycles,
```

**File:** rs/nns/cmc/src/main.rs (L1022-1033)
```rust
    mutate_state(safe_state, |state| {
        if let Some(current_conversion_rate) = state.icp_xdr_conversion_rate.as_ref()
            && proposed_conversion_rate.timestamp_seconds
                <= current_conversion_rate.timestamp_seconds
        {
            return Err(
                "Proposed conversion rate must have greater timestamp than current one".to_string(),
            );
        }

        state.icp_xdr_conversion_rate = Some(proposed_conversion_rate.clone());
        update_recent_icp_xdr_rates(state, &proposed_conversion_rate);
```

**File:** rs/nns/cmc/src/main.rs (L1139-1146)
```rust
#[update]
async fn notify_top_up(
    NotifyTopUp {
        block_index,
        canister_id,
    }: NotifyTopUp,
) -> Result<Cycles, NotifyError> {
    let caller = caller();
```

**File:** rs/nns/cmc/src/main.rs (L1900-1923)
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
}
```

**File:** rs/nns/cmc/src/main.rs (L1931-1932)
```rust
) -> Result<CanisterId, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;
```

**File:** rs/nns/cmc/src/main.rs (L1964-1965)
```rust
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L98-100)
```rust
        if current_call_state == UpdateExchangeRateState::Disabled {
            return Err(UpdateExchangeRateError::Disabled);
        }
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L133-165)
```rust
    fn schedule_next_attempt(&self, result: &Result<(), UpdateExchangeRateError>) {
        mutate_state(self.safe_state, |state| {
            if let Some(UpdateExchangeRateState::Disabled) =
                state.update_exchange_rate_canister_state
            {
                return;
            }

            match result {
                Ok(_) => {
                    state.update_exchange_rate_canister_state.replace(
                        UpdateExchangeRateState::get_rate_at_next_refresh_rate_interval(
                            self.current_minute_in_seconds,
                        ),
                    );
                }
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
                },
            }
        });
    }
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L311-314)
```rust
                UpdateIcpXdrConversionRatePayloadReason::DivergedRate => {
                    state
                        .update_exchange_rate_canister_state
                        .replace(UpdateExchangeRateState::Disabled);
```
