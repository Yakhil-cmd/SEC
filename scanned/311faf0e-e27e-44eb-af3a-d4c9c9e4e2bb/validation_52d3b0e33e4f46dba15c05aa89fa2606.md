### Title
SNS Treasury Manager Deposit Lacks Slippage Protection, Enabling Front-Running of Governance Proposals to Drain SNS Treasury Funds - (File: rs/sns/governance/src/extensions.rs)

### Summary
The SNS Treasury Manager deposit flow (`RegisterExtension` and `ExecuteExtensionOperation` deposit) validates only that the requested token amounts do not exceed 50% of the current treasury balance at proposal creation time. No price ratio or slippage bound is enforced at execution time. Because SNS governance proposals have a multi-day voting period, an unprivileged attacker can observe a pending deposit proposal and manipulate the target DEX pool's price before execution, causing the SNS treasury to deposit at a severely unfavorable ratio and lose a large portion of the deposited value. The codebase explicitly acknowledges this risk in comments but provides no on-chain mitigation.

### Finding Description

The SNS Governance canister implements a `RegisterExtension` / `ExecuteExtensionOperation` (deposit) flow for treasury asset management via DEX liquidity pool adaptors. The deposit validation in `validate_deposit_operation_impl` checks only that the requested SNS and ICP amounts are each ≤ 50% of the current treasury balance: [1](#0-0) 

No price ratio, minimum LP token output, or slippage bound is validated at proposal creation time or at execution time. The execution path in `execute_treasury_manager_deposit` simply grants ICRC-2 allowances and calls `deposit` on the treasury manager canister with the raw token amounts: [2](#0-1) 

The `approve_treasury_manager` function grants ICRC-2 allowances to the treasury manager canister for the exact amounts specified in the proposal, with no price-ratio guard: [3](#0-2) 

The `DepositRequest` interface in the treasury manager API contains only `allowances` (token amounts) and no slippage parameters: [4](#0-3) 

The codebase explicitly acknowledges this as a known risk in the treasury manager interface definition: [5](#0-4) 

And in the proposal rendering for `RegisterExtension`: [6](#0-5) 

Despite the acknowledgment, no on-chain mitigation (minimum price ratio, slippage limit, or deadline) is enforced anywhere in the SNS Governance execution path.

### Impact Explanation

**Impact: High**

When a `RegisterExtension` or `ExecuteExtensionOperation` (deposit) proposal is executed, the SNS Governance canister grants ICRC-2 allowances for the full approved SNS and ICP amounts and calls `deposit` on the treasury manager. If the underlying DEX pool price has been manipulated before execution, the treasury manager deposits at the manipulated ratio. The attacker then swaps back, extracting value from the pool at the expense of the SNS DAO. The SNS treasury permanently loses a significant portion of the deposited funds. The comment "undeposited tokens are automatically returned to the SNS treasury account" only applies to tokens that cannot be deposited at all — tokens that are deposited at a manipulated price are not returned.

### Likelihood Explanation

**Likelihood: High**

SNS governance proposals have a multi-day voting period. The proposal is public and visible on-chain from the moment it is submitted. Any unprivileged user who can interact with the target DEX pool can observe the pending proposal, compute the expected deposit amounts, and manipulate the pool price before the proposal executes. This is a standard sandwich/front-running attack that requires no special privileges, no key compromise, and no majority corruption — only the ability to make large swaps on the DEX, which is available to any token holder.

### Recommendation

1. **Add slippage parameters to `DepositRequest`**: Extend the `DepositRequest` type in `rs/sns/treasury_manager/treasury_manager.did` to include minimum LP token output or minimum price ratio bounds. The treasury manager implementation must enforce these bounds and revert if they are not met.

2. **Enforce slippage at the SNS Governance level**: In `execute_treasury_manager_deposit` (`rs/sns/governance/src/extensions.rs`), require that the `ExecuteExtensionOperation` proposal includes slippage parameters (e.g., `min_lp_tokens_out`, `max_price_deviation_bps`). Validate these at proposal creation time and pass them through to the treasury manager at execution time.

3. **Re-validate price ratio at execution time**: Before calling `approve_treasury_manager`, query the current DEX pool price and compare it against the price at proposal creation time. Abort execution if the deviation exceeds a governance-specified threshold.

4. **Consider a time-lock or commit-reveal**: Introduce a short execution window after voting ends, during which the exact execution block is not predictable, reducing the attacker's ability to time the manipulation precisely.

### Proof of Concept

**Attacker-controlled entry path** (no privileged access required):

1. An SNS DAO submits a `RegisterExtension` proposal specifying `treasury_allocation_sns_e8s = X` and `treasury_allocation_icp_e8s = Y` to deposit into a KongSwap pool.
2. The proposal enters the governance voting period (days).
3. An attacker observes the pending proposal on-chain and performs a large swap on the KongSwap pool, skewing the SNS/ICP price ratio far from the current market price (analogous to the Uniswap `initialize(1e40)` in the reference report).
4. The proposal passes and `execute_treasury_manager_deposit` is called. `approve_treasury_manager` grants ICRC-2 allowances for the full `X` SNS tokens and `Y` ICP. The treasury manager calls `deposit` on KongSwap at the manipulated price, receiving far fewer LP tokens than expected. The excess tokens that cannot be deposited at the manipulated ratio are returned, but the deposited portion is now worth far less than `X + Y`.
5. The attacker swaps back, extracting the value difference from the pool at the expense of the SNS treasury.

The root cause is that `validate_deposit_operation_impl` checks only `sns_requested <= sns_balance / 2` and `icp_requested <= icp_balance / 2` — no price ratio is ever checked or enforced anywhere in the SNS Governance execution path. [7](#0-6)

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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
