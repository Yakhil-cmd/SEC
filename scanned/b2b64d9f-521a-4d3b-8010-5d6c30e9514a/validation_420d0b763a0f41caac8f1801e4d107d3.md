### Title
SNS Treasury Manager Deposit Price Ratio Divergence Between Proposal Approval and Execution - (File: rs/sns/governance/src/extensions.rs)

### Summary
The SNS Treasury Manager deposit flow is a two-step process: (1) a governance proposal is submitted and validated using the treasury balance at proposal-submission time, and (2) the proposal is executed later, transferring funds to the treasury manager canister which then deposits them into an external DEX/liquidity pool. The price ratio (SNS token / ICP) used by the DEX at execution time may differ significantly from the ratio at proposal approval time. The `treasury_manager.did` file explicitly acknowledges this as a "Known Security Risk" but the SNS governance code provides no on-chain slippage guard at execution time, leaving SNS treasuries exposed to value loss.

### Finding Description

The SNS governance extension system implements a two-step deposit flow for treasury assets into a Treasury Manager (e.g., a DEX adaptor):

**Step 1 – Proposal Submission & Validation** (`validate_deposit_operation_impl` in `rs/sns/governance/src/extensions.rs`):

The proposal specifies fixed token amounts (`treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s`). Validation only checks that each requested amount does not exceed 50% of the current treasury balance at proposal-submission time:

```rust
if sns_requested > sns_balance.checked_div(2).unwrap() { ... }
if icp_requested > icp_balance.checked_div(2).unwrap() { ... }
```

No price ratio or slippage bound is validated. The `ValidatedDepositOperationArg` carries only the raw token amounts, not any price constraint.

**Step 2 – Proposal Execution** (`execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs`):

After the governance voting period (which can be days), the proposal executes:
1. `approve_treasury_manager` grants ICRC-2 allowances for the fixed amounts.
2. `call_canister(extension_canister_id, "deposit", arg_blob)` calls the Treasury Manager's `deposit` endpoint.

The Treasury Manager then deposits into the external DEX at whatever price ratio the pool currently has. The `treasury_manager.did` file itself documents this:

```
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

The SNS governance layer (`execute_treasury_manager_deposit`) performs **no** slippage check, no price-ratio validation, and no minimum-output assertion before or after calling `deposit`. The `Balances` response from the treasury manager is logged but never validated against any expected output.

### Impact Explanation

An attacker (or any market participant) who observes a pending SNS governance proposal to deposit treasury funds into a DEX can:

1. Wait for the proposal to pass (governance voting period is public and on-chain).
2. Just before the proposal executes, manipulate the DEX pool price ratio (e.g., by making a large swap) so that the SNS/ICP ratio in the pool diverges significantly from the ratio encoded in the proposal.
3. The governance canister calls `deposit` with the fixed amounts. The DEX accepts the deposit at the manipulated ratio, minting fewer LP tokens than expected (or returning excess of one token to the treasury manager's suspense account).
4. The attacker reverses their swap, extracting value from the pool at the SNS treasury's expense.

The SNS treasury permanently loses value proportional to the price impact of the manipulation. Because the governance canister does not validate the output of `deposit`, the proposal is marked as successfully executed regardless of how unfavorable the actual execution price was.

Additionally, even without active manipulation, natural market movement during a multi-day voting period can cause the fixed token amounts to represent a very different price ratio than what voters approved, resulting in unintended value loss for the SNS treasury.

### Likelihood Explanation

- The attack entry path is fully unprivileged: any canister or user can interact with the DEX to move the price.
- SNS governance proposals are public and their execution timing is predictable (proposals execute immediately after the voting deadline passes).
- The `treasury_manager.did` file explicitly acknowledges this risk, confirming the developers are aware of the structural gap.
- DEX price manipulation is a well-known attack class on IC (no flash loans needed; a regular large swap suffices since IC does not have atomic flash loans, but a sandwich across two blocks is feasible).
- The impact scales with the size of the treasury deposit and the liquidity depth of the DEX pool.

### Recommendation

1. **Add a minimum-LP-output parameter** to the `DepositRequest` (or as a separate field in the governance proposal arg) so that the Treasury Manager can enforce a slippage bound when calling the DEX.
2. **Validate the `Balances` response** returned by `deposit` in `execute_treasury_manager_deposit` against a minimum expected output derived from the price at proposal submission time.
3. **Re-validate the price ratio at execution time** inside `execute_treasury_manager_deposit` before calling `approve_treasury_manager`, and abort if the ratio has moved beyond a governance-configured tolerance.
4. **Shorten the execution window** or add a time-lock check so that proposals cannot be executed if the market price has moved beyond a threshold since proposal submission.

### Proof of Concept

**Attacker-controlled entry path:**

1. SNS governance proposal is submitted: deposit `X` SNS tokens + `Y` ICP into DEX pool (ratio X:Y).
2. Proposal passes after voting period. Execution is imminent (publicly observable on-chain).
3. Attacker calls the DEX swap endpoint to buy SNS tokens with ICP, moving the pool ratio so that the pool now expects more ICP per SNS token than the proposal's X:Y ratio.
4. IC governance canister executes `execute_treasury_manager_deposit`: [1](#0-0) 

   — `approve_treasury_manager` grants allowances for the original fixed amounts.
   — `call_canister(..., "deposit", ...)` is called with no slippage bound.

5. The Treasury Manager deposits X SNS + Y ICP into the DEX at the manipulated ratio. The DEX mints fewer LP tokens than the fair-price equivalent.
6. Attacker reverses their swap, profiting from the price impact paid by the SNS treasury.
7. `execute_treasury_manager_deposit` receives the `Balances` response and logs it: [2](#0-1) 

   — No output validation. Proposal is marked `Executed`.

**Root cause — no slippage guard in governance execution layer:** [3](#0-2) 

**Acknowledged in the Treasury Manager interface spec:** [4](#0-3) 

**Validation at proposal submission only checks balance, not price ratio:** [5](#0-4)

### Citations

**File:** rs/sns/governance/src/extensions.rs (L276-320)
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
```

**File:** rs/sns/governance/src/extensions.rs (L1545-1610)
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
}
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
