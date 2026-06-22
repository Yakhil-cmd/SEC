### Title
SNS Treasury Manager `DepositRequest` Lacks Slippage Protection, Enabling Loss of DAO Treasury Funds - (File: rs/sns/treasury_manager/treasury_manager.did)

### Summary
The `DepositRequest` and `WithdrawRequest` types in the Treasury Manager API contain no minimum-output (slippage protection) fields. The `execute_treasury_manager_deposit` function in SNS Governance approves and deposits treasury funds into external DEX liquidity pools with no on-chain enforcement of a minimum acceptable output. The codebase itself acknowledges this as a "Known Security Risk" in two separate locations, but the API design provides no mechanism to mitigate it.

### Finding Description
The `DepositRequest` type in `rs/sns/treasury_manager/treasury_manager.did` only carries `allowances` (the amounts to deposit) and no `min_lp_tokens_out` or equivalent slippage-protection field:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};
```

Similarly, `WithdrawRequest` carries only optional `withdraw_accounts` and no `min_tokens_out`:

```candid
type WithdrawRequest = record {
  withdraw_accounts : opt vec record { principal; Account };
};
```

In `rs/sns/governance/src/extensions.rs`, `execute_treasury_manager_deposit` performs two sequential inter-canister calls:
1. `approve_treasury_manager` — grants ICRC-2 allowances to the treasury manager canister for the exact amounts voted on in the proposal.
2. `call_canister(extension_canister_id, "deposit", arg_blob)` — calls `deposit` on the treasury manager with the `DepositRequest`.

Neither step enforces any minimum output. The proposal-time validation function `validate_deposit_operation_impl` only checks that the requested amounts do not exceed 50% of the current treasury balance; it performs no minimum-output check.

The codebase explicitly acknowledges this gap in two production files:

- `rs/sns/treasury_manager/treasury_manager.did` lines 35–40 states under "Known Security Risks": *"Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved."*
- `rs/sns/governance/src/proposal.rs` lines 1542–1545 warns in the rendered proposal text: *"Some Decentralized Exchanges lack slippage protection during deposits. Consequently, deposited asset ratios may deviate from those specified in the proposal. This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running or sandwich attacks."*

The acknowledgment exists only as documentation. The API design provides no field through which the SNS DAO voters can specify or enforce a minimum acceptable output, and the governance execution path enforces none.

### Impact Explanation
An SNS DAO's treasury funds (SNS tokens + ICP) can be deposited into a DEX liquidity pool at an arbitrarily unfavorable price ratio. Because `DepositRequest` has no `min_lp_tokens_out` field and `execute_treasury_manager_deposit` enforces no minimum output, there is no on-chain mechanism to reject a deposit that returns far fewer LP tokens than expected. The result is permanent, irreversible loss of SNS treasury value with no recourse for the DAO.

### Likelihood Explanation
The likelihood is medium. SNS governance proposals require a multi-day voting period before execution. This creates a large window during which a well-capitalised DEX participant can manipulate the pool price. Additionally, `approve_treasury_manager` and the subsequent `deposit` call are separate inter-canister calls; any price movement between them is undetected. The IC's consensus ordering prevents traditional mempool frontrunning, but the multi-day voting window is a far larger and more exploitable gap.

### Recommendation
1. Add a `min_lp_tokens_out` (or equivalent) field to `DepositRequest` in `rs/sns/treasury_manager/treasury_manager.did`.
2. Add a `min_tokens_out` field to `WithdrawRequest` in `rs/sns/treasury_manager/treasury_manager.did`.
3. Update `validate_deposit_operation_impl` in `rs/sns/governance/src/extensions.rs` to validate that these minimum-output parameters are present and non-zero.
4. Update `execute_treasury_manager_deposit` to pass these parameters to the treasury manager and verify the returned output meets the minimum before considering the operation successful.
5. Require Treasury Manager implementations to enforce these minimums as a condition of NNS blessing.

### Proof of Concept
1. An SNS DAO votes to deposit 1,000 SNS tokens + 100 ICP into a DEX pool via a registered Treasury Manager. The proposal specifies `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` but, by design, no minimum LP output.
2. During the multi-day voting period, a large DEX participant manipulates the pool price by adding single-sided liquidity, skewing the token ratio.
3. The proposal passes and `execute_treasury_manager_deposit` is called. `approve_treasury_manager` grants ICRC-2 allowances for the full amounts. `deposit` is called with a `DepositRequest` containing only the allowances.
4. The treasury manager deposits at the manipulated price ratio, receiving far fewer LP tokens than the DAO expected.
5. No minimum-output check exists anywhere in the call chain to reject or revert the operation.
6. The SNS treasury permanently loses value. The attacker can then remove their single-sided liquidity, profiting from the price impact. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** rs/sns/governance/src/proposal.rs (L1540-1550)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.

## Extension Configuration

The extension will be deployed and configured according to the provided parameters.",
    ))
```
