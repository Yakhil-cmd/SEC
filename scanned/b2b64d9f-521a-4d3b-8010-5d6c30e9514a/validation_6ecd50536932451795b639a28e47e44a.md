### Title
No Staleness Check on ICP/XDR Conversion Rate at Point of Consumption Allows Stale Prices to Drive Cycle Minting — (`rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) uses a cached `icp_xdr_conversion_rate` to convert ICP into cycles for `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles`. The rate is updated via a heartbeat-driven periodic task, but **at the point of consumption** (`tokens_to_cycles`), there is no check that the cached rate's `timestamp_seconds` is within an acceptable staleness window relative to the current canister time. If the Exchange Rate Canister (XRC) is unavailable for any reason, the CMC silently continues minting cycles at an arbitrarily old rate indefinitely.

---

### Finding Description

The CMC's `tokens_to_cycles` function reads `state.icp_xdr_conversion_rate` and uses it directly:

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

The only guard is a `None` check — there is no comparison of `rate.timestamp_seconds` against `now_seconds()`. [1](#0-0) 

This function is called by all three minting paths:
- `process_top_up` → `notify_top_up`
- `process_create_canister` → `notify_create_canister`
- `process_mint_cycles` → `notify_mint_cycles` [2](#0-1) [3](#0-2) [4](#0-3) 

The heartbeat-driven update mechanism schedules XRC calls every 5 minutes (`REFRESH_RATE_INTERVAL_SECONDS = 5 * ONE_MINUTE_SECONDS`) and retries after 1 minute on failure: [5](#0-4) [6](#0-5) 

However, this scheduling only controls *when the CMC tries to refresh*. It does not impose any maximum age on the rate that is *consumed*. If the XRC is unavailable for hours or days, the CMC continues minting cycles at the last known rate without any rejection or warning.

The `do_set_icp_xdr_conversion_rate` function only validates that a new rate's timestamp is strictly greater than the current one — it does not validate the rate against the current wall-clock time: [7](#0-6) 

The CMC is also initialized with a hardcoded default rate from 2021 (`DEFAULT_ICP_XDR_CONVERSION_RATE_TIMESTAMP_SECONDS`), meaning a freshly deployed CMC without a configured XRC would mint cycles at a years-old rate. [8](#0-7) 

---

### Impact Explanation

Any user calling `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` during a period when the XRC is unavailable receives cycles computed at a stale ICP/XDR rate. If the ICP market price has dropped significantly since the last successful rate update, users receive **more cycles per ICP than the current market rate justifies**, constituting an over-minting of cycles. Conversely, if ICP price has risen, users are under-served. The cycles ledger conservation invariant (ICP burned ↔ cycles minted at fair market rate) is violated for the duration of the staleness window.

---

### Likelihood Explanation

The XRC is a system canister that can be temporarily unavailable during:
- Canister upgrades (the XRC is upgraded periodically)
- Transient subnet issues
- The XRC running low on cycles

The CMC's heartbeat retries every minute on failure but imposes no upper bound on how stale the cached rate can become before minting is halted. On mainnet, the CMC's heartbeat fires continuously, so the window is bounded by XRC downtime duration. However, there is no protocol-level guarantee that the rate is fresh at the point of consumption. [9](#0-8) 

---

### Recommendation

Add a staleness check inside `tokens_to_cycles` (or at the call sites in `process_top_up`, `process_create_canister`, `process_mint_cycles`) that compares `rate.timestamp_seconds` against `now_seconds()` and returns an error if the rate is older than a configurable threshold (e.g., 30 minutes or 1 hour). This mirrors the fix applied in the Unitas Protocol, which added a per-token configurable threshold with a 1-day default, checked at the point of price consumption.

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        match state.icp_xdr_conversion_rate.as_ref() {
            Some(rate) => {
                let age = now_seconds().saturating_sub(rate.timestamp_seconds);
                if age > MAX_RATE_AGE_SECONDS {
                    return Err(NotifyError::Other {
                        error_code: NotifyErrorCode::Internal as u64,
                        error_message: format!("ICP/XDR rate is stale ({age}s old)"),
                    });
                }
                Ok(TokensToCycles { ... }.to_cycles(amount))
            }
            None => { /* existing error */ }
        }
    })
}
```

---

### Proof of Concept

1. The XRC becomes unavailable (e.g., during an upgrade or due to a transient issue).
2. The CMC's heartbeat fires, calls `update_exchange_rate`, which calls `xrc_client.get_icp_to_xdr_exchange_rate(None)`, receives an error, and schedules a retry in 1 minute.
3. The ICP market price drops 20% during the XRC outage.
4. A user calls `notify_top_up` with 1 ICP. `tokens_to_cycles` reads the stale `icp_xdr_conversion_rate` (which reflects the pre-drop price) and mints 20% more cycles than the current market rate justifies.
5. The user repeats this for as long as the XRC remains unavailable, extracting excess cycles.

The entry path is fully unprivileged: `notify_top_up` is a public `#[update]` endpoint callable by any principal. [10](#0-9) [1](#0-0)

### Citations

**File:** rs/nns/cmc/src/main.rs (L198-218)
```rust
#[derive(Clone, Eq, PartialEq, Debug, CandidType, Deserialize, Serialize)]
pub struct StateV2 {
    pub ledger_canister_id: CanisterId,

    pub governance_canister_id: CanisterId,

    /// An ID that provides an interface to a canister that provides exchange
    /// rate information such as the [XRC](https://github.com/dfinity/exchange-rate-canister).
    pub exchange_rate_canister_id: Option<CanisterId>,

    pub cycles_ledger_canister_id: Option<CanisterId>,

    /// Account used to burn funds.
    pub minting_account_id: Option<AccountIdentifier>,

    pub authorized_subnets: BTreeMap<PrincipalId, Vec<SubnetId>>,

    pub default_subnets: Vec<SubnetId>,

    /// How many XDR 1 ICP is worth, along with a timestamp.
    pub icp_xdr_conversion_rate: Option<IcpXdrConversionRate>,
```

**File:** rs/nns/cmc/src/main.rs (L1022-1030)
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

**File:** rs/nns/cmc/src/main.rs (L1958-1966)
```rust
async fn process_mint_cycles(
    to_account: Account,
    amount: Tokens,
    deposit_memo: Option<Vec<u8>>,
    from: AccountIdentifier,
    sub: Subaccount,
) -> NotifyMintCyclesResult {
    let cycles = tokens_to_cycles(amount)?;
    match do_mint_cycles(to_account, cycles, deposit_memo).await {
```

**File:** rs/nns/cmc/src/main.rs (L1985-1992)
```rust
async fn process_top_up(
    canister_id: CanisterId,
    from: AccountIdentifier,
    amount: Tokens,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;

```

**File:** rs/nns/cmc/src/main.rs (L2397-2428)
```rust
#[heartbeat]
async fn canister_heartbeat() {
    if with_state(|state| state.exchange_rate_canister_id.is_some()) {
        update_exchange_rate().await
    }
}

async fn update_exchange_rate() {
    let xrc_client = match with_state(|state| state.exchange_rate_canister_id) {
        Some(exchange_rate_canister_id) => {
            RealExchangeRateCanisterClient::new(exchange_rate_canister_id)
        }
        None => {
            print("[cycles] Exchange rate canister ID must be set to call the XRC");
            return;
        }
    };
    let env = CanisterEnvironment;
    let periodic_result =
        exchange_rate_canister::update_exchange_rate(&STATE, &env, &xrc_client).await;
    if let Err(ref error) = periodic_result {
        match error {
            UpdateExchangeRateError::InvalidRate(_)
            | UpdateExchangeRateError::FailedToRetrieveRate(_)
            | UpdateExchangeRateError::FailedToSetRate(_) => {
                print(format!("[cycles] {error}"));
            }
            UpdateExchangeRateError::Disabled
            | UpdateExchangeRateError::NotReadyToGetRate(_)
            | UpdateExchangeRateError::UpdateAlreadyInProgress => {}
        }
    }
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L149-165)
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
                },
            }
        });
    }
```
