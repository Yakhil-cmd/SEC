### Title
Missing Slippage Protection in SNS TreasuryManager Deposit Interface - (File: rs/sns/treasury_manager/treasury_manager.did, rs/sns/governance/src/extensions.rs)

### Summary
The SNS TreasuryManager `DepositRequest` type contains no slippage protection parameters (no `min_amount_out` or equivalent), and the `validate_deposit_operation_impl` function in SNS governance enforces no minimum output requirements when depositing treasury funds into a DEX liquidity pool. This is the direct IC analog of passing `amountAMin = 0` and `amountBMin = 0` to a liquidity router. The codebase itself explicitly acknowledges this as a "Known Security Risk."

### Finding Description

The `DepositRequest` type defined in `treasury_manager.did` carries only an `allowances` field specifying how much of each asset the TreasuryManager may spend: [1](#0-0) 

There is no field for minimum LP tokens to receive, minimum price ratio, or any other slippage bound. The file itself acknowledges this gap explicitly: [2](#0-1) 

On the governance side, `validate_deposit_operation_impl` in `rs/sns/governance/src/extensions.rs` validates only that the requested amounts do not exceed 50% of the current treasury balance: [3](#0-2) 

It does not validate any minimum output requirement. The test suite explicitly confirms that zero allocation amounts are accepted as valid ("Positive: zero amounts"): [4](#0-3) 

When the deposit is executed, `construct_treasury_manager_deposit_payload` builds the `DepositRequest` with only the allowance amounts and no slippage bound: [5](#0-4) 

The proposal rendering in `rs/sns/governance/src/proposal.rs` also warns about this: [6](#0-5) 

### Impact Explanation

When an SNS governance proposal to deposit treasury funds into a DEX liquidity pool is adopted and executed, the TreasuryManager calls the DEX with no minimum output constraint. A front-runner observing the pending governance execution can sandwich the deposit: manipulate the DEX pool price before the deposit lands, causing the SNS treasury to receive significantly fewer LP tokens than the ratio at proposal approval time. The SNS treasury suffers a direct, permanent financial loss proportional to the price impact of the sandwich. Any undeposited tokens are returned, but the ratio distortion means the deposited portion is mispriced.

### Likelihood Explanation

SNS TreasuryManager extensions are a production feature. Once any SNS deploys a TreasuryManager and passes a deposit proposal, the execution is observable on-chain. Front-running is a well-known and actively practiced attack on DEX liquidity operations. The governance execution delay (proposal voting period) gives an attacker ample time to prepare. Likelihood is medium-high for any SNS that actively uses the TreasuryManager deposit feature.

### Recommendation

1. Add a `min_lp_tokens_out` or equivalent slippage tolerance field to `DepositRequest` in `treasury_manager.did`.
2. Require TreasuryManager implementers to enforce this bound when calling the DEX.
3. In `validate_deposit_operation_impl`, reject deposit proposals that specify zero allocation amounts for both tokens simultaneously, and optionally require a non-zero minimum output field.
4. Reject deposit proposals where `treasury_allocation_sns_e8s == 0 && treasury_allocation_icp_e8s == 0` as a no-op guard.

### Proof of Concept

1. An SNS registers a TreasuryManager extension pointing to a DEX liquidity pool.
2. SNS governance adopts a `ExecuteExtensionOperation` deposit proposal with `treasury_allocation_sns_e8s = X` and `treasury_allocation_icp_e8s = Y`.
3. `validate_deposit_operation_impl` passes: amounts are non-None, correct type, and ≤ 50% of treasury balance. No minimum output is checked.
4. `construct_treasury_manager_deposit_payload` builds a `DepositRequest { allowances: [...] }` with no slippage bound.
5. An attacker front-runs the governance execution: buys one side of the DEX pool to skew the price ratio.
6. The TreasuryManager calls `deposit` on the DEX with the full allowance but no minimum LP output constraint.
7. The DEX accepts the deposit at the manipulated price, minting far fewer LP tokens than expected.
8. The attacker sells back, profiting from the price impact. The SNS treasury has permanently lost value. [7](#0-6) [1](#0-0)

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

**File:** rs/sns/governance/src/extensions.rs (L1088-1099)
```rust
fn construct_treasury_manager_deposit_payload(
    context: TreasuryManagerDepositContext,
    value: Precise,
) -> Result<Vec<u8>, String> {
    let allowances = construct_treasury_manager_deposit_allowances(context, value)?;

    let arg = DepositRequest { allowances };
    let arg =
        candid::encode_one(&arg).map_err(|err| format!("Error encoding DepositRequest: {err}"))?;

    Ok(arg)
}
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

**File:** rs/sns/governance/src/extensions.rs (L2682-2689)
```rust
            (
                "Positive: zero amounts",
                100_000_000,
                200_000_000,
                0,
                0,
                Ok(()),
            ),
```

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
