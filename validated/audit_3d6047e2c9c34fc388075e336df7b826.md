### Title
Missing Slippage Protection in SNS Treasury Manager `DepositRequest` Interface Enables Price-Manipulation Loss of DAO Treasury Funds - (File: `rs/sns/treasury_manager/treasury_manager.did`, `rs/sns/governance/src/extensions.rs`)

---

### Summary

The `DepositRequest` type in the SNS Treasury Manager interface contains no `min_lp_tokens_out` or equivalent slippage-protection field. The governance execution path (`execute_treasury_manager_deposit`) approves and forwards treasury tokens to a DEX extension canister without validating any minimum amount of LP tokens to be received. Because governance proposals are public and have multi-day voting periods, an attacker can manipulate the DEX pool price before execution, causing the SNS treasury to deposit at a severely unfavorable ratio with no on-chain protection. The codebase itself acknowledges this as a "Known Security Risk" but provides no enforcement mechanism.

---

### Finding Description

The `DepositRequest` type defined in the Treasury Manager interface specification has no slippage-protection field:

```
type DepositRequest = record {
  allowances : vec Allowance;
};
``` [1](#0-0) 

The file itself explicitly documents the gap:

> Known Security Risks: Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved. [2](#0-1) 

The governance execution function `execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` approves the treasury manager to spend SNS and ICP tokens, then calls `deposit` on the extension canister. The returned `balances` value is only logged — it is never validated against any minimum LP token threshold:

```rust
// 1. Transfer funds from treasury to treasury manager
governance
    .approve_treasury_manager(extension_canister_id, treasury_allocation_sns_e8s, treasury_allocation_icp_e8s)
    .await?;

// 2. Call deposit on treasury manager
let balances = governance
    .env
    .call_canister(extension_canister_id, "deposit", arg_blob)
    ...
log!(INFO, "TreasuryManager.deposit succeeded with response: {:?}", balances);
Ok(())
``` [3](#0-2) 

The proposal validation step (`validate_deposit_operation_impl`) only checks that the requested amount does not exceed 50% of the current treasury balance. It performs no check on the minimum LP tokens to be received: [4](#0-3) 

The proposal rendering function `validate_and_render_register_extension` includes a human-readable warning about this risk, but this warning is informational only — it does not enforce any on-chain constraint: [5](#0-4) 

---

### Impact Explanation

An SNS DAO that uses the KongSwap adaptor (or any future Treasury Manager extension) to deposit treasury funds into a DEX liquidity pool can have those funds deposited at an arbitrarily unfavorable price ratio. Because no `min_lp_tokens_out` is enforced anywhere in the IC governance or Treasury Manager interface, the deposit will succeed even if the attacker has moved the pool price to extract maximum value. The SNS treasury (holding real ICP and SNS tokens) suffers a direct, irreversible financial loss. The `approve_treasury_manager` call grants an ICRC-2 allowance that the extension canister can spend; once spent at the manipulated price, the loss cannot be recovered through the governance path. [6](#0-5) 

---

### Likelihood Explanation

Governance proposals on the SNS are public and have voting periods measured in days. Any observer can see that a deposit proposal will execute, giving ample time to manipulate the target DEX pool. On the Internet Computer there is no traditional mempool, but the attack does not require mempool-level frontrunning: the attacker simply needs to submit a large trade to the DEX pool at any point during the voting period (or immediately before the proposal round executes), skewing the price ratio. The KongSwap backend is already integrated and tested as the reference Treasury Manager implementation. [7](#0-6) 

---

### Recommendation

1. **Extend `DepositRequest`** in `treasury_manager.did` to include a `min_lp_tokens_out : opt nat` field (or equivalent per-asset minimum-received fields), allowing the SNS governance proposal to encode the minimum acceptable LP token receipt at proposal creation time.

2. **Validate the deposit result** in `execute_treasury_manager_deposit`: after calling `deposit`, decode the returned `Balances` and compare the `external_custodian` balance increment against the `min_lp_tokens_out` specified in the proposal. Revert (return `Err`) if the received amount is below the minimum.

3. **Enforce the minimum in `validate_deposit_operation_impl`**: require that a non-zero `min_lp_tokens_out` is provided in the proposal argument, rejecting proposals that omit it.

---

### Proof of Concept

1. An SNS DAO submits a governance proposal: `ExecuteExtensionOperation { operation_name: "deposit", operation_arg: { treasury_allocation_sns_e8s: 1_000_000_000, treasury_allocation_icp_e8s: 500_000_000 } }`.
2. The proposal is public. During the multi-day voting period, an attacker submits a large swap on KongSwap that moves the SNS/ICP price ratio significantly (e.g., dumps SNS tokens to make SNS cheap relative to ICP).
3. The proposal passes and `execute_treasury_manager_deposit` runs: `approve_treasury_manager` grants the extension canister an ICRC-2 allowance for the full amounts; `deposit` is called with no minimum LP token constraint.
4. The KongSwap adaptor deposits at the manipulated ratio. The SNS treasury receives far fewer LP tokens than it would have at the pre-manipulation price.
5. The attacker reverses their trade (sandwich), profiting from the spread. The SNS treasury has permanently lost value with no on-chain recourse. [8](#0-7) [1](#0-0)

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

**File:** rs/sns/governance/src/extensions.rs (L777-830)
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

**File:** MODULE.bazel (L552-558)
```text
# SNS-KongSwap Adaptor canister (an SNS extension of the TreasuryManager kind)

http_file(
    name = "kongswap-adaptor-canister",
    downloaded_file_path = "kongswap-adaptor-canister.wasm.gz",
    sha256 = "1c07ceba560e7bcffa43d1b5ae97db81151854f068b707c1728e213948212a6c",
    url = "https://github.com/dfinity/sns-kongswap-adaptor/releases/download/v1.0.0/kongswap-adaptor-canister.wasm.gz",
```
