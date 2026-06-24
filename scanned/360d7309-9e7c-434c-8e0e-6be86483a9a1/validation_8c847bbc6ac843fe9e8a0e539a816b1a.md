### Title
No Minimum Cycles Slippage Protection in CMC Notify Functions - (File: `rs/nns/cmc/src/main.rs`)

### Summary
The Cycles Minting Canister (CMC) converts ICP to cycles at whatever `icp_xdr_conversion_rate` is stored in state at the moment a notify call is processed. None of the three public notify endpoints (`notify_top_up`, `notify_mint_cycles`, `notify_create_canister`) accept a caller-supplied minimum cycles parameter. A user who queries the rate, sends ICP, and then calls a notify function has no on-chain protection against receiving far fewer cycles than expected if the rate changed between those two steps.

### Finding Description

The ICP→cycles conversion is performed by `tokens_to_cycles`, which reads the live `icp_xdr_conversion_rate` from canister state at call time: [1](#0-0) 

This function is called unconditionally inside every notify path:

- `process_top_up` → `tokens_to_cycles(amount)?` [2](#0-1) 
- `process_mint_cycles` → `tokens_to_cycles(amount)?` [3](#0-2) 
- `process_create_canister` → `tokens_to_cycles(amount)?` [4](#0-3) 

The argument structs for all three endpoints contain no `min_cycles` or `expected_rate` field:

- `NotifyTopUp { block_index, canister_id }` [5](#0-4) 
- `NotifyMintCyclesArg { block_index, to_subaccount, deposit_memo }` [6](#0-5) 

The rate is set by the NNS governance canister via `set_icp_xdr_conversion_rate` and is also updated automatically by a recurring timer task that fetches from the Exchange Rate Canister (XRC): [7](#0-6) [8](#0-7) 

The two-step protocol is:
1. User sends ICP to a CMC subaccount on the ICP ledger.
2. User calls a notify endpoint referencing the ledger block.

Between steps 1 and 2 the rate can change. Once step 2 succeeds, the ICP is burned and cycles are deposited; there is no rollback path for a rate the user did not accept.

### Impact Explanation

A user who observed rate R, sent N ICP, and then called `notify_top_up` after the rate dropped to R' < R receives `N * R'` cycles instead of the expected `N * R` cycles. The ICP is permanently burned at the lower rate with no refund. For large top-ups (e.g., subnet rental, which has its own elevated limit path) the cycle shortfall can be substantial. The user has no on-chain mechanism to express "refund me if I would receive fewer than X cycles."

### Likelihood Explanation

The ICP/XDR rate is updated automatically every day via the XRC timer task in governance, and can also be changed by an NNS governance proposal at any time. Rate movements of 10–30% within a single day are historically common for ICP. Because the two-step notify protocol requires a ledger transfer followed by a separate canister call, there is always a window—potentially minutes to hours if the user's tooling retries or the user delays—during which the rate can shift. No privileged attacker is required; ordinary market movement is sufficient to trigger the discrepancy.

### Recommendation

Add an optional `min_cycles: Option<Nat>` field to `NotifyTopUpArg`, `NotifyMintCyclesArg`, and `NotifyCreateCanisterArg`. Inside each `process_*` function, after calling `tokens_to_cycles`, compare the result against `min_cycles`; if the computed cycles fall below the caller's stated minimum, refund the ICP (using the existing `refund_icp` helper) and return a descriptive `NotifyError::Refunded`. This mirrors the recommended fix in the original report: let the caller assert the price they observed before committing the conversion.

### Proof of Concept

1. User calls `get_icp_xdr_conversion_rate()` (query) and observes rate = 100 XDR/ICP → 100 T cycles/ICP.
2. User transfers 10 ICP to the CMC subaccount for their canister.
3. The XRC timer task fires and the stored rate drops to 70 XDR/ICP.
4. User calls `notify_top_up { block_index, canister_id }`.
5. `process_top_up` calls `tokens_to_cycles(10 ICP)` which reads the new rate and returns 700 T cycles.
6. `deposit_cycles` succeeds; `burn_and_log` burns the 10 ICP.
7. User receives 700 T cycles instead of the expected 1000 T cycles, with no recourse.

The relevant code path is:

```
notify_top_up (main.rs:1140)
  └─ process_top_up (main.rs:1985)
       └─ tokens_to_cycles (main.rs:1900)   ← reads live rate, no min check
            └─ deposit_cycles + burn_and_log ← irreversible
``` [9](#0-8)

### Citations

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

**File:** rs/nns/cmc/src/main.rs (L1140-1145)
```rust
async fn notify_top_up(
    NotifyTopUp {
        block_index,
        canister_id,
    }: NotifyTopUp,
) -> Result<Cycles, NotifyError> {
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

**File:** rs/nns/cmc/src/lib.rs (L258-263)
```rust
#[derive(Clone, Eq, PartialEq, Hash, Debug, CandidType, Deserialize, Serialize)]
pub struct NotifyMintCyclesArg {
    pub block_index: BlockIndex,
    pub to_subaccount: Option<icrc_ledger_types::icrc1::account::Subaccount>,
    pub deposit_memo: Option<Vec<u8>>,
}
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L263-309)
```rust
    /// Fetches the ICP/XDR rate from XRC for `timestamp`, validates, and converts.
    /// Returns `None` if any step fails (errors are logged).
    async fn fetch_and_validate_rate(&self, timestamp: u64) -> Option<SampledPrice> {
        let exchange_rate = match self
            .xrc_client
            .get_icp_to_xdr_exchange_rate(Some(timestamp))
            .await
        {
            Ok(rate) => rate,
            Err(err) => {
                println!(
                    "{}UpdateIcpXdrRateRelatedData: XRC call failed: {}",
                    LOG_PREFIX, err
                );
                return None;
            }
        };

        if let Err(err) = validate_exchange_rate(&exchange_rate) {
            println!(
                "{}UpdateIcpXdrRateRelatedData: XRC rate failed validation: {}",
                LOG_PREFIX, err
            );
            return None;
        }

        // Verify that XRC returned a rate for the day we requested. If not, the rate
        // won't fill the expected slot and backfill would loop on the same day.
        if exchange_rate.timestamp != timestamp {
            println!(
                "{}UpdateIcpXdrRateRelatedData: requested timestamp {} but XRC returned {}; ignoring.",
                LOG_PREFIX, timestamp, exchange_rate.timestamp
            );
            return None;
        }

        let rate = SampledPrice::from(&exchange_rate);
        if rate.xdr_permyriad_per_icp == 0 {
            println!(
                "{}UpdateIcpXdrRateRelatedData: received zero XDR/ICP rate; ignoring.",
                LOG_PREFIX
            );
            return None;
        }

        Some(rate)
    }
```
