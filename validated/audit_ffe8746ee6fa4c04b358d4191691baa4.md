### Title
SNS Neuron Split Retains Original Neuron ID on Stake-Depleted Parent, Enabling OTC Front-Running - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance `split_neuron` function allows a neuron controller to split their neuron such that the parent retains its original neuron ID while its stake is reduced to the protocol minimum, while the bulk of the stake moves to a newly-created child neuron with a fresh ID. This is the direct analog of the reported EVM lockup-plan segmentation vulnerability: the original ID stays on the nearly-worthless piece, enabling an OTC seller to front-run a buyer who agreed to purchase a specific neuron ID.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `split_neuron` computes the child neuron's ID deterministically from `(caller, memo)` via `new_neuron_id`, while the parent neuron unconditionally retains its original ID:

```
let child_nid = self.new_neuron_id(caller, split.memo)?;   // new ID for child
// parent neuron keeps its original `id`
``` [1](#0-0) 

The only stake constraints enforced are:

- `split.amount_e8s >= min_stake + transaction_fee_e8s` (child must have at least `min_stake`)
- `parent_neuron.stake_e8s() >= min_stake + split.amount_e8s` (parent must retain at least `min_stake`) [2](#0-1) 

This means a controller can set `split.amount_e8s = parent_stake - min_stake`, leaving the parent with exactly `min_stake` while the child receives `parent_stake - min_stake - fee`. The parent keeps its original neuron ID.

The child neuron is constructed with `vesting_period_seconds: None`, regardless of the parent's vesting period: [3](#0-2) 

The child neuron ID is derived deterministically from `compute_neuron_staking_subaccount_bytes(caller, memo)`: [4](#0-3) 

The same pattern exists in NNS governance, where the child gets a fresh random ID and the parent retains its original: [5](#0-4) 

**Attack scenario:**

1. Attacker controls SNS neuron ID `Y` with stake `S` (e.g., 1,000,000 tokens) and a vesting period.
2. Buyer agrees off-chain to pay price `P` for neuron ID `Y`, expecting stake `S`.
3. Before completing the transfer, attacker calls `split_neuron(Y, amount = S - min_stake, memo = M)`.
4. Parent neuron `Y` now has only `min_stake` (e.g., 1 token). Child neuron (new ID) has `S - min_stake - fee` tokens and **no vesting period**.
5. Attacker transfers control of neuron `Y` (original ID, minimal stake, vesting period intact) to the buyer via `AddNeuronPermissions` / `RemoveNeuronPermissions`.
6. Buyer pays `P` for a neuron worth only `min_stake`.
7. Attacker retains the child neuron with nearly all the original value, which can be immediately dissolved (no vesting) and disbursed.

The `split_neuron` call is a standard unprivileged ingress message callable by any neuron controller.

### Impact Explanation

A buyer who agrees to purchase a specific SNS neuron ID in an OTC trade receives a neuron with only the protocol-minimum stake, while paying the agreed price for the full original stake. The attacker retains the bulk of the value in a new child neuron with no vesting restriction. Financial loss to the buyer equals approximately `original_stake - min_stake` in SNS tokens. SNS neurons are actively traded OTC as they represent governance rights and economic value in SNS DAOs.

### Likelihood Explanation

OTC trading of SNS neurons occurs in practice. Any neuron controller can execute this attack with a single `split_neuron` ingress call before completing the OTC transfer. The attack requires no privileged access, no governance majority, and no coordination beyond controlling the neuron being sold. The minimum stake floor (`neuron_minimum_stake_e8s`) is typically small relative to the neuron's total value, making the attack economically significant.

### Recommendation

Assign both the parent and child neurons new IDs during the split operation, so neither party in an OTC agreement can predict or rely on the post-split neuron ID. Alternatively, at minimum, document this behavior prominently so OTC participants know that a neuron's ID is not a stable identifier of its value after a split. The NNS governance `split_neuron` at `rs/nns/governance/src/governance.rs` line 2208 has the same structural issue and should be addressed consistently.

### Proof of Concept

```
// Attacker has neuron Y with stake = 10_000_000_000 e8s, min_stake = 100_000_000 e8s
// Buyer agrees to pay 50 ICP for neuron Y

// Step 1: Attacker calls split_neuron before transferring control
split_neuron(
    id = Y,
    caller = attacker_principal,
    split = Split {
        amount_e8s: 10_000_000_000 - 100_000_000,  // = 9_900_000_000 e8s
        memo: 42,
    }
)
// Result:
//   Parent neuron Y: stake = 100_000_000 e8s (min_stake), original ID retained
//   Child neuron Z:  stake = 9_900_000_000 - fee e8s, new ID, NO vesting period

// Step 2: Attacker adds buyer as controller of neuron Y, removes self
manage_neuron(AddNeuronPermissions { principal: buyer, ... })
manage_neuron(RemoveNeuronPermissions { principal: attacker, ... })

// Step 3: Buyer pays 50 ICP, receives neuron Y worth only 100_000_000 e8s
// Attacker retains child neuron Z worth ~9_900_000_000 e8s with no vesting
```

The `new_neuron_id` call at line 1353 of `rs/sns/governance/src/governance.rs` is the root cause: it assigns the fresh ID to the child while the parent silently retains its original ID with arbitrarily reduced stake. [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L836-848)
```rust
    fn new_neuron_id(
        &mut self,
        controller: &PrincipalId,
        memo: u64,
    ) -> Result<NeuronId, GovernanceError> {
        let subaccount = ledger::compute_neuron_staking_subaccount_bytes(*controller, memo);
        let nid = NeuronId::from(subaccount);
        // Don't allow IDs that are already in use.
        if self.proto.neurons.contains_key(&nid.to_string()) {
            return Err(Self::invalid_subaccount_with_nonce(memo));
        }
        Ok(nid)
    }
```

**File:** rs/sns/governance/src/governance.rs (L1318-1347)
```rust
        if split.amount_e8s < min_stake + transaction_fee_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Trying to split a neuron with argument {} e8s. This is too little: \
                      at the minimum, one needs the minimum neuron stake, which is {} e8s, \
                      plus the transaction fee, which is {}. Hence the minimum split amount is {}.",
                    split.amount_e8s,
                    min_stake,
                    transaction_fee_e8s,
                    min_stake + transaction_fee_e8s
                ),
            ));
        }

        if parent_neuron.stake_e8s() < min_stake + split.amount_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Trying to split {} e8s out of neuron {}. \
                     This is not allowed, because the parent has stake {} e8s. \
                     If the requested amount was subtracted from it, there would be less than \
                     the minimum allowed stake, which is {} e8s. ",
                    split.amount_e8s,
                    parent_nid,
                    parent_neuron.stake_e8s(),
                    min_stake
                ),
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L1353-1356)
```rust
        let child_nid = self.new_neuron_id(caller, split.memo)?;
        let to_subaccount = child_nid.subaccount()?;

        let staked_amount = split.amount_e8s - transaction_fee_e8s;
```

**File:** rs/sns/governance/src/governance.rs (L1364-1381)
```rust
        let child_neuron = Neuron {
            id: Some(child_nid.clone()),
            permissions: parent_neuron.permissions.clone(),
            cached_neuron_stake_e8s: 0,
            neuron_fees_e8s: 0,
            created_timestamp_seconds: creation_timestamp_seconds,
            aging_since_timestamp_seconds: parent_neuron.aging_since_timestamp_seconds,
            followees: parent_neuron.followees.clone(),
            topic_followees: parent_neuron.topic_followees.clone(),
            maturity_e8s_equivalent: 0,
            dissolve_state: parent_neuron.dissolve_state,
            voting_power_percentage_multiplier: parent_neuron.voting_power_percentage_multiplier,
            source_nns_neuron_id: parent_neuron.source_nns_neuron_id,
            staked_maturity_e8s_equivalent: None,
            auto_stake_maturity: parent_neuron.auto_stake_maturity,
            vesting_period_seconds: None,
            disburse_maturity_in_progress: vec![],
        };
```

**File:** rs/nns/governance/src/governance.rs (L2207-2222)
```rust
        let created_timestamp_seconds = self.env.now();
        let child_nid = self.neuron_store.new_neuron_id(&mut *self.randomness)?;

        let from_subaccount = parent_neuron.subaccount();

        let to_subaccount = if let Some(memo) = memo {
            let to_subaccount = Subaccount(ledger::compute_neuron_split_subaccount_bytes(
                parent_neuron.controller(),
                memo,
            ));
            self.neuron_store
                .ensure_subaccount_available(to_subaccount)?
        } else {
            self.neuron_store
                .new_neuron_subaccount(&mut *self.randomness)?
        };
```
