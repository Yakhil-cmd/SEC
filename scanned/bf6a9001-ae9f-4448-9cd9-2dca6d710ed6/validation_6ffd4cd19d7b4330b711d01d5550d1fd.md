### Title
Stale ICP/XDR Conversion Rate Used Without Freshness Check in Cycles Minting - (`rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) stores an `icp_xdr_conversion_rate` in state that is periodically refreshed via a heartbeat calling the Exchange Rate Canister (XRC). The function `tokens_to_cycles()` reads this stored rate and uses it directly to compute how many cycles to mint from a given ICP amount, without checking whether the rate's `timestamp_seconds` is recent relative to the current time. If the XRC heartbeat fails for any period, the CMC silently continues minting cycles at an arbitrarily stale rate, causing incorrect cycles issuance for all publicly callable conversion operations.

---

### Finding Description

`tokens_to_cycles()` in `rs/nns/cmc/src/main.rs` reads `state.icp_xdr_conversion_rate` and extracts only `xdr_permyriad_per_icp`, discarding the `timestamp_seconds` field entirely:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);  // timestamp ignored
        ...
    })
}
``` [1](#0-0) 

This function is called unconditionally from three publicly accessible update paths:

- `process_top_up()` (called from `notify_top_up`) [2](#0-1) 
- `process_create_canister()` (called from `notify_create_canister`) [3](#0-2) 
- `process_mint_cycles()` (called from `notify_mint_cycles`) [4](#0-3) 

All three are publicly callable by any unprivileged ingress sender, as declared in the CMC's Candid interface: [5](#0-4) 

The rate is refreshed via a heartbeat that calls the XRC every five minutes, but only when `exchange_rate_canister_id` is set: [6](#0-5) 

The `update_exchange_rate()` path validates the incoming rate and rejects it on various error conditions (invalid sources, zero rate, etc.), but on any such failure the stored rate is simply left unchanged with no expiry enforcement: [7](#0-6) 

The `do_set_icp_xdr_conversion_rate()` function only enforces that a new rate's timestamp is strictly greater than the current one — it does not enforce that the stored rate is fresh at the time of use: [8](#0-7) 

The CMC state is initialized with a hardcoded default rate timestamped **10 May 2021**, meaning a freshly deployed CMC with no XRC configured will mint cycles at a years-old rate: [9](#0-8) [10](#0-9) 

---

### Impact Explanation

`tokens_to_cycles()` is the sole conversion function used for all ICP→cycles minting. When the stored rate is stale:

- **Over-minting (more severe):** If the ICP market price has dropped significantly below the stored rate, users receive more cycles per ICP than the current market value of that ICP warrants. This breaks the cycles conservation invariant — more cycles are created than the burned ICP is worth at current market rates. Cycles are the unit of computation cost on the IC; systematic over-minting devalues them.
- **Under-minting:** If the ICP market price has risen above the stored rate, users receive fewer cycles than they should, causing user-facing financial loss.

The `TokensToCycles::to_cycles()` computation is a direct multiplication of `xdr_permyriad_per_icp` by the ICP amount: [11](#0-10) 

A stale rate that is 2× the current market rate would cause 2× the correct number of cycles to be minted per ICP burned.

---

### Likelihood Explanation

The XRC heartbeat can fail for realistic, non-adversarial reasons: the XRC canister returns insufficient data sources (validated and rejected by `validate_exchange_rate()`), the XRC is temporarily unavailable, or the rate update is disabled due to a diverged rate (`UpdateExchangeRateState::Disabled`). The CMC's own test suite demonstrates that when the XRC returns errors or insufficient sources, the stored rate is silently preserved: [12](#0-11) 

An attacker who observes that the XRC has been failing (e.g., by monitoring the `cmc_icp_xdr_conversion_rate_timestamp_seconds` metric) and that the stored rate is significantly above the current market price can exploit this window by calling `notify_top_up` or `notify_mint_cycles` to obtain more cycles than the burned ICP is worth. No privileged access is required.

---

### Recommendation

In `tokens_to_cycles()`, compare `rate.timestamp_seconds` against the current canister time and reject (or warn and reject) if the rate is older than a defined maximum staleness threshold (e.g., 1 hour or 24 hours). The governance canister already applies an analogous check via `should_refresh_xdr_rate()` which rejects rates older than one day: [13](#0-12) 

A similar guard should be applied in `tokens_to_cycles()` before using `icp_xdr_conversion_rate` for minting. If the rate is stale, the function should return a retriable error (e.g., `NotifyError::Processing` or a new `NotifyError::StaleRate`) so callers can retry once the rate is refreshed.

---

### Proof of Concept

1. Deploy the CMC with an XRC configured. Allow the CMC to receive a valid rate (e.g., 1 ICP = 5 XDR, stored as `xdr_permyriad_per_icp = 50_000`).
2. Cause the XRC to begin returning errors (e.g., `StablecoinRateTooFewRates`) — the CMC's heartbeat will fail to update the rate, leaving `icp_xdr_conversion_rate.timestamp_seconds` frozen.
3. Wait for the ICP market price to drop (e.g., to 1 ICP = 2.5 XDR, i.e., `xdr_permyriad_per_icp` should be `25_000`).
4. Call `notify_top_up` with a valid ICP transfer. `tokens_to_cycles()` reads the stale `50_000` rate and mints cycles as if 1 ICP = 5 XDR, yielding **2× the correct number of cycles** for the burned ICP.
5. The CMC's `total_cycles_minted` counter increases by the inflated amount; the burned ICP is gone. The attacker has obtained cycles at half the current market cost.

The existing integration test at line 162–185 of `rs/nns/integration_tests/src/cycles_minting_canister_with_exchange_rate_canister.rs` already demonstrates that the stored rate is preserved unchanged when the XRC returns errors — confirming the stale-rate persistence behavior. [12](#0-11)

### Citations

**File:** rs/nns/cmc/src/main.rs (L360-363)
```rust
        let initial_icp_xdr_conversion_rate = IcpXdrConversionRate {
            timestamp_seconds: DEFAULT_ICP_XDR_CONVERSION_RATE_TIMESTAMP_SECONDS,
            xdr_permyriad_per_icp: DEFAULT_XDR_PERMYRIAD_PER_ICP_CONVERSION_RATE,
        };
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

**File:** rs/nns/cmc/src/main.rs (L2397-2402)
```rust
#[heartbeat]
async fn canister_heartbeat() {
    if with_state(|state| state.exchange_rate_canister_id.is_some()) {
        update_exchange_rate().await
    }
}
```

**File:** rs/nns/cmc/cmc.did (L241-253)
```text
  // Prompts the cycles minting canister to process a payment by converting ICP
  // into cycles and sending the cycles the specified canister.
  notify_top_up : (NotifyTopUpArg) -> (NotifyTopUpResult);

  // Creates a canister using the cycles attached to the function call.
  create_canister : (CreateCanisterArg) -> (CreateCanisterResult);

  // Prompts the cycles minting canister to process a payment for canister creation.
  notify_create_canister : (NotifyCreateCanisterArg) -> (NotifyCreateCanisterResult);

  // Mints cycles and deposits them to the cycles ledger
  notify_mint_cycles : (NotifyMintCyclesArg) -> (NotifyMintCyclesResult);

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

**File:** rs/nns/cmc/src/lib.rs (L33-34)
```rust
pub const DEFAULT_ICP_XDR_CONVERSION_RATE_TIMESTAMP_SECONDS: u64 = 1_620_633_600; // 10 May 2021 10:00:00 AM CEST
pub const DEFAULT_XDR_PERMYRIAD_PER_ICP_CONVERSION_RATE: u64 = 1_000_000; // 1 ICP = 100 XDR
```

**File:** rs/nns/cmc/src/lib.rs (L358-366)
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

**File:** rs/nns/governance/src/governance.rs (L6336-6348)
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
    }
```
