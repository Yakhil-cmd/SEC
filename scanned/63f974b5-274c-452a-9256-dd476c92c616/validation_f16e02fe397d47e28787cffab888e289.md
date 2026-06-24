### Title
Stale ICP/XDR Conversion Rate Used Without Freshness Validation in `tokens_to_cycles` - (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) stores an `icp_xdr_conversion_rate` with a `timestamp_seconds` field, but the `tokens_to_cycles` function — which is invoked on every cycle-minting operation — reads only `xdr_permyriad_per_icp` and never validates whether the stored rate is recent. If the Exchange Rate Canister (XRC) experiences downtime and the CMC's periodic heartbeat fails to refresh the rate, the CMC will silently continue minting cycles at an arbitrarily stale price indefinitely.

### Finding Description

`tokens_to_cycles` in `rs/nns/cmc/src/main.rs` reads the stored conversion rate and uses only the numeric value, ignoring `timestamp_seconds`:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);   // timestamp never checked
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles { ... }.to_cycles(amount)),
            None => Err(...)
        }
    })
}
``` [1](#0-0) 

This function is called by three publicly reachable update endpoints:
- `process_top_up` → `notify_top_up`
- `process_create_canister` → `notify_create_canister`
- `process_mint_cycles` → `notify_mint_cycles` [2](#0-1) [3](#0-2) [4](#0-3) 

The CMC is designed to refresh its rate every 5 minutes via a heartbeat that calls the XRC: [5](#0-4) 

When the XRC is unavailable or the heartbeat fails, the CMC retains the last known rate with no upper bound on how old it may be. The `do_set_icp_xdr_conversion_rate` function only enforces that a new rate must have a strictly greater timestamp than the current one — it does not enforce a maximum age on the rate at point of use: [6](#0-5) 

The `IcpXdrConversionRate` struct carries a `timestamp_seconds` field that is stored but never consulted during conversion: [7](#0-6) 

### Impact Explanation

If the ICP/XDR rate becomes stale (e.g., XRC downtime, heartbeat failure, or canister upgrade interruption) and the ICP market price has moved significantly:

- **If ICP price has fallen**: the stale (higher) rate causes the CMC to mint more cycles per ICP than the current market warrants. Users can exploit this window to acquire cycles at a discount, representing a direct economic loss to the IC ecosystem (cycles underpriced relative to their true cost).
- **If ICP price has risen**: users receive fewer cycles than the current market rate, a loss to users but not a security issue.

The first scenario is the security-relevant one: any unprivileged user who notices the rate is stale can call `notify_top_up` or `notify_mint_cycles` to mint cycles at a favorable rate, draining value from the protocol.

### Likelihood Explanation

The XRC is a separate canister on the IC. Any XRC downtime, inter-canister call failure, or CMC upgrade that interrupts the heartbeat loop will cause the rate to go stale. The CMC's own comment acknowledges this dependency: [8](#0-7) 

The heartbeat guard (`UpdateExchangeRateState`) prevents concurrent refreshes but provides no protection against indefinite staleness. There is no circuit-breaker or maximum-age guard at the point of use.

### Recommendation

In `tokens_to_cycles`, check that `icp_xdr_conversion_rate.timestamp_seconds` is within an acceptable window (e.g., `MAX_RATE_AGE_SECONDS`) before using the rate:

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
                        error_message: "ICP/XDR conversion rate is stale".to_string(),
                    });
                }
                Ok(TokensToCycles {
                    xdr_permyriad_per_icp: rate.xdr_permyriad_per_icp,
                    cycles_per_xdr: state.cycles_per_xdr,
                }.to_cycles(amount))
            }
            None => Err(...)
        }
    })
}
```

A reasonable `MAX_RATE_AGE_SECONDS` would be on the order of 30–60 minutes, giving the heartbeat multiple retry windows before blocking minting operations.

### Proof of Concept

1. The CMC's heartbeat calls `update_exchange_rate` every 5 minutes to refresh `icp_xdr_conversion_rate`.
2. Simulate XRC downtime: the heartbeat fails, leaving the stored rate at its last value with an old `timestamp_seconds`.
3. ICP market price drops 30% while the XRC is down.
4. An unprivileged user calls `notify_top_up` with a valid ICP ledger block.
5. `process_top_up` calls `tokens_to_cycles`, which reads the stale (pre-drop) `xdr_permyriad_per_icp` without checking `timestamp_seconds`.
6. The user receives ~43% more cycles than the current market rate warrants (e.g., 130 cycles worth of value for 100 cycles worth of ICP at current prices).
7. The CMC burns the ICP and deposits the inflated cycle count — the transaction is irreversible. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/nns/cmc/src/main.rs (L204-206)
```rust
    /// An ID that provides an interface to a canister that provides exchange
    /// rate information such as the [XRC](https://github.com/dfinity/exchange-rate-canister).
    pub exchange_rate_canister_id: Option<CanisterId>,
```

**File:** rs/nns/cmc/src/main.rs (L218-218)
```rust
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

**File:** rs/nns/cmc/src/main.rs (L1133-1145)
```rust
///
/// # Arguments
///
/// * `block_height` -  The height of the block you would like to send a
///   notification about.
/// * `canister_id` - Canister to be topped up.
#[update]
async fn notify_top_up(
    NotifyTopUp {
        block_index,
        canister_id,
    }: NotifyTopUp,
) -> Result<Cycles, NotifyError> {
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

**File:** rs/nns/cmc/src/main.rs (L1985-2011)
```rust
async fn process_top_up(
    canister_id: CanisterId,
    from: AccountIdentifier,
    amount: Tokens,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;

    let sub = Subaccount::from(&canister_id);

    print(format!(
        "Topping up canister {canister_id} by {cycles} cycles."
    ));

    match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
        Ok(()) => {
            burn_and_log(sub, amount).await;
            Ok(cycles)
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, TOP_UP_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err.to_string(),
                block_index: refund_block,
            })
        }
    }
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```
