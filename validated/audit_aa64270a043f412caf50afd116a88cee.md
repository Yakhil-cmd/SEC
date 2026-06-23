### Title
SNS Treasury Manager `deposit()` Executes DEX Liquidity Deposit Without Slippage Tolerance Parameter - (`File: rs/sns/governance/src/extensions.rs`, `rs/sns/treasury_manager/treasury_manager.did`)

---

### Summary

The SNS Treasury Manager extension framework allows SNS DAOs to deposit treasury funds (SNS tokens + ICP) into external DEX liquidity pools via a governance-approved `ExecuteExtensionOperation` proposal. The `DepositRequest` type and the `construct_deposit_allowances` function that builds it contain only the raw token amounts to deposit — no minimum LP token output, no slippage tolerance, and no price-ratio bound. An attacker who can trade on the target DEX can sandwich the governance-executed deposit, causing the SNS treasury to receive significantly fewer LP tokens than the price at proposal-approval time implied, with the attacker extracting the difference.

---

### Finding Description

The `DepositRequest` type defined in the Treasury Manager interface contains only `allowances` — a list of `(asset, amount, owner_account)` tuples:

```
type DepositRequest = record {
  allowances : vec Allowance;
};
```

No field for a minimum acceptable LP token output, a maximum acceptable price deviation, or any other slippage bound exists in the type. [1](#0-0) 

The Rust `DepositRequest` struct mirrors this exactly: [2](#0-1) 

The function `construct_deposit_allowances` in `extensions.rs` builds the payload from only two governance-proposal fields — `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` — and produces `Allowance` entries with no slippage field: [3](#0-2) 

`execute_treasury_manager_deposit` then approves the treasury manager for those exact amounts and calls `deposit` with the resulting payload, with no post-call check on the LP tokens received: [4](#0-3) 

The proposal validation step (`validate_deposit_operation_impl`) only checks that the requested amounts do not exceed 50% of the current treasury balance — it performs no price-ratio or slippage check: [5](#0-4) 

The codebase itself acknowledges this gap in two places. The DID file labels it a "Known Security Risk": [6](#0-5) 

And the proposal-rendering function for `RegisterExtension` emits a human-readable warning to voters — but no on-chain enforcement: [7](#0-6) 

The warning is informational only. The actual `deposit` call carries no enforceable slippage bound.

---

### Impact Explanation

An attacker who holds assets in the target DEX pool can execute a classic sandwich attack:

1. **Front-run**: Before the governance-executed `deposit` call lands, the attacker makes a large swap that skews the pool's price ratio away from the ratio encoded in the proposal.
2. **Victim deposit**: The Treasury Manager deposits the SNS + ICP amounts at the manipulated ratio, receiving far fewer LP tokens than the fair-price equivalent.
3. **Back-run**: The attacker swaps back, restoring the pool price and pocketing the arbitrage profit extracted from the SNS treasury.

Because the `DepositRequest` carries no `min_lp_tokens_out` or equivalent, the DEX has no on-chain instruction to reject the deposit if the price has moved. The SNS treasury suffers a direct, quantifiable financial loss proportional to the price impact of the attacker's front-run trade. For large treasury deposits into shallow pools, this loss can be substantial.

---

### Likelihood Explanation

The attack requires only that the attacker:
- Holds enough assets to move the DEX pool price meaningfully before the deposit executes, **or**
- Can observe the pending governance execution and act within the same IC round or across adjacent rounds.

SNS governance proposals have a public voting period (days), giving any observer ample time to prepare. The execution of `execute_treasury_manager_deposit` is a publicly observable inter-canister call sequence. On IC, inter-canister calls are not atomic with respect to other canisters' state changes, so a DEX canister's pool state can be altered between the governance canister's `approve` call and the Treasury Manager's actual DEX interaction. This is a realistic, low-barrier attack for any DEX user with sufficient liquidity.

---

### Recommendation

1. **Add a `min_lp_tokens_out` (or equivalent) field to `DepositRequest`** so that the SNS governance proposal encodes an enforceable slippage bound at the time of voter approval.

```
type DepositRequest = record {
  allowances : vec Allowance;
  min_lp_tokens_out : opt nat;   // reject deposit if LP tokens received < this
};
```

2. **Extend `ValidatedDepositOperationArg`** to carry the slippage bound and pass it through `construct_treasury_manager_deposit_payload` into the `DepositRequest`. [8](#0-7) 

3. **Require Treasury Manager implementations** (as part of the NNS blessing process) to enforce the `min_lp_tokens_out` bound and return an error — not silently accept a worse price — if the DEX cannot satisfy it.

---

### Proof of Concept

**Setup**: An SNS DAO has approved a `TreasuryManagerDeposit` proposal specifying `treasury_allocation_sns_e8s = X` and `treasury_allocation_icp_e8s = Y` at a pool price ratio of `R = X/Y`.

**Attack sequence**:

1. Attacker monitors SNS governance for an approved `ExecuteExtensionOperation` proposal targeting a DEX pool.
2. Before `execute_treasury_manager_deposit` fires, attacker submits a large swap on the DEX that shifts the pool ratio from `R` to `R'` (e.g., dumps ICP into the pool, making ICP cheap relative to SNS).
3. `execute_treasury_manager_deposit` calls `approve_treasury_manager` (ICRC-2 approve for `X` SNS and `Y` ICP), then calls `deposit` with `DepositRequest { allowances: [(SNS, X, ...), (ICP, Y, ...)] }`. [9](#0-8) 
4. The Treasury Manager forwards the deposit to the DEX at ratio `R'`. The SNS treasury receives LP tokens valued at `R'`, not `R`. No on-chain check rejects this.
5. Attacker swaps back, restoring ratio `R`, and profits from the spread.

The `DepositRequest` carries no field that would allow the DEX or the Treasury Manager to reject step 4. The acknowledged "Known Security Risk" in the DID file confirms the root cause is the absence of a slippage parameter in the interface itself. [6](#0-5)

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

**File:** rs/sns/treasury_manager/src/lib.rs (L284-287)
```rust
#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
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

**File:** rs/sns/governance/src/extensions.rs (L867-917)
```rust
    pub fn construct_deposit_allowances(
        arg: Precise,
        sns_token: Asset,
        icp_token: Asset,
        treasury_sns_account: Account,
        treasury_icp_account: Account,
    ) -> Result<Vec<Allowance>, String> {
        const PREFIX: &str = "Cannot parse ExtensionInit as TreasuryManagerInit: ";

        let Precise {
            value: Some(precise::Value::Map(PreciseMap { mut map })),
        } = arg
        else {
            return Err(format!("{PREFIX}Top-level type must be PreciseMap."));
        };

        if map.len() != 2 {
            return Err(format!(
                "{PREFIX}Top-level type must be PreciseMap with exactly 2 entries."
            ));
        }

        let mut token_amount_e8s = |field_name: &str| {
            map.remove(field_name)
                .and_then(|Precise { value }| {
                    if let Some(precise::Value::Nat(amount_e8s)) = value {
                        Some(amount_e8s)
                    } else {
                        None
                    }
                })
                .ok_or_else(|| format!("{PREFIX}{field_name} must contain a precise value."))
        };

        let sns_token_amount_e8s = token_amount_e8s("treasury_allocation_sns_e8s")?;
        let icp_token_amount_e8s = token_amount_e8s("treasury_allocation_icp_e8s")?;

        let allowances = vec![
            Allowance {
                amount_decimals: Nat::from(sns_token_amount_e8s),
                asset: sns_token,
                owner_account: treasury_sns_account,
            },
            Allowance {
                amount_decimals: Nat::from(icp_token_amount_e8s),
                asset: icp_token,
                owner_account: treasury_icp_account,
            },
        ];
        Ok(allowances)
    }
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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
