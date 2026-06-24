### Title
SNS Treasury Manager `DepositRequest` Lacks Slippage Protection, Enabling Price Manipulation Between Proposal Adoption and Execution — (`rs/sns/treasury_manager/treasury_manager.did`, `rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS Treasury Manager `DepositRequest` type contains no minimum-output or price-band field. When an SNS governance proposal to deposit treasury funds into a DEX is adopted, an unprivileged attacker can manipulate the DEX pool price during the window between proposal adoption and execution, causing the SNS treasury to receive fewer LP tokens than expected. The codebase itself acknowledges this risk in two places but does not enforce any slippage bound at the protocol level.

---

### Finding Description

The `execute_treasury_manager_deposit` function in `rs/sns/governance/src/extensions.rs` executes in two steps:

1. It calls `approve_treasury_manager`, which sets ICRC-2 allowances on both the SNS token ledger and the ICP ledger for the treasury manager canister.
2. It calls `deposit` on the treasury manager canister, passing a `DepositRequest`. [1](#0-0) 

The `DepositRequest` type, defined in the Treasury Manager API, contains only `allowances` — the token amounts approved for the manager. It has no field for a minimum output amount, minimum LP tokens received, or any price band: [2](#0-1) 

The codebase explicitly acknowledges this gap in two locations. First, in the DID file itself under "Known Security Risks": [3](#0-2) 

Second, in the proposal rendering function `validate_and_render_register_extension` in `rs/sns/governance/src/proposal.rs`, which warns voters that DEX deposits may be vulnerable to front-running and sandwich attacks: [4](#0-3) 

The mitigation cited — "any undeposited tokens are automatically returned to the SNS treasury account" — only covers tokens that the DEX refuses to accept entirely. It does not prevent the treasury from depositing at a manipulated price and receiving fewer LP tokens than the fair-market rate would yield.

The `approve_treasury_manager` function sets ICRC-2 allowances with a 1-hour expiry: [5](#0-4) 

This 1-hour window, combined with the public visibility of adopted governance proposals, creates a concrete attack surface.

---

### Impact Explanation

An attacker who manipulates the DEX pool price between proposal adoption and execution causes the SNS treasury to deposit at an unfavorable ratio. The attacker profits from the price difference after reversing the manipulation. The SNS treasury (a DAO-controlled fund holding real ICP and SNS tokens) suffers a direct financial loss proportional to the size of the deposit and the degree of price manipulation achievable. This is analogous to the original report's finding: value is extracted from the protocol by exploiting the gap between an oracle/expected price and the actual execution price.

---

### Likelihood Explanation

- SNS governance proposals are public state; any observer can detect when a treasury deposit proposal is adopted.
- The IC execution model does not have a public mempool, so traditional same-block sandwich attacks are not possible. However, the window between proposal adoption and governance execution (which can span multiple rounds or even hours) is sufficient for an attacker to manipulate a DEX pool price.
- The `ALLOWED_EXTENSIONS` list in `rs/sns/governance/src/extensions.rs` is currently empty (KongSwap ceased operations April 2026), reducing immediate exploitability. However, the API is designed for future extensions, and the structural gap persists.
- The attacker requires capital to manipulate the DEX pool, which limits the attack to well-capitalized actors, but does not eliminate the risk. [6](#0-5) 

---

### Recommendation

1. Add a `min_output_decimals` or equivalent slippage-bound field to `DepositRequest` in `rs/sns/treasury_manager/treasury_manager.did`.
2. Enforce this bound in `execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` by verifying the returned `Balances` meet the minimum before accepting the result.
3. Require that any blessed Treasury Manager implementation enforce slippage bounds when calling the underlying DEX, as part of the NNS blessing criteria.

---

### Proof of Concept

1. An SNS governance proposal to deposit `X` SNS tokens and `Y` ICP into a DEX via a Treasury Manager is adopted. This is visible as public canister state.
2. An attacker observes the adoption and, before the governance canister executes the deposit, submits transactions to the DEX that skew the pool price (e.g., buys a large amount of SNS tokens, moving the price).
3. The governance canister calls `approve_treasury_manager` (setting ICRC-2 allowances) and then `deposit` on the Treasury Manager.
4. The Treasury Manager calls the DEX at the manipulated price. The treasury receives fewer LP tokens than the fair-market rate would yield.
5. The attacker reverses the price manipulation (sells the SNS tokens back), profiting from the round-trip while the treasury absorbs the loss.

The root cause is the absence of any slippage parameter in `DepositRequest`: [2](#0-1) 

and the absence of any output validation in `execute_treasury_manager_deposit`: [7](#0-6)

### Citations

**File:** rs/sns/governance/src/extensions.rs (L48-54)
```rust
thread_local! {
    static ALLOWED_EXTENSIONS: RefCell<BTreeMap<[u8; 32], ExtensionSpec>> = const { RefCell::new(btreemap! {
        // This collection is intentionally left empty. The Kong Swap extension used to be here,
        // but they ceased operations on April 6, 2026. Consequently, that was removed
        // from this list.
    }) };
}
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

**File:** rs/sns/governance/src/extensions.rs (L1566-1601)
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
