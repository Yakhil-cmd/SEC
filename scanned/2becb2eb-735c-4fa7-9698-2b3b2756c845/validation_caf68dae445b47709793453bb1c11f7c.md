### Title
SNS Governance Permanently Locked in `PreInitializationSwap` Mode After Aborted Swap, Blocking Developer Neuron Wind-Down - (`rs/sns/swap/src/swap.rs`, `rs/sns/governance/src/types.rs`)

---

### Summary

When an SNS decentralization swap is aborted (`LIFECYCLE_ABORTED`), the SNS governance canister is never transitioned out of `PreInitializationSwap` mode. This permanently blocks developer neurons from being dissolved or disbursed via normal protocol operations, leaving staked SNS tokens inaccessible — directly analogous to the Compound bug where deprecated markets (with `borrowGuardianPaused == true`) cannot be liquidated.

---

### Finding Description

**Root cause in `rs/sns/swap/src/swap.rs` — `finalize_inner`:**

The `finalize_inner` function handles both committed and aborted swap outcomes. For a **committed** swap, it eventually calls `set_sns_governance_to_normal_mode` (line 1610). For an **aborted** swap, `should_restore_dapp_control()` returns `true`, triggering an early return at line 1583 — before `set_sns_governance_to_normal_mode` is ever reached: [1](#0-0) 

This means the SNS governance canister remains in `PreInitializationSwap` mode indefinitely after a failed swap.

**Enforcement in `rs/sns/governance/src/types.rs` — `manage_neuron_command_is_allowed_in_pre_initialization_swap_or_err`:**

In `PreInitializationSwap` mode, the following `manage_neuron` commands are unconditionally rejected for all callers: [2](#0-1) 

The blocked commands include `Configure` (which covers `StartDissolving`, `StopDissolving`, `IncreaseDissolveDelay`), `Disburse`, `Split`, `MergeMaturity`, and `DisburseMaturity`.

**Developer neurons exist before the swap:**

Developer neurons are created at SNS initialization with the initial token distribution, before the swap opens. After a failed swap, these neurons — and their staked tokens — are permanently inaccessible through normal protocol operations.

**Mode definition:** [3](#0-2) 

**Confirmed by test data** — the disallowed commands in `PreInitializationSwap` mode are explicitly enumerated: [4](#0-3) 

---

### Impact Explanation

Developer neurons in a failed SNS are permanently locked in `PreInitializationSwap` mode. Developers cannot:
- Start dissolving their neurons (`Configure` → `StartDissolving` is blocked)
- Disburse their staked tokens (`Disburse` is blocked)
- Split or merge maturity

Their staked SNS tokens are inaccessible through any unprivileged protocol path. Recovery requires NNS governance intervention (a privileged operation requiring a governance proposal and vote), which is not guaranteed to be timely or to occur at all for small/failed SNS projects.

This is a direct analog to the Compound bug: just as `borrowGuardianPaused == true` prevents liquidation of bad positions in deprecated markets, `PreInitializationSwap` mode prevents wind-down of developer positions in deprecated SNS instances.

---

### Likelihood Explanation

Any SNS swap can fail if the minimum participation threshold is not reached before the deadline — this is a normal, expected protocol flow, not an edge case. The `LIFECYCLE_ABORTED` state is explicitly designed into the protocol: [5](#0-4) 

Every failed SNS swap with developer neurons (i.e., every SNS that had an initial token distribution) is affected. The entry path requires no privileged access: any unprivileged participant can observe the aborted state and attempt to dissolve/disburse a developer neuron, receiving the rejection error. The stuck state is triggered by the normal protocol flow, not by an attacker.

---

### Recommendation

In `finalize_inner` (`rs/sns/swap/src/swap.rs`), when the swap is aborted, transition the SNS governance mode to `Normal` (or a new `Deprecated` mode that permits `Configure`/`Disburse` but blocks treasury operations) before returning. Specifically, `set_sns_governance_to_normal_mode` should be called in the aborted path, not only in the committed path.

Alternatively, in `manage_neuron_command_is_allowed_in_pre_initialization_swap_or_err` (`rs/sns/governance/src/types.rs`), add a check that permits `Configure` (specifically `StartDissolving`) and `Disburse` commands when the associated swap canister reports an `ABORTED` or terminal state — analogous to the Compound fix's `isDeprecated` check that bypasses the close-factor restriction.

---

### Proof of Concept

1. Create an SNS with developer neurons (staked tokens in initial distribution).
2. Open the decentralization swap.
3. Allow the swap to expire without reaching `min_participants` or `min_direct_participation_icp_e8s` — swap transitions to `LIFECYCLE_ABORTED`.
4. `finalize` is called (manually or via auto-finalize): ICP is refunded to buyers, dapp control is restored to fallback controllers, and the function returns early without calling `set_sns_governance_to_normal_mode`.
5. Attempt to dissolve a developer neuron by calling `manage_neuron` with `Configure { StartDissolving }`.
6. Observe rejection: `"Because governance is currently in PreInitializationSwap mode, manage_neuron commands of this type are not allowed"`. [6](#0-5) 

Developer tokens remain permanently locked with no unprivileged recovery path.

### Citations

**File:** rs/sns/swap/src/swap.rs (L1572-1584)
```rust
        if self.should_restore_dapp_control() {
            // Restore controllers of dapp canisters to their original
            // owners (i.e. self.init.fallback_controller_principal_ids).
            finalize_swap_response.set_set_dapp_controllers_result(
                self.restore_dapp_controllers_for_finalize(environment.sns_root_mut())
                    .await,
            );

            // In the case of returning control of the dapp(s) to the fallback
            // controllers, finalize() need not do any more work, so always return
            // and end execution.
            return finalize_swap_response;
        }
```

**File:** rs/sns/governance/src/types.rs (L182-211)
```rust
    fn manage_neuron_command_is_allowed_in_pre_initialization_swap_or_err(
        command: &manage_neuron::Command,
        caller_is_swap_canister: bool,
    ) -> Result<(), GovernanceError> {
        use manage_neuron::Command as C;
        let ok = match command {
            C::Follow(_)
            | C::MakeProposal(_)
            | C::RegisterVote(_)
            | C::AddNeuronPermissions(_)
            | C::RemoveNeuronPermissions(_) => true,

            C::ClaimOrRefresh(_) => caller_is_swap_canister,

            _ => false,
        };

        if ok {
            return Ok(());
        }

        Err(GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!(
                "Because governance is currently in PreInitializationSwap mode, \
                 manage_neuron commands of this type are not allowed \
                 (caller_is_swap_canister={caller_is_swap_canister}). command: {command:#?}",
            ),
        ))
    }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1591-1603)
```text
  enum Mode {
    // This forces people to explicitly populate the mode field.
    MODE_UNSPECIFIED = 0;

    // All operations are allowed.
    MODE_NORMAL = 1;

    // In this mode, various operations are not allowed in order to ensure the
    // integrity of the initial token swap.
    MODE_PRE_INITIALIZATION_SWAP = 2;
  }

  Mode mode = 19;
```

**File:** rs/sns/governance/src/types/tests.rs (L304-316)
```rust
        #[rustfmt::skip]
        let disallowed_in_pre_initialization_swap = vec! [
            Command::Configure        (Default::default()),
            Command::Disburse         (Default::default()),
            Command::Split            (Default::default()),
            Command::MergeMaturity    (Default::default()),
            Command::DisburseMaturity (Default::default()),
        ];

        // Only the swap canister is allowed to do this in PreInitializationSwap.
        let claim_or_refresh = Command::ClaimOrRefresh(Default::default());

        (allowed_in_pre_initialization_swap, disallowed_in_pre_initialization_swap, claim_or_refresh)
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L40-44)
```text
  // In ABORTED state the token swap has been aborted, e.g., because the due
  // date/time occurred before the minimum (reserve) amount of ICP has been
  // retrieved. On a call to `finalize`, participants get their ICP refunded.
  LIFECYCLE_ABORTED = 4;
}
```
