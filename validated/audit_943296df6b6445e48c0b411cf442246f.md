### Title
Missing Slippage Protection on SNS Treasury Manager DEX Deposit — (`rs/sns/governance/src/extensions.rs`, `rs/sns/treasury_manager/treasury_manager.did`)

---

### Summary

The SNS Treasury Manager `deposit` operation, triggered by an adopted SNS governance proposal, provides no mechanism for the DAO to specify a minimum amount of LP tokens (or a minimum price ratio) to receive when depositing into a DEX liquidity pool. The `DepositRequest` type carries only input allowances; `execute_treasury_manager_deposit` approves tokens and calls `deposit` without validating the returned balances against any floor. A malicious actor who observes an adopted deposit proposal can manipulate the DEX pool price before the governance canister executes the deposit, causing the SNS treasury to receive fewer LP tokens than the DAO voted to accept.

---

### Finding Description

The `DepositRequest` type in the Treasury Manager API contains only `allowances` — the amounts to deposit — with no `min_lp_tokens_out` or `min_price_ratio` field: [1](#0-0) 

The API specification itself acknowledges this as a known security risk: [2](#0-1) 

The SNS governance proposal renderer also warns about this in `validate_and_render_register_extension`: [3](#0-2) 

The execution path in `execute_treasury_manager_deposit` approves the full token allowance and calls `deposit` on the Treasury Manager canister, then logs the returned balances — but never checks them against any minimum: [4](#0-3) 

The `ValidatedDepositOperationArg` struct only parses and validates `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` (input amounts). There is no `min_lp_tokens_out` field parsed or enforced anywhere: [5](#0-4) 

The validation step (`validate_deposit_operation_impl`) only checks that the requested amounts do not exceed 50% of the current treasury balance. It does not check any expected output: [6](#0-5) 

---

### Impact Explanation

An SNS DAO votes on a `RegisterExtension` or `ExecuteExtensionOperation` (deposit) proposal specifying exact amounts of SNS tokens and ICP to deposit into a DEX liquidity pool. Between proposal adoption and execution (which spans the voting period plus any execution delay), a malicious actor can:

1. Observe the adopted proposal (all SNS proposals are public).
2. Submit transactions to the DEX to manipulate the pool's price ratio (e.g., by swapping a large amount of one token to skew the ratio).
3. When the governance canister executes `execute_treasury_manager_deposit`, the SNS treasury deposits at the manipulated ratio, receiving significantly fewer LP tokens than the DAO intended.
4. The attacker reverses their manipulation trade, profiting from the arbitrage at the DAO's expense.

The SNS treasury permanently loses value — the LP tokens received represent a smaller share of the pool than the DAO voted to accept. The `approve_treasury_manager` call grants the full allowance unconditionally: [7](#0-6) 

---

### Likelihood Explanation

SNS proposals are fully public and have a mandatory voting period (typically days). The window between proposal adoption and execution is observable by any on-chain participant. On the Internet Computer, while block-level transaction reordering is not possible, any canister or user can submit transactions to a DEX during the window between proposal adoption and governance execution. The attack requires no privileged access — only the ability to interact with the same DEX the Treasury Manager targets. The codebase itself explicitly documents this as a "Known Security Risk," confirming the attack surface is recognized and unmitigated at the protocol level.

---

### Recommendation

1. Add a `min_lp_tokens_out` (or equivalent `min_price_ratio`) field to `DepositRequest` in `treasury_manager.did`.
2. In `execute_treasury_manager_deposit`, after calling `deposit`, decode the returned `Balances` and verify that the LP tokens received by `external_custodian` meet the minimum specified in the proposal.
3. In `ValidatedDepositOperationArg`, parse and validate the `min_lp_tokens_out` field from the proposal's `Precise` map.
4. If the minimum is not met, revert the deposit (or trigger a withdrawal) and fail the proposal execution with a descriptive error.

---

### Proof of Concept

1. An SNS DAO adopts a `ExecuteExtensionOperation` deposit proposal specifying `treasury_allocation_sns_e8s = X` and `treasury_allocation_icp_e8s = Y` to deposit into a DEX pool.
2. Attacker observes the adopted proposal on-chain.
3. Attacker calls the DEX directly, swapping a large amount of ICP for SNS tokens, skewing the pool ratio so SNS tokens are now overpriced relative to ICP.
4. SNS governance canister executes `execute_treasury_manager_deposit`:
   - `approve_treasury_manager` grants allowance of `X` SNS and `Y` ICP to the Treasury Manager.
   - Treasury Manager calls the DEX `deposit` at the manipulated ratio.
   - The SNS treasury receives LP tokens representing a fraction of the pool value it should have received.
5. Attacker reverses their swap, restoring the pool ratio and pocketing the arbitrage profit.
6. `execute_treasury_manager_deposit` logs the returned balances and returns `Ok(())` — no check is performed against any minimum output. [8](#0-7)

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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
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

**File:** rs/sns/governance/src/extensions.rs (L1566-1609)
```rust
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
