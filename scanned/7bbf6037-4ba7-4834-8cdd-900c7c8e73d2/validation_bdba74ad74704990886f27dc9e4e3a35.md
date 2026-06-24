### Title
SNS Treasury Manager Deposit Lacks Slippage Protection, Enabling Front-Running of Governance Proposals to Drain Treasury LP Value — (`rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS governance deposit operation for treasury manager extensions validates only that each token amount does not exceed 50% of the respective treasury balance. It does not enforce any ratio constraint against the current DEX pool price, nor does it enforce a minimum LP token output (slippage protection). Because governance proposals are public and have multi-day voting periods, any DEX participant can observe a pending deposit proposal and manipulate the pool price before execution, causing the SNS treasury to receive far fewer LP tokens than expected — a direct loss of treasury funds.

---

### Finding Description

`validate_deposit_operation_impl` in `rs/sns/governance/src/extensions.rs` is the sole validation gate for treasury manager deposit proposals. It checks:

1. That `treasury_allocation_sns_e8s ≤ sns_balance / 2`
2. That `treasury_allocation_icp_e8s ≤ icp_balance / 2` [1](#0-0) 

There is no check that:
- The ratio of `treasury_allocation_sns_e8s` to `treasury_allocation_icp_e8s` matches the current DEX pool price.
- The resulting LP token output meets a minimum threshold (slippage guard).
- The validation is re-run at execution time (it runs only at proposal submission time).

This means a proposal can legally specify, for example, `treasury_allocation_sns_e8s = 50_000_000` and `treasury_allocation_icp_e8s = 0` — a fully asymmetric deposit — and the governance canister will approve it and execute it without any price-ratio check.

The actual execution path is:

1. `execute_treasury_manager_deposit` approves the treasury manager to spend the specified amounts.
2. It calls `deposit` on the treasury manager canister, which deposits into the external DEX.
3. No minimum LP token output is checked on the returned `Balances`. [2](#0-1) 

The codebase itself acknowledges this risk in two places but provides no on-chain mitigation:

- `treasury_manager.did` lists it as a "Known Security Risk": [3](#0-2) 

- `rs/sns/proposal.rs` emits a WARNING in the proposal rendering for `RegisterExtension`: [4](#0-3) 

The warning is informational only — no enforcement exists in the execution path.

---

### Impact Explanation

An attacker who observes a pending SNS governance deposit proposal (proposals are public, stored in governance canister state, and have voting periods of days) can:

1. **Before proposal execution**: Manipulate the DEX pool price by trading heavily in one direction (e.g., buy all SNS from the pool, driving up the SNS/ICP price).
2. **Proposal executes**: The SNS treasury deposits SNS and ICP at the manipulated price, receiving far fewer LP tokens than the fair-price equivalent.
3. **After execution**: The attacker reverses their trade, profiting from the arbitrage. The SNS treasury permanently holds LP tokens worth less than the deposited assets.

This is a direct, quantifiable loss of SNS treasury funds. The loss scales with the deposit size (up to 50% of treasury per proposal) and the degree of price manipulation the attacker can sustain.

---

### Likelihood Explanation

- Governance proposals are fully public and have voting periods measured in days, giving ample time for manipulation.
- No governance access is required — any DEX trader can execute this attack.
- The attack is profitable whenever the cost of price manipulation is less than the arbitrage gain, which is achievable for large treasury deposits.
- The IC's lack of a public mempool prevents traditional transaction-level front-running, but the multi-day proposal window is a far larger attack surface.

---

### Recommendation

1. **Enforce a minimum LP token output** in `execute_treasury_manager_deposit`: after calling `deposit` on the treasury manager, verify that the returned LP token balance meets a governance-specified minimum. Reject (and attempt to reclaim allowances) if not met.
2. **Re-validate the ratio at execution time**: compare `treasury_allocation_sns_e8s / treasury_allocation_icp_e8s` against the current pool price fetched on-chain at execution time, and reject if the deviation exceeds a governance-specified slippage tolerance.
3. **Add a `min_lp_tokens_out` field** to `ValidatedDepositOperationArg` so proposers can specify their slippage tolerance explicitly, and enforce it in `execute_treasury_manager_deposit`. [5](#0-4) 

---

### Proof of Concept

**Setup**: SNS treasury holds 1,000,000 SNS and 100,000 ICP. A DEX pool has 500,000 SNS and 50,000 ICP (1 ICP = 10 SNS). A governance proposal is submitted to deposit 500,000 SNS and 50,000 ICP.

**Attack** (executed during the voting period, before proposal execution):

1. Attacker buys 40,000 ICP worth of SNS from the pool, driving the price to ~1 ICP = 5 SNS.
2. Proposal executes: treasury deposits 500,000 SNS + 50,000 ICP at the manipulated 1:5 ratio. The treasury receives LP tokens representing a pool position worth ~750,000 SNS equivalent instead of the fair ~1,000,000 SNS equivalent.
3. Attacker sells their SNS back, restoring the price and pocketing the arbitrage profit (~250,000 SNS equivalent minus trading costs).

The SNS treasury has permanently lost ~25% of the deposited value. The `validate_deposit_operation_impl` check (≤50% of balance) passed at proposal submission time and was never re-evaluated. [1](#0-0) [6](#0-5)

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

**File:** rs/sns/governance/src/extensions.rs (L1546-1609)
```rust
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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
