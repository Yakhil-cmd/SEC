### Title
Single-Step `AddNeuronPermissions` Grants `ManagePrincipals` Immediately With No Confirmation From New Principal - (File: rs/sns/governance/src/neuron.rs)

### Summary
SNS governance allows a principal holding `ManagePrincipals` to grant that same permission to any arbitrary principal in a single `manage_neuron` call. The new principal acquires full neuron control immediately, with no acceptance step required. If the wrong principal is specified, the mistake is irrecoverable: the newly-empowered principal can immediately strip the original owner of all permissions, disburse the neuron's staked tokens, and vote on governance proposals.

### Finding Description
The `AddNeuronPermissions` command in SNS governance is processed by `check_principal_authorized_to_change_permissions` in `rs/sns/governance/src/neuron.rs`. The check only verifies that the *caller* holds `ManagePrincipals`; it does not require the *target* principal to accept or confirm the grant. The permission is written to state immediately upon the call succeeding.

`ManagePrincipals` is the most powerful SNS neuron permission: it allows the holder to add or remove any permission for any principal on the neuron, including removing the original owner's `ManagePrincipals`. Once a wrong principal receives `ManagePrincipals`, they can atomically remove the original owner's permissions in a follow-up call, making the situation irrecoverable without external governance intervention.

The relevant authorization gate:

```rust
// rs/sns/governance/src/neuron.rs
pub(crate) fn check_principal_authorized_to_change_permissions(
    &self,
    caller: &PrincipalId,
    permissions_to_change: NeuronPermissionList,
) -> Result<(), GovernanceError> {
    let sufficient_permissions = if permissions_to_change.is_exclusively_voting_related() {
        vec![
            NeuronPermissionType::ManagePrincipals,
            NeuronPermissionType::ManageVotingPermission,
        ]
    } else {
        vec![NeuronPermissionType::ManagePrincipals]
    };
    // caller_authorized check only — no acceptance from target
    ...
}
``` [1](#0-0) 

The `AddNeuronPermissions` command is defined in the SNS governance protobuf and accepted as a standard `manage_neuron` ingress call: [2](#0-1) 

The permission types, including `ManagePrincipals`, are applied directly to neuron state with no pending/staged state: [3](#0-2) 

### Impact Explanation
A neuron owner who accidentally specifies the wrong principal in `AddNeuronPermissions` with `ManagePrincipals` immediately loses the ability to recover. The wrong principal can:

1. Call `RemoveNeuronPermissions` to strip the original owner of `ManagePrincipals` — irrecoverable at the neuron level.
2. Call `Disburse` to transfer the neuron's staked SNS tokens to an arbitrary ledger account.
3. Vote on all SNS governance proposals using the neuron's voting power, potentially influencing treasury transfers, upgrades, or dapp canister deregistration.

The impact is permanent loss of staked SNS tokens and governance influence, with no on-chain recovery path once the original owner's `ManagePrincipals` is removed. [4](#0-3) 

### Likelihood Explanation
SNS principal IDs are long opaque byte strings. A neuron owner managing permissions via CLI, dapp UI, or programmatic tooling can trivially paste or type the wrong principal. The operation requires only a single `manage_neuron` ingress call from the current `ManagePrincipals` holder — no additional confirmation, no time delay, no acceptance from the target. The attacker entry path is a standard authenticated ingress message from any governance user who owns an SNS neuron with `ManagePrincipals`.

### Recommendation
Implement a two-step procedure for granting `ManagePrincipals` (and other non-voting critical permissions):

1. **Step 1 — Propose**: The current `ManagePrincipals` holder nominates a new principal, storing a `pending_manage_principals_grant { target, expiry }` in neuron state.
2. **Step 2 — Accept**: The nominated principal calls a new `accept_neuron_permissions` endpoint within the expiry window, at which point the permission is committed.

This mirrors the `approve`/`transferFrom` pattern cited in the original report and ensures the target principal is reachable and intentional. At minimum, a time-locked delay (e.g., 24 hours) before the grant takes effect would allow the original owner to cancel a mistaken nomination.

### Proof of Concept

```
// Attacker-controlled entry path (standard ingress):

// Step 1: Alice (ManagePrincipals holder) calls manage_neuron
// intending to grant ManagePrincipals to Carol but types Bob's principal.
manage_neuron({
  subaccount: alice_neuron_subaccount,
  command: AddNeuronPermissions({
    principal_id: BOB_PRINCIPAL,   // <-- typo, intended CAROL_PRINCIPAL
    permissions: [ManagePrincipals, Disburse, Vote, ...]
  })
})
// Bob immediately holds ManagePrincipals. No confirmation required.

// Step 2: Bob (unprivileged, now empowered) calls manage_neuron
manage_neuron({
  subaccount: alice_neuron_subaccount,
  command: RemoveNeuronPermissions({
    principal_id: ALICE_PRINCIPAL,
    permissions: [ManagePrincipals]
  })
})
// Alice can no longer manage her neuron. State is irrecoverable.

// Step 3: Bob disburses Alice's staked tokens
manage_neuron({
  subaccount: alice_neuron_subaccount,
  command: Disburse({ to_account: BOB_ACCOUNT, amount: ALL })
})
```

The root cause is in `rs/sns/governance/src/neuron.rs` `check_principal_authorized_to_change_permissions` — only the caller is checked; the target principal has no role in the authorization flow and the grant is applied atomically to neuron state. [1](#0-0)

### Citations

**File:** rs/sns/governance/src/neuron.rs (L142-179)
```rust
    /// Returns Ok if the caller has ManagePrincipals, or if the caller has
    /// ManageVotingPermission and the permissions to change relate to voting.
    pub(crate) fn check_principal_authorized_to_change_permissions(
        &self,
        caller: &PrincipalId,
        permissions_to_change: NeuronPermissionList,
    ) -> Result<(), GovernanceError> {
        // If the permissions to change are exclusively voting related,
        // ManagePrincipals or ManageVotingPermission is sufficient.
        // Otherwise, only ManagePrincipals is sufficient.
        let sufficient_permissions = if permissions_to_change.is_exclusively_voting_related() {
            vec![
                NeuronPermissionType::ManagePrincipals,
                NeuronPermissionType::ManageVotingPermission,
            ]
        } else {
            vec![NeuronPermissionType::ManagePrincipals]
        };

        // The caller is authorized if they have any of the sufficient permissions
        let caller_authorized = sufficient_permissions
            .iter()
            .any(|sufficient_permission| self.is_authorized(caller, *sufficient_permission));

        if caller_authorized {
            Ok(())
        } else {
            let caller_permissions = self.permissions_for_principal(caller);
            Err(GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                format!(
                    "Caller '{caller:?}' is not authorized to modify permissions {permissions_to_change} for neuron '{}' as it does not have any of {sufficient_permissions:?}. (Caller's permissions are {caller_permissions})",
                    self.id.as_ref().expect("Neuron must have a NeuronId"),
                ),
            ))
        }
    }

```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1808-1856)
```text
// An operation that modifies a neuron.
message ManageNeuron {
  // The modified neuron's subaccount which also serves as the neuron's ID.
  bytes subaccount = 1;

  // The operation that increases a neuron's dissolve delay. It can be
  // increased up to a maximum defined in the nervous system parameters.
  message IncreaseDissolveDelay {
    // The additional dissolve delay that should be added to the neuron's
    // current dissolve delay.
    uint32 additional_dissolve_delay_seconds = 1;
  }

  // The operation that starts dissolving a neuron, i.e., changes a neuron's
  // state such that it is dissolving.
  message StartDissolving {}

  // The operation that stops dissolving a neuron, i.e., changes a neuron's
  // state such that it is non-dissolving.
  message StopDissolving {}

  // An (idempotent) alternative to IncreaseDissolveDelay where the dissolve delay
  // is passed as an absolute timestamp in seconds since the Unix epoch.
  message SetDissolveTimestamp {
    // The time when the neuron (newly) should become dissolved, in seconds
    // since the Unix epoch.
    uint64 dissolve_timestamp_seconds = 1;
  }

  // Changes auto-stake maturity for this Neuron. While on, auto-stake
  // maturity will cause all the maturity generated by voting rewards
  // to this neuron to be automatically staked and contribute to the
  // voting power of the neuron.
  message ChangeAutoStakeMaturity {
    bool requested_setting_for_auto_stake_maturity = 1;
  }

  // Commands that only configure a given neuron, but do not interact
  // with the outside world. They all require the caller to have
  // `NeuronPermissionType::ConfigureDissolveState` for the neuron.
  message Configure {
    oneof operation {
      IncreaseDissolveDelay increase_dissolve_delay = 1;
      StartDissolving start_dissolving = 2;
      StopDissolving stop_dissolving = 3;
      SetDissolveTimestamp set_dissolve_timestamp = 4;
      ChangeAutoStakeMaturity change_auto_stake_maturity = 5;
    }
  }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1858-1875)
```text
  // The operation that disburses a given number of tokens or all of a
  // neuron's tokens (if no argument is provided) to a given ledger account.
  // Thereby, the neuron's accumulated fees are burned and (if relevant in
  // the given nervous system) the token equivalent of the neuron's accumulated
  // maturity is minted and also transferred to the specified account.
  message Disburse {
    message Amount {
      uint64 e8s = 1;
    }

    // The (optional) amount to disburse out of the neuron. If not specified the cached
    // stake is used.
    Amount amount = 1;

    // The ledger account to which the disbursed tokens are transferred.
    Account to_account = 2;
  }

```
