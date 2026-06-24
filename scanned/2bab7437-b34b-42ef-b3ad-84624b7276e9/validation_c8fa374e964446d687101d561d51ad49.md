### Title
Instantaneous ICP/XDR Rate Change in CMC Enables Timing Exploitation of Cycles Minting - (`rs/nns/cmc/src/main.rs`)

### Summary

The Cycles Minting Canister (CMC) applies the spot `icp_xdr_conversion_rate` at the moment `notify_top_up` / `notify_mint_cycles` is called, not at the moment the ICP was transferred. Because the ICP ledger transfer and the CMC notification are two separate transactions, any user who has already transferred ICP to the CMC subaccount holds an open "option" to convert at whatever rate is current when they choose to call `notify_top_up`. When governance executes a proposal that raises the ICP/XDR rate, that rate takes effect instantaneously with no ramping, so a user who times their notification call to land after the rate increase receives more cycles per ICP than they would have received at the pre-change rate — at no cost to themselves.

### Finding Description

`do_set_icp_xdr_conversion_rate` writes the new rate directly into `state.icp_xdr_conversion_rate` in a single atomic mutation: [1](#0-0) 

`tokens_to_cycles`, called by every minting path (`process_top_up`, `process_mint_cycles`, `process_create_canister`), reads that same spot field at call time: [2](#0-1) 

The two-step user flow is:

1. **Transfer ICP** to the CMC subaccount keyed to the target canister. This produces a ledger `block_index` that is valid for notification as long as it has not been purged (`block_index > last_purged_notification`).
2. **Delay `notify_top_up`** until a governance proposal that raises `xdr_permyriad_per_icp` has been executed, then submit the notification.

The notification is accepted because the block index is still within the notification window enforced by `MAX_NOTIFY_HISTORY`: [3](#0-2) 

The user receives cycles computed at the post-increase rate, extracting value that the protocol did not intend to grant for that ICP amount.

The governance-triggered path (`set_icp_xdr_conversion_rate`) is the most predictable vector because NNS proposals are public and their execution block is observable: [4](#0-3) 

The heartbeat-driven path (`update_exchange_rate` every 5 minutes) also produces instantaneous step changes, but those are harder to predict precisely. [5](#0-4) 

### Impact Explanation

A user who holds a pending (un-notified) ICP transfer to the CMC subaccount receives more cycles per ICP than the protocol intended for that transfer, whenever they time the notification to follow a rate increase. The excess cycles represent value extracted from the protocol's ICP burn accounting: the ICP burned is fixed by the transfer amount, but the cycles minted scale with the post-change rate. At scale (large ICP amounts, large rate jumps), the discrepancy is material. The CMC's rate-limit guard (`DEFAULT_CYCLES_LIMIT = 150e15` cycles per hour) bounds per-call exposure but does not prevent the timing exploitation itself. [6](#0-5) 

### Likelihood Explanation

NNS governance proposals are fully public. Any observer can see a pending `UpdateIcpXdrConversionRate` proposal, pre-stage an ICP transfer to the CMC subaccount, and submit `notify_top_up` immediately after the proposal executes. The IC consensus model prevents a true mempool sandwich (no reordering of messages within a round), but the two-transaction split (transfer then notify) means the user controls the timing of the second step across rounds. This is a realistic, low-skill attack requiring only a standard ICP wallet and the ability to read the NNS dashboard.

### Recommendation

Use `average_icp_xdr_conversion_rate` (the multi-day moving average already computed and stored in CMC state) instead of the spot `icp_xdr_conversion_rate` in `tokens_to_cycles`. The average is already certified and available: [7](#0-6) 

Alternatively, adopt a gradual ramp for governance-triggered rate changes (analogous to the speed limit already implemented for NNS maturity modulation): [8](#0-7) 

### Proof of Concept

```
1. Observe NNS proposal N: "Set xdr_permyriad_per_icp = X+Δ" (currently X).
2. Transfer T ICP to CMC subaccount for canister C → ledger block_index B.
3. Wait for proposal N to execute (rate becomes X+Δ).
4. Call notify_top_up({block_index: B, canister_id: C}).
   → tokens_to_cycles uses X+Δ instead of X.
   → Cycles received = T * (X+Δ) * cycles_per_xdr / 10_000
     vs. expected T * X * cycles_per_xdr / 10_000.
   → Extra cycles = T * Δ * cycles_per_xdr / 10_000.
```

For a 10 ICP transfer and a 5% rate increase (Δ/X = 0.05), at `cycles_per_xdr = 1e12` and `X = 50_000` permyriad, the extra cycles ≈ 2.5 × 10¹² cycles — well within the per-call rate limit but freely repeatable across multiple pre-staged transfers.

### Citations

**File:** rs/nns/cmc/src/main.rs (L80-87)
```rust
/// Prior to 2024-12-10, we used 50e15, but legitimate users started running
/// into this. At that time, prices had recently gone up, so we resolved to
/// increase this by 3x.
const DEFAULT_CYCLES_LIMIT: u128 = 150e15 as u128;

/// The limit for the number of cycles that can be minted by the Subnet Rental Canister in a month.
const SUBNET_RENTAL_DEFAULT_CYCLES_LIMIT: u128 = 500e15 as u128;

```

**File:** rs/nns/cmc/src/main.rs (L891-912)
```rust
#[query(hidden = true)]
fn get_average_icp_xdr_conversion_rate(_: ()) -> IcpXdrConversionRateCertifiedResponse {
    with_state(|state| {
        let witness_generator = convert_data_to_mixed_hash_tree(state);
        let average_icp_xdr_conversion_rate = state
            .average_icp_xdr_conversion_rate
            .as_ref()
            .expect("average_icp_xdr_conversion_rate is not set");

        let payload = convert_conversion_rate_to_payload(
            average_icp_xdr_conversion_rate,
            Label::from(LABEL_AVERAGE_ICP_XDR_CONVERSION_RATE),
            witness_generator,
        );

        IcpXdrConversionRateCertifiedResponse {
            data: average_icp_xdr_conversion_rate.clone(),
            hash_tree: payload,
            certificate: ic_cdk::api::data_certificate().unwrap_or_default(),
        }
    })
}
```

**File:** rs/nns/cmc/src/main.rs (L978-1005)
```rust
#[update(hidden = true)]
fn set_icp_xdr_conversion_rate(
    proposed_conversion_rate: UpdateIcpXdrConversionRatePayload,
) -> Result<(), String> {
    let caller = caller();

    assert_eq!(
        caller,
        GOVERNANCE_CANISTER_ID.into(),
        "{} is not authorized to call this method: {}",
        caller,
        "set_icp_xdr_conversion_rate"
    );

    let env = CanisterEnvironment;
    let rate = IcpXdrConversionRate::from(&proposed_conversion_rate);
    let rate_timestamp_seconds = rate.timestamp_seconds;
    let result = do_set_icp_xdr_conversion_rate(&STATE, &env, rate);
    if result.is_ok() && with_state(|state| state.exchange_rate_canister_id.is_some()) {
        exchange_rate_canister::set_update_exchange_rate_state(
            &STATE,
            &proposed_conversion_rate.reason,
            rate_timestamp_seconds,
        );
    }

    result
}
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

**File:** rs/nns/cmc/src/main.rs (L1172-1207)
```rust
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
```

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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L160-197)
```rust
    let speed_limited = match previous {
        // First calculation: no baseline to smooth from, so jump straight to target.
        None => target_modulation,
        Some((previous_permyriad, previous_day)) => {
            // Limit day-to-day change.
            let days_elapsed = current_day.saturating_sub(previous_day);
            let max_change = if days_elapsed > 1 {
                // The timer missed one or more days — allow proportionally more change.
                println!(
                    "{}compute_maturity_modulation_permyriad: {} days elapsed since last update (current_day={}, previous_day={})",
                    LOG_PREFIX, days_elapsed, current_day, previous_day
                );
                MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD.saturating_mul(days_elapsed as i64)
            } else if days_elapsed == 1 {
                MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD
            } else {
                // days_elapsed == 0: either same day or current_day < previous_day (should not happen).
                // Allow at least one day of movement.
                println!(
                    "{}compute_maturity_modulation_permyriad: days_elapsed=0 (current_day={}, previous_day={}); treating as 1 day",
                    LOG_PREFIX, current_day, previous_day
                );
                MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD
            };
            target_modulation.clamp(
                previous_permyriad.saturating_sub(max_change) as i128,
                previous_permyriad.saturating_add(max_change) as i128,
            )
        }
    };

    // Global bounds have final say. The result is within [MIN, MAX] which fit in i64, so the
    // cast is safe.
    Ok(speed_limited.clamp(
        MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i128,
        MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 as i128,
    ) as i64)
}
```
