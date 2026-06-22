### Title
SNS Treasury Manager Deposit Lacks Slippage Protection, Enabling Sandwich Attacks on SNS Treasury Deposits — (File: rs/sns/governance/src/extensions.rs)

### Summary
The SNS governance `execute_treasury_manager_deposit` function calls the Treasury Manager's `deposit` endpoint with no minimum-LP-tokens (slippage) parameter. Because SNS governance proposals are publicly visible on-chain during their voting period, an unprivileged attacker can observe a pending deposit proposal, sandwich the execution by manipulating the liquidity pool price ratio, and cause the SNS treasury to receive significantly fewer LP tokens than expected.

### Finding Description
The `execute_treasury_manager_deposit` function in `rs/sns/governance/src/extensions.rs` constructs a `DepositRequest` from the validated proposal argument and calls `deposit` on the Treasury Manager canister: [1](#0-0) 

The `DepositRequest` type, defined in the Treasury Manager interface, contains only `allowances` — the amounts to deposit — and no `min_lp_tokens` or slippage bound: [2](#0-1) 

The `TreasuryManager` Rust trait mirrors this, accepting only a `DepositRequest` with no slippage field: [3](#0-2) 

The codebase itself acknowledges this gap in two places. The DID interface file explicitly labels it a "Known Security Risk": [4](#0-3) 

And the proposal rendering function for `RegisterExtension` warns voters at submission time: [5](#0-4) 

The validation step (`validate_deposit_operation_impl`) only checks that the requested amounts do not exceed 50% of the current treasury balance; it performs no slippage check: [6](#0-5) 

### Impact Explanation
An attacker who observes a pending `ExecuteExtensionOperation` deposit proposal can execute a classic sandwich attack:

1. **Front-run**: Before the proposal executes, the attacker makes large trades in the IC-native liquidity pool (e.g., KongSwap on IC) to skew the pool price ratio unfavorably for the SNS treasury.
2. **Victim execution**: The governance proposal executes `execute_treasury_manager_deposit`, which calls `deposit` on the Treasury Manager. The deposit occurs at the manipulated price, so the SNS treasury receives far fewer LP tokens than the pre-proposal price implied.
3. **Back-run**: The attacker reverses their trades, restoring the pool price and pocketing the arbitrage profit extracted from the SNS treasury.

The financial loss is bounded only by the attacker's capital and the pool's depth. For large SNS treasuries depositing into shallow pools, the loss can be substantial. The integration test comment confirms that deposit ratios can already deviate from market ratios: [7](#0-6) 

### Likelihood Explanation
- SNS governance proposals are publicly visible on-chain for their entire voting period (days), giving ample time to prepare the attack.
- The attacker requires no privileged role — only the ability to trade in the target liquidity pool, which is an unprivileged action open to any IC principal.
- Sandwich attacks on governance-triggered DeFi operations are a well-documented and actively exploited class of attack in the broader DeFi ecosystem.
- The codebase's own warning text confirms the developers are aware the attack surface exists.

### Recommendation
Add a `min_lp_tokens : opt nat` (or equivalent slippage bound) field to `DepositRequest` in `treasury_manager.did` and propagate it through the `TreasuryManager` trait and `execute_treasury_manager_deposit`. The governance proposal validation (`validate_deposit_operation_impl`) should require this field to be set and non-zero for any deposit into a pool that does not natively enforce slippage. The Treasury Manager implementation must abort the deposit and refund the allowance if the LP tokens received fall below the specified minimum — analogous to the `DepositStakeWithSlippage` / `DepositSolWithSlippage` instructions introduced in the referenced patch.

### Proof of Concept
1. An SNS submits an `ExecuteExtensionOperation` proposal with `operation_name = "deposit"` targeting a KongSwap Adaptor Treasury Manager, specifying e.g. 10 000 ICP + 10 000 SNS tokens.
2. The proposal enters the voting period and is publicly visible.
3. The attacker calls the KongSwap pool canister directly (unprivileged ingress) to buy a large amount of SNS tokens, driving the SNS/ICP price up sharply.
4. The proposal passes and `execute_treasury_manager_deposit` fires, calling `deposit` with the original `allowances` but no slippage bound. The Treasury Manager deposits at the now-inflated SNS price, receiving far fewer LP tokens than the pre-attack price would have yielded.
5. The attacker sells their SNS tokens back into the pool, restoring the price and realizing a profit at the SNS treasury's expense.
6. The SNS treasury's LP position is worth materially less than the deposited assets, with no on-chain mechanism to detect or revert the loss. [8](#0-7) [2](#0-1)

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

**File:** rs/sns/treasury_manager/src/lib.rs (L250-256)
```rust
pub trait TreasuryManager {
    /// Implements the `deposit` API function.
    fn deposit(
        &mut self,
        request: DepositRequest,
    ) -> impl std::future::Future<Output = TreasuryManagerResult> + Send;

```

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```

**File:** rs/nervous_system/integration_tests/tests/sns_extension_test.rs (L454-456)
```rust
        // Second deposit takes place with deposit ratio (SNS/ICP)
        // lower than the market ratio (SNS/ICP in the pool). Hence,
        // the excess amount of ICP is returned to the treasury owner.
```
