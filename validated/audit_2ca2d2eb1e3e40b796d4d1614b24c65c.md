### Title
Stale Cached ICP/XDR Conversion Rate Used in Cycle Minting Without Freshness Check - (File: rs/nns/cmc/src/main.rs)

### Summary

The Cycles Minting Canister (CMC) caches the ICP/XDR spot conversion rate in `State.icp_xdr_conversion_rate` and updates it via a heartbeat-driven call to the Exchange Rate Canister (XRC) every five minutes. The `tokens_to_cycles` function — called by every cycle-minting path (`notify_top_up`, `notify_mint_cycles`, `notify_create_canister`) — reads this cached rate without any staleness check. If the XRC is unavailable for an extended period, the cached rate silently ages while cycle minting continues at the outdated price, producing incorrect cycle amounts.

### Finding Description

`tokens_to_cycles` in `rs/nns/cmc/src/main.rs` reads `state.icp_xdr_conversion_rate` and uses it directly:

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
            None => { /* error */ }
        }
    })
}
```

The only guard is a `None` check — there is no check on `icp_xdr_conversion_rate.timestamp_seconds` relative to the current time. [1](#0-0) 

The rate is refreshed by `update_exchange_rate` in `exchange_rate_canister.rs`, which is scheduled every `REFRESH_RATE_INTERVAL_SECONDS` (5 minutes) via the heartbeat. On XRC failure the guard reschedules for the next minute, but the cached rate is never invalidated or age-bounded: [2](#0-1) 

The `UpdateExchangeRateGuard` only prevents concurrent calls; it does not enforce a maximum age on the stored rate before it is consumed by minting: [3](#0-2) 

All three public cycle-minting update methods call `tokens_to_cycles` before any other rate-freshness logic:

- `process_top_up` → `tokens_to_cycles` [4](#0-3) 
- `process_mint_cycles` → `tokens_to_cycles` [5](#0-4) 
- `process_create_canister` → `tokens_to_cycles` [6](#0-5) 

The `icp_xdr_conversion_rate` field carries a `timestamp_seconds` that is never consulted at minting time: [7](#0-6) 

### Impact Explanation

**Vulnerability class: cycles/resource accounting bug.**

If the XRC subnet is degraded or the XRC canister is temporarily unavailable, the CMC's heartbeat fails to update `icp_xdr_conversion_rate`. The rate can remain stale for an arbitrarily long period. During that window:

- If the real ICP market price **falls** while the cached rate is high, every `notify_top_up` / `notify_mint_cycles` call mints **more cycles than the deposited ICP is worth** at the current market price. This is a direct cycles conservation violation: cycles are created in excess of the ICP value burned.
- If the real ICP market price **rises** while the cached rate is low, callers receive fewer cycles than they are entitled to — a loss to users but not a conservation break.

The over-minting scenario is the security-relevant one: an attacker who observes that the XRC is down and the cached rate is stale-high can repeatedly call `notify_top_up` to obtain cycles at a favorable rate, effectively extracting value from the network. The per-hour rate limiter (`base_cycles_limit`) bounds the total damage per hour but does not prevent the attack while the stale rate persists. [8](#0-7) 

### Likelihood Explanation

The XRC is a canister hosted on a system subnet. Transient subnet degradation, canister upgrades, or a persistent XRC error response (e.g., `StablecoinRateTooFewRates`) can prevent rate updates for minutes to hours. The CMC retries every minute on failure but never ages out the stale rate. Any unprivileged principal with ICP can call `notify_top_up` — no special role is required. The window of exploitation is bounded by XRC downtime duration and ICP price volatility during that window. Given that the IC has experienced subnet degradation events historically, this is a realistic scenario.

### Recommendation

Add a maximum-age check inside `tokens_to_cycles`. If `now - icp_xdr_conversion_rate.timestamp_seconds` exceeds a configured threshold (e.g., `MAX_RATE_AGE_SECONDS`, suggested ≥ 30 minutes), return a retriable `NotifyError` rather than minting at the stale rate. This mirrors the approach used in `should_refresh_xdr_rate` in governance, which already checks staleness before using the XDR rate for node-provider rewards: [9](#0-8) 

### Proof of Concept

1. Observe that the XRC canister is returning errors (e.g., `StablecoinRateTooFewRates`). The CMC's `update_exchange_rate_canister_state` transitions to `GetRateAt(next_minute)` but the cached `icp_xdr_conversion_rate` is not cleared.
2. Wait for the ICP market price to drop (e.g., 20%) while the XRC remains unavailable.
3. Call `notify_top_up` with a valid ICP ledger block. `tokens_to_cycles` reads the stale `icp_xdr_conversion_rate` (still reflecting the pre-drop price) and mints cycles at the old, higher rate.
4. The caller receives ~20% more cycles than the current ICP value warrants. Repeat up to the hourly rate limit.

The root cause is confirmed at: [1](#0-0)  — no `timestamp_seconds` freshness check exists before the rate is consumed.

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

**File:** rs/nns/cmc/src/main.rs (L1925-1932)
```rust
async fn process_create_canister(
    controller: PrincipalId,
    from: AccountIdentifier,
    amount: Tokens,
    subnet_selection: Option<SubnetSelection>,
    settings: Option<CanisterSettings>,
) -> Result<CanisterId, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L86-124)
```rust
impl UpdateExchangeRateGuard {
    /// Set the calling status to active.
    fn new(
        safe_state: &'static LocalKey<RefCell<Option<State>>>,
        current_minute_in_seconds: u64,
    ) -> Result<Self, UpdateExchangeRateError> {
        let current_call_state = read_state(safe_state, |state| {
            state
                .update_exchange_rate_canister_state
                .unwrap_or_default()
        });

        if current_call_state == UpdateExchangeRateState::Disabled {
            return Err(UpdateExchangeRateError::Disabled);
        }

        if current_call_state == UpdateExchangeRateState::InProgress {
            return Err(UpdateExchangeRateError::UpdateAlreadyInProgress);
        }

        if let UpdateExchangeRateState::GetRateAt(next_attempt_seconds) = current_call_state
            && current_minute_in_seconds < next_attempt_seconds
        {
            return Err(UpdateExchangeRateError::NotReadyToGetRate(
                next_attempt_seconds,
            ));
        }

        mutate_state(safe_state, |state| {
            state
                .update_exchange_rate_canister_state
                .replace(UpdateExchangeRateState::InProgress);
        });

        Ok(Self {
            safe_state,
            current_minute_in_seconds,
        })
    }
```

**File:** rs/nns/governance/src/governance.rs (L6336-6347)
```rust
    fn should_refresh_xdr_rate(&self) -> bool {
        let xdr_conversion_rate = &self.heap_data.xdr_conversion_rate;

        let now_seconds = self.env.now();

        let seconds_since_last_conversion_rate_refresh =
            now_seconds.saturating_sub(xdr_conversion_rate.timestamp_seconds);

        // Return `true` if more than 1 day has passed since the last `xdr_conversion_rate` was
        // updated. This assumes that `xdr_conversion_rate.timestamp_seconds` is rounded down to
        // the nearest day's beginning.
        seconds_since_last_conversion_rate_refresh > ONE_DAY_SECONDS
```
