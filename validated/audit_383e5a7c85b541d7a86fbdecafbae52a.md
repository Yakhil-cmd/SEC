### Title
Missing Slippage Protection in SNS Treasury Manager Deposit Allows Front-Running of Governance-Approved DEX Deposits - (File: rs/sns/treasury_manager/treasury_manager.did, rs/sns/governance/src/extensions.rs)

### Summary
The SNS Treasury Manager `DepositRequest` interface contains no slippage protection parameter (no minimum LP tokens out), and the governance execution path `execute_treasury_manager_deposit` enforces no price bounds before depositing SNS treasury funds into a DEX. Because governance proposals are public and their execution timing is predictable, an unprivileged attacker can front-run a passed deposit proposal by manipulating the DEX price, causing the SNS treasury to receive fewer LP tokens than expected at the time of the vote.

### Finding Description
The `DepositRequest` type in `rs/sns/treasury_manager/treasury_manager.did` only carries `allowances` (the token amounts to deposit) with no field for a minimum acceptable LP token output or price ratio:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};
``` [1](#0-0) 

The DID file itself acknowledges this as a "Known Security Risk":

> Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved. [2](#0-1) 

The governance execution path in `execute_treasury_manager_deposit` approves allowances and then calls `deposit` on the treasury manager canister with no price-bound enforcement:

```rust
governance.approve_treasury_manager(...).await?;
let balances = governance.env.call_canister(extension_canister_id, "deposit", arg_blob).await ...
``` [3](#0-2) 

The only validation performed by `validate_deposit_operation_impl` is that the requested amounts do not exceed 50% of the current treasury balance — no minimum output or price ratio check is performed: [4](#0-3) 

The proposal rendering for `RegisterExtension` explicitly warns about sandwich attacks but treats this as an accepted risk rather than enforcing a protocol-level guard: [5](#0-4) 

### Impact Explanation
When an `ExecuteExtensionOperation` proposal to deposit into a DEX passes governance, the proposal and its parameters are publicly visible on-chain. An attacker can:

1. Observe the passed proposal and the token amounts to be deposited.
2. Submit a large swap to the DEX canister to move the pool price unfavorably before the governance heartbeat executes the deposit.
3. The governance heartbeat calls `execute_treasury_manager_deposit`, which deposits at the manipulated price, yielding fewer LP tokens than the SNS community voted for.
4. The attacker reverses their swap, extracting value from the SNS treasury.

The SNS treasury suffers a direct financial loss proportional to the price impact of the manipulation. The partial mitigation noted in the DID ("undeposited tokens are automatically returned") only applies to excess tokens that cannot be deposited at the current ratio — it does not protect against receiving fewer LP tokens than expected for the tokens that are deposited.

### Likelihood Explanation
**Medium-Low.** On the Internet Computer there is no public mempool, making traditional front-running harder than on EVM chains. However, governance proposals are fully public and their execution timing is predictable (next governance heartbeat after proposal passes). Any IC user can submit transactions to the DEX canister. The attack is realistic whenever a large deposit proposal passes for a DEX with shallow liquidity. The risk is explicitly acknowledged in the codebase, indicating the developers are aware it is a real concern.

### Recommendation
**Short term:** Add a `minimum_lp_tokens_out` (or equivalent minimum price ratio) field to `DepositRequest` in `treasury_manager.did`. The governance proposal should include this bound, and `execute_treasury_manager_deposit` should pass it to the DEX call and verify the returned LP token amount meets the minimum before accepting the result.

**Long term:** Require that `ExecuteExtensionOperation` deposit proposals include an on-chain price snapshot at proposal submission time, and enforce that execution is rejected if the DEX price has deviated beyond a configurable tolerance (e.g., 1–2%) from the snapshot. This mirrors the oracle-based slippage guard recommended in the original report.

### Proof of Concept
1. An SNS passes an `ExecuteExtensionOperation` proposal with `operation_name = "deposit"` and `treasury_allocation_sns_e8s = X`, `treasury_allocation_icp_e8s = Y`.
2. Attacker observes the passed proposal on-chain.
3. Attacker calls the DEX canister (e.g., KongSwap) with a large swap that moves the SNS/ICP pool price significantly against the SNS treasury's deposit direction.
4. Governance heartbeat fires, calling `execute_treasury_manager_deposit` → `approve_treasury_manager` → `deposit` on the treasury manager → DEX `add_liquidity` at the manipulated price.
5. The SNS treasury receives LP tokens worth significantly less than `X + Y` at the pre-manipulation price.
6. Attacker reverses their swap, profiting from the price impact absorbed by the treasury deposit.
7. No IC-level check rejects or reverts the deposit because `DepositRequest` carries no `minimum_lp_tokens_out` field and `execute_treasury_manager_deposit` enforces no price bound. [6](#0-5) [1](#0-0)

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

**File:** rs/sns/governance/src/extensions.rs (L307-320)
```rust
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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
