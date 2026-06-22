### Title
Inadequate Slippage Control in SNS Treasury Manager Withdraw/Deposit API — (File: `rs/sns/treasury_manager/treasury_manager.did`, `rs/sns/governance/src/extensions.rs`)

### Summary
The SNS Treasury Manager API's `WithdrawRequest` and `DepositRequest` types contain no minimum received amount (slippage bound) parameters. Additionally, `ValidatedWithdrawOperationArg` in `rs/sns/governance/src/extensions.rs` explicitly rejects any non-empty arguments passed to the withdraw operation. This means the SNS governance layer has no protocol-level mechanism to enforce slippage bounds when executing treasury withdrawals from DEX liquidity pools, leaving SNS treasury funds exposed to unfavorable exchange rates between proposal approval and execution.

### Finding Description

The `WithdrawRequest` type in `rs/sns/treasury_manager/treasury_manager.did` contains only an optional `withdraw_accounts` field and no minimum received amount parameters: [1](#0-0) 

Similarly, `DepositRequest` contains only `allowances` with no slippage bounds: [2](#0-1) 

The `treasury_manager.did` itself explicitly acknowledges the slippage risk for deposits: [3](#0-2) 

In `rs/sns/governance/src/extensions.rs`, `ValidatedWithdrawOperationArg::try_from` explicitly rejects any non-empty arguments, making it impossible for a governance proposal to pass slippage bounds to the withdraw operation: [4](#0-3) 

The `construct_treasury_manager_withdraw_payload` function always constructs a `WithdrawRequest` with no minimum amounts and ignores its `_value: Precise` argument entirely: [5](#0-4) 

The `execute_treasury_manager_withdraw` function calls this payload constructor and forwards the result directly to the Treasury Manager canister with no slippage enforcement: [6](#0-5) 

The `validate_and_render_register_extension` function in `rs/sns/governance/src/proposal.rs` even warns voters that DEX deposits are "vulnerable to front-running or sandwich attacks," but no analogous protection is enforced at the protocol level for withdrawals: [7](#0-6) 

### Impact Explanation

When an SNS Treasury Manager implementation withdraws liquidity from a DEX canister, there is no protocol-level mechanism to specify minimum received amounts. Because `ValidatedWithdrawOperationArg` rejects any arguments, SNS token holders cannot specify slippage bounds in governance proposals, and the Treasury Manager implementation cannot receive them from the governance layer. A DEX canister that manipulates its internal state upon receiving the `withdraw` call, or simply adverse price movements between proposal approval and execution, can result in the SNS treasury receiving significantly fewer tokens than expected. The SNS treasury — holding real ICP and SNS tokens — is the direct victim, with no recourse after execution.

### Likelihood Explanation

The SNS Treasury Manager framework is production code actively integrated into SNS governance. Any Treasury Manager implementation that interacts with a DEX to remove liquidity is affected. The SNS governance voting period can span multiple days, making price movements between proposal approval and execution common and potentially large. A DEX canister that is not fully DAO-controlled (or whose DAO is compromised) could also exploit this by manipulating its internal price state at the moment the `withdraw` call arrives. The `treasury_manager.did` itself acknowledges this class of risk, confirming it is a realistic threat.

### Recommendation

1. Add `min_amount_a` and `min_amount_b` (or equivalent) fields to `WithdrawRequest` in `rs/sns/treasury_manager/treasury_manager.did`.
2. Update `ValidatedWithdrawOperationArg` in `rs/sns/governance/src/extensions.rs` to accept and validate slippage bound arguments instead of rejecting all non-empty arguments.
3. Update `construct_treasury_manager_withdraw_payload` to pass slippage bounds through to the Treasury Manager canister.
4. Require Treasury Manager implementations to enforce these bounds when calling DEX canisters, analogous to how `amountAMin`/`amountBMin` are used in EVM-based DEX routers.
5. Apply the same fix to `DepositRequest` and the deposit path, which the `treasury_manager.did` already acknowledges as vulnerable.

### Proof of Concept

1. An SNS governance proposal is submitted to withdraw liquidity from a DEX via the Treasury Manager (`TreasuryManagerWithdraw` operation).
2. The proposal passes after the voting period (potentially several days).
3. Between proposal approval and execution, the DEX price moves adversely — or a DEX canister manipulates its internal state upon receiving the call.
4. `execute_treasury_manager_withdraw` in `rs/sns/governance/src/extensions.rs` calls `construct_treasury_manager_withdraw_payload`, which constructs a `WithdrawRequest { withdraw_accounts: None }` with no minimum amounts.
5. The Treasury Manager calls the DEX to remove liquidity with no slippage protection enforced at the protocol level.
6. The SNS treasury receives far fewer tokens than expected. Because `ValidatedWithdrawOperationArg` rejects any arguments, there was no way for the governance proposal to have specified slippage bounds in the first place.

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

**File:** rs/sns/treasury_manager/treasury_manager.did (L88-93)
```text
type WithdrawRequest = record {
  // Maps Ledger canister IDs of assets to be withdrawn to the respective withdraw accounts.
  //
  // If not set, accounts specified at the time of deposit will be used for the withdrawal.
  withdraw_accounts : opt vec record { principal; Account };
};
```

**File:** rs/sns/governance/src/extensions.rs (L1102-1110)
```rust
fn construct_treasury_manager_withdraw_payload(_value: Precise) -> Result<Vec<u8>, String> {
    let arg = WithdrawRequest {
        withdraw_accounts: None,
    };
    let arg =
        candid::encode_one(&arg).map_err(|err| format!("Error encoding WithdrawRequest: {err}"))?;

    Ok(arg)
}
```

**File:** rs/sns/governance/src/extensions.rs (L1612-1628)
```rust
/// Execute a treasury manager withdraw operation
async fn execute_treasury_manager_withdraw(
    governance: &Governance,
    extension_canister_id: CanisterId,
    arg: ValidatedWithdrawOperationArg,
) -> Result<(), GovernanceError> {
    let arg_blob = construct_treasury_manager_withdraw_payload(arg.original).map_err(|err| {
        GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!("Failed to construct treasury manager withdraw payload: {err}"),
        )
    })?;

    let balances = governance
        .env
        .call_canister(extension_canister_id, "withdraw", arg_blob)
        .await
```

**File:** rs/sns/governance/src/extensions.rs (L1733-1747)
```rust
impl TryFrom<Option<Precise>> for ValidatedWithdrawOperationArg {
    type Error = String;

    fn try_from(value: Option<Precise>) -> Result<Self, Self::Error> {
        let original = value.unwrap_or_default();

        // For now, only allow empty arguments
        // This ensures withdraw operations don't accept parameters yet
        if original.value.is_some() {
            return Err("Withdraw operation does not accept arguments at this time".to_string());
        }

        Ok(Self { original })
    }
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
