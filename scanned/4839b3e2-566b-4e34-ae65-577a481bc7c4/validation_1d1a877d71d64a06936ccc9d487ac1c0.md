### Title
Stale ICP/XDR Rate Used for Cycles Minting Allows Over-Minting During 5-Minute Update Window — (`rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) converts ICP to cycles using a cached `icp_xdr_conversion_rate` that is refreshed only every 5 minutes via a periodic heartbeat. The conversion function `tokens_to_cycles` applies this cached rate with no staleness check. Because the XRC rate is publicly observable, an unprivileged caller can monitor the live market rate, detect when the CMC's cached rate is materially higher than the current XRC rate (i.e., ICP has just dropped in value), and immediately call `notify_top_up` or `notify_mint_cycles` to convert ICP to cycles at the inflated stale rate — receiving more cycles per ICP than the current market warrants.

---

### Finding Description

`tokens_to_cycles` in `rs/nns/cmc/src/main.rs` reads `state.icp_xdr_conversion_rate` unconditionally:

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
            ...
        }
    })
}
```

There is no check on `rate.timestamp_seconds` to verify the rate is recent. The rate is refreshed by `update_exchange_rate` (called from the CMC heartbeat) at most once every `REFRESH_RATE_INTERVAL_SECONDS = 5 * ONE_MINUTE_SECONDS`. Between refreshes, the cached rate can diverge significantly from the live XRC rate.

`do_set_icp_xdr_conversion_rate` only validates that the incoming rate has a strictly greater timestamp than the current one — it does not enforce any maximum age on the rate used for minting:

```rust
if let Some(current_conversion_rate) = state.icp_xdr_conversion_rate.as_ref()
    && proposed_conversion_rate.timestamp_seconds
        <= current_conversion_rate.timestamp_seconds
{
    return Err("Proposed conversion rate must have greater timestamp than current one"...);
}
state.icp_xdr_conversion_rate = Some(proposed_conversion_rate.clone());
```

The live XRC rate is publicly queryable. The CMC's current cached rate is also publicly queryable via `get_icp_xdr_conversion_rate` (a certified query). An attacker can therefore:

1. Continuously monitor both the XRC rate and the CMC's cached rate.
2. When the XRC rate drops significantly (ICP has lost value) but the CMC's cached rate has not yet been updated, call `notify_top_up` or `notify_mint_cycles` with ICP.
3. Receive cycles computed at the stale (higher) `xdr_permyriad_per_icp`, yielding more cycles per ICP than the current market rate.
4. Repeat until the CMC's next heartbeat updates the rate.

The attack is purely ingress-driven and requires no privileged access.

---

### Impact Explanation

The attacker receives more cycles than the current ICP market value justifies. Cycles represent real compute resources on the IC. Over-minting cycles at a stale rate means the IC network subsidizes the attacker's computation: ICP is burned at a rate that no longer reflects its current value, while cycles are issued at the old (higher) rate. In a sharp ICP price drop, the gap between the stale CMC rate and the live XRC rate can be several percent within a single 5-minute window, and the hourly minting cap of 150 × 10¹⁵ cycles (`CYCLES_MINTING_LIMIT`) is large enough that a well-capitalized attacker can extract meaningful value before the rate corrects.

---

### Likelihood Explanation

The XRC rate and the CMC's cached rate are both publicly observable with no authentication required. ICP price volatility of several percent within 5 minutes is not uncommon during market events. The attack requires only a standard ICP ledger transfer followed by a `notify_top_up` or `notify_mint_cycles` call — both are permissionless ingress messages. No special tooling beyond a price feed and a canister call is needed.

---

### Recommendation

1. **Staleness guard in `tokens_to_cycles`**: Reject conversions when `now() - rate.timestamp_seconds` exceeds a configurable threshold (e.g., 10 minutes). Return a retriable `NotifyError` so callers can retry after the next heartbeat updates the rate.
2. **Reduce the refresh interval**: Lower `REFRESH_RATE_INTERVAL_SECONDS` from 5 minutes to 1–2 minutes to shrink the exploitation window.
3. **Bound the rate age at minting time**: Record the rate's `timestamp_seconds` alongside the minting event and refuse to mint if the rate is older than N seconds at the moment `process_top_up` / `process_mint_cycles` executes.

---

### Proof of Concept

**Attacker-controlled entry path** (unprivileged ingress):

1. Query `get_icp_xdr_conversion_rate` on the CMC → observe cached rate R_cmc (e.g., 50 000 XDR/ICP).
2. Query the XRC canister directly → observe live rate R_xrc (e.g., 45 000 XDR/ICP, a 10% drop).
3. Transfer N ICP to the CMC's subaccount for a target canister on the ICP ledger.
4. Call `notify_top_up` before the next CMC heartbeat fires.
5. `process_top_up` calls `tokens_to_cycles(amount)`, which reads `state.icp_xdr_conversion_rate.xdr_permyriad_per_icp = 50 000` (stale) and mints cycles at that rate.
6. Attacker receives ~11% more cycles than the current market rate would give (50 000 vs 45 000 XDR/ICP).
7. After the heartbeat fires and the rate corrects to 45 000, the attacker's cycles are already credited.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1007-1040)
```rust
/// Validates the proposed conversion rate, sets it in state, and sets the
/// canister's certified data
fn do_set_icp_xdr_conversion_rate(
    safe_state: &'static LocalKey<RefCell<Option<State>>>,
    env: &impl Environment,
    proposed_conversion_rate: IcpXdrConversionRate,
) -> Result<(), String> {
    print(format!(
        "[cycles] conversion rate update: {proposed_conversion_rate:?}"
    ));

    if proposed_conversion_rate.xdr_permyriad_per_icp == 0 {
        return Err("Proposed conversion rate must be greater than 0".to_string());
    }

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

        let witness_generator = convert_data_to_mixed_hash_tree(state);
        env.set_certified_data(&witness_generator.hash_tree().digest().0[..]);

        Ok(())
    })
}
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

**File:** rs/nns/cmc/src/main.rs (L1985-2012)
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
}
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L236-280)
```rust
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
