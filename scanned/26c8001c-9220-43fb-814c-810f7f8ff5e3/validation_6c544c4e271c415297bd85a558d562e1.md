### Title
No Minimum Cycles Output Protection in CMC `notify_*` Functions - (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) converts ICP to cycles based on the current `icp_xdr_conversion_rate` at notification-processing time, without allowing callers to specify a minimum acceptable cycles output. Because the ICP/XDR rate is updated every five minutes from the Exchange Rate Canister, a user who sends ICP and then calls a `notify_*` function can receive materially fewer cycles than they observed when they initiated the transfer, with no on-chain recourse.

### Finding Description
The CMC's conversion pipeline is:

1. User transfers ICP to the CMC's ledger sub-account.
2. User calls `notify_mint_cycles` / `notify_top_up` / `notify_create_canister` with the ledger block index.
3. CMC reads the ICP amount from the confirmed ledger block, then converts it to cycles using the **current** `icp_xdr_conversion_rate` (or `average_icp_xdr_conversion_rate`) stored in `StateV2`.
4. Cycles are minted and delivered.

The state that drives the conversion is: [1](#0-0) 

The rate is updated asynchronously via `update_exchange_rate`, which calls the Exchange Rate Canister on every heartbeat tick that falls on a five-minute boundary: [2](#0-1) 

The `TokensToCycles` helper in `lib.rs` performs the conversion using whichever rate is passed to it at call time, with no floor check: [3](#0-2) 

A search across all CMC source files confirms that no field named `minimum_cycles_expected`, `min_cycles`, `expected_cycles`, or any slippage guard exists in the notify argument structures or in the processing logic. The `NotifyMintCycles`, `NotifyTopUp`, and `NotifyCreateCanister` structs carry only the block index and optional destination; they provide no way for the caller to express an acceptable output bound.

### Impact Explanation
A user who observes a rate of, say, 1 ICP = 5 T cycles, transfers 10 ICP, and then calls `notify_mint_cycles` after a rate update that drops the rate to 1 ICP = 4 T cycles will receive 40 T cycles instead of the 50 T cycles they planned for. The ICP has already been burned from the ledger; there is no refund path. For large conversions (e.g., subnet rental, large canister top-ups) the absolute loss in cycles — and therefore in ICP value — can be significant. The CMC's `ensure_balance` path mints cycles directly: [4](#0-3) 

### Likelihood Explanation
ICP is a volatile asset; its XDR price routinely moves several percent within a single five-minute window. The window of exposure is the time between the user's ICP transfer (which must be confirmed on the ICP ledger, typically one to two seconds) and the next heartbeat-driven rate update. Any user performing a large ICP-to-cycles conversion is exposed. The entry path requires only a standard ICP ledger transfer followed by a canister call — no privileged access is needed. [5](#0-4) 

### Recommendation
Add an optional `minimum_cycles_expected: Option<Cycles>` field to the `NotifyMintCycles` (and analogous) argument structs. After computing the cycles amount from the current rate, check:

```rust
if let Some(min) = args.minimum_cycles_expected {
    if computed_cycles < min {
        return Err("Conversion rate too unfavorable; aborting to protect caller.");
    }
}
```

This mirrors Uniswap V2's `amountOutMin` pattern and gives callers a deterministic slippage bound without breaking backward compatibility (the field is optional).

### Proof of Concept
1. Caller queries `get_icp_xdr_conversion_rate`; observes 1 ICP = 5 XDR → 5 T cycles.
2. Caller transfers 100 ICP to the CMC's ICP ledger sub-account (block N).
3. The Exchange Rate Canister heartbeat fires; `do_set_icp_xdr_conversion_rate` updates the stored rate to 1 ICP = 4 XDR → 4 T cycles.
4. Caller calls `notify_mint_cycles { block_index: N, ... }`.
5. CMC reads 100 ICP from block N, applies the **new** rate, and mints 400 T cycles.
6. Caller expected 500 T cycles; received 400 T cycles; 100 T cycles of value (~20 ICP equivalent) is lost with no recourse. [6](#0-5)

### Citations

**File:** rs/nns/cmc/src/main.rs (L217-230)
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

    /// How many cycles 1 XDR is worth.
    pub cycles_per_xdr: Cycles,
```

**File:** rs/nns/cmc/src/main.rs (L1008-1039)
```rust
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
```

**File:** rs/nns/cmc/src/main.rs (L2306-2324)
```rust
fn ensure_balance(
    cycles: Cycles,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<(), String> {
    let now = now_system_time();

    let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
    let cycles_to_mint = cycles - current_balance;

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;
        state.total_cycles_minted += cycles_to_mint;
        Ok::<_, String>(())
    })?;

    // unused because of check above
    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
    assert!(ic_cdk::api::canister_balance128() >= cycles.get());
    Ok(())
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L236-279)
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
```

**File:** rs/nns/cmc/src/lib.rs (L22-34)
```rust
pub const DEFAULT_CYCLES_PER_XDR: u128 = 1_000_000_000_000_u128; // 1T cycles = 1 XDR

pub const PERMYRIAD_DECIMAL_PLACES: u32 = 4;

pub const CREATE_CANISTER_REFUND_FEE: Tokens = Tokens::from_e8s(DEFAULT_TRANSFER_FEE.get_e8s() * 4);
pub const TOP_UP_CANISTER_REFUND_FEE: Tokens = Tokens::from_e8s(DEFAULT_TRANSFER_FEE.get_e8s() * 2);
pub const MINT_CYCLES_REFUND_FEE: Tokens = Tokens::from_e8s(DEFAULT_TRANSFER_FEE.get_e8s() * 2);

/// Cycles penalty charged for sending bad requests that incur a lot of work.
pub const BAD_REQUEST_CYCLES_PENALTY: u128 = 100_000_000; // TODO(SDK-1248) revisit fair pricing. Currently costs significantly more than an update call

pub const DEFAULT_ICP_XDR_CONVERSION_RATE_TIMESTAMP_SECONDS: u64 = 1_620_633_600; // 10 May 2021 10:00:00 AM CEST
pub const DEFAULT_XDR_PERMYRIAD_PER_ICP_CONVERSION_RATE: u64 = 1_000_000; // 1 ICP = 100 XDR
```

**File:** rs/nns/cmc/src/lib.rs (L548-573)
```rust
#[cfg(test)]
mod tests {
    use ic_xrc_types::{Asset, AssetClass, ExchangeRateMetadata};

    use super::*;

    #[test]
    fn tokens_to_cycles() {
        assert_eq!(
            (TokensToCycles {
                xdr_permyriad_per_icp: 10_000,
                cycles_per_xdr: Cycles::new(1234)
            })
            .to_cycles(Tokens::new(1, 0).unwrap()),
            Cycles::new(1234)
        );

        assert_eq!(
            (TokensToCycles {
                xdr_permyriad_per_icp: 21_042,
                cycles_per_xdr: 123_456_789_123_u128.into()
            })
            .to_cycles(Tokens::new(123, 0).unwrap()),
            31952666407731_u128.into()
        );
    }
```
