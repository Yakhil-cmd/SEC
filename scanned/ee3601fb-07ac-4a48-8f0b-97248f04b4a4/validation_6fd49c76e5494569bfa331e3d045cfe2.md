### Title
Unbounded `disburse_maturity_in_progress` List in SNS Governance Allows Timer Resource Exhaustion - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

SNS governance's `disburse_maturity` function pushes entries into the `disburse_maturity_in_progress` list with no cap on list size. NNS governance enforces `MAX_NUM_DISBURSEMENTS = 10` for the equivalent operation. A neuron controller with sufficient maturity can call `DisburseMaturity` repeatedly with small percentages, growing the list arbitrarily large and exhausting the finalization timer's per-round processing budget.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, `disburse_maturity` unconditionally pushes a new `DisburseMaturityInProgress` entry:

```rust
neuron
    .disburse_maturity_in_progress
    .push(disbursement_in_progress);
```

The only guards are that `percentage_to_disburse` is 1–100 and that the worst-case modulated amount exceeds the transaction fee. There is no check on the current length of `disburse_maturity_in_progress`. [1](#0-0) 

NNS governance defines an explicit cap and enforces it before mutating state:

```rust
const MAX_NUM_DISBURSEMENTS: usize = 10;
...
if num_disbursements >= MAX_NUM_DISBURSEMENTS {
    return Err(InitiateMaturityDisbursementError::TooManyDisbursements);
}
``` [2](#0-1) [3](#0-2) 

The SNS `Neuron` proto field is a plain repeated message with no enforced bound:

```proto
repeated DisburseMaturityInProgress disburse_maturity_in_progress = 18;
``` [4](#0-3) 

The finalization timer (`finalize_maturity_disbursement`) processes **one** disbursement per invocation. A large list therefore requires proportionally many timer rounds to drain, consuming instruction budget and delaying all other periodic SNS governance tasks.

---

### Impact Explanation

A neuron controller with large `maturity_e8s_equivalent` calls `manage_neuron { DisburseMaturity { percentage_to_disburse: 1 } }` in a loop. Each call:
1. Deducts 1 % of remaining maturity (so maturity decreases geometrically).
2. Appends one `DisburseMaturityInProgress` entry.

For a neuron with 1 billion e8s of maturity and a transaction fee of 1 000 e8s, the minimum disbursable amount is ≈ 1 053 e8s (fee / 0.95 worst-case modulation). The geometric series terminates after ≈ 920 calls, leaving ~920 entries in the list. With a smaller SNS transaction fee the list can be orders of magnitude larger.

Each entry causes one extra ledger call from the finalization timer, consuming cycles and delaying reward distribution, proposal execution, and other periodic tasks for all SNS participants. This is a **cycles/resource accounting bug** with a realistic DoS impact on SNS governance liveness.

---

### Likelihood Explanation

The attacker must control a neuron with `DisburseMaturity` permission and meaningful maturity. This is a realistic scenario for any SNS participant who has accumulated voting rewards. The attack is a single-party action requiring no coordination, no privileged access beyond neuron ownership, and no external dependencies. The attacker sacrifices their own maturity, but the cost is bounded while the disruption to the SNS canister's timer budget is proportional to the list size.

---

### Recommendation

Add a cap on `disburse_maturity_in_progress` in SNS governance, mirroring NNS governance:

```rust
const MAX_DISBURSE_MATURITY_IN_PROGRESS: usize = 10; // or a suitable SNS-specific value

let current_len = neuron.disburse_maturity_in_progress.len();
if current_len >= MAX_DISBURSE_MATURITY_IN_PROGRESS {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        format!("Too many disbursements in progress (max {MAX_DISBURSE_MATURITY_IN_PROGRESS})"),
    ));
}
```

This check should be placed before the mutable borrow of the neuron, consistent with the NNS pattern.

---

### Proof of Concept

```
Attacker: neuron controller with DisburseMaturity permission
Neuron maturity: 1_000_000_000 e8s
SNS transaction fee: 1_000 e8s

Loop until maturity < minimum_disbursement:
    manage_neuron(DisburseMaturity { percentage_to_disburse: 1, to_account: None })

After ~920 iterations:
    neuron.disburse_maturity_in_progress.len() ≈ 920
    neuron.maturity_e8s_equivalent ≈ 0

Effect:
    The SNS finalization timer must now make ~920 sequential ledger calls
    (one per invocation) to drain the list, consuming the canister's
    instruction budget for many consecutive rounds and delaying all other
    periodic governance tasks (proposal finalization, reward distribution, etc.)
    for all SNS participants.
```

The root cause — `disburse_maturity` in `rs/sns/governance/src/governance.rs` appending to `disburse_maturity_in_progress` without a length guard — is directly reachable via the public `manage_neuron` ingress endpoint by any unprivileged neuron controller. [5](#0-4) [6](#0-5)

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

**File:** rs/sns/governance/src/governance.rs (L1692-1698)
```rust
        let neuron = self.get_neuron_result_mut(id)?;
        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_deduct);
        neuron
            .disburse_maturity_in_progress
            .push(disbursement_in_progress);
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L38-40)
```rust
/// The maximum number of disbursements in a neuron. This makes it possible to do daily
/// disbursements after every reward event (as 10 > 7).
const MAX_NUM_DISBURSEMENTS: usize = 10;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L306-308)
```rust
    if num_disbursements >= MAX_NUM_DISBURSEMENTS {
        return Err(InitiateMaturityDisbursementError::TooManyDisbursements);
    }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L236-240)
```text
  // Disburse maturity operations that are currently underway.
  // The entries are sorted by `timestamp_of_disbursement_seconds`-values,
  // with the oldest entries first, i.e. it holds for all i that:
  // entry[i].timestamp_of_disbursement_seconds <= entry[i+1].timestamp_of_disbursement_seconds
  repeated DisburseMaturityInProgress disburse_maturity_in_progress = 18;
```
