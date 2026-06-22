### Title
Rounding-Down in `calculate_split_neuron_effect` Allows Repeated Neuron Splits to Retain More Maturity Than Entitled - (File: rs/nns/governance/src/governance/split_neuron.rs)

### Summary

The `calculate_split_neuron_effect` function in the NNS governance canister uses integer division (truncation toward zero) when computing how much maturity to transfer to a child neuron during a split. Because the division always rounds down, a neuron controller can repeatedly split their neuron using the minimum allowed `split_amount_e8s` such that `transfer_maturity_e8s` rounds down to zero, retaining all maturity in the parent while still reducing its stake. Over many such splits, the parent neuron retains more maturity than it is proportionally entitled to.

### Finding Description

In `rs/nns/governance/src/governance/split_neuron.rs`, the function `calculate_split_neuron_effect` computes the maturity to transfer to the child neuron as:

```rust
let transfer_maturity_e8s: u64 = (source_neuron_maturity_e8s as u128)
    .checked_mul(split_amount_e8s as u128)
    .expect("Two u64s can't overflow when multiplied")
    .checked_div(source_neuron_stake_e8s as u128)   // integer division, truncates
    ...
```

This is equivalent to:
```
transfer_maturity_e8s = floor(maturity * split_amount / stake)
```

If `split_amount < stake / maturity` (i.e., `maturity * split_amount < stake`), the result is zero. The only lower bound on `split_amount_e8s` is `min_stake + transaction_fee_e8s` (enforced in `split_neuron` in `rs/nns/governance/src/governance.rs`). The NNS minimum neuron stake is `100_000_000` e8s (1 ICP). If a neuron has, say, 1 e8s of maturity and 10 ICP of stake, then any split of less than 10 ICP will transfer zero maturity to the child. By splitting at exactly the minimum stake each time, the attacker can drain the parent's stake entirely while keeping all maturity in the parent neuron.

The same truncation applies to `transfer_staked_maturity_e8s` and `transfer_eight_year_gang_bonus_base_e8s` in the same function.

The analogous vulnerability class from the external report is: **a proportional deduction is rounded down to zero by choosing a small-enough input, allowing the user to bypass the deduction entirely through repeated small operations**.

### Impact Explanation

A neuron controller can split their neuron many times using the minimum split amount, each time transferring zero maturity to the child neuron. After all splits, the parent neuron retains its full maturity while having distributed its stake across many child neurons. This allows the controller to:

1. Retain 100% of accumulated maturity in the parent neuron while splitting out all stake.
2. Dissolve the child neurons (which have zero maturity) to recover ICP.
3. Then disburse or spawn from the parent neuron's inflated maturity.

This is a **ledger conservation bug** / **cycles/resource accounting bug** in the NNS governance canister: maturity that should be proportionally distributed is instead retained by the parent, violating the invariant that `sum(child maturity) + remaining parent maturity == original parent maturity`.

### Likelihood Explanation

The attack is reachable by any unprivileged NNS neuron controller via the standard `manage_neuron` ingress message with `Command::Split`. No special privileges are required. The minimum split amount (1 ICP + transaction fee) is a realistic amount for any neuron holder. The number of splits required depends on the maturity-to-stake ratio, but for neurons with small maturity relative to stake (common for recently-created neurons), the attack is practical. The NNS neuron rate limiter (`NEURON_RATE_LIMITER_KEY`) may slow the attack but does not prevent it.

### Recommendation

Round up `transfer_maturity_e8s` (and the analogous staked maturity and bonus base fields) in `calculate_split_neuron_effect` using ceiling division:

```rust
// Instead of:
let transfer_maturity_e8s = (maturity * split_amount) / stake;

// Use ceiling division:
let transfer_maturity_e8s = (maturity * split_amount + stake - 1) / stake;
```

This ensures that any non-zero proportional share always transfers at least 1 e8s to the child, preventing the parent from retaining maturity it is not entitled to. Alternatively, add a check that if `maturity > 0` and `split_amount > 0`, then `transfer_maturity_e8s` must be at least 1.

### Proof of Concept

Consider a neuron with:
- `stake = 1_000_000_000` e8s (10 ICP)
- `maturity = 1` e8s
- `min_stake = 100_000_000` e8s (1 ICP)
- `transaction_fee = 10_000` e8s

Each call to `split_neuron` with `split_amount = min_stake + transaction_fee = 100_010_000`:

```
transfer_maturity = floor(1 * 100_010_000 / 1_000_000_000) = floor(0.10001) = 0
```

The child neuron receives 0 maturity. After 9 such splits (consuming ~9 ICP of stake), the parent retains its full 1 e8s of maturity while having transferred ~9 ICP of stake to child neurons. The parent's remaining stake is ~1 ICP, and it still holds all its maturity.

For a neuron with larger maturity (e.g., `maturity = 99` e8s, `stake = 10_000_000_000` e8s / 100 ICP), each minimum split of 1 ICP transfers `floor(99 * 100_010_000 / 10_000_000_000) = floor(0.99...) = 0` maturity. The attacker can split 90 times (leaving 10 ICP minimum stake), retaining all 99 e8s of maturity in the parent.

The root cause is at: [1](#0-0) 

called from: [2](#0-1) 

with the minimum split amount enforced at: [3](#0-2)

### Citations

**File:** rs/nns/governance/src/governance/split_neuron.rs (L20-26)
```rust
    let transfer_maturity_e8s: u64 = (source_neuron_maturity_e8s as u128)
        .checked_mul(split_amount_e8s as u128)
        .expect("Two u64s can't overflow when multiplied")
        .checked_div(source_neuron_stake_e8s as u128)
        .expect("The input source_neuron_stake_e8s should be greater than zero")
        .try_into()
        .expect("The result should be smaller than source_neuron_maturity_e8s which should fit into u64");
```

**File:** rs/nns/governance/src/governance.rs (L2179-2191)
```rust
        if split_amount_e8s < min_stake + transaction_fee_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Trying to split a neuron with argument {} e8s. This is too little: \
                      at the minimum, one needs the minimum neuron stake, which is {} e8s, \
                      plus the transaction fee, which is {}. Hence the minimum split amount is {}.",
                    split_amount_e8s,
                    min_stake,
                    transaction_fee_e8s,
                    min_stake + transaction_fee_e8s
                ),
            ));
```

**File:** rs/nns/governance/src/governance.rs (L2343-2354)
```rust
        // proportion of the split.
        let SplitNeuronEffect {
            transfer_maturity_e8s,
            transfer_staked_maturity_e8s,
            transfer_eight_year_gang_bonus_base_e8s,
        } = calculate_split_neuron_effect(
            split_amount_e8s,
            minted_stake_e8s,
            parent_maturity_e8s,
            parent_staked_maturity_e8s,
            parent_eight_year_gang_bonus_base_e8s,
        );
```
