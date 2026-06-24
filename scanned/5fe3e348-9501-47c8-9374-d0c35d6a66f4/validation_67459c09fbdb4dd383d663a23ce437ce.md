### Title
SNS Governance `disburse_neuron` and `disburse_maturity` Default Destination Uses Caller Instead of Neuron Owner, Enabling Authorized-Delegate Fund Drain - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

In SNS Governance, `disburse_neuron` and `disburse_maturity` perform a permission check that correctly allows any principal holding `NeuronPermissionType::Disburse` / `NeuronPermissionType::DisburseMaturity` to act on a neuron. However, when no explicit destination account is provided, both functions default the transfer target to `Account { owner: caller.0, ... }` — the **caller's** account — rather than the neuron owner's account. A principal that has been granted `Disburse` or `DisburseMaturity` permission (e.g., a hot-wallet or operational key) can therefore drain the neuron's entire stake or maturity to their own ledger account without the neuron owner's consent.

---

### Finding Description

`disburse_neuron` in SNS Governance first verifies the caller holds the `Disburse` permission:

```rust
neuron.check_authorized(caller, NeuronPermissionType::Disburse)?;
``` [1](#0-0) 

Then, when `to_account` is absent, it constructs the destination as:

```rust
// If no account was provided, transfer to the caller's (default) account.
let to_account = match disburse.to_account.as_ref() {
    None => Account {
        owner: caller.0,
        subaccount: None,
    },
``` [2](#0-1) 

The identical pattern appears in `disburse_maturity`:

```rust
neuron.check_authorized(caller, NeuronPermissionType::DisburseMaturity)?;
// If no account was provided, transfer to the caller's account.
let to_account: Account = match disburse_maturity.to_account.as_ref() {
    None => Account {
        owner: caller.0,
        subaccount: None,
    },
``` [3](#0-2) 

`check_authorized` succeeds for **any** principal listed in the neuron's permissions with the matching type — not only the neuron's original creator:

```rust
pub(crate) fn check_authorized(
    &self,
    principal: &PrincipalId,
    permission: NeuronPermissionType,
) -> Result<(), GovernanceError> {
    if !self.is_authorized(principal, permission) {
``` [4](#0-3) 

By contrast, NNS Governance restricts `disburse_neuron` to the neuron controller only (`is_controlled_by`), so `caller == controller` always holds and the default destination is safe: [5](#0-4) 

SNS Governance deliberately supports a richer permission model (`NeuronPermissionType`) that separates the disbursing principal from the neuron owner, but the default-destination logic was not updated to reflect this.

---

### Impact Explanation

A principal (e.g., a hot-wallet, an operational key, or a smart-contract canister) that has been granted `NeuronPermissionType::Disburse` on an SNS neuron can call `manage_neuron` → `Disburse { amount: None, to_account: None }`. Because `to_account` is absent, the entire staked balance is transferred to the **caller's** ledger account rather than the neuron owner's account. The neuron owner loses their full stake with no recourse. The same applies to accumulated maturity via `DisburseMaturity { percentage_to_disburse: 100, to_account: None }`.

**Impact: High** — complete, irreversible loss of neuron stake or maturity for the neuron owner.

---

### Likelihood Explanation

The attack requires that the neuron owner has previously granted `Disburse` or `DisburseMaturity` permission to a second principal. This is a common operational pattern in SNS (hot-wallet keys, DAO-controlled operational accounts, third-party integrations). The SNS permission model is explicitly designed to support this delegation. Once the permission exists, the attack requires only a single `manage_neuron` call with `to_account: None`, which is the natural default when no destination is specified.

**Likelihood: Medium** — depends on permission delegation, which is a standard SNS use case.

---

### Recommendation

When `to_account` is `None` and the caller is not the sole/primary owner of the neuron, the default destination should be the neuron's **owner** (the principal that created/controls the neuron), not the caller. Concretely:

1. Resolve the neuron's primary owner principal before the default-destination branch.
2. Use `Account { owner: neuron_owner_principal, subaccount: None }` as the fallback when `to_account` is absent.
3. Alternatively, require `to_account` to be explicitly set whenever the caller is not the neuron's sole controller, rejecting the call with `InvalidCommand` if it is absent.

Apply the same fix to both `disburse_neuron` and `disburse_maturity` in `rs/sns/governance/src/governance.rs`.

---

### Proof of Concept

1. Alice creates an SNS neuron with 1 000 SNS tokens staked.
2. Alice calls `manage_neuron` → `AddNeuronPermissions` to grant Bob's principal `NeuronPermissionType::Disburse`.
3. Alice's neuron dissolves (or is already dissolved).
4. Bob calls `manage_neuron` with:
   ```
   Command::Disburse(Disburse { amount: None, to_account: None })
   ```
5. `check_authorized(bob, Disburse)` passes.
6. `to_account` defaults to `Account { owner: bob.0, subaccount: None }`.
7. The SNS ledger transfers 1 000 SNS (minus fee) to Bob's account.
8. Alice's neuron stake is now zero; Bob holds Alice's tokens.

The same steps apply to `DisburseMaturity` with `percentage_to_disburse: 100, to_account: None`.

### Citations

**File:** rs/sns/governance/src/governance.rs (L1125-1127)
```rust
        // First check authorized
        let neuron = self.get_neuron_result(id)?;
        neuron.check_authorized(caller, NeuronPermissionType::Disburse)?;
```

**File:** rs/sns/governance/src/governance.rs (L1142-1147)
```rust
        // If no account was provided, transfer to the caller's (default) account.
        let to_account = match disburse.to_account.as_ref() {
            None => Account {
                owner: caller.0,
                subaccount: None,
            },
```

**File:** rs/sns/governance/src/governance.rs (L1615-1623)
```rust
        let neuron = self.get_neuron_result(id)?;
        neuron.check_authorized(caller, NeuronPermissionType::DisburseMaturity)?;

        // If no account was provided, transfer to the caller's account.
        let to_account: Account = match disburse_maturity.to_account.as_ref() {
            None => Account {
                owner: caller.0,
                subaccount: None,
            },
```

**File:** rs/sns/governance/src/neuron.rs (L104-109)
```rust
    pub(crate) fn check_authorized(
        &self,
        principal: &PrincipalId,
        permission: NeuronPermissionType,
    ) -> Result<(), GovernanceError> {
        if !self.is_authorized(principal, permission) {
```

**File:** rs/nns/governance/src/governance.rs (L1970-1999)
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

        if neuron_state != NeuronState::Dissolved {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Neuron {} has NOT been dissolved. It is in state {:?}",
                    id.id, neuron_state
                ),
            ));
        }

        if !is_neuron_kyc_verified {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Neuron {} is not kyc verified.", id.id),
            ));
        }

        // If no account was provided, transfer to the caller's account.
        let to_account: AccountIdentifier = match disburse.to_account.as_ref() {
            None => AccountIdentifier::new(*caller, None),
```
