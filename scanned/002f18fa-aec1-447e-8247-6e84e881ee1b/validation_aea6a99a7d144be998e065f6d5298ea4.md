### Title
SNS Treasury Manager Deposit Executes Without Price/Slippage Bounds, Enabling DEX Price Manipulation to Drain SNS Treasury - (File: rs/sns/governance/src/extensions.rs)

### Summary

The SNS governance framework's `execute_treasury_manager_deposit` function approves and deposits SNS treasury funds into a DEX-backed treasury manager extension without any price or slippage check at execution time. An attacker who can manipulate the DEX price during the governance voting window can cause the treasury to deposit all its approved tokens at an extremely unfavorable ratio, then drain the pool with a small counter-swap.

### Finding Description

The SNS extension system allows an SNS DAO to deposit treasury funds (SNS tokens + ICP) into a DEX via a `TreasuryManagerDeposit` governance proposal. The execution path is:

1. **Proposal submission/validation**: `validate_and_render_execute_extension_operation` → `validate_execute_extension_operation` → `validate_deposit_operation_impl` [1](#0-0) 

The only check performed is that the requested amounts do not exceed 50% of the current treasury balance. No DEX price, pool ratio, or minimum-output bound is validated.

2. **Proposal execution** (after governance voting, potentially days later): `perform_execute_extension_operation` re-runs `validate_execute_extension_operation` (same 50%-balance check only), then calls `execute_treasury_manager_deposit`: [2](#0-1) 

`execute_treasury_manager_deposit` (a) grants ICRC-2 allowances to the treasury manager canister via `approve_treasury_manager`, then (b) calls `deposit` on the treasury manager canister with no price guard whatsoever. [3](#0-2) 

The `DepositRequest` passed to the treasury manager contains only token amounts and owner accounts — no minimum price, no acceptable slippage range, no pool-ratio assertion: [4](#0-3) 

The codebase itself acknowledges this gap in two places. The treasury manager DID file labels it a "Known Security Risk": [5](#0-4) 

And the proposal-rendering code for `RegisterExtension` warns voters that DEXes may lack slippage protection, but the warning is informational only — no enforcement exists in the execution path: [6](#0-5) 

### Impact Explanation

An attacker who holds enough DEX liquidity to move the pool price can:

1. Wait for an SNS DAO to submit a `TreasuryManagerDeposit` proposal (publicly visible on-chain).
2. During the governance voting window (days), execute large swaps on the DEX canister to push the pool price to an extreme tick (e.g., dump SNS tokens so the pool is almost entirely ICP).
3. When the proposal is executed, `execute_treasury_manager_deposit` approves the full `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` and calls `deposit`. The treasury manager deposits at the current (manipulated) price, contributing nearly all ICP and almost no SNS tokens.
4. The attacker swaps a tiny amount of SNS tokens back, extracting the bulk of the ICP that was just deposited.

The result is loss of up to 50% of the SNS treasury's ICP and SNS token balances per proposal execution (the 50%-cap check limits per-proposal exposure but does not prevent the price-manipulation drain). [7](#0-6) 

### Likelihood Explanation

- **Entry point**: Any unprivileged canister caller with sufficient DEX liquidity. No admin key, governance majority, or subnet compromise is required.
- **Timing**: The IC has no public mempool, so traditional frontrunning is not possible. However, the governance voting period is days long, giving the attacker ample time to manipulate the DEX price before execution.
- **Current state**: The `ALLOWED_EXTENSIONS` whitelist is currently empty (KongSwap ceased operations April 6, 2026), so no extension can be registered today. The vulnerability becomes exploitable the moment any new treasury manager extension is whitelisted and an SNS DAO submits a deposit proposal. [8](#0-7) 

- **Realistic scenario**: The framework is explicitly designed to support future DEX integrations. The integration test confirms the full deposit→DEX flow works end-to-end, including a second deposit at a different pool ratio. [9](#0-8) 

### Recommendation

The SNS governance layer should enforce price bounds at execution time, not just balance bounds. Concretely:

1. **Extend `DepositRequest`** (or the `ExtensionOperationArg` map) to include caller-specified minimum acceptable output amounts or a maximum acceptable slippage percentage, committed at proposal-submission time.
2. **Re-validate price bounds at execution time** inside `execute_treasury_manager_deposit`, querying the DEX canister for the current pool price and rejecting the deposit if it falls outside the proposal-specified bounds.
3. Alternatively, require the treasury manager implementation to expose a `min_output` parameter and have governance pass the proposal-time price as a hard floor.

### Proof of Concept

**Setup**: An SNS DAO has 100 ICP and 100 SNS tokens in treasury. A `TreasuryManagerDeposit` proposal is submitted for 50 ICP + 50 SNS (within the 50% cap).

**Attack**:
```
// During the governance voting window (days before execution):
// Attacker calls the DEX canister directly (unprivileged):
dex_canister.swap(
    recipient = attacker,
    zero_for_one = true,          // sell SNS, buy ICP
    amount_specified = large_sns_amount,
    sqrt_price_limit = MIN_SQRT_PRICE,  // push price to extreme
    data = ...
);
// Pool is now ~100% ICP, 0% SNS at the extreme tick.

// Proposal executes:
// execute_treasury_manager_deposit approves 50 ICP + 50 SNS to treasury manager
// treasury manager calls DEX deposit at current (manipulated) price
// Result: ~50 ICP deposited, ~0 SNS deposited (all SNS returned as "excess")
// Pool now holds ~50 ICP contributed by treasury

// Attacker swaps 0.01 SNS → receives ~50 ICP from the pool
dex_canister.swap(
    recipient = attacker,
    zero_for_one = false,         // sell SNS, buy ICP
    amount_specified = 0.01_SNS,
    sqrt_price_limit = MAX_SQRT_PRICE,
    data = ...
);
// Attacker nets ~50 ICP for 0.01 SNS cost.
```

The root cause is that `execute_treasury_manager_deposit` at lines 1566–1578 of `rs/sns/governance/src/extensions.rs` issues the ICRC-2 approval and calls `deposit` with no price guard, and `validate_deposit_operation_impl` at lines 276–321 enforces only a 50%-balance cap with no pool-price assertion. [3](#0-2) [1](#0-0)

### Citations

**File:** rs/sns/governance/src/extensions.rs (L48-54)
```rust
thread_local! {
    static ALLOWED_EXTENSIONS: RefCell<BTreeMap<[u8; 32], ExtensionSpec>> = const { RefCell::new(btreemap! {
        // This collection is intentionally left empty. The Kong Swap extension used to be here,
        // but they ceased operations on April 6, 2026. Consequently, that was removed
        // from this list.
    }) };
}
```

**File:** rs/sns/governance/src/extensions.rs (L276-321)
```rust
async fn validate_deposit_operation_impl(
    governance: &Governance,
    value: Option<Precise>,
) -> Result<ValidatedDepositOperationArg, String> {
    let structurally_valid = ValidatedDepositOperationArg::try_from(value)?;

    let sns_subaccount = governance.sns_treasury_subaccount();
    let icp_subaccount = governance.icp_treasury_subaccount();

    // Fail if either is asking for more than 50% of current balance.  The balance could have changed
    // since the proposal was created, and we don't assume that the proposal should work
    let sns_balance = governance
        .ledger
        .account_balance(Account {
            owner: governance.env.canister_id().get().0,
            subaccount: sns_subaccount,
        })
        .await
        .map_err(|e| format!("Failed to get SNS treasury balance: {e:?}"))?;
    let icp_balance = governance
        .nns_ledger
        .account_balance(Account {
            owner: governance.env.canister_id().get().0,
            subaccount: icp_subaccount,
        })
        .await
        .map_err(|e| format!("Failed to get ICP treasury balance: {e:?}"))?;

    let icp_requested = Tokens::from_e8s(structurally_valid.treasury_allocation_icp_e8s);
    let sns_requested = Tokens::from_e8s(structurally_valid.treasury_allocation_sns_e8s);

    // Unwrap is safe, only fails if divisor is zero, which we don't do.
    if sns_requested > sns_balance.checked_div(2).unwrap() {
        return Err(format!(
            "SNS treasury deposit request of {sns_requested} exceeds 50% of current SNS Token balance of {sns_balance}"
        ));
    }

    if icp_requested > icp_balance.checked_div(2).unwrap() {
        return Err(format!(
            "ICP treasury deposit request of {icp_requested} exceeds 50% of current ICP balance of {icp_balance}"
        ));
    }

    Ok(structurally_valid)
}
```

**File:** rs/sns/governance/src/extensions.rs (L1546-1609)
```rust
async fn execute_treasury_manager_deposit(
    governance: &Governance,
    extension_canister_id: CanisterId,
    arg: ValidatedDepositOperationArg,
) -> Result<(), GovernanceError> {
    let ValidatedDepositOperationArg {
        treasury_allocation_sns_e8s,
        treasury_allocation_icp_e8s,
        original,
    } = arg;

    let context = governance.treasury_manager_deposit_context().await?;
    let arg_blob =
        construct_treasury_manager_deposit_payload(context, original).map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Failed to construct treasury manager deposit payload: {err}"),
            )
        })?;

    // 1. Transfer funds from treasury to treasury manager
    governance
        .approve_treasury_manager(
            extension_canister_id,
            treasury_allocation_sns_e8s,
            treasury_allocation_icp_e8s,
        )
        .await?;

    // 2. Call deposit on treasury manager
    let balances = governance
        .env
        .call_canister(extension_canister_id, "deposit", arg_blob)
        .await
        .map_err(|(code, err)| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!(
                    "Canister method call {extension_canister_id}.deposit failed with code {code:?}: {err}"
                ),
            )
        })
        .and_then(|blob| {
            Decode!(&blob, sns_treasury_manager::TreasuryManagerResult).map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error decoding TreasuryManager.deposit response: {err:?}"),
                )
            })
        })?
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("TreasuryManager.deposit failed: {err:?}"),
            )
        })?;

    log!(
        INFO,
        "TreasuryManager.deposit succeeded with response: {:?}",
        balances
    );

    Ok(())
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L84-86)
```text
type DepositRequest = record {
  allowances : vec Allowance;
};
```

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```

**File:** rs/nervous_system/integration_tests/tests/sns_extension_test.rs (L454-457)
```rust
        // Second deposit takes place with deposit ratio (SNS/ICP)
        // lower than the market ratio (SNS/ICP in the pool). Hence,
        // the excess amount of ICP is returned to the treasury owner.
        let expected_icp_fee_collector = 9 * ICP_FEE;
```
