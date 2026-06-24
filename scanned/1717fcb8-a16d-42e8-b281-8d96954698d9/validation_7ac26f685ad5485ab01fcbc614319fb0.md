### Title
SNS Neuron Seller Can Drain All Maturity Before Completing Ownership Transfer - (File: `rs/sns/governance/src/governance.rs`)

### Summary
SNS neurons are transferred by granting the buyer permissions then revoking the seller's permissions — a multi-step, non-atomic process. Because `disburse_maturity` is a synchronous, immediately-effective operation that requires only `DisburseMaturity` permission, a malicious seller can drain 100% of a neuron's maturity to their own account at any point before the buyer removes the seller's permissions. The buyer receives a neuron stripped of its maturity while the seller keeps both the payment and the maturity.

### Finding Description

SNS governance uses a fine-grained permission model (`NeuronPermissionType`) instead of a single controller. There is no atomic "transfer neuron" primitive. The conventional transfer flow is:

1. Seller calls `AddNeuronPermissions` to grant the buyer `ManagePrincipals` (and other permissions).
2. Buyer verifies the neuron state and sends payment.
3. Buyer calls `RemoveNeuronPermissions` to strip the seller's permissions.

The `disburse_maturity` function in `rs/sns/governance/src/governance.rs` is **synchronous** (not `async`), checks only for `DisburseMaturity` permission, and **immediately** deducts `maturity_e8s_equivalent` from the neuron in the same message execution:

```rust
// rs/sns/governance/src/governance.rs  lines 1609-1706
pub fn disburse_maturity(
    &mut self,
    id: &NeuronId,
    caller: &PrincipalId,
    disburse_maturity: &DisburseMaturity,
) -> Result<DisburseMaturityResponse, GovernanceError> {
    let neuron = self.get_neuron_result(id)?;
    neuron.check_authorized(caller, NeuronPermissionType::DisburseMaturity)?;
    // ... no dissolve-state check ...
    let neuron = self.get_neuron_result_mut(id)?;
    neuron.maturity_e8s_equivalent = neuron
        .maturity_e8s_equivalent
        .saturating_sub(maturity_to_deduct);   // ← immediate, synchronous deduction
    neuron.disburse_maturity_in_progress.push(disbursement_in_progress);
``` [1](#0-0) 

Unlike `disburse_neuron`, there is **no check on the neuron's dissolve state**, so this can be called on any neuron regardless of whether it is locked, dissolving, or dissolved.

The `DisburseMaturity` permission is one of the standard grantable permissions:

```proto
// rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto  line 46
NEURON_PERMISSION_TYPE_DISBURSE_MATURITY = 8;
``` [2](#0-1) 

The `AddNeuronPermissions` path that enables the transfer is:

```rust
// rs/sns/governance/src/governance.rs  lines 4570-4642
fn add_neuron_permissions(
    &mut self,
    neuron_id: &NeuronId,
    caller: &PrincipalId,
    add_neuron_permissions: &AddNeuronPermissions,
) -> Result<(), GovernanceError> {
``` [3](#0-2) 

### Impact Explanation

A buyer who purchases an SNS neuron off-chain (e.g., via an OTC desk or NFT marketplace that wraps neuron ownership) can receive a neuron whose entire maturity has been silently drained. The maturity is queued for disbursement to the seller's account via `disburse_maturity_in_progress` and will be minted as SNS tokens after the 7-day delay. The buyer suffers a direct financial loss equal to the full maturity value; the seller double-collects (payment + maturity).

### Likelihood Explanation

SNS neurons with large accumulated maturity are actively traded. The attack requires no special privilege — only the seller's existing `DisburseMaturity` permission, which every neuron owner holds by default. The attack is a single synchronous canister call that completes in one round, leaving no observable window for the buyer to detect or prevent it. The IC has no mempool, but the ordering advantage is even stronger: the seller controls exactly when they call `disburse_maturity` relative to granting the buyer permissions, so no probabilistic race is needed.

### Recommendation

1. **Introduce an atomic `TransferNeuron` command** in SNS governance that atomically replaces all permissions in a single message, preventing any intermediate state where both seller and buyer hold permissions simultaneously.
2. **Alternatively, add a neuron-level transfer lock**: when a neuron has a pending `disburse_maturity_in_progress` entry, block `AddNeuronPermissions` / `RemoveNeuronPermissions` until all disbursements finalize, or vice versa.
3. **At minimum, document** that off-chain neuron sales must verify `disburse_maturity_in_progress` is empty and `maturity_e8s_equivalent` matches the agreed value immediately before the buyer removes the seller's permissions.

### Proof of Concept

```
1. Seller owns SNS neuron N with maturity_e8s_equivalent = 1_000_000_000 (≈ 10 SNS tokens).
2. Seller and buyer agree off-chain: buyer pays 8 ICP for neuron N.
3. Buyer sends 8 ICP to seller.
4. Seller calls manage_neuron(N, DisburseMaturity { percentage_to_disburse: 100,
       to_account: seller_account })
   → neuron.maturity_e8s_equivalent is immediately set to 0;
     disbursement of 1_000_000_000 e8s queued to seller_account.
5. Seller calls manage_neuron(N, AddNeuronPermissions { principal_id: buyer,
       permissions_to_add: [all permissions] })
6. Buyer calls manage_neuron(N, RemoveNeuronPermissions { principal_id: seller,
       permissions_to_remove: [all permissions] })
7. Buyer now controls neuron N, but maturity_e8s_equivalent == 0.
8. After 7 days, seller receives ~10 SNS tokens from the queued disbursement.
   Seller has collected 8 ICP + 10 SNS tokens; buyer has a depleted neuron.
```

### Citations

**File:** rs/sns/governance/src/governance.rs (L1609-1698)
```rust
    pub fn disburse_maturity(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse_maturity: &DisburseMaturity,
    ) -> Result<DisburseMaturityResponse, GovernanceError> {
        let neuron = self.get_neuron_result(id)?;
        neuron.check_authorized(caller, NeuronPermissionType::DisburseMaturity)?;

        // If no account was provided, transfer to the caller's account.
        let to_account: Account = match disburse_maturity.to_account.as_ref() {
            None => Account {
                owner: caller.0,
                subaccount: None,
            },
            Some(account) => Account::try_from(account.clone()).map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    format!("The given account to disburse the maturity to is invalid due to: {e}"),
                )
            })?,
        };
        let to_account_proto: AccountProto = AccountProto::from(to_account);

        if disburse_maturity.percentage_to_disburse > 100
            || disburse_maturity.percentage_to_disburse == 0
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "The percentage of maturity to disburse must be a value between 1 and 100 (inclusive).",
            ));
        }

        // The amount to deduct = the amount in the neuron * request.percentage / 100.
        let maturity_to_deduct = neuron
            .maturity_e8s_equivalent
            .checked_mul(disburse_maturity.percentage_to_disburse as u64)
            .expect("Overflow while processing maturity to disburse.")
            .checked_div(100)
            .expect("Error when processing maturity to disburse.")
            as u128;

        let maturity_to_deduct = maturity_to_deduct as u64;

        let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();
        let worst_case_maturity_modulation =
            apply_maturity_modulation(maturity_to_deduct, MIN_MATURITY_MODULATION_PERMYRIAD)
                // Applying maturity modulation is a safe operation.
                // However, in the case that the method fails to apply the equation, return an
                // error instead of throwing a panic.
                .map_err(|err| {
                    GovernanceError::new_with_message(
                        ErrorType::PreconditionFailed,
                        format!(
                            "Could not calculate worst case maturity modulation \
                            and therefore cannot disburse maturity. Err: {err}"
                        ),
                    )
                })?;

        if worst_case_maturity_modulation < transaction_fee_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "If worst case maturity modulation is applied (-5%) then this neuron would \
                     disburse {worst_case_maturity_modulation} e8s, but can't disburse an amount less than the transaction fee \
                     of {transaction_fee_e8s} e8s."
                ),
            ));
        }

        let now_seconds = self.env.now();
        let disbursement_in_progress = DisburseMaturityInProgress {
            amount_e8s: maturity_to_deduct,
            timestamp_of_disbursement_seconds: now_seconds,
            account_to_disburse_to: Some(to_account_proto),
            finalize_disbursement_timestamp_seconds: Some(
                now_seconds + MATURITY_DISBURSEMENT_DELAY_SECONDS,
            ),
        };

        // Re-borrow the neuron mutably to update now that the maturity has been
        // deducted and is waiting until the end of the window to modulate and disburse.
        let neuron = self.get_neuron_result_mut(id)?;
        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_deduct);
        neuron
            .disburse_maturity_in_progress
            .push(disbursement_in_progress);
```

**File:** rs/sns/governance/src/governance.rs (L4570-4575)
```rust
    fn add_neuron_permissions(
        &mut self,
        neuron_id: &NeuronId,
        caller: &PrincipalId,
        add_neuron_permissions: &AddNeuronPermissions,
    ) -> Result<(), GovernanceError> {
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L44-47)
```text
  // The principal has permission to disburse the neuron's maturity to a
  // given ledger account.
  NEURON_PERMISSION_TYPE_DISBURSE_MATURITY = 8;

```
