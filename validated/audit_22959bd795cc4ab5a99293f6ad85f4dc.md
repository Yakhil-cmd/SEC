### Title
Stale Spot Oracle Rate in CMC `tokens_to_cycles` Allows Cycles to Be Minted at Favorable Stale Rates - (File: rs/nns/cmc/src/main.rs)

---

### Summary

The Cycles Minting Canister (CMC) converts ICP to cycles using a cached spot ICP/XDR rate that is only refreshed every 5 minutes via heartbeat. Any unprivileged user who observes a discrepancy between the CMC's stale cached rate and the current Exchange Rate Canister (XRC) rate can time their `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` calls to mint cycles at a rate more favorable than the current market price of ICP justifies.

---

### Finding Description

The `tokens_to_cycles` function in `rs/nns/cmc/src/main.rs` reads `state.icp_xdr_conversion_rate` — the most recently cached spot rate — to compute how many cycles to mint per ICP token:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        ...
    })
}
``` [1](#0-0) 

This cached rate is updated only once every 5 minutes via the CMC heartbeat, governed by `REFRESH_RATE_INTERVAL_SECONDS = 5 * ONE_MINUTE_SECONDS`: [2](#0-1) 

The heartbeat calls `update_exchange_rate`, which fetches the latest rate from the XRC and stores it in `state.icp_xdr_conversion_rate`: [3](#0-2) 

The CMC also maintains `average_icp_xdr_conversion_rate` (a multi-day moving average), but this field is **not** used by `tokens_to_cycles`. Only the spot rate is used for all three conversion entry points: `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles`. [4](#0-3) 

The XRC rate and the CMC's cached rate are both publicly readable. A user can query `get_icp_xdr_conversion_rate` on the CMC and compare it against the XRC's current rate. If the CMC's cached rate is higher than the XRC's current rate (i.e., ICP price has dropped but the CMC hasn't updated yet), the user can submit an ICP transfer and immediately call `notify_top_up` or `notify_mint_cycles` to lock in the stale, more favorable rate before the next heartbeat fires.

---

### Impact Explanation

An attacker who converts ICP to cycles during the up-to-5-minute staleness window when the CMC's cached rate is above the current XRC rate receives more cycles per ICP than the current market price of ICP justifies. Cycles are the network's compute currency; minting them at a discount relative to current ICP value dilutes the cycle economy — existing cycle holders effectively subsidize the attacker's below-market conversion. With large ICP amounts, the absolute gain per 5-minute window can be material. The attack is repeatable every rate-update cycle.

---

### Likelihood Explanation

The attack requires no special privileges. Both the CMC's cached rate (`get_icp_xdr_conversion_rate` is a public query) and the XRC's current rate (the XRC is a public system canister) are observable by anyone. The IC has no public mempool, so the attacker cannot frontrun in the Ethereum sense, but they do not need to: they simply need to observe the discrepancy and submit their ICP transfer + notify call before the next heartbeat fires. ICP price moves frequently enough that the CMC's cached rate will regularly lag the XRC rate by a meaningful margin within the 5-minute window.

---

### Recommendation

Replace the spot `icp_xdr_conversion_rate` in `tokens_to_cycles` with the already-computed `average_icp_xdr_conversion_rate` (the multi-day moving average). A time-weighted average is far more resistant to short-term price movements and eliminates the incentive to time conversions around heartbeat updates. Alternatively, reduce the refresh interval significantly (e.g., to 1 minute or less), though this only narrows the window rather than eliminating the root cause.

---

### Proof of Concept

1. At time T, the CMC's cached rate is 100 XDR/ICP (last updated at T-4m59s).
2. The XRC's current rate is 90 XDR/ICP (ICP price dropped ~10% in the last 5 minutes).
3. The attacker queries `get_icp_xdr_conversion_rate` on the CMC → sees 100 XDR/ICP.
4. The attacker queries the XRC directly → sees 90 XDR/ICP.
5. The attacker transfers 1000 ICP to the CMC subaccount and calls `notify_mint_cycles`.
6. `tokens_to_cycles` reads `state.icp_xdr_conversion_rate = 100 XDR/ICP` and mints cycles at that rate.
7. The attacker receives cycles equivalent to 100,000 XDR worth of compute, but only paid ICP worth 90,000 XDR at current market prices — a ~11% discount.
8. At T+1s, the CMC heartbeat fires, updates the rate to 90 XDR/ICP, and the window closes.
9. The attacker repeats this at every rate-update cycle where ICP price has moved downward.

The entry path is: unprivileged ingress sender → `notify_top_up` / `notify_mint_cycles` / `notify_create_canister` → `tokens_to_cycles` → stale `icp_xdr_conversion_rate`. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1900-1911)
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L232-280)
```rust
/// The periodic task for collecting the ICP/XDR rate from the Exchange Rate Canister.
/// To avoid having multiple calls sent to the Exchange Rate Canister,
/// this function contains a guard to ensure multiple calls cannot be made until
/// the prior call is complete.
pub async fn update_exchange_rate(
    safe_state: &'static LocalKey<RefCell<Option<State>>>,
    env: &impl Environment,
    xrc_client: &impl ExchangeRateCanisterClient,
) -> Result<(), UpdateExchangeRateError> {
    let now_timestamp_seconds = env.now_timestamp_seconds();
    let current_minute_seconds =
        round_down_to_multiple_of(now_timestamp_seconds, ONE_MINUTE_SECONDS);

    UpdateExchangeRateGuard::with_guard(safe_state, current_minute_seconds, async {
        let call_xrc_result = xrc_client.get_icp_to_xdr_exchange_rate(None).await;
        // Check if updating the rate via the exchange rate canister was disabled while retrieving the rate.
        // If it has, exit early.
        let is_updating_rate_disabled = read_state(safe_state, |state| {
            state
                .update_exchange_rate_canister_state
                .unwrap_or_default()
                == UpdateExchangeRateState::Disabled
        });
        if is_updating_rate_disabled {
            return Err(UpdateExchangeRateError::Disabled);
        }

        match call_xrc_result {
            Ok(exchange_rate) => {
                validate_exchange_rate(&exchange_rate)
                    .map_err(|error| UpdateExchangeRateError::InvalidRate(error.to_string()))?;
                let icp_xdr_conversion_rate = IcpXdrConversionRate::from(exchange_rate);
                if let Err(error) =
                    do_set_icp_xdr_conversion_rate(safe_state, env, icp_xdr_conversion_rate)
                {
                    return Err(UpdateExchangeRateError::FailedToSetRate(error));
                }
            }
            Err(error) => {
                return Err(UpdateExchangeRateError::FailedToRetrieveRate(
                    error.to_string(),
                ));
            }
        };

        Ok(())
    })
    .await
}
```
