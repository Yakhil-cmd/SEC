### Title
SNS Treasury Manager Deposit Lacks Slippage Protection, Enabling Front-Running/Sandwich Attacks on DEX Liquidity Pool Deposits - (File: rs/sns/governance/src/extensions.rs)

---

### Summary

The SNS Treasury Manager framework's `execute_treasury_manager_deposit` function approves ICRC-2 allowances and calls `deposit` on a DEX-backed treasury manager canister with no minimum price ratio, no slippage bound, and no oracle price check. Any observer of a pending or adopted `ExecuteExtensionOperation` governance proposal can front-run the deposit by manipulating the DEX pool price, causing the SNS treasury to receive fewer LP tokens than expected at a fair market price, and then back-run to extract the difference as profit.

---

### Finding Description

`execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` executes in two steps:

1. `approve_treasury_manager` — issues ICRC-2 allowances on both the SNS and ICP ledgers to the treasury manager canister, with a 1-hour expiry.
2. `call_canister(..., "deposit", arg_blob)` — calls the treasury manager's `deposit` method. [1](#0-0) 

The `DepositRequest` passed to `deposit` contains only `allowances` (token amounts and refund accounts). There is no field for a minimum acceptable price ratio, maximum slippage, or oracle-derived belief price: [2](#0-1) 

The `ValidatedDepositOperationArg` validation only checks that the requested amounts do not exceed 50% of the current treasury balance. It performs no price or ratio validation: [3](#0-2) 

The framework itself acknowledges this gap explicitly in two places. In the interface definition: [4](#0-3) 

And in the proposal rendering warning shown to voters: [5](#0-4) 

Despite the acknowledgment, the framework provides no mechanism to enforce a minimum price ratio at execution time, and the `approve_treasury_manager` function sets allowances unconditionally: [6](#0-5) 

---

### Impact Explanation

An attacker who observes a pending or adopted `ExecuteExtensionOperation` proposal (all SNS proposals are public on-chain) targeting a DEX-backed treasury manager can:

1. **Front-run**: Before the proposal executes, manipulate the DEX pool price by buying one side of the pair, skewing the ratio.
2. **Let the deposit execute**: The SNS governance canister calls `deposit` at the manipulated price, receiving fewer LP tokens than the fair-market equivalent.
3. **Back-run**: Sell the position acquired in step 1 at the now-restored price, extracting the difference as profit.

The SNS treasury suffers a direct financial loss proportional to the pool depth and the size of the deposit. The `treasury_manager.did` comment that "any undeposited tokens are automatically returned" only applies to tokens that the DEX refuses entirely; tokens deposited at a bad ratio are not returned.

---

### Likelihood Explanation

SNS governance proposals are fully public and observable by any IC participant. The voting period (days to weeks) gives ample time to prepare a front-running transaction. Any canister or user with sufficient liquidity to move the DEX pool price can execute this attack. The attack does not require any privileged access, leaked keys, or governance majority — only the ability to observe a public proposal and submit transactions to the DEX canister before the governance execution fires.

---

### Recommendation

The `DepositRequest` type and the `execute_treasury_manager_deposit` execution path should be extended to include a mandatory minimum price ratio or maximum slippage parameter. Concretely:

- Add a `min_price_ratio` or `max_slippage_bps` field to `DepositRequest` in `treasury_manager.did`.
- Require the `ExecuteExtensionOperation` proposal's deposit argument (`ValidatedDepositOperationArg`) to include a minimum acceptable LP token output or price ratio, validated against an on-chain oracle (e.g., the Exchange Rate Canister) at execution time.
- Enforce this bound inside `execute_treasury_manager_deposit` before calling `deposit`, rejecting the execution if the current DEX price deviates beyond the approved threshold. [7](#0-6) 

---

### Proof of Concept

1. An SNS DAO adopts an `ExecuteExtensionOperation` proposal to deposit X SNS tokens and Y ICP into a DEX liquidity pool via a registered treasury manager.
2. An attacker observes the adopted proposal (public on-chain state).
3. Before the proposal executes, the attacker calls the DEX canister to buy a large amount of SNS tokens, skewing the SNS/ICP pool ratio.
4. The SNS governance canister executes `execute_treasury_manager_deposit`, calling `approve_treasury_manager` (sets ICRC-2 allowances) and then `deposit` on the treasury manager, which deposits into the DEX at the manipulated price. The SNS treasury receives fewer LP tokens than at fair market price.
5. The attacker calls the DEX canister to sell their SNS tokens back, restoring the pool ratio and pocketing the spread.

No privileged access is required. The attacker only needs to be a DEX participant and observe the public proposal state.

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

**File:** rs/sns/governance/src/extensions.rs (L1566-1578)
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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
