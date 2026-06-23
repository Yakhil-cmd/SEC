### Title
NNS Governance Hotkeys Cannot Call `stake_maturity_of_neuron` Despite Delegation Design - (File: `rs/nns/governance/src/governance.rs`)

---

### Summary

The NNS Governance canister's `stake_maturity_of_neuron` function uses `neuron.is_controlled_by(caller)` to authorize callers, restricting the operation to the neuron's controller only. However, the NNS governance system explicitly supports hotkeys as a delegation mechanism for neuron management, and the proto definition for `StakeMaturity` states "**The caller** can choose a percentage of the current maturity to stake" — not "the controller" — creating an inconsistency with the implementation. Hotkey holders, who are authorized delegates for neuron management, cannot stake maturity on behalf of the controller even though they can perform other neuron management operations of comparable or greater financial significance (e.g., `JoinCommunityFund`, `Follow`).

---

### Finding Description

The NNS Governance canister has an explicit two-tier authorization model:

1. **Controller** — the primary owner of a neuron (`is_controlled_by`)
2. **Hot keys** — delegated principals that can perform certain operations on behalf of the controller (`is_hotkey_or_controller`)

The proto documentation for `AddHotKey` explicitly states hotkeys provide "an alternative to using the controller principal's cold key to manage the neuron." The `is_authorized_to_configure_or_err` function in `rs/nns/governance/src/neuron/types.rs` allows hotkeys to call `JoinCommunityFund` and `LeaveCommunityFund` (which commits the neuron's maturity to the Neurons' Fund — a significant financial commitment). The `follow` function in `rs/nns/governance/src/governance.rs` allows hotkeys to change followees.

However, `stake_maturity_of_neuron` at line 2763 uses `neuron.is_controlled_by(caller)`:

```rust
let (neuron_state, is_neuron_controlled_by_caller, neuron_maturity_e8s_equivalent) =
    self.with_neuron(id, |neuron| {
        (
            neuron.state(self.env.now()),
            neuron.is_controlled_by(caller),   // ← controller-only check
            neuron.maturity_e8s_equivalent,
        )
    })?;
...
if !is_neuron_controlled_by_caller {
    return Err(GovernanceError::new(ErrorType::NotAuthorized));
}
```

The proto definition for `StakeMaturity` in `rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto` says:

```
// Stake the maturity of a neuron.
// The caller can choose a percentage of of the current maturity to stake.
```

This is in direct contrast to `DisburseMaturity` in the same proto file, which explicitly says:

```
// The controller can choose a percentage of the current maturity to disburse
```

The distinction is intentional in the proto: `DisburseMaturity` moves ICP out of the neuron (controller-only is appropriate), while `StakeMaturity` only converts maturity to staked ICP within the neuron (no ICP leaves the neuron). The proto says "the caller" for `StakeMaturity`, implying hotkeys should be allowed — but the implementation uses `is_controlled_by`, which only allows the controller.

This inconsistency is further confirmed by the SNS governance canister, which implements the same `stake_maturity_of_neuron` operation using a permission-based system (`NeuronPermissionType::StakeMaturity`) that can be delegated to any principal — not just the controller.

---

### Impact Explanation

A hotkey holder who has been delegated neuron management authority cannot call `StakeMaturity` on behalf of the controller. This forces the controller to use their cold key (e.g., a hardware wallet or air-gapped device) for an operation that:
- Does not move ICP out of the neuron
- Is non-destructive (only increases staked ICP within the neuron)
- Is explicitly described in the proto as callable by "the caller" (not "the controller")

The proto documentation implies hotkeys should be able to stake maturity, but the implementation silently rejects their calls with `NotAuthorized`. This defeats the purpose of the hotkey delegation system for this specific operation and creates an inconsistency with the stated design intent.

---

### Likelihood Explanation

Any neuron owner who has set up hotkeys for day-to-day neuron management and wants to stake maturity without using their cold key will encounter this issue. Hotkeys are a commonly used feature of the NNS governance system. The attacker-controlled entry path is straightforward: a hotkey holder sends an ingress message to the NNS governance canister calling `manage_neuron` with a `StakeMaturity` command. No special privileges are required — the hotkey holder is an unprivileged ingress sender who has been legitimately delegated authority.

---

### Recommendation

Change `stake_maturity_of_neuron` to use `is_hotkey_or_controller(caller)` instead of `is_controlled_by(caller)`, consistent with the proto documentation and the delegation model. This aligns with how `JoinCommunityFund` and `LeaveCommunityFund` are handled in `is_authorized_to_configure_or_err`, and with how the SNS governance canister handles the same operation via `NeuronPermissionType::StakeMaturity`.

---

### Proof of Concept

1. Create a neuron with controller principal A (cold key).
2. Add hotkey principal B to the neuron via `AddHotKey` (succeeds — hotkeys can be added by the controller).
3. As hotkey B, call `manage_neuron` on the NNS governance canister with `StakeMaturity { percentage_to_stake: Some(100) }`.
4. The call fails with `GovernanceError { error_type: NotAuthorized }` — hotkey B is rejected.
5. The same call succeeds when made by controller A.

The root cause is at: [1](#0-0) 

The controller-only check `is_controlled_by(caller)` at line 2763 is inconsistent with the proto documentation: [2](#0-1) 

Which says "The **caller** can choose" (not "the controller"), in contrast to `DisburseMaturity`: [3](#0-2) 

The hotkey delegation model is defined in: [4](#0-3) 

And the inconsistency with `JoinCommunityFund`/`LeaveCommunityFund` (which allow hotkeys) is visible in: [5](#0-4) 

The SNS governance correctly uses a permission-based system for the same operation: [6](#0-5)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2759-2784)
```rust
        let (neuron_state, is_neuron_controlled_by_caller, neuron_maturity_e8s_equivalent) =
            self.with_neuron(id, |neuron| {
                (
                    neuron.state(self.env.now()),
                    neuron.is_controlled_by(caller),
                    neuron.maturity_e8s_equivalent,
                )
            })?;

        if neuron_state == NeuronState::Spawning {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Can't perform operation on neuron: Neuron is spawning.",
            ));
        }

        if neuron_state == NeuronState::Dissolved {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Can't perform operation on neuron: Neuron is dissolved.",
            ));
        }

        if !is_neuron_controlled_by_caller {
            return Err(GovernanceError::new(ErrorType::NotAuthorized));
        }
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L883-890)
```text
  // Stake the maturity of a neuron.
  // The caller can choose a percentage of of the current maturity to stake.
  // If 'percentage_to_stake' is not provided, all of the neuron's current
  // maturity will be staked.
  message StakeMaturity {
    // The percentage of maturity to stake, from 1 to 100 (inclusive).
    optional uint32 percentage_to_stake = 1;
  }
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L973-979)
```text
  // Disburse the maturity of a neuron to any ledger account. If an account is not specified, the
  // controller's account will be used. The controller can choose a percentage of the current
  // maturity to disburse to the ledger account. The resulting amount to disburse must be at least 1
  // ICP. The disbursement has a 7-day delay before it is finalized. At the finalization time, the
  // maturity modulation will be applied to the amount, which can make the amount [95%, 105%] of the
  // original amount.
  message DisburseMaturity {
```

**File:** rs/nns/governance/src/neuron/types.rs (L253-256)
```rust
    /// Returns true if and only if `principal` is either the controller or a hotkey
    fn is_hotkey_or_controller(&self, principal: &PrincipalId) -> bool {
        self.is_controlled_by(principal) || self.hot_keys.contains(principal)
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L771-807)
```rust
    fn is_authorized_to_configure_or_err(
        &self,
        caller: &PrincipalId,
        configure: &Operation,
    ) -> Result<(), GovernanceError> {
        use Operation::{JoinCommunityFund, LeaveCommunityFund};

        match configure {
            // The controller and hotkeys are allowed to change Neuron Fund membership.
            JoinCommunityFund(_) | LeaveCommunityFund(_) => {
                if self.is_hotkey_or_controller(caller) {
                    Ok(())
                } else {
                    Err(GovernanceError::new_with_message(
                        ErrorType::NotAuthorized,
                        format!(
                            "Caller '{caller:?}' must be the controller or hotkey of the neuron to join or leave the neuron fund.",
                        ),
                    ))
                }
            }

            // Only the controller is allowed to perform other configure operations.
            _ => {
                if self.is_controlled_by(caller) {
                    Ok(())
                } else {
                    Err(GovernanceError::new_with_message(
                        ErrorType::NotAuthorized,
                        format!(
                            "Caller '{caller:?}' must be the controller of the neuron to perform this operation:\n{configure:#?}",
                        ),
                    ))
                }
            }
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L1550-1552)
```rust
        if !neuron.is_authorized(caller, NeuronPermissionType::StakeMaturity) {
            return Err(GovernanceError::new(ErrorType::NotAuthorized));
        }
```
