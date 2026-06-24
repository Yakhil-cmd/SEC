### Title
Spot ICP/XDR Rate Used for Cycle Minting Without Deviation Guard — (`rs/nns/cmc/src/main.rs`, `rs/nervous_system/clients/src/exchange_rate_canister_client.rs`)

---

### Summary

The Cycles Minting Canister (CMC) converts ICP to cycles using the **spot** `icp_xdr_conversion_rate` (refreshed every 5 minutes from the Exchange Rate Canister) rather than the 30-day moving average `average_icp_xdr_conversion_rate` that is also maintained in state. The `validate_exchange_rate` helper that gates rate acceptance checks only that enough data sources responded; it does not check the `standard_deviation` field present in `ExchangeRateMetadata`, nor does it compare the incoming rate against the stored historical average. A temporarily inflated rate — caused by natural market volatility or coordinated price movement across the CEX APIs queried by the XRC — therefore flows directly into every `notify_top_up`, `notify_mint_cycles`, and `notify_create_canister` call made during that window, minting more cycles per ICP than the long-term rate justifies.

---

### Finding Description

**Root cause 1 — spot rate used for minting:**

`tokens_to_cycles` reads `state.icp_xdr_conversion_rate` (the most-recently accepted spot rate) rather than `state.average_icp_xdr_conversion_rate`:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate          // ← spot rate, not 30-day average
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles {
                xdr_permyriad_per_icp,
                cycles_per_xdr: state.cycles_per_xdr,
            }
            .to_cycles(amount)),
``` [1](#0-0) 

The state holds both values: [2](#0-1) 

The spot rate is accepted and stored every 5 minutes via `update_exchange_rate` → `do_set_icp_xdr_conversion_rate`: [3](#0-2) 

**Root cause 2 — `validate_exchange_rate` ignores `standard_deviation`:**

The only quality gate applied before accepting a new rate checks source count only:

```rust
pub fn validate_exchange_rate(
    exchange_rate: &ExchangeRate,
) -> Result<(), ValidateExchangeRateError> {
    if exchange_rate.metadata.base_asset_num_received_rates < MINIMUM_ICP_SOURCES {
        ...
    }
    if exchange_rate.metadata.quote_asset_num_received_rates < MINIMUM_CXDR_SOURCES {
        ...
    }
    Ok(())
}
``` [4](#0-3) 

`ExchangeRateMetadata` carries a `standard_deviation` field that quantifies disagreement among the queried sources, but it is never inspected. There is also no comparison of the incoming rate against `average_icp_xdr_conversion_rate` to detect anomalous deviations.

**Root cause 3 — no staleness guard at minting time:**

`tokens_to_cycles` does not check the `timestamp_seconds` of `icp_xdr_conversion_rate` against the current time. If the XRC fails to update for an extended period, a stale (potentially outdated) rate continues to be used for all minting operations.

**Conversion formula:**

```rust
impl TokensToCycles {
    pub fn to_cycles(&self, icpts: Tokens) -> Cycles {
        Cycles::new(
            icpts.get_e8s() as u128
                * self.xdr_permyriad_per_icp as u128   // ← directly from spot rate
                * self.cycles_per_xdr.get()
                / (icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000),
        )
    }
}
``` [5](#0-4) 

All three public minting entry points call `tokens_to_cycles` with the spot rate: [6](#0-5) [7](#0-6) [8](#0-7) 

---

### Impact Explanation

Cycles are the unit of computation on the IC. They are minted by burning ICP at the current `icp_xdr_conversion_rate`. If that rate is temporarily inflated — even by a few percent — every ICP burned during the window yields proportionally more cycles than the long-term rate justifies. This:

1. **Over-mints cycles**, diluting the purchasing power of cycles already held by other canisters and users.
2. **Allows an attacker to acquire computation at a discount**, effectively extracting value from the protocol.
3. **Bypasses the intent of the 30-day average**, which exists precisely to smooth out short-term price volatility.

The hourly rate limiter (`base_cycles_limit`, 150 P cycles/hour) bounds the per-hour damage but does not prevent the per-unit over-minting. At a 10 % rate spike and ~10 XDR/ICP, the attacker extracts roughly 1,500 ICP-equivalent of excess cycles per hour.

---

### Likelihood Explanation

The XRC aggregates rates from multiple CEX APIs via HTTPS outcalls and uses median aggregation, making single-exchange manipulation insufficient. However:

- **Natural market volatility** (flash spikes during high-volume events) regularly causes short-lived deviations between the spot rate and the 30-day average. No attacker action is required; any user who times a large `notify_top_up` call during such a spike benefits.
- **Coordinated price movement** across the ≥4 exchanges the XRC queries is expensive but feasible for a well-capitalised actor, especially given the cycles rate limiter caps the maximum extractable value per hour.
- The `standard_deviation` field in `ExchangeRateMetadata` is never checked, so a rate returned with high inter-source disagreement (a signal of anomalous conditions) is accepted without any additional scrutiny.
- The 5-minute refresh window means the inflated rate persists for up to 5 minutes, giving ample time for a prepared attacker to submit minting transactions.

---

### Recommendation

1. **Use the 30-day moving average for cycle minting.** Replace `state.icp_xdr_conversion_rate` with `state.average_icp_xdr_conversion_rate` in `tokens_to_cycles`. The average is already computed and stored; using it directly mirrors the recommendation in the external report to avoid relying on a manipulable spot price.

2. **Add a `standard_deviation` guard in `validate_exchange_rate`.** Reject (or flag) rates whose `standard_deviation` exceeds a configurable fraction of the rate value (e.g., 5 %), indicating high inter-source disagreement.

3. **Add a deviation check against the stored average.** Before accepting a new rate in `do_set_icp_xdr_conversion_rate`, compare it against `average_icp_xdr_conversion_rate` and reject rates that deviate by more than a threshold (e.g., 20 %). The existing `DivergedRate` / `Disabled` mechanism already handles the governance-proposal path; the same logic should apply to the automatic XRC path.

4. **Add a staleness guard in `tokens_to_cycles`.** Reject minting if `icp_xdr_conversion_rate.timestamp_seconds` is older than a configurable threshold (e.g., 30 minutes) relative to the current time.

---

### Proof of Concept

1. Observe that `state.icp_xdr_conversion_rate` is updated every 5 minutes from the XRC via `update_exchange_rate` in `rs/nns/cmc/src/exchange_rate_canister.rs`.
2. Observe that `validate_exchange_rate` accepts any rate with ≥4 ICP sources and ≥4 CXDR sources, regardless of `standard_deviation`.
3. Suppose the XRC returns a rate of `xdr_permyriad_per_icp = 150_000` (15 XDR/ICP) during a market spike, while the 30-day average is `100_000` (10 XDR/ICP).
4. An attacker sends 1,000 ICP to the CMC subaccount and calls `notify_top_up`.
5. `tokens_to_cycles` computes: `1_000 * 1e8 * 150_000 * 1e12 / (1e8 * 10_000)` = **150 T cycles** instead of the fair **100 T cycles**.
6. The attacker receives 50 % more cycles than the long-term rate justifies, at no additional cost.
7. The rate limiter (150 P/hour) would allow this to continue for the full 5-minute window before the rate is refreshed, yielding up to `150P * (5/60)` = 12.5 P excess cycles in a single window. [1](#0-0) [4](#0-3) [9](#0-8)

### Citations

**File:** rs/nns/cmc/src/main.rs (L217-227)
```rust
    /// How many XDR 1 ICP is worth, along with a timestamp.
    pub icp_xdr_conversion_rate: Option<IcpXdrConversionRate>,

    /// The average ICP/XDR rate over `NUM_DAYS_FOR_ICP_XDR_AVERAGE` days. The
    /// timestamp is the UNIX epoch time in seconds at the start of the last
    /// considered day, which should correspond to midnight of the current
    /// day.
    pub average_icp_xdr_conversion_rate: Option<IcpXdrConversionRate>,

    /// The recent ICP/XDR rates used to compute the average rate.
    pub recent_icp_xdr_rates: Option<Vec<IcpXdrConversionRate>>,
```

**File:** rs/nns/cmc/src/main.rs (L1009-1040)
```rust
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

**File:** rs/nns/cmc/src/main.rs (L1925-1956)
```rust
async fn process_create_canister(
    controller: PrincipalId,
    from: AccountIdentifier,
    amount: Tokens,
    subnet_selection: Option<SubnetSelection>,
    settings: Option<CanisterSettings>,
) -> Result<CanisterId, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;

    let sub = Subaccount::from(&controller);

    print(format!(
        "Creating canister with controller {controller} with {cycles} cycles.",
    ));

    // Create the canister. If this fails, refund. Either way,
    // return a result so that the notification cannot be retried.
    // If refund fails, we allow to retry.
    match do_create_canister(controller, cycles, subnet_selection, settings).await {
        Ok(canister_id) => {
            burn_and_log(sub, amount).await;
            Ok(canister_id)
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, CREATE_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err,
                block_index: refund_block,
            })
        }
    }
}
```

**File:** rs/nns/cmc/src/main.rs (L1958-1983)
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
        Ok(deposit_result) => {
            burn_and_log(sub, amount).await;
            Ok(NotifyMintCyclesSuccess {
                block_index: deposit_result.block_index,
                minted: cycles.into(),
                balance: deposit_result.balance,
            })
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, MINT_CYCLES_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err,
                block_index: refund_block,
            })
        }
    }
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

**File:** rs/nervous_system/clients/src/exchange_rate_canister_client.rs (L111-129)
```rust
pub fn validate_exchange_rate(
    exchange_rate: &ExchangeRate,
) -> Result<(), ValidateExchangeRateError> {
    if exchange_rate.metadata.base_asset_num_received_rates < MINIMUM_ICP_SOURCES {
        return Err(ValidateExchangeRateError::NotEnoughIcpSources {
            received: exchange_rate.metadata.base_asset_num_received_rates,
            queried: exchange_rate.metadata.base_asset_num_queried_sources,
        });
    }

    if exchange_rate.metadata.quote_asset_num_received_rates < MINIMUM_CXDR_SOURCES {
        return Err(ValidateExchangeRateError::NotEnoughCxdrSources {
            received: exchange_rate.metadata.quote_asset_num_received_rates,
            queried: exchange_rate.metadata.quote_asset_num_queried_sources,
        });
    }

    Ok(())
}
```

**File:** rs/nns/cmc/src/lib.rs (L358-367)
```rust
impl TokensToCycles {
    pub fn to_cycles(&self, icpts: Tokens) -> Cycles {
        Cycles::new(
            icpts.get_e8s() as u128
                * self.xdr_permyriad_per_icp as u128
                * self.cycles_per_xdr.get()
                / (icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000),
        )
    }
}
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L259-268)
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
```
