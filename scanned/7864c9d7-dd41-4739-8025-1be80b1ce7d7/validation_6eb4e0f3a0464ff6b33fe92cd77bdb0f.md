### Title
Delegated `Disburse`/`DisburseMaturity` Permission Allows Caller to Redirect Neuron Funds to Their Own Account - (File: rs/sns/governance/src/governance.rs)

### Summary
In SNS governance, `disburse_neuron` and `disburse_maturity` accept an optional `to_account`. When `to_account` is omitted, both functions default to `Account { owner: caller.0, ... }` — the **caller's** account — rather than the neuron owner's account. Because SNS uses a fine-grained permission model where `NeuronPermissionType::Disburse` and `NeuronPermissionType::DisburseMaturity` can be granted to any arbitrary principal via `AddNeuronPermissions`, a delegated principal can call either function without specifying `to_account` and receive the neuron's entire stake or maturity into their own wallet.

### Finding Description

SNS neurons support a granular permission model. A neuron owner (or any principal with `ManagePrincipals`) can grant `NeuronPermissionType::Disburse` or `NeuronPermissionType::DisburseMaturity` to any external principal. [1](#0-0) 

The `disburse_neuron` function checks authorization correctly: [2](#0-1) 

But then resolves the destination account using `caller` instead of the neuron owner: [3](#0-2) 

The same pattern exists in `disburse_maturity`: [4](#0-3) 

This is the direct IC analog of the ZeroLocker `_burn()` bug: both use the **caller's identity** in an internal operation where the **owner's identity** should be used. In ZeroLocker, `_removeTokenFrom(msg.sender, _tokenId)` causes a revert for approved users. Here, `owner: caller.0` silently redirects funds to the delegate.

By contrast, NNS governance's `disburse_neuron` applies the same `caller` default but is safe because it gates the entire function behind `is_controlled_by(caller)`: [5](#0-4) 

In SNS, no such restriction exists — `Disburse` is a separately grantable permission, so the caller and the neuron owner can be different principals.

Permissions are granted via `AddNeuronPermissions`, which is callable by any principal holding `ManagePrincipals`: [6](#0-5) 

### Impact Explanation

A principal granted `NeuronPermissionType::Disburse` can call `manage_neuron` with `Command::Disburse { amount: None, to_account: None }` on a dissolved neuron. The entire cached stake is transferred to the **delegate's** account, not the neuron owner's. Similarly, a principal granted `NeuronPermissionType::DisburseMaturity` can drain all accrued maturity to themselves by calling `disburse_maturity` with `to_account: None`. Both operations are irreversible on-chain ledger transfers.

### Likelihood Explanation

The `Disburse` and `DisburseMaturity` permissions are explicitly listed as grantable in `NervousSystemParameters::neuron_grantable_permissions`. SNS documentation and the permission system encourage delegation. A neuron owner who grants these permissions to a "trusted" party (e.g., a dapp frontend canister, a co-signer, or an automation bot) is exposed. The attacker-controlled entry path is a standard `manage_neuron` ingress call — no privileged access, no key compromise, no threshold attack required.

### Recommendation

When `to_account` is `None` and the caller is not the neuron owner, either:

1. **Reject the call** — require `to_account` to be explicitly specified when the caller does not hold `ManagePrincipals` on the neuron, or
2. **Default to the neuron owner's account** — resolve the neuron's original claimer/controller principal and use that as the default destination.

```rust
// disburse_neuron — suggested fix
let to_account = match disburse.to_account.as_ref() {
    None => {
        // Resolve the neuron owner, not the caller
        let owner = neuron.owner_principal()?; // e.g., principal with ManagePrincipals
        Account { owner: owner.0, subaccount: None }
    }
    Some(ai_pb) => Account::try_from(ai_pb.clone())...
};
```

The same fix applies to `disburse_maturity`.

### Proof of Concept

1. Neuron owner `Alice` claims an SNS neuron with 100 SNS tokens and dissolves it.
2. Alice grants `NeuronPermissionType::Disburse` to `Bob` via `AddNeuronPermissions`.
3. Bob sends an ingress `manage_neuron` call:
   ```
   Command::Disburse(Disburse { amount: None, to_account: None })
   ```
4. `disburse_neuron` passes `neuron.check_authorized(&Bob, Disburse)` ✓
5. `to_account` resolves to `Account { owner: Bob, subaccount: None }` per line 1144–1146.
6. The ledger transfer sends ~100 SNS tokens to Bob's account.
7. Alice's neuron stake is now zero; Bob holds the funds. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L34-46)
```text
  // The principal has permission to disburse the neuron.
  NEURON_PERMISSION_TYPE_DISBURSE = 5;

  // The principal has permission to split the neuron.
  NEURON_PERMISSION_TYPE_SPLIT = 6;

  // The principal has permission to merge the neuron's maturity into
  // the neuron's stake.
  NEURON_PERMISSION_TYPE_MERGE_MATURITY = 7;

  // The principal has permission to disburse the neuron's maturity to a
  // given ledger account.
  NEURON_PERMISSION_TYPE_DISBURSE_MATURITY = 8;
```

**File:** rs/sns/governance/src/governance.rs (L1119-1154)
```rust
    pub async fn disburse_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse: &manage_neuron::Disburse,
    ) -> Result<u64, GovernanceError> {
        // First check authorized
        let neuron = self.get_neuron_result(id)?;
        neuron.check_authorized(caller, NeuronPermissionType::Disburse)?;

        // Check that the neuron is dissolved.
        let state = neuron.state(self.env.now());
        if state != NeuronState::Dissolved {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Neuron {id} is NOT dissolved. It is in state {state:?}"),
            ));
        }

        let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();

        let from_subaccount = neuron.subaccount()?;

        // If no account was provided, transfer to the caller's (default) account.
        let to_account = match disburse.to_account.as_ref() {
            None => Account {
                owner: caller.0,
                subaccount: None,
            },
            Some(ai_pb) => Account::try_from(ai_pb.clone()).map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    format!("The recipient's subaccount is invalid due to: {e}"),
                )
            })?,
        };
```

**File:** rs/sns/governance/src/governance.rs (L1609-1630)
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
```

**File:** rs/sns/governance/src/governance.rs (L4570-4597)
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
```

**File:** rs/nns/governance/src/governance.rs (L1970-1978)
```rust
        if !is_neuron_controlled_by_caller {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                format!(
                    "Caller '{:?}' is not authorized to control neuron '{}'.",
                    caller, id.id
                ),
            ));
        }
```
