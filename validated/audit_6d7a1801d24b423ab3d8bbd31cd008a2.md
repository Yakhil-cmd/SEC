### Title
u64 Multiplication Overflow in `disburse_maturity` Causes SNS Governance Canister Trap - (File: rs/sns/governance/src/governance.rs)

### Summary
The `disburse_maturity` function in SNS governance performs an unchecked `checked_mul` on a u64 `maturity_e8s_equivalent` field multiplied by a user-supplied percentage (1–100). When the product exceeds `u64::MAX`, the `.expect()` call panics, trapping the SNS governance canister. An analogous panic exists in NNS governance's `spawn_neuron`. This is the direct IC analog of the Phoenix overflow-abort class: a multi-factor u64 multiplication whose product can exceed the representable limit, causing an abort.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, `disburse_maturity` computes the amount to deduct as:

```rust
let maturity_to_deduct = neuron
    .maturity_e8s_equivalent
    .checked_mul(disburse_maturity.percentage_to_disburse as u64)
    .expect("Overflow while processing maturity to disburse.")
    .checked_div(100)
    ...
``` [1](#0-0) 

`maturity_e8s_equivalent` is a `u64` field. `percentage_to_disburse` is validated to be in `[1, 100]`. The multiplication `maturity_e8s_equivalent * percentage_to_disburse` overflows `u64` when:

```
maturity_e8s_equivalent > u64::MAX / percentage_to_disburse
```

For `percentage_to_disburse = 100`, the threshold is `u64::MAX / 100 ≈ 1.84 × 10^17 e8s` (≈ 1.84 billion tokens with 8 decimals). When overflow occurs, `checked_mul` returns `None`, and `.expect(...)` **panics**, trapping the SNS governance canister.

The identical pattern exists in NNS governance's `spawn_neuron`:

```rust
let maturity_to_spawn = parent_neuron
    .maturity_e8s_equivalent
    .checked_mul(percentage as u64)
    .expect("Overflow while processing maturity to spawn.");
``` [2](#0-1) 

A related silent-corruption variant exists in both NNS and SNS `stake_maturity_of_neuron`, which uses `saturating_mul` instead of `checked_mul`. When saturation occurs, the computed `maturity_to_stake` is silently capped at `u64::MAX / 100`, causing the neuron to stake far less than the requested percentage with no error returned:

```rust
let mut maturity_to_stake = (neuron
    .maturity_e8s_equivalent
    .saturating_mul(percentage_to_stake as u64))
    / 100;
``` [3](#0-2) [4](#0-3) 

The `maturity_e8s_equivalent` field is declared as a plain `u64` with no enforced upper bound below `u64::MAX`: [5](#0-4) 

---

### Impact Explanation

**`disburse_maturity` / `spawn_neuron` (panic path):** A neuron controller whose neuron's `maturity_e8s_equivalent` exceeds `u64::MAX / percentage` can trigger a canister trap in the SNS governance canister by calling `disburse_maturity` (or `spawn_neuron` in NNS governance) with a sufficiently large percentage. A canister trap in the governance canister halts all in-flight message processing for that call and, depending on the canister's error-handling posture, could leave the canister in a degraded state or cause repeated DoS if the condition persists.

**`stake_maturity_of_neuron` (silent saturation path):** When `saturating_mul` saturates, the neuron stakes a fraction of the requested percentage (e.g., requesting 100% stakes only ~1% of actual maturity). The user's intent is silently violated with no error. This is a **governance correctness bug**: the neuron's maturity accounting diverges from user expectations without any indication of failure.

---

### Likelihood Explanation

**For NNS governance (`spawn_neuron`):** The total ICP supply is approximately 500 million ICP ≈ 5 × 10^16 e8s. The overflow threshold is `u64::MAX / 100 ≈ 1.84 × 10^17 e8s ≈ 1.84 billion ICP`. Since no neuron's maturity can exceed the total ICP supply, this overflow is **practically impossible** for ICP under current supply conditions.

**For SNS governance (`disburse_maturity`, `stake_maturity_of_neuron`):** SNS tokens can have arbitrary total supplies. An SNS with a total supply of ≥ 2 billion tokens (8 decimals, i.e., ≥ 2 × 10^17 e8s) and a neuron that has accumulated maturity approaching the total supply could trigger the overflow. While no single neuron would realistically hold 100% of supply and all rewards, the absence of any parameter constraint or runtime guard means the code path is reachable in principle for any sufficiently large SNS token supply. Likelihood is **low but non-zero** for SNS tokens with large supplies and concentrated neuron ownership.

---

### Recommendation

Replace the u64 intermediate multiplication with a u128 promotion before multiplying, matching the pattern already used correctly elsewhere in the codebase (e.g., `calculate_split_neuron_effect`, `voting_power`):

```rust
// Safe: promote to u128 before multiplying
let maturity_to_deduct = (neuron.maturity_e8s_equivalent as u128)
    .checked_mul(disburse_maturity.percentage_to_disburse as u128)
    .and_then(|v| v.checked_div(100))
    .and_then(|v| u64::try_from(v).ok())
    .ok_or_else(|| GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        "Overflow computing maturity to disburse",
    ))?;
```

Apply the same fix to `spawn_neuron` in NNS governance and to both `stake_maturity_of_neuron` implementations (replacing `saturating_mul` with a u128-promoted checked path). Additionally, consider enforcing an upper bound on `maturity_e8s_equivalent` at accumulation time, analogous to the Phoenix recommendation to constrain lot sizes.

---

### Proof of Concept

**Trigger condition for `disburse_maturity` panic:**

```
maturity_e8s_equivalent = u64::MAX / 100 + 1  ≈ 1.844674407370955 × 10^17 e8s
percentage_to_disburse  = 100

checked_mul result: None  →  .expect() panics  →  canister trap
```

**Trigger condition for `stake_maturity_of_neuron` silent error:**

```
maturity_e8s_equivalent = u64::MAX / 50 + 1   (requesting 50% stake)
percentage_to_stake     = 50

saturating_mul(50) = u64::MAX  (saturated)
/ 100              = u64::MAX / 100  ≈ 1.84 × 10^17

Actual staked: ~1.84 × 10^17 e8s
Expected staked: ~(u64::MAX / 50 + 1) / 2 ≈ 9.22 × 10^18 e8s
→ User stakes ~2% of requested amount with no error
``` [1](#0-0) [6](#0-5) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1563-1566)
```rust
        let mut maturity_to_stake = (neuron
            .maturity_e8s_equivalent
            .saturating_mul(percentage_to_stake as u64))
            / 100;
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

**File:** rs/nns/governance/src/governance.rs (L2643-2647)
```rust
        let maturity_to_spawn = parent_neuron
            .maturity_e8s_equivalent
            .checked_mul(percentage as u64)
            .expect("Overflow while processing maturity to spawn.");
        let maturity_to_spawn = maturity_to_spawn.checked_div(100).unwrap();
```

**File:** rs/nns/governance/src/governance.rs (L2795-2796)
```rust
        let mut maturity_to_stake =
            (neuron_maturity_e8s_equivalent.saturating_mul(percentage_to_stake as u64)) / 100;
```

**File:** rs/ledger_suite/common/ledger_core/src/tokens.rs (L128-133)
```rust
pub struct Tokens {
    /// Number of 10^-8 Tokens.
    /// Named because the equivalent part of a Bitcoin is called a Satoshi
    #[n(0)]
    e8s: u64,
}
```
