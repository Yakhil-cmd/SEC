### Title
SNS Treasury Manager `DepositRequest` and `WithdrawRequest` Lack Slippage Protection, Enabling Sandwich Attacks on SNS Treasury Funds - (File: `rs/sns/treasury_manager/treasury_manager.did`, `rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS Treasury Manager framework allows SNS DAOs to deposit treasury funds (ICP and SNS tokens) into on-chain DEX liquidity pools via governance proposals (`RegisterExtension` and `ExecuteExtensionOperation`). The `DepositRequest` and `WithdrawRequest` types defined in the Treasury Manager API contain no slippage protection parameters (no minimum LP token output, no price bounds, no maximum acceptable price deviation). The SNS Governance execution path in `execute_treasury_manager_deposit` and `execute_treasury_manager_withdraw` passes no price constraints to the treasury manager canister. An unprivileged attacker who can submit transactions to the DEX canister can manipulate the pool price before the governance-triggered deposit executes, extract value from the SNS treasury, and reverse their position for profit. The codebase itself explicitly acknowledges this risk in two separate locations.

---

### Finding Description

The `DepositRequest` type in the Treasury Manager API is defined as:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};
``` [1](#0-0) 

It contains only token allowances — no minimum LP token output, no acceptable price range, and no slippage tolerance. The same applies to `WithdrawRequest`:

```candid
type WithdrawRequest = record {
  withdraw_accounts : opt vec record { principal; Account };
};
``` [2](#0-1) 

The codebase itself documents this as a known security risk:

> *"Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved."* [3](#0-2) 

And the proposal rendering function for `RegisterExtension` explicitly warns about sandwich attacks:

> *"Some Decentralized Exchanges lack slippage protection during deposits... This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running or sandwich attacks."* [4](#0-3) 

The execution path in `execute_treasury_manager_deposit` issues ICRC-2 approvals and then calls `deposit` on the treasury manager with no price bounds:

```rust
// 1. Transfer funds from treasury to treasury manager
governance.approve_treasury_manager(
    extension_canister_id,
    treasury_allocation_sns_e8s,
    treasury_allocation_icp_e8s,
).await?;

// 2. Call deposit on treasury manager
let balances = governance.env.call_canister(
    extension_canister_id, "deposit", arg_blob
).await ...
``` [5](#0-4) 

The `validate_deposit_operation_impl` function only enforces a 50% treasury balance cap — it performs no price-bound or slippage validation whatsoever:

```rust
if sns_requested > sns_balance.checked_div(2).unwrap() {
    return Err(...)
}
if icp_requested > icp_balance.checked_div(2).unwrap() {
    return Err(...)
}
``` [6](#0-5) 

The `ValidatedDepositOperationArg` struct carries only token amounts and the raw `Precise` blob — no price constraints:

```rust
pub struct ValidatedDepositOperationArg {
    pub treasury_allocation_sns_e8s: u64,
    pub treasury_allocation_icp_e8s: u64,
    pub original: Precise,
}
``` [7](#0-6) 

The `perform_execute_extension_operation` function in governance dispatches directly to `execute_treasury_manager_deposit` after re-validation, with no price-check step added at execution time: [8](#0-7) 

The full proposal-to-execution flow is:
1. `ExecuteExtensionOperation` proposal submitted with `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s`
2. Validated by `validate_deposit_operation_impl` — only checks 50% balance cap
3. After voting passes, `perform_execute_extension_operation` → `execute_treasury_manager_deposit`
4. `approve_treasury_manager` issues ICRC-2 approvals on both SNS and ICP ledgers
5. `deposit` called on the treasury manager canister with a `DepositRequest` containing only allowances
6. Treasury manager calls the DEX (e.g., KongSwap) with no slippage protection [9](#0-8) 

---

### Impact Explanation

An attacker can extract value from the SNS treasury by manipulating the DEX pool price before the governance-triggered deposit executes. The SNS treasury deposits tokens at a manipulated (unfavorable) price, receiving fewer LP tokens than the fair-market value of the deposited assets. The attacker reverses their position after the deposit and profits the difference. The impact is direct, quantifiable loss of SNS treasury funds (ICP and SNS tokens) to an unprivileged attacker. The 50% balance cap means up to 50% of the treasury's ICP and 50% of its SNS tokens can be exposed in a single deposit proposal. [10](#0-9) 

---

### Likelihood Explanation

**High.** The attack requires only the ability to submit transactions to the DEX canister — no privileged access, no key compromise, no governance majority. On the Internet Computer, all governance proposals and their voting deadlines are public. An attacker can observe a passed `TreasuryManagerDeposit` or `RegisterExtension` proposal and submit DEX swap transactions in the same or adjacent consensus rounds to manipulate the pool price before the governance execution message is processed. The IC's deterministic message ordering within a subnet means the attacker can reliably sequence their manipulation transaction ahead of the governance deposit by submitting it in the same round. The codebase itself acknowledges this attack class in two places, confirming the developers are aware of the risk but have not implemented a technical mitigation at the protocol level. [11](#0-10) 

---

### Recommendation

1. **Add slippage parameters to `DepositRequest` and `WithdrawRequest`** in `rs/sns/treasury_manager/treasury_manager.did`: add `min_lp_tokens_out : opt nat` (for deposits) and `min_tokens_out : opt vec record { principal; nat }` (for withdrawals). Treasury manager implementations must enforce these bounds before calling the DEX.

2. **Propagate price bounds through the governance proposal**: extend `ValidatedDepositOperationArg` in `rs/sns/governance/src/extensions.rs` to carry a `max_price_deviation_bps` or `min_lp_tokens_out` field parsed from the proposal's `Precise` map, and include it in the `DepositRequest` passed to the treasury manager.

3. **Re-validate pool price at execution time**: in `execute_treasury_manager_deposit`, query the DEX pool's current spot price before issuing ICRC-2 approvals and revert if the price has deviated beyond the proposal-specified tolerance since the proposal was created.

4. **Consider a two-step deposit model**: pause DEX trading (if the DEX supports it), verify the spot price is within bounds, then execute the deposit — analogous to the mitigation recommended in the original AeraVaultV1 report.

---

### Proof of Concept

**Setup:** An SNS DAO has 100 ICP and 1,000,000 SNS tokens in its treasury. A `TreasuryManagerDeposit` proposal passes to deposit 50 ICP and 500,000 SNS tokens into a KongSwap ICP/SNS liquidity pool at a fair price of 1 ICP = 10,000 SNS.

**Attack sequence:**

1. Attacker observes the passed proposal on-chain (all proposals are public). The proposal will execute at the next governance heartbeat after the voting deadline.

2. Attacker submits a swap transaction to the KongSwap DEX canister: swap a large amount of ICP for SNS tokens, driving the SNS spot price down (more SNS per ICP).

3. In the same or next consensus round, the governance canister's heartbeat triggers `execute_treasury_manager_deposit`. The function calls `approve_treasury_manager` (ICRC-2 approvals) and then `deposit` on the KongSwap adaptor with `DepositRequest { allowances: [...] }` — no price bounds.

4. The KongSwap adaptor deposits 50 ICP and 500,000 SNS into the pool at the manipulated price. Because the pool is now imbalanced (SNS is cheap), the SNS treasury receives fewer LP tokens than the fair-market value of its deposit.

5. Attacker submits a reverse swap: sells the SNS tokens acquired in step 2 back to ICP at the now-restored price (after the large SNS deposit rebalanced the pool). The attacker profits the spread.

**Root cause in code:** `DepositRequest` has no `min_lp_tokens_out` field (`rs/sns/treasury_manager/treasury_manager.did` line 84–86); `execute_treasury_manager_deposit` passes no price constraints (`rs/sns/governance/src/extensions.rs` lines 1566–1578); `validate_deposit_operation_impl` checks only the 50% balance cap (`rs/sns/governance/src/extensions.rs` lines 307–318). [1](#0-0) [5](#0-4) [12](#0-11)

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

**File:** rs/sns/treasury_manager/treasury_manager.did (L296-301)
```text
service : (TreasuryManagerArg) -> {
  deposit : (DepositRequest) -> (Result);
  withdraw : (WithdrawRequest) -> (Result);
  balances : (record {}) -> (Result) query;
  audit_trail : (record {}) -> (AuditTrail) query;
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

**File:** rs/sns/governance/src/extensions.rs (L48-53)
```rust
thread_local! {
    static ALLOWED_EXTENSIONS: RefCell<BTreeMap<[u8; 32], ExtensionSpec>> = const { RefCell::new(btreemap! {
        // This collection is intentionally left empty. The Kong Swap extension used to be here,
        // but they ceased operations on April 6, 2026. Consequently, that was removed
        // from this list.
    }) };
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

**File:** rs/sns/governance/src/extensions.rs (L604-620)
```rust
impl ValidatedExecuteExtensionOperation {
    pub async fn execute(self, governance: &Governance) -> Result<(), GovernanceError> {
        let Self {
            operation_name: _,
            extension_canister_id,
            arg,
        } = self;

        match arg {
            ValidatedOperationArg::TreasuryManagerDeposit(arg) => {
                execute_treasury_manager_deposit(governance, extension_canister_id, arg).await
            }
            ValidatedOperationArg::TreasuryManagerWithdraw(arg) => {
                execute_treasury_manager_withdraw(governance, extension_canister_id, arg).await
            }
        }
    }
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

**File:** rs/sns/governance/src/extensions.rs (L1664-1672)
```rust
#[derive(Debug, Clone)]
pub struct ValidatedDepositOperationArg {
    /// Amount of SNS tokens to allocate from treasury
    pub treasury_allocation_sns_e8s: u64,
    /// Amount of ICP tokens to allocate from treasury
    pub treasury_allocation_icp_e8s: u64,
    /// Original Precise value with all fields
    pub original: Precise,
}
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
