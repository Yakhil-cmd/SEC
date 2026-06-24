### Title
SNS Neuron Seller Can Drain Maturity Before Completing Neuron Permission Transfer, Defrauding Buyer - (`rs/sns/governance/src/governance.rs`)

### Summary

In the SNS governance canister, a neuron owner can drain all accumulated maturity rewards via `DisburseMaturity` and then complete a neuron "transfer" (via `AddNeuronPermissions` + `RemoveNeuronPermissions`) in separate, non-atomic transactions. A buyer who pays for a neuron expecting to receive its accumulated maturity receives a neuron with zero maturity instead, suffering a direct financial loss.

### Finding Description

SNS neurons accumulate `maturity_e8s_equivalent` as voting rewards over time. Neuron "ownership transfer" in SNS is accomplished through the permission system: the seller grants the buyer all permissions via `AddNeuronPermissions`, then removes their own permissions via `RemoveNeuronPermissions`. These are separate, independent canister update calls with no atomicity guarantee between them.

The `disburse_maturity` function in `rs/sns/governance/src/governance.rs` only checks that the caller holds `NeuronPermissionType::DisburseMaturity` on the neuron: [1](#0-0) 

It performs no check for any pending or in-progress ownership transfer. After the call, `maturity_e8s_equivalent` is zeroed and a `DisburseMaturityInProgress` entry is pushed, routing the funds to the seller's account: [2](#0-1) 

The `add_neuron_permissions` function similarly has no awareness of a pending maturity drain: [3](#0-2) 

The SNS permission model explicitly supports granting `DisburseMaturity` as a separable, grantable permission: [4](#0-3) 

There is no atomic "transfer neuron with maturity" operation in the SNS governance interface: [5](#0-4) 

### Impact Explanation

A buyer who purchases an SNS neuron (off-chain or via a third-party marketplace canister) expecting to receive its accumulated maturity receives a neuron with zero maturity. The seller collects both the sale price and the maturity. The buyer suffers a direct, unrecoverable financial loss equal to the maturity that was drained. Since SNS neurons can accumulate substantial maturity over months or years of voting, the loss can be significant. The neuron's staked tokens remain intact, so the buyer cannot detect the drain until after the transfer is complete.

### Likelihood Explanation

SNS neuron secondary markets and OTC neuron sales are a realistic and documented use case. Any user with `DisburseMaturity` permission on a neuron (which is granted by default to the neuron claimer) can execute this sequence. No privileged access, governance majority, or threshold key is required. The attacker-controlled entry path is: call `manage_neuron` with `DisburseMaturity`, then call `manage_neuron` with `AddNeuronPermissions` for the buyer, then call `manage_neuron` with `RemoveNeuronPermissions` for self. All three are standard unprivileged ingress calls to the SNS governance canister.

### Recommendation

1. **Atomic transfer operation**: Introduce a dedicated `TransferNeuron` command that atomically transfers all permissions and preserves the current `maturity_e8s_equivalent` snapshot at the time of the transfer agreement, preventing the seller from draining maturity between agreement and settlement.
2. **Maturity lock on pending transfer**: Add a "transfer-in-progress" lock state to the neuron that blocks `DisburseMaturity`, `MergeMaturity`, and `StakeMaturity` while a permission handover is pending.
3. **Documentation**: At minimum, document prominently that neuron buyers must verify `maturity_e8s_equivalent` on-chain immediately before and after the permission transfer, and that no atomicity guarantee exists between `DisburseMaturity` and `AddNeuronPermissions`.

### Proof of Concept

```
// Alice owns SNS neuron N with 1,000,000 e8s of maturity.
// Bob agrees off-chain to pay 900,000 SNS tokens for the neuron.

// Step 1: Bob pays Alice 900,000 SNS tokens (off-chain or via escrow canister).

// Step 2: Alice drains maturity BEFORE completing the transfer.
manage_neuron(
    subaccount: N.id,
    command: DisburseMaturity { percentage_to_disburse: 100, to_account: Alice's account }
)
// Alice's account will receive ~1,000,000 SNS tokens after the 7-day delay.
// Neuron N now has maturity_e8s_equivalent = 0.

// Step 3: Alice adds Bob's permissions (appears to complete the sale).
manage_neuron(
    subaccount: N.id,
    command: AddNeuronPermissions { principal_id: Bob, permissions_to_add: [all permissions] }
)

// Step 4: Alice removes her own permissions.
manage_neuron(
    subaccount: N.id,
    command: RemoveNeuronPermissions { principal_id: Alice, permissions_to_remove: [all permissions] }
)

// Result: Bob controls neuron N but it has zero maturity.
// Alice collected 900,000 (sale) + ~1,000,000 (maturity) = ~1,900,000 SNS tokens.
// Bob paid 900,000 SNS tokens for a neuron with no maturity to claim.
```

The `disburse_maturity` function at `rs/sns/governance/src/governance.rs:1609` is the necessary vulnerable step: it accepts the drain without any check for a pending transfer, and the SNS governance protocol provides no atomic transfer primitive to prevent this ordering. [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1609-1616)
```rust
    pub fn disburse_maturity(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse_maturity: &DisburseMaturity,
    ) -> Result<DisburseMaturityResponse, GovernanceError> {
        let neuron = self.get_neuron_result(id)?;
        neuron.check_authorized(caller, NeuronPermissionType::DisburseMaturity)?;
```

**File:** rs/sns/governance/src/governance.rs (L1680-1698)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L4570-4634)
```rust
    fn add_neuron_permissions(
        &mut self,
        neuron_id: &NeuronId,
        caller: &PrincipalId,
        add_neuron_permissions: &AddNeuronPermissions,
    ) -> Result<(), GovernanceError> {
        let neuron = self.get_neuron_result(neuron_id)?;

        let permissions_to_add = add_neuron_permissions
            .permissions_to_add
            .as_ref()
            .ok_or_else(|| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    "AddNeuronPermissions command must provide permissions to add",
                )
            })?;

        // A simple check to prevent DoS attack with large number of permission changes.
        if permissions_to_add.permissions.len() > NeuronPermissionType::all().len() {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "AddNeuronPermissions command provided more permissions than exist in the system",
            ));
        }

        neuron
            .check_principal_authorized_to_change_permissions(caller, permissions_to_add.clone())?;

        self.nervous_system_parameters_or_panic()
            .check_permissions_are_grantable(permissions_to_add)?;

        let principal_id = add_neuron_permissions.principal_id.ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "AddNeuronPermissions command must provide a PrincipalId to add permissions to",
            )
        })?;

        let existing_permissions = neuron
            .permissions
            .iter()
            .find(|permission| permission.principal == Some(principal_id));

        let max_number_of_principals_per_neuron = self
            .nervous_system_parameters_or_panic()
            .max_number_of_principals_per_neuron
            .expect("NervousSystemParameters.max_number_of_principals_per_neuron must be present");

        // If the PrincipalId does not already exist in the neuron, make sure it can be added
        if existing_permissions.is_none()
            && neuron.permissions.len() == max_number_of_principals_per_neuron as usize
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Cannot add permission to neuron. Max \
                    number of principals reached {max_number_of_principals_per_neuron}"
                ),
            ));
        }

        // Re-borrow the neuron mutably to update now that the preconditions have been met
        self.get_neuron_result_mut(neuron_id)?
            .add_permissions_for_principal(principal_id, permissions_to_add.permissions.clone());
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L44-46)
```text
  // The principal has permission to disburse the neuron's maturity to a
  // given ledger account.
  NEURON_PERMISSION_TYPE_DISBURSE_MATURITY = 8;
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L2034-2050)
```text
  oneof command {
    Configure configure = 2;
    Disburse disburse = 3;
    Follow follow = 4;
    SetFollowing set_following = 14;
    // Making a proposal is defined by a proposal, which contains the proposer neuron.
    // Making a proposal will implicitly cast a yes vote for the proposing neuron.
    Proposal make_proposal = 5;
    RegisterVote register_vote = 6;
    Split split = 7;
    ClaimOrRefresh claim_or_refresh = 8;
    MergeMaturity merge_maturity = 9;
    DisburseMaturity disburse_maturity = 10;
    AddNeuronPermissions add_neuron_permissions = 11;
    RemoveNeuronPermissions remove_neuron_permissions = 12;
    StakeMaturity stake_maturity = 13;
  }
```
