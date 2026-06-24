### Title
Missing Slippage Protection in SNS Treasury Manager `DepositRequest` API — (File: `rs/sns/treasury_manager/treasury_manager.did`)

### Summary
The `DepositRequest` type in the IC's Treasury Manager API contains only `allowances` (deposit amounts) and no minimum output amount or slippage tolerance field. When SNS governance executes a treasury deposit into a DEX-backed Treasury Manager, there is no mechanism to enforce a minimum LP token output or acceptable price ratio at the governance level. This makes SNS treasury deposits structurally vulnerable to front-running and sandwich attacks between proposal approval and execution.

### Finding Description
The `DepositRequest` type is defined as:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};
```

It carries only the amounts to deposit, with no `min_lp_tokens_out`, `min_amounts_out`, or slippage tolerance field. [1](#0-0) 

The `execute_treasury_manager_deposit` function in SNS governance constructs this payload and calls `deposit` on the Treasury Manager canister with only the deposit amounts: [2](#0-1) 

The `construct_treasury_manager_deposit_payload` function builds the `DepositRequest` from the governance-approved `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` values, with no price expectation or slippage bound: [3](#0-2) 

The `approve_treasury_manager` function then grants ICRC-2 allowances to the Treasury Manager canister for the full approved amounts: [4](#0-3) 

Because the `DepositRequest` API structurally cannot carry slippage parameters, no Treasury Manager implementation can communicate the governance-approved price expectation to the DEX. The governance proposal captures the intent (deposit X SNS + Y ICP at approximately the current price), but by the time the proposal executes after the voting period, market conditions may have shifted significantly and there is no on-chain mechanism to abort the deposit if the price has moved beyond an acceptable range.

The IC codebase itself acknowledges this structural gap explicitly: [5](#0-4) 

The proposal rendering code also warns about it but treats it as an accepted risk: [6](#0-5) 

### Impact Explanation
An attacker monitoring the IC mempool or governance canister state can observe a pending treasury deposit execution and front-run it on the underlying DEX (e.g., by manipulating the token ratio in the liquidity pool). The SNS treasury then receives significantly fewer LP tokens than expected at proposal creation time. Because the `DepositRequest` carries no minimum output constraint, the Treasury Manager implementation has no governance-approved price floor to enforce, and the deposit proceeds regardless of how unfavorable the price has become. SNS token holders who voted on the proposal based on a specific price ratio have no on-chain protection against this outcome.

### Likelihood Explanation
Medium. The IC's deterministic execution model means that once a governance proposal passes and the execution timer fires, the deposit call is predictable. An attacker with access to a DEX that the Treasury Manager uses can observe the governance canister state (via query calls, which are public), calculate the expected deposit, and sandwich it. The voting period (typically days) gives ample time to prepare the attack. The constraint is that the attacker must control sufficient liquidity on the target DEX to meaningfully move the price.

### Recommendation
Add a `min_lp_tokens_out` or `min_amounts_out` field to `DepositRequest` so that governance proposals can encode an acceptable slippage bound at proposal creation time:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
  // Optional: minimum LP tokens or output amounts the Treasury Manager
  // must receive, or the deposit should be aborted.
  min_output : opt vec record { Asset; nat };
};
```

The `validate_deposit_operation_impl` function should also validate that any specified minimum output is consistent with current on-chain prices at proposal submission time. [7](#0-6) 

### Proof of Concept
1. An SNS governance proposal is submitted to deposit 1,000,000 SNS tokens and 500 ICP into a DEX liquidity pool via a registered Treasury Manager. At proposal creation, the SNS/ICP ratio is 2000:1.
2. The proposal passes after the voting period (e.g., 4 days).
3. Before the governance canister executes the proposal, an attacker calls the DEX directly to buy SNS tokens, shifting the ratio to 500:1 (4x worse for the SNS treasury).
4. `execute_treasury_manager_deposit` fires, calls `approve_treasury_manager` granting the full 1,000,000 SNS + 500 ICP allowance, then calls `deposit` with a `DepositRequest` containing only those amounts.
5. The Treasury Manager deposits at the manipulated 500:1 ratio. The SNS treasury receives LP tokens worth ~25% of the intended value.
6. The attacker sells their SNS position after the deposit, profiting from the price impact.

No minimum output field exists in `DepositRequest` to abort this deposit, and the IC governance layer has no mechanism to enforce the price expectation that existed when the proposal was voted on. [1](#0-0) [8](#0-7)

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

**File:** rs/sns/governance/src/extensions.rs (L777-831)
```rust
    async fn approve_treasury_manager(
        &self,
        treasury_manager_canister_id: CanisterId,
        sns_amount_e8s: u64,
        icp_amount_e8s: u64,
    ) -> Result<(), GovernanceError> {
        let to = Account {
            owner: treasury_manager_canister_id.get().0,
            subaccount: None,
        };

        let expiry_time_sec = self.env.now().saturating_add(ONE_HOUR_SECONDS);
        let expiry_time_nsec = expiry_time_sec.saturating_mul(NANO_SECONDS_PER_SECOND);

        // If expected_allowance is None, the ledger *blindly* overwrites any existing
        // allowance (even if non-zero). Therefore, there is no risk of double spending.

        self.ledger
            .icrc2_approve(
                to,
                sns_amount_e8s,
                Some(expiry_time_nsec),
                self.transaction_fee_e8s_or_panic(),
                self.sns_treasury_subaccount(),
                None,
            )
            .await
            .map(|_| ())
            .map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error making SNS Token treasury transfer: {e}"),
                )
            })?;

        self.nns_ledger
            .icrc2_approve(
                to,
                icp_amount_e8s,
                Some(expiry_time_nsec),
                icp_ledger::DEFAULT_TRANSFER_FEE.get_e8s(),
                self.icp_treasury_subaccount(),
                None,
            )
            .await
            .map(|_| ())
            .map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error making ICP Token treasury transfer: {e}"),
                )
            })?;

        Ok(())
    }
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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
