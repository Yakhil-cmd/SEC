### Title
Uninitialized `MaybeUninit` Static Globals Used Before `init()` in `delegated_u256` — (`supporting_crates/delegated_u256/src/arithmetic.rs`)

---

### Summary

`supporting_crates/delegated_u256/src/arithmetic.rs` declares two `static mut MaybeUninit<DelegatedU256>` globals (`ZERO`, `ONE`) that must be explicitly initialized by calling `delegated_u256::init()` before any arithmetic method that references them is invoked. In the production proving path, `delegated_u256::init()` is **never called**. On RISC-V (the proving target), BSS zero-initialization means `ZERO` happens to be correct, but `ONE` is silently all-zeros instead of `[1, 0, 0, 0]`. Every method that reads `ONE.as_ptr()` — `is_one()`, `one()`, `write_one()`, `write_one_into_ptr()` — operates on a garbage/wrong sentinel, producing incorrect arithmetic results exclusively in the prover. The forward system (host) uses the naive U256 and is unaffected, creating a forward/proving divergence.

---

### Finding Description

`supporting_crates/delegated_u256/src/arithmetic.rs` declares:

```rust
pub static mut ZERO: MaybeUninit<DelegatedU256> = MaybeUninit::uninit();
pub static mut ONE:  MaybeUninit<DelegatedU256> = MaybeUninit::uninit();
``` [1](#0-0) 

An explicit `init()` function must be called to write valid values into them:

```rust
pub(super) fn init() {
    unsafe {
        ZERO.write(DelegatedU256::ZERO);
        ONE.write(DelegatedU256::ONE);
    }
}
``` [2](#0-1) 

The public `delegated_u256::init()` entry point simply delegates to this: [3](#0-2) 

Multiple methods read from these statics without any initialization guard:

- `DelegatedU256::zero()` / `one()` — copy from `ZERO.as_ptr()` / `ONE.as_ptr()`
- `DelegatedU256::write_zero()` / `write_one()` — memcopy from `ZERO` / `ONE`
- `DelegatedU256::is_zero()` / `is_zero_mut()` / `is_one()` — equality delegation against `ZERO` / `ONE`
- `write_zero_into_ptr()` / `write_one_into_ptr()` — public unsafe helpers [4](#0-3) 

A grep across all production `.rs` files for `delegated_u256::init()` returns **only two hits**: the definition itself and a test in `supporting_crates/u256/src/lib.rs` (line 71). No production call site exists. [5](#0-4) 

The `proof_running_system` bootloader calls `init_allocator` and `CsrBasedIOOracle::init()`, but never `delegated_u256::init()`: [6](#0-5) 

By contrast, the analogous local `u256.rs` inside the `modexp` delegation **does** call its own `init()` at the top of `modexp_inner`: [7](#0-6) 

This shows the pattern is understood and applied locally for modexp, but was omitted for the shared `delegated_u256` crate used by the RISC-V U256 implementation.

The RISC-V U256 (`supporting_crates/u256/src/risc_v/mod.rs`) is compiled under `cfg(all(feature = "delegation", target_arch = "riscv32"))` and uses `delegated_u256` for all arithmetic. On RISC-V, BSS zero-initializes uninitialized statics, so `ZERO = [0,0,0,0]` (accidentally correct) but `ONE = [0,0,0,0]` (wrong; should be `[1,0,0,0]`).

---

### Impact Explanation

On the RISC-V proving target with the `delegation` feature enabled:

- `is_one()` compares against `[0,0,0,0]` instead of `[1,0,0,0]`, returning `true` for zero values and `false` for one values.
- `one()` returns zero.
- `write_one()` / `write_one_into_ptr()` write zero.

This corrupts EVM arithmetic in the prover wherever `U256::one()`, `is_one()`, or `write_one()` are exercised — e.g., increment/decrement, modular exponentiation base cases, loop counters, and opcode semantics that check for unit values.

The forward system (host) uses the naive U256 (`cfg(not(all(feature = "delegation", target_arch = "riscv32")))`) and is unaffected. The result is a **forward/proving divergence**: the sequencer produces a correct state root; the prover computes a different one. This either makes valid blocks unprovable (liveness failure) or, in the worst case, allows the prover to accept an incorrect state transition (soundness failure), depending on which direction the corrupted `is_one` check is exercised.

---

### Likelihood Explanation

The bug is **always present** when the proving binary is compiled with the `delegation` feature on RISC-V — no special attacker action is required. Any EVM transaction that exercises U256 arithmetic touching the `one` sentinel (e.g., `EXP`, `ADD` with carry, `DIV` by one, loop counters) will trigger the divergence. This is an extremely common code path in normal EVM execution.

---

### Recommendation

Call `delegated_u256::init()` unconditionally at the start of the proving entry point, before any arithmetic is performed. The correct location is in `proof_running_system/src/system/bootloader.rs` inside `run_proving`, immediately after `init_allocator` and before `run_proving_inner`:

```rust
pub fn run_proving<I: NonDeterminismCSRSourceImplementation, L: Logger + Default>(
    heap_start: *mut usize,
    heap_end: *mut usize,
) -> [u32; 8] {
    unsafe { init_allocator(heap_start, heap_end); }
    delegated_u256::init();   // <-- add this
    let oracle = CsrBasedIOOracle::<I>::init();
    run_proving_inner::<_, I, L>(oracle)
}
``` [8](#0-7) 

Alternatively, replace the `MaybeUninit` statics with directly initialized statics (since `DelegatedU256::ZERO` and `DelegatedU256::ONE` are `const`), eliminating the need for a separate `init()` call entirely:

```rust
pub static mut ZERO: DelegatedU256 = DelegatedU256::ZERO;
pub static mut ONE:  DelegatedU256 = DelegatedU256::ONE;
```

---

### Proof of Concept

1. Compile `zksync_os` for `riscv32i-unknown-none-elf` with the `delegation` feature.
2. Run the proving binary on any EVM transaction that calls `U256::is_one()` (e.g., `EXP` with exponent 1, or any loop that decrements a counter to 1).
3. Observe that `is_one()` returns `false` for the value `1` (since `ONE` is `[0,0,0,0]`), causing the prover to compute a different result than the forward system.
4. Confirm the divergence by comparing the state root produced by `forward_system` against the public input produced by `proof_running_system`.

The root cause is at: [9](#0-8) 
with the missing call site in: [6](#0-5)

### Citations

**File:** supporting_crates/delegated_u256/src/arithmetic.rs (L7-16)
```rust
pub static mut ZERO: MaybeUninit<DelegatedU256> = MaybeUninit::uninit();
pub static mut ONE: MaybeUninit<DelegatedU256> = MaybeUninit::uninit();

pub(super) fn init() {
    #[allow(static_mut_refs)]
    unsafe {
        ZERO.write(DelegatedU256::ZERO);
        ONE.write(DelegatedU256::ONE);
    }
}
```

**File:** supporting_crates/delegated_u256/src/arithmetic.rs (L67-122)
```rust
    pub fn zero() -> Self {
        #[allow(static_mut_refs)]
        unsafe {
            copy_from_operand(ZERO.as_ptr())
        }
    }

    pub fn one() -> Self {
        #[allow(static_mut_refs)]
        unsafe {
            copy_from_operand(ONE.as_ptr())
        }
    }

    pub fn write_zero(&mut self) {
        #[allow(static_mut_refs)]
        unsafe {
            let _ = bigint_op_delegation::<MEMCOPY_BIT_IDX>(self as *mut Self, ZERO.as_ptr());
        }
    }

    pub fn write_one(&mut self) {
        #[allow(static_mut_refs)]
        unsafe {
            let _ = bigint_op_delegation::<MEMCOPY_BIT_IDX>(self as *mut Self, ONE.as_ptr());
        }
    }

    pub fn is_zero_mut(&mut self) -> bool {
        #[allow(static_mut_refs)]
        let eq = unsafe { bigint_op_delegation::<EQ_OP_BIT_IDX>(self as *mut Self, ZERO.as_ptr()) };

        eq != 0
    }

    pub fn is_zero(&self) -> bool {
        let eq = unsafe {
            let src = copy_if_needed(self as *const Self);
            // we can cast constness since equality is non-destructive
            #[allow(static_mut_refs)]
            bigint_op_delegation::<EQ_OP_BIT_IDX>(src.cast_mut(), ZERO.as_ptr())
        };

        eq != 0
    }

    pub fn is_one(&self) -> bool {
        let eq = unsafe {
            let src = copy_if_needed(self as *const Self);
            // we can cast constness since equality is non-destructive
            #[allow(static_mut_refs)]
            bigint_op_delegation::<EQ_OP_BIT_IDX>(src.cast_mut(), ONE.as_ptr())
        };

        eq != 0
    }
```

**File:** supporting_crates/delegated_u256/src/lib.rs (L18-20)
```rust
pub fn init() {
    arithmetic::init();
}
```

**File:** supporting_crates/u256/src/lib.rs (L69-72)
```rust
    #[test]
    fn compare_arithmetic() {
        delegated_u256::init();

```

**File:** proof_running_system/src/system/bootloader.rs (L151-171)
```rust
pub fn run_proving<I: NonDeterminismCSRSourceImplementation, L: Logger + Default>(
    heap_start: *mut usize,
    heap_end: *mut usize,
) -> [u32; 8] {
    logger_log!(L::default(), "Enter proving bootloader");

    // init allocator
    // allocator is a global singleton object, that can be later accessed by ProxyAllocator
    unsafe {
        init_allocator(heap_start, heap_end);
    }

    logger_log!(L::default(), "Allocator init is complete");

    // oracle is just a thin proxy
    let oracle = CsrBasedIOOracle::<I>::init();

    logger_log!(L::default(), "Oracle init is complete");

    run_proving_inner::<_, I, L>(oracle)
}
```

**File:** basic_system/src/system_functions/modexp/delegation/mod.rs (L46-46)
```rust
    self::u256::init();
```
