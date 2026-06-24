### Title
Stale ICP/XDR Conversion Rate Used Without Staleness Check in CMC `tokens_to_cycles` — (`rs/nns/cmc/src/main.rs`)

### Summary

The Cycles Minting Canister (CMC) converts ICP to cycles using a stored ICP/XDR conversion rate. The `tokens_to_cycles` function uses this rate without any staleness check. If the exchange rate canister fails to update for an extended period and the ICP market price drops significantly, any unprivileged user can call `notify_top_up` or `notify_mint_cycles` and receive more cycles than the current market rate warrants — extracting value from the protocol at the expense of the cycles economy.

This is the direct IC analog of the OUSG/USDC depeg bug: both protocols assume a payment asset's value is pegged to a reference unit (USDC ≈ USD; ICP ≈ last-known XDR rate) without a live oracle guard at the point of minting.

---

### Finding Description

`tokens_to_cycles` in `rs/nns/cmc/src/main.rs` reads `state.icp_xdr_conversion_rate` and uses it unconditionally:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);   // ← timestamp never checked
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
``` [1](#0-0) 

The stored rate carries a `timestamp_seconds` field:

```rust
pub icp_xdr_conversion_rate: Option<IcpXdrConversionRate>,
``` [2](#0-1) 

but `tokens_to_cycles` never reads it. The rate is refreshed by a heartbeat-driven call to the exchange rate canister every `REFRESH_RATE_INTERVAL_SECONDS` (5 minutes): [3](#0-2) 

If the exchange rate canister is unavailable (returns an error), the CMC logs the failure and retains the last-known rate: [4](#0-3) 

There is no maximum-age guard before the rate is used for minting. The `do_set_icp_xdr_conversion_rate` function only enforces that a new rate's timestamp is strictly greater than the current one — it does not enforce freshness at consumption time: [5](#0-4) 

---

### Impact Explanation

Cycles are priced in XDR (1 XDR = `cycles_per_xdr` cycles, a fixed constant). ICP is converted to XDR using the stored rate. If the stored rate reflects a higher ICP/XDR value than the current market (because the exchange rate canister has been unavailable), a user depositing ICP receives more cycles than the current market rate warrants.

Example: rate is stale at 100 XDR/ICP; current market is 50 XDR/ICP. A user depositing 1 ICP receives 100 XDR worth of cycles instead of 50 XDR worth — a 2× extraction. The `base_cycles_limit` rate-limiter caps the per-hour damage but does not eliminate it; an attacker can sustain extraction across multiple hours. [6](#0-5) 

---

### Likelihood Explanation

The exchange rate canister is a system canister on the NNS subnet. It can return errors (e.g., `StablecoinRateTooFewRates`, network issues, canister traps). The CMC's test suite explicitly exercises the case where the exchange rate canister returns an error and the CMC silently retains the old rate: [7](#0-6) 

ICP price is volatile. A sustained outage of the exchange rate canister (minutes to hours) combined with a sharp ICP price drop is a realistic, monitorable condition. An attacker watching on-chain metrics can detect when the CMC's published rate diverges from market price and exploit the window.

---

### Recommendation

Add a staleness guard in `tokens_to_cycles` (or in `process_top_up` / `process_mint_cycles`) that rejects minting if the stored rate's `timestamp_seconds` is older than a configurable threshold (e.g., 30 minutes):

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let rate = state.icp_xdr_conversion_rate.as_ref()
            .ok_or_else(|| /* no rate error */)?;

        let age_seconds = now_seconds().saturating_sub(rate.timestamp_seconds);
        if age_seconds > MAX_RATE_AGE_SECONDS {
            return Err(NotifyError::Other {
                error_code: NotifyErrorCode::Internal as u64,
                error_message: format!(
                    "ICP/XDR conversion rate is stale ({age_seconds}s old); minting suspended"
                ),
            });
        }

        Ok(TokensToCycles {
            xdr_permyriad_per_icp: rate.xdr_permyriad_per_icp,
            cycles_per_xdr: state.cycles_per_xdr,
        }.to_cycles(amount))
    })
}
```

This mirrors the mitigation recommended in the OUSG report: block mints when the price feed is outside acceptable bounds.

---

### Proof of Concept

1. Observe that the CMC's published `icp_xdr_conversion_rate` timestamp is stale (exchange rate canister is returning errors).
2. Confirm that the current ICP market price is significantly below the stale rate.
3. Call `notify_top_up` (or `notify_mint_cycles`) with ICP tokens.
4. The CMC calls `tokens_to_cycles`, which reads the stale rate without a freshness check, and mints cycles at the inflated XDR/ICP ratio.
5. Repeat up to the `base_cycles_limit` per hour to extract cycles at below-market cost. [8](#0-7) [1](#0-0)

### Citations

**File:** rs/nns/cmc/src/main.rs (L218-218)
```rust
    pub icp_xdr_conversion_rate: Option<IcpXdrConversionRate>,
```

**File:** rs/nns/cmc/src/main.rs (L232-233)
```rust
    /// How many cycles are allowed to be minted in an hour.
    pub base_cycles_limit: Cycles,
```

**File:** rs/nns/cmc/src/main.rs (L1018-1032)
```rust
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
```

**File:** rs/nns/cmc/src/main.rs (L1140-1227)
```rust
async fn notify_top_up(
    NotifyTopUp {
        block_index,
        canister_id,
    }: NotifyTopUp,
) -> Result<Cycles, NotifyError> {
    let caller = caller();

    let src_canister_principal = SUBNET_RENTAL_CANISTER_ID.get();
    let limiter_to_use =
        if caller == src_canister_principal && canister_id.get() == src_canister_principal {
            // caller and destination needs to be src_canister_principal to get alternate limiter
            CyclesMintingLimiterSelector::SubnetRentalLimit
        } else {
            CyclesMintingLimiterSelector::BaseLimit
        };

    let (amount, from) = fetch_transaction(
        block_index,
        Subaccount::from(&canister_id),
        MEMO_TOP_UP_CANISTER,
    )
    .await?;

    // Try to set the status of this block to Processing. In order for this to
    // succeed, two conditions must hold:
    //
    //     1. It must not already have a status.
    //
    //     2. The block is "sufficiently recent". More precisely, it must be
    //        more recent than last_purged_notification. (To avoid unbounded
    //        growth of the blocks_notified.)
    let maybe_early_result = with_state_mut(|state| {
        state.purge_old_notifications(MAX_NOTIFY_HISTORY);

        if block_index <= state.last_purged_notification {
            return Some(Err(NotifyError::TransactionTooOld(
                state.last_purged_notification + 1,
            )));
        }

        match state.blocks_notified.entry(block_index) {
            Entry::Occupied(entry) => match entry.get() {
                NotificationStatus::Processing => Some(Err(NotifyError::Processing)),

                // If the user makes a duplicate request, we respond as though
                // the current request is the original one.
                NotificationStatus::NotifiedTopUp(result) => Some(result.clone()),
                NotificationStatus::NotifiedCreateCanister(_) => {
                    Some(Err(NotifyError::InvalidTransaction(
                        "The same payment is already processed as create canister request".into(),
                    )))
                }
                NotificationStatus::NotifiedMint(_) => Some(Err(NotifyError::InvalidTransaction(
                    "The same payment is already processed as mint request".into(),
                ))),
                NotificationStatus::NotMeaningfulMemo(_) => {
                    Some(Err(NotifyError::InvalidTransaction(
                        "The same payment is already processed as automatic refund".into(),
                    )))
                }
            },
            Entry::Vacant(entry) => {
                entry.insert(NotificationStatus::Processing);
                None
            }
        }
    });

    match maybe_early_result {
        Some(result) => result,
        None => {
            let result = process_top_up(canister_id, from, amount, limiter_to_use).await;

            with_state_mut(|state| {
                state.blocks_notified.insert(
                    block_index,
                    NotificationStatus::NotifiedTopUp(result.clone()),
                );
                if is_transient_error(&result) {
                    state.blocks_notified.remove(&block_index);
                }
            });

            result
        }
    }
}
```

**File:** rs/nns/cmc/src/main.rs (L1899-1923)
```rust
// If conversion fails, log and return an error
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L259-275)
```rust
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
```

**File:** rs/nns/integration_tests/src/cycles_minting_canister_with_exchange_rate_canister.rs (L162-185)
```rust
    // Step 4: Ensure that the cycles minting canister handles errors correctly
    // from the exchange rate canister by attempting to call the exchange rate canister
    // a minute later.
    reinstall_mock_exchange_rate_canister(
        &state_machine,
        EXCHANGE_RATE_CANISTER_ID,
        XrcMockInitPayload {
            response: Response::Error(ExchangeRateError::StablecoinRateTooFewRates),
        },
    );

    // Advance the time to ensure to ensure the cycles minting canister is ready
    // to call the exchange rate canister again.
    state_machine.advance_time(Duration::from_secs(FIVE_MINUTES_SECONDS));
    // Trigger the heartbeat.
    state_machine.tick();

    let response = get_icp_xdr_conversion_rate(&state_machine);
    // The rate's timestamp should be the previous timestamp.
    assert_eq!(
        response.data.timestamp_seconds,
        cmc_first_rate_timestamp_seconds + (FIVE_MINUTES_SECONDS * 2) + 10
    );
    assert_eq!(response.data.xdr_permyriad_per_icp, 200_000);
```
