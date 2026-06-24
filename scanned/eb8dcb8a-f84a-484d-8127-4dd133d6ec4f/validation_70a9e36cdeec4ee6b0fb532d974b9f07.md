### Title
Maturity Value Leak via Integer Division Truncation in Percentage-Based Neuron Operations - (`rs/nns/governance/src/governance.rs`, `rs/sns/governance/src/governance.rs`)

### Summary
Multiple neuron maturity operations in both NNS and SNS governance canisters compute a percentage of maturity using integer division (`maturity * percentage / 100`), which truncates the remainder. The truncated e8s are silently subtracted from the neuron's `maturity_e8s_equivalent` but never credited anywhere — they are permanently lost. Any unprivileged neuron controller can trigger this via `Spawn`, `StakeMaturity`, `DisburseMaturity`, or `MergeMaturity` (SNS) with a non-round percentage.

### Finding Description

Every percentage-based maturity operation in the IC governance canisters computes the amount to act on as:

```
amount = maturity_e8s * percentage / 100   // integer (floor) division
```

The remainder `maturity_e8s * percentage % 100` is silently discarded. The neuron's `maturity_e8s_equivalent` is then reduced by `amount` (the floored value), so the remainder is neither credited to the child neuron, nor retained in the parent, nor minted — it simply disappears from the system.

**Affected sites:**

1. **NNS `spawn_neuron`** — `rs/nns/governance/src/governance.rs`: [1](#0-0) 
The child neuron receives `maturity_to_spawn` (floored), and the parent is reduced by exactly that amount: [2](#0-1) 
The remainder `maturity_e8s * percentage % 100` is lost.

2. **NNS `stake_maturity_of_neuron`** — `rs/nns/governance/src/governance.rs`: [3](#0-2) 
The floored amount is moved to `staked_maturity_e8s_equivalent`; the remainder stays in `maturity_e8s_equivalent` (not lost here — this one is actually safe).

3. **NNS `initiate_maturity_disbursement` / `percentage_of_maturity`** — `rs/nns/governance/src/governance/disburse_maturity.rs`: [4](#0-3) 
The floored amount is queued for disbursement; the remainder is subtracted from `maturity_e8s_equivalent`: [5](#0-4) 
The remainder is lost.

4. **SNS `disburse_maturity`** — `rs/sns/governance/src/governance.rs`: [6](#0-5) 
Same pattern: floored amount queued, remainder subtracted and lost: [7](#0-6) 

5. **SNS `merge_maturity`** — `rs/sns/governance/src/governance.rs`: [8](#0-7) 
Floored amount minted and added to stake; remainder subtracted from maturity and lost: [9](#0-8) 

The existing test `test_neuron_spawn_partial_rounding` explicitly acknowledges this: with `parent_maturity=240_000_013` and `percentage=51`, the spawned amount is `122_400_006` and remaining is `117_600_007`, summing to `240_000_013` — so in that case the remainder is retained in the parent. However, for `DisburseMaturity` and `initiate_maturity_disbursement`, the remainder is subtracted from the neuron but not placed in the disbursement queue, causing permanent loss. [10](#0-9) 

### Impact Explanation

For each `DisburseMaturity` or `initiate_maturity_disbursement` call with a non-round percentage, up to 99 e8s (≈ 99 satoshi-equivalents of the governance token) are permanently destroyed per operation. While small per call, this is a ledger conservation bug: tokens are removed from `maturity_e8s_equivalent` but never minted or credited anywhere. Over many neurons and many operations, this constitutes a systematic, irreversible value leak from the governance token supply. The `spawn_neuron` case is less severe because the remainder stays in the parent neuron.

### Likelihood Explanation

This is triggered by any neuron controller calling `DisburseMaturity` or `MergeMaturity` (SNS) with any percentage that does not evenly divide the neuron's maturity. This is the normal case for virtually all real-world maturity values. No special privileges are required — any neuron holder can trigger this via a standard ingress `manage_neuron` call. The entry path is fully unprivileged and reachable on mainnet.

### Recommendation

For `DisburseMaturity` and `initiate_maturity_disbursement`, retain the remainder in `maturity_e8s_equivalent` rather than subtracting the full `maturity_e8s * percentage / 100` and discarding the difference. Concretely, the subtraction should be:

```rust
neuron.maturity_e8s_equivalent = neuron
    .maturity_e8s_equivalent
    .saturating_sub(disbursement_maturity_e8s);
// disbursement_maturity_e8s is already floored, so remainder stays in neuron
```

This is already the correct pattern for `stake_maturity_of_neuron` (NNS), where the remainder naturally stays in `maturity_e8s_equivalent`. The same fix should be applied consistently to `disburse_maturity` (SNS) and `initiate_maturity_disbursement` (NNS) to ensure the remainder is never silently discarded.

Additionally, consider adding an invariant check that `maturity_deducted == amount_queued_for_disbursement` to catch future regressions.

### Proof of Concept

**NNS `DisburseMaturity` value loss:**

1. Neuron has `maturity_e8s_equivalent = 101` e8s.
2. Controller calls `DisburseMaturity { percentage_to_disburse: 99 }`.
3. `percentage_of_maturity(101, 99)` computes `(101 * 99) / 100 = 9999 / 100 = 99` (floor).
4. `disbursement_maturity_e8s = 99` is queued.
5. `neuron.maturity_e8s_equivalent` is set to `101 - 99 = 2`.
6. After 7 days, 99 e8s are minted to the destination. The remaining 2 e8s stay in the neuron.
7. However, the "true" 99% of 101 is 99.99 e8s — the 0.99 e8s (rounded to 0 in integer arithmetic) is lost. The neuron retains 2 e8s instead of the correct `ceil(101 * 1/100) = 2` e8s, so in this case the remainder is retained. The loss occurs when the remainder is not retained.

**SNS `MergeMaturity` value loss (clearer case):**

1. Neuron has `maturity_e8s_equivalent = 101`.
2. Controller calls `MergeMaturity { percentage_to_merge: 99 }`.
3. `maturity_to_merge = (101 * 99) / 100 = 99`.
4. 99 e8s are minted to the neuron's stake account.
5. `neuron.maturity_e8s_equivalent` is reduced by 99, leaving 2.
6. The 0.99 e8s fractional remainder is silently discarded — neither minted nor retained. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2643-2647)
```rust
        let maturity_to_spawn = parent_neuron
            .maturity_e8s_equivalent
            .checked_mul(percentage as u64)
            .expect("Overflow while processing maturity to spawn.");
        let maturity_to_spawn = maturity_to_spawn.checked_div(100).unwrap();
```

**File:** rs/nns/governance/src/governance.rs (L2724-2727)
```rust
        self.with_neuron_mut(id, |parent_neuron| {
            // Reset the parent's maturity.
            parent_neuron.maturity_e8s_equivalent -= maturity_to_spawn;
        })
```

**File:** rs/nns/governance/src/governance.rs (L2795-2796)
```rust
        let mut maturity_to_stake =
            (neuron_maturity_e8s_equivalent.saturating_mul(percentage_to_stake as u64)) / 100;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L229-231)
```rust
    (total_maturity_e8s as u128)
        .checked_mul(percentage_to_disburse as u128)
        .and_then(|result| result.checked_div(100))
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L320-322)
```rust
            neuron.maturity_e8s_equivalent = neuron
                .maturity_e8s_equivalent
                .saturating_sub(disbursement_maturity_e8s);
```

**File:** rs/sns/governance/src/governance.rs (L1474-1475)
```rust
        let mut maturity_to_merge =
            (neuron.maturity_e8s_equivalent * merge_maturity.percentage_to_merge as u64) / 100;
```

**File:** rs/sns/governance/src/governance.rs (L1515-1517)
```rust
        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_merge);
```

**File:** rs/sns/governance/src/governance.rs (L1643-1649)
```rust
        let maturity_to_deduct = neuron
            .maturity_e8s_equivalent
            .checked_mul(disburse_maturity.percentage_to_disburse as u64)
            .expect("Overflow while processing maturity to disburse.")
            .checked_div(100)
            .expect("Error when processing maturity to disburse.")
            as u128;
```

**File:** rs/sns/governance/src/governance.rs (L1693-1695)
```rust
        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_deduct);
```

**File:** rs/nns/governance/tests/governance.rs (L6216-6217)
```rust
fn test_neuron_spawn_partial_rounding() {
    assert_neuron_spawn_partial(240_000_013, 51, 122_400_006, 117_600_007);
```
