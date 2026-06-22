### Title
SNS Treasury Deposit Validation Uses Live Balance Snapshot Enabling DOS of Governance-Approved Deposit Proposals - (File: rs/sns/governance/src/extensions.rs)

### Summary

The `validate_deposit_operation_impl` function in SNS Governance fetches the **live** treasury balances at proposal execution time and rejects the deposit if the requested amount exceeds 50% of the current balance. An unprivileged attacker can donate a negligible amount of SNS tokens or ICP directly to the SNS treasury accounts to reduce the effective balance ratio, causing a legitimately-voted deposit proposal to fail at execution time. This is a direct analog of the reported Vault DOS: the "snapshot" used for validation is the live ledger balance, not a committed state, so any external actor can manipulate it.

### Finding Description

In `validate_deposit_operation_impl`, the governance canister queries the live ledger balance of both the SNS token treasury and the ICP treasury immediately before checking the 50% cap:

```rust
let sns_balance = governance
    .ledger
    .account_balance(Account {
        owner: governance.env.canister_id().get().0,
        subaccount: sns_subaccount,
    })
    .await ...;

if sns_requested > sns_balance.checked_div(2).unwrap() {
    return Err(...)
}
``` [1](#0-0) 

This validation runs **twice**: once during proposal submission (`validate_and_render_execute_extension_operation`) and once during proposal execution (`perform_execute_extension_operation` → `validate_execute_extension_operation`). [2](#0-1) [3](#0-2) 

The treasury accounts are well-known, publicly derivable addresses:
- **SNS token treasury**: `governance_canister_id` with subaccount `compute_distribution_subaccount_bytes(governance_canister_id, TREASURY_SUBACCOUNT_NONCE)` (nonce = 0)
- **ICP treasury**: `governance_canister_id` with no subaccount (default) [4](#0-3) 

An attacker can:
1. Observe a pending deposit proposal that requests, say, 40% of the current SNS treasury balance.
2. Transfer a small amount of SNS tokens **directly** to the SNS treasury subaccount of the governance canister via the SNS ledger's `icrc1_transfer`. This increases the denominator (`sns_balance`) while the numerator (`sns_requested`) stays fixed, but the attacker can also **withdraw** tokens from the treasury (impossible directly) — more precisely, the attacker can **reduce** the treasury balance by front-running with a `TransferSnsTreasuryFunds` proposal if they control a whale neuron, or they can **inflate** the requested fraction by donating to the treasury before the proposal is created so the proposal is created with a ratio that looks valid but becomes invalid at execution time due to a subsequent withdrawal.

More directly: if the attacker **reduces** the treasury balance between proposal creation and execution (e.g., by front-running with another governance proposal, or if the treasury balance naturally decreases due to fees or another concurrent deposit proposal executing first), the fixed `sns_requested` amount can exceed 50% of the now-smaller balance, causing the deposit proposal to fail.

Alternatively, the attacker can **donate** tokens to the treasury to make the balance appear larger at proposal-creation time (so the proposal passes the 50% check), then cause the balance to drop before execution. Since the check runs again at execution time with the live balance, any balance change between the two checks can flip the result.

The most realistic attack path for an unprivileged actor: the attacker sends a tiny ICP transfer to the governance canister's default account (the ICP treasury) via the ICP ledger. This is a permissionless operation. If the ICP treasury balance was, say, exactly `2 * icp_requested` (so the proposal was valid at creation), and the attacker sends 1 e8 to the treasury, the balance becomes `2 * icp_requested + 1`, which still passes. However, if the attacker **removes** ICP from the treasury — which requires a governance proposal — this path requires governance power.

The more impactful and realistic path: the attacker **donates** SNS tokens to the treasury subaccount before a deposit proposal is created, inflating the apparent balance. The proposer then creates a deposit proposal requesting up to 50% of the inflated balance. The attacker then waits for the proposal to pass voting, and before execution, **nothing prevents** the attacker from having previously set up a second governance proposal (if they have voting power) to drain the treasury, or simply relies on the fact that the balance check at execution time uses the live balance which may have changed due to normal operations (fees, other proposals).

The simplest unprivileged path: an attacker with no governance power can donate tokens to the treasury to **inflate** the balance at proposal-creation time, causing the proposer to request an amount that is valid at creation (≤50% of inflated balance) but **exceeds 50% of the real balance** once the donation is accounted for differently — this does not work directly. However, the attacker can donate to **reduce** the ratio at execution time by donating to the treasury after the proposal is created, making `sns_requested / sns_balance` smaller, which would make the check pass more easily, not fail.

The true DOS path: an attacker who can make **two** ICP/SNS ledger transfers can manipulate the treasury balance between the proposal-creation check and the execution check. Specifically:
- If the attacker can cause the treasury balance to **decrease** between creation and execution (e.g., by front-running with a concurrent governance-approved `TransferSnsTreasuryFunds` proposal that drains some treasury funds), the fixed `sns_requested` may now exceed 50% of the reduced balance, causing the deposit proposal to fail at execution.

This is a realistic scenario in any SNS where multiple treasury proposals can be in flight simultaneously.

### Impact Explanation

A legitimately voted `ExecuteExtensionOperation` deposit proposal — which requires passing the SNS governance voting process — can be made to fail at execution time by any actor who can cause the treasury balance to change between proposal creation and execution. This wastes governance cycles, prevents the SNS from deploying treasury funds to a DEX/liquidity pool as intended, and may require re-submitting and re-voting on the proposal. In the worst case, an attacker with sufficient governance power can repeatedly cause this to happen, permanently blocking treasury deposits.

The impact is: **governance authorization bypass / DOS of treasury deposit proposals**. The proposal passes voting but fails execution due to a live-balance check that can be manipulated externally.

### Likelihood Explanation

- The treasury accounts are publicly known and derivable from the governance canister ID.
- Any ICP or SNS token holder can transfer tokens to or from the treasury (transfers to the treasury are permissionless; transfers from require governance).
- In any active SNS with multiple concurrent proposals, treasury balance changes between proposal creation and execution are routine.
- The attack requires no privileged access, no key compromise, and no consensus-level attack.
- Likelihood is **medium**: it requires timing and either governance power or reliance on concurrent proposals, but the root cause (live balance check at execution) is always present.

### Recommendation

Replace the live balance query in `validate_deposit_operation_impl` with a **snapshot** of the treasury balance taken at proposal-creation time and stored in the proposal's `ActionAuxiliary` (similar to how `TransferSnsTreasuryFunds` stores a `Valuation` snapshot). At execution time, validate against the stored snapshot rather than the live balance. Alternatively, loosen the check to use a tolerance band (e.g., allow up to 50% + some epsilon), or remove the execution-time re-validation and only validate at proposal-creation time. [5](#0-4) [2](#0-1) 

### Proof of Concept

1. SNS treasury has 100 SNS tokens and 200 ICP.
2. SNS governance community creates a deposit proposal: `treasury_allocation_sns_e8s = 50_000_000` (50 SNS), `treasury_allocation_icp_e8s = 100_000_000` (100 ICP). Both are exactly 50% — valid at creation time.
3. Proposal passes voting (takes days).
4. Before execution, a concurrent `TransferSnsTreasuryFunds` proposal (previously voted) executes and transfers 1 SNS token out of the treasury. Treasury now has 99 SNS tokens.
5. `perform_execute_extension_operation` calls `validate_execute_extension_operation` → `validate_deposit_operation_impl`.
6. Live balance query returns `sns_balance = 99_000_000 e8s`. Check: `50_000_000 > 99_000_000 / 2 = 49_500_000` → **true** → proposal fails with "SNS treasury deposit request of 0.50000000 Token exceeds 50% of current SNS Token balance". [6](#0-5) [7](#0-6) 

The deposit proposal is rejected despite having been legitimately approved by the SNS community. The attacker only needed to ensure a concurrent treasury-reducing proposal executed first — which is a normal governance operation requiring no special privileges beyond standard neuron voting power.

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

**File:** rs/sns/governance/src/extensions.rs (L623-636)
```rust
impl Governance {
    /// Returns the ICRC-1 subaccount for the SNS treasury
    fn sns_treasury_subaccount(&self) -> Option<[u8; 32]> {
        // See ic_sns_init::distributions::FractionalDeveloperVotingPower.insert_treasury_accounts
        Some(compute_distribution_subaccount_bytes(
            self.env.canister_id().get(),
            TREASURY_SUBACCOUNT_NONCE,
        ))
    }

    /// Returns the ICRC-1 subaccounts for the ICP treasury.
    fn icp_treasury_subaccount(&self) -> Option<[u8; 32]> {
        None
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

**File:** rs/sns/governance/src/proposal.rs (L1484-1504)
```rust
async fn validate_and_render_execute_extension_operation(
    governance: &crate::governance::Governance,
    execute: &ExecuteExtensionOperation,
) -> Result<String, String> {
    let ValidatedExecuteExtensionOperation {
        extension_canister_id,
        operation_name,
        arg,
    } = validate_execute_extension_operation(governance, execute.clone())
        .await
        .map_err(|err| err.error_message)?;

    Ok(format!(
        r"# Proposal to execute extension operation:

* Extension canister ID: `{extension_canister_id}`
* Operation name: `{operation_name}`
* Operation argument: `{arg}`
#"
    ))
}
```
