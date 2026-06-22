### Title
Missing Slippage Protection in SNS Treasury Manager Deposit Operation — (File: `rs/sns/governance/src/extensions.rs`)

### Summary
The `execute_treasury_manager_deposit` function in SNS Governance approves and deposits fixed token amounts into a DEX-backed Treasury Manager without any slippage protection. The `ValidatedDepositOperationArg` struct and the `DepositRequest` interface in `treasury_manager.did` contain no minimum acceptable price ratio or maximum slippage tolerance fields. Between proposal approval and execution — a window spanning multiple days — an adversary can manipulate the DEX pool price, causing the SNS treasury to deposit at an unfavorable ratio. The codebase itself explicitly acknowledges this as a "Known Security Risk."

### Finding Description
`execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` executes a two-step deposit:

1. Calls `approve_treasury_manager` with fixed `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` amounts.
2. Calls `deposit` on the treasury manager canister. [1](#0-0) 

The `ValidatedDepositOperationArg` struct only contains the two token amounts and the raw payload — no `min_price_ratio`, `max_slippage_bps`, or any other slippage protection field: [2](#0-1) 

The `DepositRequest` type in `treasury_manager.did` similarly only contains `allowances` with no slippage parameters: [3](#0-2) 

The governance proposal execution path (`perform_execute_extension_operation` → `validated_operation.execute()` → `execute_treasury_manager_deposit`) performs no price ratio check at execution time: [4](#0-3) 

The codebase explicitly acknowledges this risk in two places. First, in `treasury_manager.did`: [5](#0-4) 

Second, in the proposal rendering for `RegisterExtension`: [6](#0-5) 

The root cause is in the IC governance code itself: the `ValidatedDepositOperationArg` struct and the `DepositRequest` interface do not support slippage protection parameters, so even if a Treasury Manager implementation wanted to enforce a price ratio, the governance proposal format provides no mechanism for voters to specify one.

### Impact Explanation
An SNS DAO's treasury funds (ICP and SNS tokens) can be deposited into a DEX liquidity pool at a significantly worse price ratio than what was implicitly expected at proposal approval time. The DAO receives fewer LP tokens or a worse position than expected, resulting in a direct, irreversible financial loss to the SNS treasury. The magnitude of loss scales with the size of the deposit and the degree of price manipulation.

### Likelihood Explanation
SNS governance proposals have a voting period of several days. During this window, a sophisticated adversary can:
1. Monitor for pending `ExecuteExtensionOperation` deposit proposals on-chain.
2. Manipulate the DEX pool price (e.g., via large trades) to create an unfavorable SNS/ICP ratio.
3. Wait for the proposal to execute at the manipulated price.
4. Reverse the manipulation after execution to extract profit (sandwich attack).

The codebase explicitly labels this a "Known Security Risk" and warns of "front-running or sandwich attacks" in the proposal rendering code, confirming the developers are aware of the exposure. The `approve_treasury_manager` function grants a 1-hour expiry allowance, but the deposit call happens immediately after approval within the same execution, so the expiry does not mitigate the price manipulation window. [7](#0-6) 

### Recommendation
Add slippage protection parameters to `ValidatedDepositOperationArg` and the `DepositRequest` interface in `treasury_manager.did`:
- `min_sns_per_icp_ratio_e8s`: minimum acceptable SNS/ICP price ratio at execution time.
- `max_slippage_bps`: maximum acceptable price impact in basis points.

The `execute_treasury_manager_deposit` function should query the current DEX price and validate it against these limits before calling `approve_treasury_manager` and `deposit`. If the price has moved beyond the specified tolerance, the execution should fail with a descriptive error, allowing the DAO to resubmit with updated parameters.

### Proof of Concept
1. An SNS governance proposal is submitted: deposit 1,000 SNS + 100 ICP into a DEX pool (implied ratio: 10 SNS/ICP).
2. The proposal enters the voting period (several days).
3. An adversary monitors the pending proposal and executes large SNS purchases from the DEX pool, pushing the price to 20 SNS/ICP.
4. The proposal passes and `execute_treasury_manager_deposit` is called with `treasury_allocation_sns_e8s = 1_000_e8s` and `treasury_allocation_icp_e8s = 100_e8s`.
5. `approve_treasury_manager` grants the treasury manager a 1-hour allowance for both amounts.
6. `deposit` is called; the DEX deposits at the manipulated 20 SNS/ICP ratio — the SNS treasury receives LP tokens representing a position worth significantly less than the deposited value.
7. The adversary sells SNS back to the pool, profiting from the price manipulation while the SNS DAO absorbs the loss.

### Citations

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

**File:** rs/sns/governance/src/extensions.rs (L1545-1573)
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
```

**File:** rs/sns/governance/src/extensions.rs (L1663-1708)
```rust
/// Validated deposit operation arguments
#[derive(Debug, Clone)]
pub struct ValidatedDepositOperationArg {
    /// Amount of SNS tokens to allocate from treasury
    pub treasury_allocation_sns_e8s: u64,
    /// Amount of ICP tokens to allocate from treasury
    pub treasury_allocation_icp_e8s: u64,
    /// Original Precise value with all fields
    pub original: Precise,
}

impl TryFrom<Option<Precise>> for ValidatedDepositOperationArg {
    type Error = String;

    fn try_from(value: Option<Precise>) -> Result<Self, Self::Error> {
        let Some(original) = value else {
            return Err("Deposit operation arguments must be provided".to_string());
        };

        let map = match &original.value {
            Some(precise::Value::Map(PreciseMap { map })) => map,
            _ => return Err("Deposit operation arguments must be a PreciseMap".to_string()),
        };

        let treasury_allocation_sns_e8s = map
            .get("treasury_allocation_sns_e8s")
            .and_then(|p| match &p.value {
                Some(precise::Value::Nat(n)) => Some(*n),
                _ => None,
            })
            .ok_or_else(|| "treasury_allocation_sns_e8s must be a Nat value".to_string())?;

        let treasury_allocation_icp_e8s = map
            .get("treasury_allocation_icp_e8s")
            .and_then(|p| match &p.value {
                Some(precise::Value::Nat(n)) => Some(*n),
                _ => None,
            })
            .ok_or_else(|| "treasury_allocation_icp_e8s must be a Nat value".to_string())?;

        Ok(Self {
            treasury_allocation_sns_e8s,
            treasury_allocation_icp_e8s,
            original,
        })
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

**File:** rs/sns/governance/src/governance.rs (L2558-2576)
```rust
    async fn perform_execute_extension_operation(
        &self,
        execute_extension_operation: ExecuteExtensionOperation,
    ) -> Result<(), GovernanceError> {
        // Check if SNS extensions are enabled
        if !crate::is_sns_extensions_enabled() {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "SNS extensions are not enabled",
            ));
        }

        let validated_operation =
            validate_execute_extension_operation(self, execute_extension_operation).await?;

        // Execute the validated operation
        validated_operation.execute(self).await?;

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
