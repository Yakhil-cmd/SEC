### Title
SNS TreasuryManager Deposit to DEX Lacks Slippage Enforcement, Enabling Front-Running/Sandwich Attacks on SNS Treasury Funds - (File: `rs/sns/governance/src/extensions.rs`)

### Summary

The SNS extension framework's `execute_treasury_manager_deposit` function deposits SNS treasury tokens into an external DEX via a `TreasuryManager` extension canister without any slippage protection enforced at the governance layer. The `DepositRequest` interface defined in `treasury_manager.did` contains no slippage fields. Because governance proposals and their execution timing are fully public, an unprivileged attacker with sufficient DEX liquidity can front-run or sandwich the deposit, causing the SNS treasury to receive fewer LP tokens than expected while the attacker profits.

### Finding Description

The `execute_treasury_manager_deposit` function in `rs/sns/governance/src/extensions.rs` is the on-chain execution path triggered when an SNS governance proposal of type `ExecuteExtensionOperation` with operation `TreasuryManagerDeposit` is adopted and executed. [1](#0-0) 

The function:
1. Calls `approve_treasury_manager` to grant ICRC-2 allowances over SNS and ICP treasury funds.
2. Calls `deposit` on the extension canister, passing a `DepositRequest` blob.

The `DepositRequest` type, defined in the `treasury_manager.did` interface, contains only `allowances` — no `min_lp_tokens_out`, no `max_price_impact`, no slippage bound of any kind: [2](#0-1) 

The codebase itself acknowledges this as a known risk: [3](#0-2) 

And the proposal rendering code for `RegisterExtension` explicitly warns voters: [4](#0-3) 

The same absence of slippage enforcement applies to the `ExecuteExtensionOperation` deposit path, which is the live execution path after a proposal passes.

The `EXTENSION_OPERATION_SPECS` map confirms `TreasuryManagerDeposit` is a supported, live operation: [5](#0-4) 

### Impact Explanation

An attacker who observes a passed `TreasuryManagerDeposit` proposal (all SNS proposals are public) can:

1. Manipulate the DEX pool price before the deposit executes (e.g., by swapping a large amount of one token to skew the pool ratio).
2. The governance canister executes `execute_treasury_manager_deposit`, which calls `deposit` on the TreasuryManager extension with no slippage bound.
3. The deposit executes at the manipulated price, and the SNS treasury receives significantly fewer LP tokens than the fair-market equivalent.
4. The attacker reverses their position, extracting value directly from the SNS treasury.

The financial loss is bounded only by the size of the treasury allocation approved in the proposal (`treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s`), which can be substantial for large SNS DAOs. The "undeposited tokens are automatically returned" note in the warning only applies to tokens that fail to deposit at all — it does not protect against value loss from a successful deposit at a manipulated price.

### Likelihood Explanation

Likelihood is **medium-high**:

- SNS governance proposals and their execution timing are fully public and predictable (voting periods have fixed deadlines).
- Any unprivileged user with sufficient capital to move a DEX pool can execute this attack — no privileged access, no key compromise, no governance majority is required.
- The IC's asynchronous execution model means there is a window between proposal adoption and execution during which the attacker can act.
- The KongSwap adaptor is already referenced in integration tests as the target DEX, confirming this is a production-intended flow. [6](#0-5) 

### Recommendation

1. Add slippage parameters to the `DepositRequest` type in `treasury_manager.did` (e.g., `min_lp_tokens_out : opt nat` or `max_price_impact_bps : opt nat`).
2. Require that `execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` passes caller-specified slippage bounds to the extension canister's `deposit` method.
3. Require that `RegisterExtension` and `ExecuteExtensionOperation` proposals include slippage parameters as mandatory fields, validated during proposal submission.
4. Require that blessed `TreasuryManager` implementations enforce the slippage bound when interacting with the underlying DEX.

### Proof of Concept

1. An SNS DAO passes an `ExecuteExtensionOperation` proposal with `operation_name = "deposit"` and `treasury_allocation_icp_e8s = 10_000_000_000` (100 ICP) and `treasury_allocation_sns_e8s = 50_000_000_000`.
2. The proposal voting period ends; execution is imminent (public information).
3. An attacker swaps a large amount of ICP for SNS tokens on the KongSwap pool, skewing the pool ratio significantly.
4. The IC governance canister executes `execute_treasury_manager_deposit`:
   - `approve_treasury_manager` grants allowances over 100 ICP and 50B SNS tokens.
   - `call_canister(extension_canister_id, "deposit", arg_blob)` is called with no slippage bound.
5. The TreasuryManager deposits at the manipulated price; the SNS treasury receives LP tokens worth far less than 100 ICP + 50B SNS tokens at fair market value.
6. The attacker reverses their swap, profiting from the price impact absorbed by the SNS treasury deposit. [7](#0-6)

### Citations

**File:** rs/sns/governance/src/extensions.rs (L323-351)
```rust
// This map contains the ExtensionOperationSpecs for operations supported by governance.
lazy_static! {
    pub static ref EXTENSION_OPERATION_SPECS: BTreeMap<String, ExtensionOperationSpec> = {
        let specs = vec![
            ExtensionOperationSpec {
                operation_type: OperationType::TreasuryManagerDeposit,
                description: "Deposit funds into the treasury manager.".to_string(),
                extension_type: ExtensionType::TreasuryManager,
                topic: Topic::TreasuryAssetManagement,
            },
            ExtensionOperationSpec {
                operation_type: OperationType::TreasuryManagerWithdraw,
                description: "Withdraw funds from the treasury manager.".to_string(),
                extension_type: ExtensionType::TreasuryManager,
                topic: Topic::TreasuryAssetManagement,
            },
        ];

        let mut map = BTreeMap::new();
        for spec in specs {
            let key = spec.name();
            assert!(
                !map.contains_key(&key),
                "Duplicate operation name detected: '{key}'. Each operation must have a unique name."
            );
            map.insert(key, spec);
        }
        map
    };
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

**File:** rs/nervous_system/integration_tests/tests/sns_extension_test.rs (L228-232)
```rust

        let wasm_path = std::env::var("KONGSWAP_ADAPTOR_CANISTER_WASM_PATH")
            .expect("KONGSWAP_ADAPTOR_CANISTER_WASM_PATH must be set.");

        let wasm_path = PathBuf::from(wasm_path);
```
