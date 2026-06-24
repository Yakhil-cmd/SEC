### Title
Missing Minimum Cycles Slippage Protection in CMC ICP-to-Cycles Conversion - (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) converts ICP to cycles using the current `icp_xdr_conversion_rate` at the time the user calls a notification function, not at the time the ICP was sent. None of the three public notification endpoints — `notify_top_up`, `notify_mint_cycles`, and `notify_create_canister` — accept a `min_cycles_expected` parameter. A user who sends ICP and then calls notify after the rate has dropped receives substantially fewer cycles than expected, with no on-chain protection and no refund path for the rate-change case.

### Finding Description
The CMC implements a mandatory two-step ICP-to-cycles flow:

**Step 1** — User transfers ICP to a CMC subaccount on the ICP ledger.  
**Step 2** — User calls `notify_top_up`, `notify_mint_cycles`, or `notify_create_canister`.

The conversion rate is applied exclusively at Step 2 inside `tokens_to_cycles`: [1](#0-0) 

This function reads `state.icp_xdr_conversion_rate` from live canister state. That rate is mutated by two independent paths:

1. **Heartbeat** — every five minutes the CMC calls the Exchange Rate Canister and may update the rate via `do_set_icp_xdr_conversion_rate`: [2](#0-1) 

2. **NNS governance proposal** — `set_icp_xdr_conversion_rate` (callable only by the Governance canister) can set an arbitrary new rate at any time: [3](#0-2) 

The three notification functions that burn ICP and mint cycles all call `tokens_to_cycles` with no floor check: [4](#0-3) [5](#0-4) [6](#0-5) 

The public interface confirms that none of the notify arguments carry a minimum-cycles field: [7](#0-6) [8](#0-7) 

### Impact Explanation
A user who:
1. Queries `get_icp_xdr_conversion_rate()` and observes rate R,
2. Sends X ICP to the CMC subaccount expecting `X * R * cycles_per_xdr` cycles, and
3. Calls `notify_top_up` / `notify_mint_cycles` / `notify_create_canister` after the rate has dropped to R′ < R,

receives only `X * R′ * cycles_per_xdr` cycles. The ICP is burned regardless — there is no refund path triggered by a rate change. The shortfall is permanent and unrecoverable. Integration tests confirm the rate can shift by 20 % or more within a single five-minute heartbeat window: [9](#0-8) 

For large ICP amounts (e.g., the Subnet Rental Canister topping up hundreds of thousands of ICP worth of cycles), the absolute cycle shortfall can be enormous.

### Likelihood Explanation
The rate is updated automatically every five minutes. Any user who experiences a delay between sending ICP and calling notify — due to UI latency, wallet confirmation flows, manual scripting, or network congestion — is exposed. No attacker is required; the rate drift is a normal operational condition. The heartbeat-driven update is unconditional when an exchange rate canister is configured: [2](#0-1) 

### Recommendation
Add an optional `min_cycles_expected : opt nat` field to `NotifyTopUpArg`, `NotifyMintCyclesArg`, and `NotifyCreateCanisterArg`. Inside `process_top_up`, `process_mint_cycles`, and `process_create_canister`, after calling `tokens_to_cycles`, compare the result against `min_cycles_expected`. If the computed cycles fall below the caller's floor, refund the ICP (using the existing `refund_icp` helper) and return a descriptive `NotifyError` instead of burning the ICP. This mirrors the `maxAmount` slippage guard recommended in the original report.

### Proof of Concept
```
1. User calls get_icp_xdr_conversion_rate() → xdr_permyriad_per_icp = 100_000
   (= 10 XDR/ICP; with cycles_per_xdr = 1_000_000_000_000 → 10T cycles per ICP)

2. User transfers 100 ICP to CMC subaccount on ICP ledger,
   expecting 1_000T cycles.

3. CMC heartbeat fires; exchange rate canister returns a lower rate.
   do_set_icp_xdr_conversion_rate sets xdr_permyriad_per_icp = 50_000
   (= 5 XDR/ICP → 5T cycles per ICP).

4. User calls notify_top_up { block_index, canister_id }.
   tokens_to_cycles(100 ICP) now returns 500T cycles.

5. deposit_cycles succeeds; burn_and_log burns 100 ICP.
   User receives 500T cycles — 50% less than expected.
   No refund is issued; no error is returned.
   The shortfall of 500T cycles is permanent.
```

The root cause — `tokens_to_cycles` reading live state with no caller-supplied floor — is at: [10](#0-9)

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

**File:** rs/nns/cmc/src/main.rs (L2397-2402)
```rust
#[heartbeat]
async fn canister_heartbeat() {
    if with_state(|state| state.exchange_rate_canister_id.is_some()) {
        update_exchange_rate().await
    }
}
```

**File:** rs/nns/cmc/cmc.did (L26-34)
```text
// The argument of the [notify_top_up] method.
type NotifyTopUpArg = record {
  // Index of the block on the ICP ledger that contains the payment.
  block_index : BlockIndex;

  // The canister to top up.
  canister_id : principal;
};

```

**File:** rs/nns/cmc/cmc.did (L240-253)
```text
service : (opt CyclesCanisterInitPayload) -> {
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

**File:** rs/nns/integration_tests/src/cycles_minting_canister_with_exchange_rate_canister.rs (L135-160)
```rust
    assert_eq!(response.data.xdr_permyriad_per_icp, 250_000);

    // Step 4: Change the rate and check that the cycles minting canister captures
    // the new rate.
    // Reinstall the mock exchange rate canister with an updated payload.
    reinstall_mock_exchange_rate_canister(
        &state_machine,
        EXCHANGE_RATE_CANISTER_ID,
        new_icp_cxdr_mock_exchange_rate_canister_init_payload(20_000_000_000, None, None),
    );

    // Advance the time 5 minutes into the future so the heartbeat will trigger.
    state_machine.advance_time(Duration::from_secs(FIVE_MINUTES_SECONDS));
    // Trigger the heartbeat.
    state_machine.tick();

    let response = get_icp_xdr_conversion_rate(&state_machine);
    // The rate's timestamp should be the CMC's first rate timestamp + 5 minutes + 10 secs.
    // Note on the 10 secs:
    // Similar to retrieving the first rate. Another 2 seconds are tacked on
    // for retrieve the rate initially.
    assert_eq!(
        response.data.timestamp_seconds,
        cmc_first_rate_timestamp_seconds + (FIVE_MINUTES_SECONDS * 2) + 10
    );
    assert_eq!(response.data.xdr_permyriad_per_icp, 200_000);
```
