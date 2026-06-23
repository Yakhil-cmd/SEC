### Title
Stale Spot ICP/XDR Rate Used for Cycle Minting Enables Arbitrage Without Standard-Deviation Guard - (File: rs/nns/cmc/src/main.rs)

### Summary

The Cycles Minting Canister (CMC) converts ICP to cycles using the most recently received **spot** `icp_xdr_conversion_rate`, which is refreshed at most every 5 minutes from the Exchange Rate Canister (XRC). The rate-acceptance logic (`validate_exchange_rate`) only checks that enough sources responded; it never inspects the `standard_deviation` field of the returned `ExchangeRateMetadata`. During periods of ICP price volatility, an unprivileged user can observe a favorable discrepancy between the stale on-chain rate and the current market price and call `notify_top_up` / `notify_mint_cycles` to obtain more cycles than the current market rate justifies, extracting value from the protocol.

### Finding Description

**Rate used for minting is the spot rate, not the average.**

`tokens_to_cycles` in `rs/nns/cmc/src/main.rs` reads `state.icp_xdr_conversion_rate` — the most recently accepted spot rate — and passes it directly to `TokensToCycles::to_cycles`:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate   // ← spot rate, up to 5 min stale
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        ...
        Ok(TokensToCycles { xdr_permyriad_per_icp, cycles_per_xdr: state.cycles_per_xdr }
            .to_cycles(amount))
    })
}
```

The 30-day `average_icp_xdr_conversion_rate` is maintained in state and used for node-provider rewards and maturity modulation, but **never** for cycle minting.

**Rate acceptance does not check standard deviation.**

`validate_exchange_rate` in `rs/nervous_system/clients/src/exchange_rate_canister_client.rs` only verifies that at least `MINIMUM_ICP_SOURCES = 4` and `MINIMUM_CXDR_SOURCES = 4` sources responded:

```rust
pub fn validate_exchange_rate(exchange_rate: &ExchangeRate)
    -> Result<(), ValidateExchangeRateError>
{
    if exchange_rate.metadata.base_asset_num_received_rates < MINIMUM_ICP_SOURCES { ... }
    if exchange_rate.metadata.quote_asset_num_received_rates < MINIMUM_CXDR_SOURCES { ... }
    Ok(())
}
```

The `standard_deviation` field of `ExchangeRateMetadata` — which quantifies how much the individual exchange quotes diverge — is never read. A rate returned with a high standard deviation (sources disagree by, say, 2–3%) is accepted and immediately written to `state.icp_xdr_conversion_rate` and used for all subsequent minting until the next 5-minute heartbeat.

**Rate staleness is only checked for monotonicity, not age.**

`do_set_icp_xdr_conversion_rate` rejects a new rate only if its timestamp is not strictly greater than the current one. There is no upper-bound check: if the XRC fails to update for an extended period, the CMC silently continues using an arbitrarily old rate.

**Conversion formula amplifies the discrepancy.**

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

With `DEFAULT_CYCLES_PER_XDR = 1_000_000_000_000` (1 T cycles per XDR), a 2% stale-rate premium on a 100 ICP deposit yields ~2 T extra cycles — real economic value extracted from the protocol.

### Impact Explanation

An unprivileged user who observes that the on-chain `xdr_permyriad_per_icp` is higher than the current market rate (ICP has fallen but the CMC has not yet received the updated rate) can:

1. Buy ICP at the lower market price.
2. Transfer ICP to the CMC subaccount and call `notify_top_up` or `notify_mint_cycles`.
3. Receive cycles computed at the stale, higher rate.

The cycles minted exceed the ICP's current market value. The ICP is burned, so the protocol permanently loses the difference. At scale (the hourly minting cap is 150 T cycles ≈ 150 ICP-equivalent), a sustained 2% discrepancy represents ~3 ICP of value extracted per hour. During high-volatility periods or XRC outages the discrepancy can be larger and persist longer.

### Likelihood Explanation

The 5-minute XRC polling interval (`REFRESH_RATE_INTERVAL_SECONDS = 5 * ONE_MINUTE_SECONDS`) means a fresh arbitrage window opens with every heartbeat during volatile markets. ICP regularly moves 1–3% in 5-minute windows during active trading sessions. The attack requires no privileged access: any principal can transfer ICP to the CMC and call `notify_top_up`. The on-chain rate is publicly readable via `get_icp_xdr_conversion_rate`, and market prices are publicly available, so the discrepancy is trivially observable.

### Recommendation

1. **Add a standard-deviation guard in `validate_exchange_rate`**: reject rates whose `standard_deviation` exceeds a configurable permyriad threshold (e.g., 200 = 2%). This mirrors the deviation-threshold concept in Chainlink feeds and prevents high-uncertainty rates from being used for minting.

2. **Add a rate-age check in `do_set_icp_xdr_conversion_rate` / `tokens_to_cycles`**: if `now - icp_xdr_conversion_rate.timestamp_seconds > MAX_RATE_AGE_SECONDS` (e.g., 15 minutes), refuse to mint and return an error rather than silently using a stale rate.

3. **Consider using a short-window moving average** (e.g., the median of the last N accepted rates) instead of the raw spot rate for minting, analogous to how the 30-day average is already computed and stored.

### Proof of Concept

```
1. Read on-chain rate:
   dfx canister call rkp4c-7iaaa-aaaaa-aaaca-cai get_icp_xdr_conversion_rate
   → xdr_permyriad_per_icp = 35_400  (e.g., 3.54 XDR/ICP)

2. Observe market rate has dropped to 3.47 XDR/ICP (−2% in last 4 minutes).

3. Buy 100 ICP at market price (cost: 100 * 3.47 XDR = 347 XDR).

4. Transfer 100 ICP to CMC subaccount for target canister, call notify_top_up.
   CMC computes: 100 * 35_400 * 1_000_000_000_000 / (100_000_000 * 10_000)
               = 354_000_000_000_000 cycles  (354 T cycles)

5. At the current market rate the same 347 XDR buys only 347 T cycles.
   Profit: 7 T cycles ≈ 2% of the deposit, extracted from the protocol.

6. Repeat every 5 minutes during volatile sessions.
```

The attacker-controlled entry path is `notify_top_up` / `notify_mint_cycles` — both are open to any unprivileged ingress sender.

---

**Key file references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1009-1039)
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

**File:** rs/nervous_system/clients/src/exchange_rate_canister_client.rs (L110-129)
```rust
/// Validates that an ICP/CXDR exchange rate has enough sources.
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

**File:** rs/nns/cmc/src/lib.rs (L351-367)
```rust
pub struct TokensToCycles {
    /// Number of 1/10,000ths of XDR that 1 ICP is worth.
    pub xdr_permyriad_per_icp: u64,
    /// Number of cycles that 1 XDR is worth.
    pub cycles_per_xdr: Cycles,
}

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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```
