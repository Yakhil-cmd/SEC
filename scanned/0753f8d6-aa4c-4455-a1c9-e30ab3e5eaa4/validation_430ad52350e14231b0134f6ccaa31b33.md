### Title
Stale ICP/XDR Conversion Rate Used in Cycles Minting Without Freshness Check — (File: `rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) caches the ICP/XDR conversion rate in state and uses it directly for all ICP-to-cycles conversions (`notify_top_up`, `notify_create_canister`, `notify_mint_cycles`) without ever checking whether the cached rate is fresh. If the Exchange Rate Canister (XRC) becomes unavailable and the heartbeat-driven rate update fails, the cached rate can become arbitrarily stale. Any unprivileged user can then call the public minting endpoints and receive cycles computed from an outdated rate.

---

### Finding Description

The CMC stores the current ICP/XDR rate in `StateV2::icp_xdr_conversion_rate`, an `Option<IcpXdrConversionRate>` that carries both a `xdr_permyriad_per_icp` value and a `timestamp_seconds` field. [1](#0-0) 

The rate is refreshed via the canister heartbeat, which calls `update_exchange_rate()` at most once every five minutes (`REFRESH_RATE_INTERVAL_SECONDS = 5 * ONE_MINUTE_SECONDS`). [2](#0-1) [3](#0-2) 

When the XRC call fails, the guard schedules a retry one minute later, but the cached rate in state is **not invalidated or flagged as stale** — it simply remains at its last successfully fetched value indefinitely. [4](#0-3) 

All ICP-to-cycles conversions go through `tokens_to_cycles()`. This function reads `state.icp_xdr_conversion_rate` and checks only whether it is `Some` — it **never inspects `timestamp_seconds`** to verify the rate is recent:

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

`tokens_to_cycles()` is called unconditionally by all three public minting paths: [6](#0-5) [7](#0-6) [8](#0-7) 

---

### Impact Explanation

If the ICP market price drops sharply while the XRC is unavailable (e.g., due to a subnet upgrade, XRC canister failure, or sustained inter-canister call errors), the CMC continues minting cycles at the old, higher ICP/XDR rate. Any user who calls `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` during this window receives more cycles per ICP than the current market rate justifies. This constitutes an over-issuance of cycles — a direct ledger conservation / cycles accounting bug — because cycles are backed by ICP at the prevailing market rate, and the CMC is the sole authorized minter.

The inverse (stale rate that is too low) causes users to receive fewer cycles than they should, which is a user-harm issue but not a protocol-level loss.

---

### Likelihood Explanation

The XRC is a separate system canister on the NNS subnet. Any transient or sustained failure of the XRC (upgrade window, canister trap, inter-canister call timeout) causes the CMC heartbeat to log an error and schedule a retry, but leaves the cached rate unchanged. The CMC's `UpdateExchangeRateState::Disabled` path (triggered by a diverged rate) also permanently stops updates while leaving the stale rate in place. [9](#0-8) 

The three minting endpoints are publicly callable by any principal with no rate-freshness guard, making exploitation straightforward whenever the rate is stale and ICP price has moved downward. [10](#0-9) 

---

### Recommendation

In `tokens_to_cycles()`, after reading `state.icp_xdr_conversion_rate`, compare `rate.timestamp_seconds` against `ic_cdk::api::time() / 1_000_000_000` (current time in seconds). If the rate is older than a defined maximum age (e.g., 30 minutes or a configurable threshold), return a `NotifyError` indicating the rate is stale rather than proceeding with the conversion. This mirrors the short-term recommendation in the external report: store and enforce a freshness bound before using cached price data for financial operations.

---

### Proof of Concept

1. The XRC becomes unavailable (e.g., canister upgrade or sustained call failure).
2. The CMC heartbeat fires, calls `update_exchange_rate()`, the XRC call fails, and the guard schedules a retry — but `state.icp_xdr_conversion_rate` retains its last value (e.g., ICP = 10 XDR, set hours ago).
3. ICP market price drops to 5 XDR.
4. An attacker calls `notify_top_up` with a valid ICP ledger block, transferring 1 ICP to the CMC subaccount.
5. `process_top_up` → `tokens_to_cycles` reads `xdr_permyriad_per_icp` from the stale cached rate (10 XDR/ICP) and mints cycles equivalent to 10 XDR worth of cycles.
6. The attacker receives ~2× the cycles they should receive at the current market rate, with no check preventing this. [11](#0-10) [6](#0-5)

### Citations

**File:** rs/nns/cmc/src/main.rs (L217-218)
```rust
    /// How many XDR 1 ICP is worth, along with a timestamp.
    pub icp_xdr_conversion_rate: Option<IcpXdrConversionRate>,
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

**File:** rs/nns/cmc/src/main.rs (L1925-1933)
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

**File:** rs/nns/cmc/src/main.rs (L2397-2402)
```rust
#[heartbeat]
async fn canister_heartbeat() {
    if with_state(|state| state.exchange_rate_canister_id.is_some()) {
        update_exchange_rate().await
    }
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L149-162)
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
```
