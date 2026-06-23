### Title
Missing Slippage Parameter in SNS Treasury Manager `DepositRequest` Enables Sandwich Attacks on DAO Treasury Deposits - (File: rs/sns/treasury_manager/treasury_manager.did)

---

### Summary

The SNS Treasury Manager's `DepositRequest` type, defined in the IC codebase, contains no slippage protection parameter. When an SNS DAO executes a treasury deposit into a DEX via an `ExecuteExtensionOperation` governance proposal, the deposit is forwarded to the treasury manager canister without any minimum LP token guarantee or price ratio constraint. Because governance proposals are public and their execution is predictable, an attacker who monitors the SNS governance canister can front-run the deposit by manipulating the DEX pool price, causing the DAO treasury to receive fewer LP tokens than expected at the time the proposal was approved.

---

### Finding Description

The `DepositRequest` type in `rs/sns/treasury_manager/treasury_manager.did` specifies only the token allowances to deposit, with no field for a minimum acceptable output (LP tokens) or slippage tolerance:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};
``` [1](#0-0) 

The `Allowance` type itself only encodes the asset and the amount to deposit, with no minimum-output field:

```candid
type Allowance = record {
  asset : Asset;
  amount_decimals : nat;
  owner_account : Account;
};
``` [2](#0-1) 

The `execute_treasury_manager_deposit` function in `rs/sns/governance/src/extensions.rs` constructs this payload via `construct_treasury_manager_deposit_payload` and calls `deposit` on the treasury manager canister. No minimum LP token amount or price ratio is included at any point in this call chain:

```rust
// 2. Call deposit on treasury manager
let balances = governance
    .env
    .call_canister(extension_canister_id, "deposit", arg_blob)
    ...
``` [3](#0-2) 

The `construct_treasury_manager_deposit_payload` function only encodes the `allowances` (amounts to deposit) into the `DepositRequest`, with no slippage field: [4](#0-3) 

The IC codebase itself acknowledges this gap explicitly in `treasury_manager.did`:

```
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
``` [5](#0-4) 

The proposal rendering function `validate_and_render_register_extension` in `rs/sns/governance/src/proposal.rs` also includes a WARNING about this risk in the rendered proposal text, but provides no mechanism to mitigate it — no slippage field exists in the `DepositRequest` type for the governance canister to populate: [6](#0-5) 

The same warning is absent from the `ExecuteExtensionOperation` (top-up deposit) proposal rendering path (`validate_and_render_execute_extension_operation`), meaning voters approving a follow-on deposit proposal receive no warning at all: [7](#0-6) 

---

### Impact Explanation

An SNS DAO that registers a Treasury Manager extension and deposits treasury funds (SNS tokens + ICP) into a DEX liquidity pool has no on-chain guarantee about the LP token ratio it will receive. Because the `DepositRequest` carries no `min_lp_tokens` or `min_price_ratio` field, the treasury manager canister cannot enforce any slippage bound on behalf of the DAO. An attacker who observes the adopted governance proposal can manipulate the DEX pool price before the deposit is executed, causing the DAO to receive significantly fewer LP tokens than the price at proposal adoption implied. The attacker then reverses their position after the deposit, extracting value directly from the SNS treasury. This is a direct, quantifiable loss of DAO treasury funds.

---

### Likelihood Explanation

IC governance proposals are fully public. After a proposal is adopted, its execution is deterministic and predictable (it runs in the next heartbeat/timer cycle of the governance canister). Any canister or user who monitors the SNS governance canister can observe the exact deposit amounts and timing. On the IC, there is no traditional mempool, but the attacker can submit their DEX manipulation transaction in the same or immediately preceding round. The attack requires no privileged access — only the ability to interact with the DEX canister as an unprivileged caller. The risk is elevated for SNS DAOs that use popular DEXes (e.g., KongSwap, as referenced in the integration tests) where pool liquidity is shallow relative to the deposit size.

---

### Recommendation

**Short term:** Add an optional `min_lp_tokens` (or equivalent `min_price_ratio`) field to `DepositRequest` in `rs/sns/treasury_manager/treasury_manager.did`. The SNS governance proposal payload (`ExecuteExtensionOperation` / `RegisterExtension`) should allow the proposer to encode this minimum, and the treasury manager canister must enforce it before executing the DEX deposit. The `validate_and_render_execute_extension_operation` rendering path should also display the same slippage warning shown in `validate_and_render_register_extension`.

**Long term:** Consider requiring all `TreasuryManager` implementations to enforce a slippage bound as part of the NNS blessing criteria. The `treasury_manager.did` spec should mandate a `min_output` field in `DepositRequest` rather than treating slippage protection as an optional implementation detail.

---

### Proof of Concept

1. An SNS DAO adopts a `RegisterExtension` or `ExecuteExtensionOperation` proposal to deposit `X` SNS tokens and `Y` ICP into a DEX pool via a blessed Treasury Manager canister (e.g., KongSwap adaptor).
2. The proposal is public. An attacker observes the adopted proposal and the exact deposit amounts.
3. Before the governance canister executes the deposit, the attacker calls the DEX canister directly to buy a large amount of one token, skewing the pool price.
4. The governance canister calls `execute_treasury_manager_deposit` → `construct_treasury_manager_deposit_payload` → `DepositRequest { allowances: [...] }` → `deposit` on the treasury manager. No slippage bound is present in the request.
5. The treasury manager deposits into the DEX at the manipulated price, receiving far fewer LP tokens than the DAO expected.
6. The attacker sells their position, restoring the pool price and pocketing the spread — at the SNS treasury's expense.
7. The governance canister logs success (`TreasuryManager.deposit succeeded`) with no awareness that the received LP tokens were below any acceptable threshold. [8](#0-7) [1](#0-0) [5](#0-4)

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

**File:** rs/sns/treasury_manager/treasury_manager.did (L48-54)
```text
type Allowance = record {
  asset : Asset;
  amount_decimals : nat;

  // Needed to refund excess assets that cannot be managed at this time.
  owner_account : Account;
};
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L84-86)
```text
type DepositRequest = record {
  allowances : vec Allowance;
};
```

**File:** rs/sns/governance/src/extensions.rs (L1087-1099)
```rust
/// Returns `arg_blob` in the Ok result.
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

**File:** rs/sns/governance/src/proposal.rs (L1484-1504)
```rust
async fn validate_and_render_execute_extension_operation(
    governance: &crate::governance::Governance,
    execute: &ExecuteExtensionOperation,
) -> Result<String, String> {
    let ValidatedExecuteExtensionOperation {
        extension_canister_id,
        operation_name,
        arg,
    } = validate_execute_extension_operation(governance, execute.clone())
        .await
        .map_err(|err| err.error_message)?;

    Ok(format!(
        r"# Proposal to execute extension operation:

* Extension canister ID: `{extension_canister_id}`
* Operation name: `{operation_name}`
* Operation argument: `{arg}`
#"
    ))
}
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
