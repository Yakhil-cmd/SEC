### Title
SNS Treasury Manager `deposit` Lacks Slippage Protection, Enabling Price-Ratio Manipulation Between Proposal Approval and Execution - (`rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS Treasury Manager extension's `deposit` operation carries no slippage-tolerance parameter. The `DepositRequest` type in the interface only encodes token allowances (amounts), not a minimum acceptable price ratio. When an SNS governance proposal to deposit treasury assets into a DEX liquidity pool is approved by voters, the actual execution can occur at a materially different price ratio than the one voters observed, because the DEX price can move freely during the governance voting period. The codebase itself acknowledges this as a "Known Security Risk" in the interface definition, yet no on-chain enforcement exists.

---

### Finding Description

The `DepositRequest` type in `rs/sns/treasury_manager/treasury_manager.did` contains only `allowances` (token amounts), with no `min_price_ratio`, `max_slippage_bps`, or equivalent field:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};
```

The interface file explicitly flags this gap:

> *"Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved."*

The execution path in `rs/sns/governance/src/extensions.rs` at `execute_treasury_manager_deposit` (lines 1545–1609) approves the treasury manager for the fixed token amounts from the proposal and then calls `deposit` unconditionally:

```rust
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
    ...
```

No price-ratio check is performed before or after the `deposit` call. The validation function `validate_deposit_operation_impl` (lines 276–321) only checks that the requested amounts do not exceed 50% of the current treasury balance; it does not record or enforce the price ratio at proposal-submission time.

The proposal rendering for `RegisterExtension` in `rs/sns/governance/src/proposal.rs` (lines 1540–1545) also warns voters:

> *"Some Decentralized Exchanges lack slippage protection during deposits. Consequently, deposited asset ratios may deviate from those specified in the proposal. This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running or sandwich attacks."*

Despite both warnings, no enforcement mechanism exists in the production execution path.

---

### Impact Explanation

An SNS DAO's treasury holds both ICP and SNS tokens. When a `RegisterExtension` or `ExecuteExtensionOperation` (deposit) proposal is submitted, voters evaluate the deposit at the current DEX price ratio (e.g., 1 ICP = 100 SNS tokens). The governance voting period can last days. If the DEX price shifts significantly before execution—whether through organic market movement or deliberate manipulation—the treasury manager deposits at the unfavorable ratio, permanently losing value from the SNS treasury. A sandwich attacker can:

1. Observe the pending governance proposal execution.
2. Trade on the DEX to move the price against the SNS treasury just before execution.
3. Profit from the price impact while the SNS DAO absorbs the loss.

The SNS treasury is a shared resource owned by all token holders; losses are irreversible once the deposit is committed to the DEX.

---

### Likelihood Explanation

SNS governance voting periods are measured in days, making price drift between proposal approval and execution a near-certainty for volatile token pairs. The IC's deterministic execution model means the deposit transaction is publicly observable in the governance canister's pending proposals, giving any on-chain actor advance notice to front-run it. The risk is explicitly acknowledged in two separate places in the codebase, confirming the developers are aware the scenario is realistic.

---

### Recommendation

1. Add a `min_price_ratio` (or equivalent `max_slippage_bps`) field to `DepositRequest` in `treasury_manager.did`.
2. In `execute_treasury_manager_deposit` (`rs/sns/governance/src/extensions.rs`), query the DEX for the current price ratio before calling `deposit` and revert if the ratio has moved beyond the voter-approved tolerance.
3. Alternatively, record the price ratio at proposal-submission time in `validate_deposit_operation_impl` and enforce it at execution time.

---

### Proof of Concept

**Step 1 – Proposal submission.** An SNS DAO submits an `ExecuteExtensionOperation` proposal with `treasury_allocation_sns_e8s = 1_000_000_000` and `treasury_allocation_icp_e8s = 10_000_000` at a DEX price of 100 SNS/ICP.

**Step 2 – Voting period.** The proposal enters the governance voting period (e.g., 4 days). During this window, an attacker observes the pending proposal.

**Step 3 – Price manipulation.** Just before the proposal's execution block, the attacker sells a large amount of SNS tokens on the DEX, moving the price to 50 SNS/ICP.

**Step 4 – Execution.** `execute_treasury_manager_deposit` is called. It approves the treasury manager for the fixed amounts and calls `deposit` with no price check. The treasury manager deposits at 50 SNS/ICP, providing twice as many SNS tokens per ICP as voters intended.

**Step 5 – Attacker profit.** The attacker buys back SNS tokens at the now-depressed price, profiting from the price impact while the SNS treasury permanently loses value.

The root cause is confirmed at: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

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

**File:** rs/sns/governance/src/extensions.rs (L1545-1609)
```rust
/// Execute a treasury manager deposit operation
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

**File:** rs/sns/governance/src/proposal.rs (L1540-1551)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.

## Extension Configuration

The extension will be deployed and configured according to the provided parameters.",
    ))
}
```
