### Title
No Slippage Protection in SNS Treasury Manager Deposit Flow - (File: `rs/sns/governance/src/extensions.rs`)

### Summary
The `execute_treasury_manager_deposit` function in SNS governance deposits treasury funds (ICP + SNS tokens) into a DEX via a TreasuryManager extension canister without any slippage or minimum-price check. The `DepositRequest` type carries no `min_lp_tokens_out` or price-limit field, and the governance canister does not validate the value received after the deposit completes. The codebase itself acknowledges this gap in two places but does not enforce any protection.

### Finding Description

When an SNS governance proposal of type `ExecuteExtensionOperation` with `operation_name = "deposit"` is executed, the call chain is:

```
ValidatedExecuteExtensionOperation::execute()
  → execute_treasury_manager_deposit()
      1. approve_treasury_manager(sns_e8s, icp_e8s)   // ICRC-2 allowance
      2. call_canister(extension_canister_id, "deposit", arg_blob)
      3. log result, return Ok(())
``` [1](#0-0) 

The `DepositRequest` type that is sent to the TreasuryManager canister is:

```candid
type DepositRequest = record {
  allowances : vec Allowance;   // only amounts, no price floor
};
``` [2](#0-1) 

There is no `min_lp_tokens_out`, `min_price`, or any slippage-tolerance field. The proposal-validation step (`validate_deposit_operation_impl`) only enforces that the requested amounts do not exceed 50 % of the current treasury balance; it performs no price or value check: [3](#0-2) 

After the deposit call returns, `execute_treasury_manager_deposit` logs the `Balances` response and returns `Ok(())` unconditionally — it never compares the LP value received against the value deposited. [4](#0-3) 

The codebase acknowledges this risk explicitly in two places but provides no enforcement:

- `treasury_manager.did` lines 35–40: *"Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved."* [5](#0-4) 

- `proposal.rs` lines 1542–1545: a WARNING rendered in the proposal text that DEXes may lack slippage protection and that LP adaptors are vulnerable to front-running or sandwich attacks. [6](#0-5) 

### Impact Explanation

An SNS treasury deposit into a DEX liquidity pool can execute at an arbitrarily unfavorable token ratio. Because the IC's deterministic execution model makes the exact block at which a proposal executes observable in advance, a malicious DEX canister (the `external_custodian` in the TreasuryManager model) or a well-timed market participant can shift the pool price between proposal adoption and execution. The SNS treasury then receives LP tokens whose underlying value is materially less than the deposited assets — a direct financial loss to the SNS DAO. Unlike the original M-03 finding, the loss manifests as reduced LP value rather than a reduced LP count, making it invisible to any count-only check.

### Likelihood Explanation

Medium. Every `TreasuryManagerDeposit` proposal execution is a publicly observable, scheduled event. The IC has no mempool reordering, but the DEX canister itself (the `external_custodian`) can observe the incoming `deposit` call and adjust pool state before responding, or normal market volatility between proposal adoption and execution can cause the same effect. The 50 % treasury cap limits the maximum loss per proposal but does not prevent repeated exploitation across multiple proposals.

### Recommendation

1. **Add a slippage parameter to `DepositRequest`** in `rs/sns/treasury_manager/treasury_manager.did`:
   ```candid
   type DepositRequest = record {
     allowances : vec Allowance;
     min_lp_tokens_out : opt nat;   // minimum acceptable LP value
   };
   ```
2. **Add a `min_value_out_e8s` field to `ValidatedDepositOperationArg`** in `rs/sns/governance/src/extensions.rs` and enforce it in `validate_deposit_operation_impl`.
3. **Validate the returned `Balances`** in `execute_treasury_manager_deposit`: compare the post-deposit LP value against the pre-deposit treasury value and revert (or at minimum emit a governance error) if the slippage threshold is breached.

### Proof of Concept

1. SNS governance adopts a `TreasuryManagerDeposit` proposal to deposit 1 000 ICP and 500 000 SNS tokens into a DEX pool.
2. Between adoption and execution, the DEX pool price is shifted (by the DEX canister operator or by market activity) so that the ICP/SNS ratio is 3× worse than at proposal time.
3. `execute_treasury_manager_deposit` calls `deposit` with only the raw allowance amounts — no price floor.
4. The TreasuryManager deposits at the new ratio; the SNS treasury receives LP tokens worth ~33 % of the deposited value.
5. `execute_treasury_manager_deposit` receives `Ok(Balances{…})`, logs it, and returns `Ok(())` — no error is raised, no slippage is detected.
6. The SNS treasury has permanently lost ~67 % of the deposited value with no on-chain indication of the loss.

### Citations

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
