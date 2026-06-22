### Title
Hardcoded Default ICP/XDR Conversion Rate Allows Over-Minting of Cycles at CMC Initialization - (File: rs/nns/cmc/src/lib.rs, rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) initializes its ICP/XDR conversion rate with a hardcoded default of `1_000_000` permyriad (= 100 XDR per ICP), a value that is far above the actual market rate. Any user who calls `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` before governance submits the first real rate update will receive cycles computed at this inflated rate, minting far more cycles per ICP than they should.

### Finding Description
In `rs/nns/cmc/src/lib.rs`, two constants define the hardcoded bootstrap rate:

```rust
pub const DEFAULT_ICP_XDR_CONVERSION_RATE_TIMESTAMP_SECONDS: u64 = 1_620_633_600; // 10 May 2021
pub const DEFAULT_XDR_PERMYRIAD_PER_ICP_CONVERSION_RATE: u64 = 1_000_000; // 1 ICP = 100 XDR
``` [1](#0-0) 

`State::default()` in `rs/nns/cmc/src/main.rs` uses these constants to populate both `icp_xdr_conversion_rate` and `average_icp_xdr_conversion_rate`:

```rust
let initial_icp_xdr_conversion_rate = IcpXdrConversionRate {
    timestamp_seconds: DEFAULT_ICP_XDR_CONVERSION_RATE_TIMESTAMP_SECONDS,
    xdr_permyriad_per_icp: DEFAULT_XDR_PERMYRIAD_PER_ICP_CONVERSION_RATE,
};
``` [2](#0-1) 

The `init()` function calls `State::default()` unconditionally, setting this hardcoded rate as the live rate: [3](#0-2) 

The `tokens_to_cycles()` function, called by all three public minting endpoints, reads directly from `state.icp_xdr_conversion_rate`:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        ...
        Ok(TokensToCycles { xdr_permyriad_per_icp, cycles_per_xdr: state.cycles_per_xdr }
            .to_cycles(amount))
    })
}
``` [4](#0-3) 

The `TokensToCycles::to_cycles` formula is:
```
cycles = icpts.get_e8s() * xdr_permyriad_per_icp * cycles_per_xdr / (TOKEN_SUBDIVIDABLE_BY * 10_000)
``` [5](#0-4) 

The three minting endpoints that call `tokens_to_cycles` are `process_create_canister`, `process_top_up`, and `process_mint_cycles`: [6](#0-5) [7](#0-6) [8](#0-7) 

The rate can only be updated by governance via `set_icp_xdr_conversion_rate`, which requires the caller to be `GOVERNANCE_CANISTER_ID`: [9](#0-8) 

### Impact Explanation
The actual ICP/XDR rate in recent years has been approximately 3–5 XDR per ICP (30,000–50,000 permyriad), while the hardcoded default is 100 XDR per ICP (1,000,000 permyriad) — a factor of **20–33x higher**. During the window between CMC initialization and the first governance rate update, any unprivileged user who sends ICP to the CMC and calls `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` will receive 20–33x more cycles than they should. This is a **cycles/resource accounting bug**: cycles are minted against a fabricated rate, inflating the total supply and allowing the attacker to acquire compute resources at a fraction of their true cost.

### Likelihood Explanation
Medium. The CMC is a system canister that is initialized fresh on new subnet deployments or protocol-level reinstalls. The governance proposal to update the rate is a separate, subsequent action. The window between `init()` and the first `set_icp_xdr_conversion_rate` call is real and observable on-chain. Any user monitoring the chain can detect a fresh CMC initialization and race to mint cycles before the rate is corrected. The `do_set_icp_xdr_conversion_rate` guard only requires the new timestamp to exceed `1_620_633_600` (May 2021), so it does not prevent exploitation during the initialization window: [10](#0-9) 

### Recommendation
The `CyclesCanisterInitPayload` should include an optional `initial_icp_xdr_conversion_rate` field. If provided, `init()` should use it instead of the hardcoded default. Governance should always supply the current market rate as part of the CMC installation proposal. Alternatively, the CMC should refuse to process any minting calls until at least one governance-supplied rate has been set (i.e., treat `None` as the initial state and return an error rather than falling back to the hardcoded constant). [11](#0-10) 

### Proof of Concept
1. A new CMC is installed (e.g., on a new NNS subnet). `init()` calls `State::default()`, setting `icp_xdr_conversion_rate = { timestamp_seconds: 1_620_633_600, xdr_permyriad_per_icp: 1_000_000 }`.
2. Before governance submits a `set_icp_xdr_conversion_rate` proposal with the real rate, an attacker sends 1 ICP to the CMC's top-up subaccount for their canister.
3. The attacker calls `notify_top_up`. `tokens_to_cycles(1 ICP)` computes: `1e8 * 1_000_000 * 1e12 / (1e8 * 10_000) = 1e12 * 100 = 100T cycles`.
4. At the real rate of ~40,000 permyriad (4 XDR/ICP), the correct amount would be: `1e8 * 40_000 * 1e12 / (1e8 * 10_000) = 4T cycles`.
5. The attacker receives **~25x more cycles** than they should, at the expense of the protocol's cycle economy.

### Citations

**File:** rs/nns/cmc/src/lib.rs (L33-34)
```rust
pub const DEFAULT_ICP_XDR_CONVERSION_RATE_TIMESTAMP_SECONDS: u64 = 1_620_633_600; // 10 May 2021 10:00:00 AM CEST
pub const DEFAULT_XDR_PERMYRIAD_PER_ICP_CONVERSION_RATE: u64 = 1_000_000; // 1 ICP = 100 XDR
```

**File:** rs/nns/cmc/src/lib.rs (L116-123)
```rust
pub struct CyclesCanisterInitPayload {
    pub ledger_canister_id: Option<CanisterId>,
    pub governance_canister_id: Option<CanisterId>,
    pub minting_account_id: Option<AccountIdentifier>,
    pub last_purged_notification: Option<BlockIndex>,
    pub exchange_rate_canister: Option<ExchangeRateCanister>,
    pub cycles_ledger_canister_id: Option<CanisterId>,
}
```

**File:** rs/nns/cmc/src/lib.rs (L359-366)
```rust
    pub fn to_cycles(&self, icpts: Tokens) -> Cycles {
        Cycles::new(
            icpts.get_e8s() as u128
                * self.xdr_permyriad_per_icp as u128
                * self.cycles_per_xdr.get()
                / (icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000),
        )
    }
```

**File:** rs/nns/cmc/src/main.rs (L360-363)
```rust
        let initial_icp_xdr_conversion_rate = IcpXdrConversionRate {
            timestamp_seconds: DEFAULT_ICP_XDR_CONVERSION_RATE_TIMESTAMP_SECONDS,
            xdr_permyriad_per_icp: DEFAULT_XDR_PERMYRIAD_PER_ICP_CONVERSION_RATE,
        };
```

**File:** rs/nns/cmc/src/main.rs (L466-466)
```rust
    STATE.with(|state| state.replace(Some(State::default())));
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

**File:** rs/nns/cmc/src/main.rs (L1932-1932)
```rust
    let cycles = tokens_to_cycles(amount)?;
```

**File:** rs/nns/cmc/src/main.rs (L1965-1965)
```rust
    let cycles = tokens_to_cycles(amount)?;
```

**File:** rs/nns/cmc/src/main.rs (L1991-1991)
```rust
    let cycles = tokens_to_cycles(amount)?;
```
