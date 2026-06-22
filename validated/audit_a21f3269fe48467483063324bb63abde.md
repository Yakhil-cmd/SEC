### Title
Missing Minimum Cycles Output Protection in CMC `notify_top_up` and `notify_mint_cycles` - (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) converts ICP to cycles using a dynamically read `icp_xdr_conversion_rate` at execution time. Neither `notify_top_up` nor `notify_mint_cycles` accepts a caller-specified minimum cycles output (`min_cycles_out`). If the ICP/XDR rate drops between the user's ICP ledger transfer and the notification call, the user's ICP is burned and they receive fewer cycles than anticipated, with no ability to revert.

### Finding Description
The ICP-to-cycles conversion flow is a two-step process:

1. The user sends ICP to a CMC subaccount via the ICP ledger (irrevocably committing the ICP).
2. The user calls `notify_top_up` or `notify_mint_cycles` with the block index.

In step 2, `tokens_to_cycles` reads the live `icp_xdr_conversion_rate` from CMC state at the moment of execution: [1](#0-0) 

This rate is refreshed every five minutes from the Exchange Rate Canister: [2](#0-1) 

Neither `NotifyTopUpArg` nor `NotifyMintCyclesArg` exposes a `min_cycles_out` field: [3](#0-2) [4](#0-3) 

Once `tokens_to_cycles` succeeds, the ICP is burned unconditionally via `burn_and_log`: [5](#0-4) 

There is no guard that allows the user to abort if the computed cycles fall below an acceptable threshold.

### Impact Explanation
A user who sends ICP to the CMC subaccount and then calls `notify_top_up` or `notify_mint_cycles` after the ICP/XDR rate has dropped receives fewer cycles than expected. The ICP is burned regardless; no refund path exists for an unfavorable rate. In a volatile market, a 5-minute rate window is sufficient for a meaningful loss. The impact is a **cycles/resource accounting loss** for any unprivileged ingress caller using the CMC's public minting endpoints.

### Likelihood Explanation
The ICP/XDR rate is updated every five minutes and reflects real market movements. The two-step protocol (ledger transfer → notification) inherently creates a race window. Any user who does not complete both steps within the same rate epoch is exposed. This is a routine operational condition, not a contrived edge case.

### Recommendation
- **Short term:** Add an optional `min_cycles_out : opt nat` field to `NotifyTopUpArg` and `NotifyMintCyclesArg`. In `process_top_up` and `process_mint_cycles`, after calling `tokens_to_cycles`, compare the result against `min_cycles_out` and issue a refund (via the existing `refund_icp` path) if the computed cycles fall below the caller's threshold.
- **Long term:** Apply the same pattern to `notify_create_canister` and any future CMC endpoint where an output amount is computed from a live exchange rate.

### Proof of Concept
1. Alice checks the current ICP/XDR rate: 50,000 permyriad (5 XDR/ICP → 500 T cycles per ICP).
2. Alice sends 10 ICP to her CMC subaccount via the ICP ledger.
3. Before Alice calls `notify_top_up`, the XRC updates the rate to 40,000 permyriad (4 XDR/ICP → 400 T cycles per ICP).
4. Alice calls `notify_top_up` with her block index. `tokens_to_cycles` reads the new rate and computes 4,000 T cycles instead of the expected 5,000 T cycles.
5. The ICP is burned. Alice receives 1,000 T fewer cycles than she anticipated, with no recourse.

The `NotifyTopUpArg` struct carries only `block_index` and `canister_id`: [6](#0-5) 

There is no field through which Alice could have expressed `min_cycles_out = 5_000_000_000_000`, which would have caused the call to refund instead of burning at the unfavorable rate.

### Citations

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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L232-246)
```rust
/// The periodic task for collecting the ICP/XDR rate from the Exchange Rate Canister.
/// To avoid having multiple calls sent to the Exchange Rate Canister,
/// this function contains a guard to ensure multiple calls cannot be made until
/// the prior call is complete.
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

**File:** rs/nns/cmc/cmc.did (L27-33)
```text
type NotifyTopUpArg = record {
  // Index of the block on the ICP ledger that contains the payment.
  block_index : BlockIndex;

  // The canister to top up.
  canister_id : principal;
};
```

**File:** rs/nns/cmc/cmc.did (L200-204)
```text
type NotifyMintCyclesArg = record {
  block_index : BlockIndex;
  to_subaccount : Subaccount;
  deposit_memo : Memo;
};
```

**File:** rs/nns/cmc/src/lib.rs (L126-130)
```rust
#[derive(Clone, Eq, PartialEq, Hash, Debug, CandidType, Deserialize, Serialize)]
pub struct NotifyTopUp {
    pub block_index: BlockIndex,
    pub canister_id: CanisterId,
}
```
