### Title
Missing Zero-Divisor Guard in `i256_mod` Causes Panic on SMOD with Zero Denominator - (File: `evm_interpreter/src/i256.rs`)

---

### Summary

`i256_mod` in `evm_interpreter/src/i256.rs` does not guard against a zero divisor before calling `div_rem`, which panics by design. Any unprivileged user can trigger this by submitting a transaction containing the EVM `SMOD` opcode with a zero denominator, causing a runtime panic in forward execution and a forward/proving divergence.

---

### Finding Description

`i256_div` and `i256_mod` are the signed integer division and modulo helpers used by the EVM interpreter. `i256_div` correctly short-circuits when the divisor is zero: [1](#0-0) 

```rust
pub fn i256_div(dividend: &mut U256, divisor_or_quotient: &mut U256) {
    let divisor_sign = i256_sign::<true>(divisor_or_quotient);
    if divisor_sign == Sign::Zero {
        *divisor_or_quotient = U256::ZERO;
        return;          // ← correct EVM semantics: SDIV(x, 0) = 0
    }
```

`i256_mod` does **not** perform the equivalent check. It discards the divisor's sign result and proceeds unconditionally to `div_rem`: [2](#0-1) 

```rust
pub fn i256_mod(dividend: &mut U256, divisor_or_remainder: &mut U256) {
    let dividend_sign = i256_sign::<true>(dividend);
    if dividend_sign == Sign::Zero {          // only checks dividend
        *divisor_or_remainder = U256::ZERO;
        return;
    }

    let _ = i256_sign::<true>(divisor_or_remainder);  // result discarded

    // this is unsigned division of moduli
    let (_q, r) = dividend.div_rem(*divisor_or_remainder);  // ← panics if divisor == 0
```

The `div_rem` implementation explicitly panics on a zero divisor, as confirmed by the test suite: [3](#0-2) 

```rust
#[test]
#[should_panic]
fn naive_divrem_by_zero_panics() { ... }

#[test]
#[should_panic]
fn riscv_divrem_by_zero_panics() { ... }
```

The EVM Yellow Paper specifies `SMOD(x, 0) = 0`. The missing guard means the ZKsync OS forward executor panics instead of returning 0, diverging from both the EVM specification and the prover's expected behavior.

---

### Impact Explanation

A panic in the forward executor during block processing causes:

1. **Forward/proving divergence** — the prover, which follows EVM semantics and returns 0 for `SMOD(x, 0)`, produces a valid proof for a state transition the forward executor cannot complete, breaking the state-transition function.
2. **Block production halt** — if the panic is unrecoverable, the sequencer cannot finalize the block containing the offending transaction.
3. **Denial of service** — a single cheap transaction can repeatedly trigger this path.

---

### Likelihood Explanation

- No privileges required; any EOA can submit a transaction.
- Crafting a contract or raw calldata that executes `SMOD` with a zero denominator is trivial.
- The path is deterministic and 100% reproducible.

---

### Recommendation

Add the same zero-divisor guard to `i256_mod` that already exists in `i256_div`:

```rust
pub fn i256_mod(dividend: &mut U256, divisor_or_remainder: &mut U256) {
    // Guard: SMOD(x, 0) = 0 per EVM spec
    let divisor_sign = i256_sign::<true>(divisor_or_remainder);
    if divisor_sign == Sign::Zero {
        *divisor_or_remainder = U256::ZERO;
        return;
    }

    let dividend_sign = i256_sign::<true>(dividend);
    if dividend_sign == Sign::Zero {
        *divisor_or_remainder = U256::ZERO;
        return;
    }
    // ... rest unchanged
```

---

### Proof of Concept

1. Deploy or call any contract that executes `PUSH1 0x00 PUSH1 0x01 SMOD` (i.e., `1 SMOD 0`).
2. Submit the transaction to ZKsync OS.
3. The forward executor reaches `i256_mod` with `divisor_or_remainder == U256::ZERO`.
4. `div_rem` is called with a zero divisor and panics.
5. The prover, following EVM semantics, computes `SMOD(1, 0) = 0` and generates a valid proof.
6. The forward executor and prover are now diverged; the block cannot be finalized. [4](#0-3)

### Citations

**File:** evm_interpreter/src/i256.rs (L84-89)
```rust
pub fn i256_div(dividend: &mut U256, divisor_or_quotient: &mut U256) {
    let divisor_sign = i256_sign::<true>(divisor_or_quotient);
    if divisor_sign == Sign::Zero {
        *divisor_or_quotient = U256::ZERO;
        return;
    }
```

**File:** evm_interpreter/src/i256.rs (L133-153)
```rust
#[inline(always)]
pub fn i256_mod(dividend: &mut U256, divisor_or_remainder: &mut U256) {
    let dividend_sign = i256_sign::<true>(dividend);
    if dividend_sign == Sign::Zero {
        *divisor_or_remainder = U256::ZERO;
        return;
    }

    let _ = i256_sign::<true>(divisor_or_remainder);

    // this is unsigned division of moduli
    let (_q, r) = dividend.div_rem(*divisor_or_remainder);
    *divisor_or_remainder = r;

    if divisor_or_remainder.is_zero() {
        return;
    }
    if dividend_sign == Sign::Minus {
        two_compl_mut(divisor_or_remainder);
    }
}
```

**File:** supporting_crates/u256/src/lib.rs (L451-466)
```rust
    #[should_panic]
    fn naive_divrem_by_zero_panics() {
        let mut x = naive::U256::one();
        let mut y = naive::U256::zero();

        naive::U256::div_rem(&mut x, &mut y);
    }

    #[test]
    #[should_panic]
    fn riscv_divrem_by_zero_panics() {
        let mut x = risc_v::U256::one();
        let mut y = risc_v::U256::zero();

        risc_v::U256::div_rem(&mut x, &mut y);
    }
```
