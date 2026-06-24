### Title
Silent u64 Overflow in SNS Governance `merge_maturity` Maturity Calculation — (`rs/sns/governance/src/governance.rs`)

---

### Summary

The `merge_maturity` function in SNS governance performs a plain, unchecked `u64 × u64` multiplication when computing the amount of maturity to merge into a neuron's stake. In Rust release builds, integer overflow wraps silently. If a neuron's `maturity_e8s_equivalent` exceeds `u64::MAX / 100 ≈ 1.84 × 10¹⁷` (≈ 1.84 billion tokens in e8s), multiplying by `percentage_to_merge` (up to 100) silently wraps to a much smaller value. The neuron's maturity is then debited by the wrong (smaller) amount while the stake is credited by the same wrong amount, breaking the conservation invariant of the SNS token ledger.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `merge_maturity` function computes:

```rust
let mut maturity_to_merge =
    (neuron.maturity_e8s_equivalent * merge_maturity.percentage_to_merge as u64) / 100;
``` [1](#0-0) 

Both operands are `u64`. The `percentage_to_merge` field is validated to be in `[1, 100]`, so the maximum product is `maturity_e8s_equivalent × 100`. When `maturity_e8s_equivalent > u64::MAX / 100 ≈ 1.844 × 10¹⁷`, this multiplication overflows `u64`. In Rust release builds (which is how IC canisters are compiled), this wraps silently to a much smaller value.

The guard that follows:

```rust
if maturity_to_merge > neuron.maturity_e8s_equivalent {
    maturity_to_merge = neuron.maturity_e8s_equivalent;
}
``` [2](#0-1) 

…only catches the case where the result is *larger* than the original maturity. A wrapped (smaller) value passes this check silently.

The subsequent ledger transfer mints `maturity_to_merge` tokens into the neuron's stake account, and the neuron's `maturity_e8s_equivalent` is reduced by `maturity_to_merge`:

```rust
neuron.maturity_e8s_equivalent = neuron
    .maturity_e8s_equivalent
    .saturating_sub(maturity_to_merge);
let new_stake = neuron
    .cached_neuron_stake_e8s
    .saturating_add(maturity_to_merge);
neuron.update_stake(new_stake, now);
``` [3](#0-2) 

The analogous safe pattern used elsewhere in the same codebase (e.g., `stake_maturity_of_neuron`) uses `saturating_mul`:

```rust
let mut maturity_to_stake = (neuron
    .maturity_e8s_equivalent
    .saturating_mul(percentage_to_stake as u64))
    / 100;
``` [4](#0-3) 

The NNS governance `spawn_neuron` uses `checked_mul` with an explicit `expect`:

```rust
let maturity_to_spawn = parent_neuron
    .maturity_e8s_equivalent
    .checked_mul(percentage as u64)
    .expect("Overflow while processing maturity to spawn.");
``` [5](#0-4) 

`merge_maturity` is the only maturity-percentage calculation that uses raw `*` without any overflow protection.

---

### Impact Explanation

**Concrete scenario**: Suppose an SNS has a total supply of 2 billion tokens (e8s: `2 × 10¹⁷`). A neuron accumulates `maturity_e8s_equivalent = 2 × 10¹⁷`. The controller calls `merge_maturity` with `percentage_to_merge = 100`.

- Expected: `2 × 10¹⁷ × 100 / 100 = 2 × 10¹⁷` e8s merged.
- Actual (wrapped): `(2 × 10¹⁷ × 100) mod 2⁶⁴ / 100 ≈ 1.56 × 10¹⁶` e8s merged.

The neuron's maturity is reduced by `1.56 × 10¹⁶` e8s (not `2 × 10¹⁷`), and the stake is increased by the same wrong amount. The remaining `≈ 1.84 × 10¹⁷` e8s of maturity is neither returned to the neuron nor minted — it is silently destroyed, breaking the SNS token conservation invariant. The neuron controller suffers a direct financial loss proportional to the overflow magnitude.

---

### Likelihood Explanation

The overflow threshold is `u64::MAX / 100 ≈ 1.84 × 10¹⁷` e8s ≈ 1.84 billion tokens. Many SNS DAOs launch with total supplies in the billions of tokens. A single large neuron (e.g., a founding team neuron or treasury neuron) accumulating maturity over years could reach this threshold. The call path is fully unprivileged: any neuron controller can invoke `manage_neuron` → `MergeMaturity`. No admin key, governance majority, or privileged role is required.

---

### Recommendation

Replace the plain multiplication with `checked_mul` (consistent with `spawn_neuron` in NNS governance) or `saturating_mul` (consistent with `stake_maturity_of_neuron` in the same file):

```rust
// Option A: checked (returns error on overflow)
let mut maturity_to_merge = neuron
    .maturity_e8s_equivalent
    .checked_mul(merge_maturity.percentage_to_merge as u64)
    .ok_or_else(|| GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        "Overflow while computing maturity to merge.",
    ))?
    / 100;

// Option B: saturating (consistent with stake_maturity_of_neuron)
let mut maturity_to_merge = neuron
    .maturity_e8s_equivalent
    .saturating_mul(merge_maturity.percentage_to_merge as u64)
    / 100;
```

---

### Proof of Concept

```
maturity_e8s_equivalent = 200_000_000_000_000_000  // 2 × 10^17 (2 billion tokens)
percentage_to_merge     = 100

// Overflow:
200_000_000_000_000_000 * 100 = 20_000_000_000_000_000_000
u64::MAX                      = 18_446_744_073_709_551_615
wrapped value                 =  1_553_255_926_290_448_384  // ≈ 1.55 × 10^18

maturity_to_merge = 1_553_255_926_290_448_384 / 100 = 15_532_559_262_904_483

// Neuron loses 200_000_000_000_000_000 - 15_532_559_262_904_483
//            ≈ 184_467_440_737_095_517 e8s of maturity silently
```

The neuron controller receives ≈ 7.8% of the expected stake increase while losing 100% of the maturity, with no error returned.

### Citations

**File:** rs/sns/governance/src/governance.rs (L1474-1476)
```rust
        let mut maturity_to_merge =
            (neuron.maturity_e8s_equivalent * merge_maturity.percentage_to_merge as u64) / 100;

```

**File:** rs/sns/governance/src/governance.rs (L1478-1481)
```rust
        // need to account for this possibility.
        if maturity_to_merge > neuron.maturity_e8s_equivalent {
            maturity_to_merge = neuron.maturity_e8s_equivalent;
        }
```

**File:** rs/sns/governance/src/governance.rs (L1515-1521)
```rust
        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_merge);
        let new_stake = neuron
            .cached_neuron_stake_e8s
            .saturating_add(maturity_to_merge);
        neuron.update_stake(new_stake, now);
```

**File:** rs/sns/governance/src/governance.rs (L1563-1566)
```rust
        let mut maturity_to_stake = (neuron
            .maturity_e8s_equivalent
            .saturating_mul(percentage_to_stake as u64))
            / 100;
```

**File:** rs/nns/governance/src/governance.rs (L2643-2646)
```rust
        let maturity_to_spawn = parent_neuron
            .maturity_e8s_equivalent
            .checked_mul(percentage as u64)
            .expect("Overflow while processing maturity to spawn.");
```
