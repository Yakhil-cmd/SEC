### Title
SNS Governance `PreInitializationSwap` Mode Blocks User Neuron Disburse/Dissolve Operations, Locking Staked Tokens - (File: rs/sns/governance/src/types.rs)

### Summary
The SNS Governance canister's `PreInitializationSwap` mode blocks user-initiated `Disburse`, `DisburseMaturity`, `Split`, `Configure` (including `StartDissolving`/`StopDissolving`), and `MergeMaturity` commands via `manage_neuron`. This mode is set by the swap canister (controlled by NNS governance) and, critically, is **not automatically cleared if the swap is aborted**. Users who staked SNS tokens before the swap and whose neurons are in a dissolved state cannot withdraw their funds while the governance canister remains in `PreInitializationSwap` mode.

### Finding Description

The `manage_neuron_internal` function in the SNS Governance canister checks the current governance mode before executing any neuron command:

```rust
self.mode()
    .allows_manage_neuron_command_or_err(command, self.is_swap_canister(*caller))?;
```

The `allows_manage_neuron_command_or_err` implementation in `rs/sns/governance/src/types.rs` explicitly blocks the following commands when in `PreInitializationSwap` mode:

- `Command::Configure` (includes `StartDissolving`, `StopDissolving`, `IncreaseDissolveDelay`, `SetDissolveTimestamp`)
- `Command::Disburse`
- `Command::Split`
- `Command::MergeMaturity`
- `Command::DisburseMaturity`

The `_ => false` catch-all means any command not in the explicit allowlist is rejected. The allowlist only permits `Follow`, `MakeProposal`, `RegisterVote`, `AddNeuronPermissions`, `RemoveNeuronPermissions`, and `ClaimOrRefresh` (only for the swap canister).

The `PreInitializationSwap` mode is set when the NNS governance adopts a proposal to open a decentralization swap. Integration tests confirm that when a swap is **aborted**, the SNS governance mode **remains** `PreInitializationSwap` rather than transitioning to `Normal`:

```rust
if swap_finalization_status == SwapFinalizationStatus::Aborted {
    assert_eq!(
        sns.governance.get_mode(...).mode.unwrap(),
        sns_pb::governance::Mode::PreInitializationSwap as i32,
    );
```

This means that after a failed swap, all pre-existing neuron holders are permanently unable to disburse, dissolve, or otherwise manage their staked tokens through the governance canister, as long as the mode is not manually reset via an upgrade.

### Impact Explanation

**High.** Users who staked SNS tokens prior to the swap (e.g., founding team neurons, early contributors) have their funds locked in the SNS governance canister. They cannot:
- Call `Disburse` to withdraw dissolved neuron stake to their ledger account.
- Call `Configure` → `StartDissolving` to begin the dissolve process.
- Call `DisburseMaturity` to claim earned maturity rewards.

These are user-owned funds held in neuron subaccounts on the SNS ledger. The governance canister acts as the custodian, and while in `PreInitializationSwap` mode, it refuses to process any withdrawal-equivalent commands. If the mode is never cleared (e.g., if the SNS is abandoned after a failed swap, or if no upgrade proposal passes), the funds are permanently inaccessible.

### Likelihood Explanation

**Medium.** The `PreInitializationSwap` mode is a normal part of the SNS launch lifecycle and is triggered by NNS governance adopting a `CreateServiceNervousSystem` proposal. A swap abort is a realistic scenario (insufficient participation, swap timeout). The code explicitly confirms that an aborted swap leaves governance in `PreInitializationSwap` mode. Recovery requires a subsequent NNS governance proposal to upgrade the SNS governance canister and reset the mode — a process that may not happen if the SNS project is abandoned or if NNS voters do not act. No malicious actor is required; the condition arises from the normal protocol flow.

### Recommendation

1. In the SNS swap finalization path, when the swap is aborted (`Lifecycle::Aborted`), the swap canister should call `set_mode(Normal)` on the SNS governance canister (analogous to how it calls `set_sns_governance_to_normal_mode` on commit).
2. Alternatively, the `allows_manage_neuron_command_or_err` function should explicitly allow user exit/withdrawal commands (`Disburse`, `DisburseMaturity`, `Configure::StartDissolving`) even in `PreInitializationSwap` mode, since these do not threaten the integrity of the swap.
3. At minimum, document that an aborted swap leaves governance in a mode that locks user funds, and ensure the NNS has a clear recovery path.

### Proof of Concept

**Root cause — blocked commands in `PreInitializationSwap` mode:** [1](#0-0) 

**Explicit list of disallowed commands (includes `Disburse`, `DisburseMaturity`, `Configure`, `Split`, `MergeMaturity`):** [2](#0-1) 

**Mode check applied before every `manage_neuron` command:** [3](#0-2) 

**Confirmed by integration test: aborted swap leaves governance in `PreInitializationSwap` mode (not `Normal`):** [4](#0-3) 

**The `set_sns_governance_to_normal_mode` call only happens on committed swap, not on abort:** [5](#0-4) 

**`disburse_neuron` in SNS governance — a legitimate user withdrawal that is blocked by the mode check before it is ever reached:** [6](#0-5)

### Citations

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

**File:** rs/sns/governance/src/types/tests.rs (L304-317)
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
    };
```

**File:** rs/sns/governance/src/governance.rs (L1119-1136)
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
```

**File:** rs/sns/governance/src/governance.rs (L4781-4782)
```rust
        self.mode()
            .allows_manage_neuron_command_or_err(command, self.is_swap_canister(*caller))?;
```

**File:** rs/nervous_system/integration_tests/tests/sns_lifecycle.rs (L1118-1138)
```rust
    if swap_finalization_status == SwapFinalizationStatus::Aborted {
        assert_eq!(
            sns.governance
                .get_mode(&pocket_ic)
                .await
                .unwrap()
                .mode
                .unwrap(),
            sns_pb::governance::Mode::PreInitializationSwap as i32,
        );
    } else {
        assert_eq!(
            sns.governance
                .get_mode(&pocket_ic)
                .await
                .unwrap()
                .mode
                .unwrap(),
            sns_pb::governance::Mode::Normal as i32
        );
    }
```

**File:** rs/sns/swap/src/swap.rs (L1893-1904)
```rust
    pub async fn set_sns_governance_to_normal_mode(
        sns_governance_client: &mut impl SnsGovernanceClient,
    ) -> SetModeCallResult {
        // The SnsGovernanceClient Trait converts any errors to Err(CanisterCallError)
        // No panics should occur when issuing this message.
        sns_governance_client
            .set_mode(SetMode {
                mode: governance::Mode::Normal as i32,
            })
            .await
            .into()
    }
```
