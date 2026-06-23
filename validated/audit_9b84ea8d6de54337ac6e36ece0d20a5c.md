### Title
Cycles Minting Canister Uses Spot ICP/XDR Rate Instead of Multi-Day Average for Cycles Minting — (`File: rs/nns/cmc/src/main.rs`)

### Summary
The Cycles Minting Canister (CMC) computes and stores a multi-day average ICP/XDR rate (`average_icp_xdr_conversion_rate`) but uses only the latest spot rate (`icp_xdr_conversion_rate`) when converting ICP to cycles in `tokens_to_cycles()`. An attacker who can temporarily move the ICP market price upward can cause the CMC to mint more cycles per ICP than the long-term average warrants, extracting excess cycles from the protocol.

### Finding Description
The CMC maintains two distinct rate fields in its state:

- `icp_xdr_conversion_rate` — the latest spot rate, refreshed every ~5 minutes from the Exchange Rate Canister (XRC) via heartbeat.
- `average_icp_xdr_conversion_rate` — a multi-day simple moving average computed over `NUM_DAYS_FOR_ICP_XDR_AVERAGE` days, already stored in state. [1](#0-0) 

The function `tokens_to_cycles()`, which is the sole conversion path for all three public minting endpoints (`notify_top_up`, `notify_create_canister`, `notify_mint_cycles`), reads exclusively from the spot field:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate   // <-- spot rate, NOT average
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        ...
    })
}
``` [2](#0-1) 

All three minting paths call `tokens_to_cycles()` with the spot rate: [3](#0-2) [4](#0-3) [5](#0-4) 

The spot rate is updated every 5 minutes from the XRC via the canister heartbeat: [6](#0-5) [7](#0-6) 

The average rate is computed and stored but only used for maturity modulation and external queries — never for cycles minting: [8](#0-7) 

### Impact Explanation
This is a **cycles/resource accounting bug**. Cycles are the IC's computational fuel and are minted by burning ICP at the current spot rate. If the spot rate is temporarily inflated (ICP price spike), an attacker can call `notify_top_up` or `notify_mint_cycles` during that window and receive more cycles per ICP than the protocol's long-term average would grant. This represents a conservation violation: the protocol mints more cycles than the burned ICP's long-term value justifies, diluting the cycles supply relative to the ICP burned. The multi-day average already exists in state and is the correct rate to use for this purpose.

### Likelihood Explanation
**Medium-Low.** The XRC already aggregates prices from multiple exchange sources and uses a median, providing partial protection. However, the CMC's 5-minute update cadence means a sustained price spike of even 5–10 minutes is sufficient to exploit the window. The base cycles minting rate limiter (`base_cycles_limit`, ~150P cycles/hour) bounds the per-hour damage but does not eliminate it. An attacker with sufficient capital to move ICP price on multiple exchanges simultaneously — a realistic scenario for a well-funded actor — can exploit this without any privileged access. The entry path requires only a standard ICP transfer followed by a public `notify_top_up` call. [9](#0-8) 

### Recommendation
Replace the spot rate read in `tokens_to_cycles()` with `average_icp_xdr_conversion_rate`, which is already computed and stored in CMC state. This directly mirrors the TWAP recommendation in the external report and eliminates the short-window manipulation surface. The average rate is already certified and exposed via `get_average_icp_xdr_conversion_rate`. [10](#0-9) 

### Proof of Concept
1. Attacker monitors the CMC heartbeat cadence (~5 min).
2. Attacker executes large buy orders for ICP on multiple exchanges, temporarily spiking the ICP/XDR price (e.g., from 5 XDR to 8 XDR).
3. Within the next heartbeat cycle, the XRC picks up the elevated price and the CMC updates `icp_xdr_conversion_rate` to the inflated value via `do_set_icp_xdr_conversion_rate`. [11](#0-10) 

4. Attacker calls `notify_top_up` (a public ingress endpoint) referencing a pre-staged ICP transfer. `tokens_to_cycles()` reads the inflated spot rate and mints ~60% more cycles than the long-term average rate would produce.
5. Attacker unwinds the ICP position. The CMC's next heartbeat corrects the spot rate, but the excess cycles have already been minted and deposited.

The `average_icp_xdr_conversion_rate` (computed over `NUM_DAYS_FOR_ICP_XDR_AVERAGE` days) would have been unaffected by the short spike and would have produced the correct, manipulation-resistant cycle count. [12](#0-11)

### Citations

**File:** rs/nns/cmc/src/main.rs (L218-227)
```rust
    pub icp_xdr_conversion_rate: Option<IcpXdrConversionRate>,

    /// The average ICP/XDR rate over `NUM_DAYS_FOR_ICP_XDR_AVERAGE` days. The
    /// timestamp is the UNIX epoch time in seconds at the start of the last
    /// considered day, which should correspond to midnight of the current
    /// day.
    pub average_icp_xdr_conversion_rate: Option<IcpXdrConversionRate>,

    /// The recent ICP/XDR rates used to compute the average rate.
    pub recent_icp_xdr_rates: Option<Vec<IcpXdrConversionRate>>,
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

**File:** rs/nns/cmc/src/main.rs (L936-941)
```rust
        // Update the average ICP/XDR rate and the maturity modulation.
        let time = now_seconds();
        state.average_icp_xdr_conversion_rate =
            compute_average_icp_xdr_rate_at_time(recent_rates, time);
        state.maturity_modulation_permyriad = Some(compute_maturity_modulation(recent_rates, time));
    }
```

**File:** rs/nns/cmc/src/main.rs (L949-976)
```rust
fn compute_average_icp_xdr_rate_at_time(
    recent_rates: &[IcpXdrConversionRate],
    time_s: u64,
) -> Option<IcpXdrConversionRate> {
    let day = time_s / 86_400;
    // Filter the rates based on valid days, i.e., days not before day
    // `day - NUM_ICP_XDR_RATES_FOR_AVERAGE` and not later than the given day.
    let filtered_rates: Vec<u64> = recent_rates
        .iter()
        .filter(|rate| {
            (rate.timestamp_seconds / 86_400) > day - (NUM_DAYS_FOR_ICP_XDR_AVERAGE as u64)
                && (rate.timestamp_seconds / 86_400) <= day
        })
        .map(|rate| rate.xdr_permyriad_per_icp)
        .collect();
    let size = filtered_rates.len() as u64;
    // If there are rates that meet the age requirement, compute the sum and compute
    // the average.
    if size > 0 {
        let sum: u64 = filtered_rates.into_iter().sum();
        Some(IcpXdrConversionRate {
            timestamp_seconds: day * 86_400,   // Start of the current day.
            xdr_permyriad_per_icp: sum / size, // The average of the valid data points.
        })
    } else {
        None
    }
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

**File:** rs/nns/cmc/src/main.rs (L1140-1162)
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L236-246)
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
```
