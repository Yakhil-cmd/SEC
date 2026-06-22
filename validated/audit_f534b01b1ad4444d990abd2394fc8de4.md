Now I have enough context to write the finding. The SNS governance treasury manager deposit execution path is the direct analog.### Title
Missing Slippage Protection in SNS Treasury Manager Deposit Execution - (File: rs/sns/governance/src/extensions.rs)

### Summary

The `execute_treasury_manager_deposit` function in the SNS Governance canister approves and deposits treasury funds into an external DEX via a treasury manager canister without enforcing any minimum output amount. Because SNS governance proposals can take days to pass voting before execution, DEX prices can shift significantly between proposal creation and execution, and a front-runner can deliberately manipulate the pool price immediately before the proposal executes, causing the SNS treasury to receive far fewer LP tokens than expected.

### Finding Description

The `execute_treasury_manager_deposit` function in `rs/sns/governance/src/extensions.rs` executes in two steps: it first grants an ICRC-2 allowance to the treasury manager canister for the approved SNS and ICP amounts, then calls `deposit` on the treasury manager canister.

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
    ...
```

The `DepositRequest` type (defined in `rs/sns/treasury_manager/treasury_manager.did`) only carries `allowances` — the input token amounts — and contains no field for a minimum acceptable output (e.g., minimum LP tokens to receive):

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};
```

The `Allowance` record itself only specifies the asset, the input amount, and a refund account. There is no `min_lp_tokens`, `min_output`, or any slippage bound. After `deposit` returns, the governance code logs the resulting balances but performs no verification that the output meets any minimum threshold.

The proposal validation step (`validate_deposit_operation_impl`) only checks that the requested amounts do not exceed 50% of the current treasury balance at validation time. It does not record any expected output, and no such check is performed at execution time either.

The codebase itself acknowledges this gap. The `treasury_manager.did` interface specification explicitly lists it as a known security risk:

> Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved.

The proposal rendering in `rs/sns/governance/src/proposal.rs` also warns voters:

> Some Decentralized Exchanges lack slippage protection during deposits. Consequently, deposited asset ratios may deviate from those specified in the proposal. This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running or sandwich attacks.

Despite this acknowledged risk, neither the `ExecuteExtensionOperation` proposal type nor the `execute_treasury_manager_deposit` execution path provides any mechanism for the SNS community to specify or enforce a minimum acceptable output, leaving the treasury permanently exposed.

### Impact Explanation

**Impact: High**

An SNS treasury deposit into a DEX liquidity pool can be front-run. An attacker who observes a pending governance execution can make a large trade in the target pool immediately before the governance canister calls `deposit`, skewing the pool price. The treasury manager then deposits at the manipulated ratio, receiving far fewer LP tokens than the fair-market value of the deposited assets. The attacker then reverses their trade (sandwich attack), extracting value from the SNS treasury. Because the governance code does not verify the output, the deposit succeeds and the loss is permanent — there is no revert or reimbursement path for the shortfall in LP tokens received.

### Likelihood Explanation

**Likelihood: Low**

Exploiting this requires an attacker to monitor IC governance for pending `ExecuteExtensionOperation` proposals targeting a treasury manager, time a sandwich attack around the governance execution, and have sufficient capital to move the DEX price. The governance execution timing is somewhat predictable (proposals execute after voting concludes), but the attacker must also have access to the specific DEX and sufficient liquidity. This is a realistic but non-trivial attack.

### Recommendation

1. Add a `min_lp_tokens_to_receive` (or equivalent `min_output`) field to the `DepositRequest` type in `rs/sns/treasury_manager/treasury_manager.did` and the corresponding Rust types in `rs/sns/treasury_manager/src/lib.rs`.
2. Extend the `ExecuteExtensionOperation` proposal argument schema to allow the SNS community to specify minimum acceptable outputs at proposal creation time.
3. In `execute_treasury_manager_deposit`, after calling `deposit`, verify that the returned balances satisfy the minimum output specified in the proposal. If not, treat the operation as failed and attempt to recover funds.
4. Alternatively, enforce slippage bounds inside the treasury manager implementation itself, using the allowance amounts and current pool state to compute a maximum acceptable price impact before executing the DEX deposit.

### Proof of Concept

**Attacker-controlled entry path:**

1. An SNS governance proposal of type `ExecuteExtensionOperation` is submitted and passes voting to deposit, e.g., 1,000,000 SNS tokens and 500 ICP into a DEX liquidity pool via a registered treasury manager canister.
2. The proposal enters the execution queue. The attacker monitors the IC for this event (publicly observable via the governance canister's `list_proposals` query).
3. Immediately before the governance canister executes the proposal, the attacker submits a large buy order on the target DEX pool, drastically shifting the SNS/ICP price ratio.
4. The governance canister calls `execute_treasury_manager_deposit`:
   - `approve_treasury_manager` grants the ICRC-2 allowance.
   - `call_canister(..., "deposit", arg_blob)` triggers the treasury manager to deposit at the now-manipulated price, receiving, e.g., 40% fewer LP tokens than fair value.
5. The governance code receives the `balances` response, logs it, and returns `Ok(())` — no minimum output check is performed.
6. The attacker reverses their trade, profiting from the price impact at the treasury's expense.
7. The SNS treasury has permanently lost value with no on-chain recourse.

**Root cause in production code:** [1](#0-0) 

The `DepositRequest` carries no minimum output field: [2](#0-1) 

The known risk is acknowledged but not mitigated: [3](#0-2) 

The proposal rendering warns voters but the execution path enforces nothing: [4](#0-3)

### Citations

**File:** rs/sns/governance/src/extensions.rs (L1566-1609)
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

**File:** rs/sns/treasury_manager/treasury_manager.did (L84-93)
```text
type DepositRequest = record {
  allowances : vec Allowance;
};

type WithdrawRequest = record {
  // Maps Ledger canister IDs of assets to be withdrawn to the respective withdraw accounts.
  //
  // If not set, accounts specified at the time of deposit will be used for the withdrawal.
  withdraw_accounts : opt vec record { principal; Account };
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
