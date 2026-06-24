### Title
Off-by-One in `is_vesting` Boundary Check Blocks Neuron Operations at Exact Vesting Expiry - (File: rs/sns/governance/src/neuron.rs)

### Summary
The `is_vesting` function in SNS governance uses a `>=` comparison instead of `>` when checking whether a neuron's vesting period has elapsed. This causes the neuron to be incorrectly treated as still-vesting at the exact second the vesting period expires, blocking the neuron owner from performing operations (StartDissolving, Disburse, Split, etc.) for one second beyond the intended vesting period end.

### Finding Description
The `is_vesting` method in `rs/sns/governance/src/neuron.rs` is:

```rust
pub fn is_vesting(&self, now: u64) -> bool {
    self.vesting_period_seconds
        .map(|vesting_period_seconds| {
            self.created_timestamp_seconds + vesting_period_seconds >= now
        })
        .unwrap_or_default()
}
```

The condition `self.created_timestamp_seconds + vesting_period_seconds >= now` evaluates to `true` when `now` equals exactly `created_timestamp_seconds + vesting_period_seconds`. At that instant, the full vesting duration has elapsed — the neuron should no longer be vesting — but the `>=` check incorrectly keeps it in the vesting state for one additional second.

The existing unit test confirms this behavior:

```rust
// created_timestamp_seconds = 3400, vesting_period_seconds = 600
// vesting expires at 3400 + 600 = 4000
assert!(neuron.is_vesting(4000));  // BUG: still vesting at expiry
assert!(!neuron.is_vesting(4001)); // not vesting one second later
``` [1](#0-0) 

The `is_vesting` result gates `check_command_is_valid_if_neuron_is_vesting`, which blocks the following operations when `is_vesting` returns `true`:

- `StartDissolving`
- `IncreaseDissolveDelay`
- `StopDissolving`
- `SetDissolveTimestamp`
- `Disburse`
- `Split` [2](#0-1) 

### Impact Explanation
At the exact second `now == created_timestamp_seconds + vesting_period_seconds`, an SNS neuron owner whose vesting period has fully elapsed is incorrectly denied the ability to start dissolving, disburse, or split their neuron. The neuron owner must wait one additional second before these operations are permitted. This is a governance authorization bug: a user with a fully-elapsed vesting lock is incorrectly treated as still-locked.

### Likelihood Explanation
Low. The IC governance canister measures time in seconds. The off-by-one only manifests at the single second `now == created_timestamp_seconds + vesting_period_seconds`. A user would need to submit a `manage_neuron` call that is processed at exactly that second. In practice this is rare, but the boundary condition is definitively wrong and the test suite explicitly demonstrates the incorrect behavior.

### Recommendation
Change `>=` to `>` in `is_vesting`:

```rust
pub fn is_vesting(&self, now: u64) -> bool {
    self.vesting_period_seconds
        .map(|vesting_period_seconds| {
-           self.created_timestamp_seconds + vesting_period_seconds >= now
+           self.created_timestamp_seconds + vesting_period_seconds > now
        })
        .unwrap_or_default()
}
```

This matches the semantic intent: the neuron is vesting while `now` is strictly less than the expiry timestamp, and is no longer vesting once the full vesting duration has elapsed. [3](#0-2) 

### Proof of Concept
Using the existing test fixture with `created_timestamp_seconds = 3400` and `vesting_period_seconds = 600` (expiry at `now = 4000`):

1. At `now = 4000`, `is_vesting` returns `true` (bug: should return `false`).
2. `check_command_is_valid_if_neuron_is_vesting` is called with `now = 4000`.
3. The neuron owner attempts `StartDissolving` — receives `PreconditionFailed: Neuron is vesting and cannot call StartDissolving`.
4. At `now = 4001`, `is_vesting` returns `false` and the same call succeeds. [4](#0-3)

### Citations

**File:** rs/sns/governance/src/neuron.rs (L795-802)
```rust
    /// Returns true if this neuron is vesting, false otherwise
    pub fn is_vesting(&self, now: u64) -> bool {
        self.vesting_period_seconds
            .map(|vesting_period_seconds| {
                self.created_timestamp_seconds + vesting_period_seconds >= now
            })
            .unwrap_or_default()
    }
```

**File:** rs/sns/governance/src/governance.rs (L4844-4895)
```rust
    /// Returns an error if the given neuron is vesting and the given command cannot be called by
    /// a vesting neuron
    fn check_command_is_valid_if_neuron_is_vesting(
        &self,
        neuron_id: &NeuronId,
        command: &manage_neuron::Command,
    ) -> Result<(), GovernanceError> {
        use manage_neuron::{Command::*, configure::Operation::*};

        // If this is a "claim" call, the neuron doesn't exist yet, so we return (because no checks
        // can be made). A "refresh" call can be made on a vesting neuron, so in this case also
        // results in returning Ok.
        if let ClaimOrRefresh(_) = command {
            return Ok(());
        }

        let neuron = self.get_neuron_result(neuron_id)?;

        if !neuron.is_vesting(self.env.now()) {
            return Ok(());
        }

        let err = |op: &str| -> Result<(), GovernanceError> {
            Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Neuron {neuron_id} is vesting and cannot call {op}"),
            ))
        };

        match command {
            Configure(configure) => match configure.operation {
                Some(IncreaseDissolveDelay(_)) => err("IncreaseDissolveDelay"),
                Some(StartDissolving(_)) => err("StartDissolving"),
                Some(StopDissolving(_)) => err("StopDissolving"),
                Some(SetDissolveTimestamp(_)) => err("SetDissolveTimestamp"),
                Some(ChangeAutoStakeMaturity(_)) => Ok(()),
                None => Ok(()),
            },
            Disburse(_) => err("Disburse"),
            Split(_) => err("Split"),
            Follow(_)
            | SetFollowing(_)
            | MakeProposal(_)
            | RegisterVote(_)
            | ClaimOrRefresh(_)
            | MergeMaturity(_)
            | DisburseMaturity(_)
            | AddNeuronPermissions(_)
            | RemoveNeuronPermissions(_)
            | StakeMaturity(_) => Ok(()),
        }
    }
```

**File:** rs/sns/governance/src/neuron/tests.rs (L61-75)
```rust
#[test]
fn test_is_vesting() {
    let mut neuron = Neuron {
        created_timestamp_seconds: 3400,
        ..Default::default()
    };

    assert!(!neuron.is_vesting(0));
    assert!(!neuron.is_vesting(10000));
    neuron.vesting_period_seconds = Some(600);
    assert!(neuron.is_vesting(3600));
    assert!(neuron.is_vesting(4000));
    assert!(!neuron.is_vesting(4001));
    assert!(!neuron.is_vesting(10000));
}
```
