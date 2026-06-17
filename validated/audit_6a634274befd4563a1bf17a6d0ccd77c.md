### Title
Missing `delegated_u256::init()` Call in Proving Path Leaves `ZERO`/`ONE` Statics Uninitialized — (`supporting_crates/delegated_u256/src/arithmetic.rs`)

---

### Summary

`supporting_crates/delegated_u256/src/arithmetic.rs` declares two `static mut MaybeUninit<DelegatedU256>` globals — `ZERO` and `ONE` — that must be explicitly initialized via `delegated_u256::init()` before any `DelegatedU256` arithmetic is performed. The production proving-path entry point (`proof_running_system/src/system/bootloader.rs::run_proving`) never calls `delegated_u256::init()`. Every `DelegatedU256` method that reads these statics (`zero()`, `one()`, `is_zero()`, `is_one()`, `write_zero()`, `write_one()`) therefore operates on uninitialized memory, constituting undefined behavior and causing incorrect arithmetic results in the RISC-V proving mode.

---

### Finding Description

`DelegatedU256` is the RISC-V hardware-delegation-backed 256-bit integer type used throughout the EVM interpreter when compiled with `target_arch = "riscv32"` and the `delegation` feature. Its arithmetic methods rely on two module-level statics that serve as reference operands for the hardware bigint delegation instructions:

```rust
// supporting_crates/delegated_u256/src/arithmetic.rs
pub static mut ZERO: MaybeUninit<DelegatedU256> = MaybeUninit::uninit();
pub static mut ONE:  MaybeUninit<DelegatedU256> = MaybeUninit::uninit();

pub(super) fn init() {
    unsafe {
        ZERO.write(DelegatedU256::ZERO);   // [0u64; 4]
        ONE.write(DelegatedU256::ONE);     // [1, 0, 0, 0]
    }
}
``` [1](#0-0) 

Every method that compares or copies zero/one reads directly from these statics:

```rust
pub fn is_zero(&self) -> bool {
    let eq = unsafe {
        let src = copy_if_needed(self as *const Self);
        bigint_op_delegation::<EQ_OP_BIT_IDX>(src.cast_mut(), ZERO.as_ptr())  // reads ZERO
    };
    eq != 0
}

pub fn is_one(&self) -> bool {
    let eq = unsafe {
        let src = copy_if_needed(self as *const Self);
        bigint_op_delegation::<EQ_OP_BIT_IDX>(src.cast_mut(), ONE.as_ptr())   // reads ONE
    };
    eq != 0
}
``` [2](#0-1) 

The public `init()` wrapper in the crate root simply delegates to `arithmetic::init()`: [3](#0-2) 

A grep across the entire codebase for calls to `delegated_u256::init()` shows it is **only called inside a test** in `supporting_crates/u256/src/lib.rs`:

```rust
// supporting_crates/u256/src/lib.rs  (test only)
#[test]
fn compare_arithmetic() {
    delegated_u256::init();   // ← only call site
    ...
}
``` [4](#0-3) 

The production proving-path entry point initializes the allocator and the CSR oracle, but **never calls `delegated_u256::init()`**:

```rust
// proof_running_system/src/system/bootloader.rs
pub fn run_proving<I: NonDeterminismCSRSourceImplementation, L: Logger + Default>(
    heap_start: *mut usize,
    heap_end: *mut usize,
) -> [u32; 8] {
    unsafe { init_allocator(heap_start, heap_end); }   // allocator only
    let oracle = CsrBasedIOOracle::<I>::init();         // oracle only
    run_proving_inner::<_, I, L>(oracle)
    // ← delegated_u256::init() is never called
}
``` [5](#0-4) 

Note that `basic_system/src/system_functions/modexp/delegation/mod.rs` calls `self::u256::init()` at the top of `modexp_inner`, but this initializes a **separate, modexp-local** pair of `ZERO`/`ONE` statics inside `basic_system/src/system_functions/modexp/delegation/u256.rs` — it does **not** initialize the `delegated_u256` crate's statics. [6](#0-5) [7](#0-6) 

---

### Impact Explanation

On RISC-V with the `delegation` feature, `U256` resolves to `DelegatedU256`:

```rust
#[cfg(all(feature = "delegation", target_arch = "riscv32"))]
pub use self::risc_v::U256;
``` [8](#0-7) 

Reading from `MaybeUninit::uninit()` without a prior `write()` is **undefined behavior** in Rust. In practice on bare-metal RISC-V, the BSS segment is typically zeroed at startup, so `ZERO` would hold `[0u64; 4]` (accidentally correct) while `ONE` would hold `[0u64; 4]` (wrong — should be `[1, 0, 0, 0]`). Consequently:

- `is_one()` always returns `false` regardless of the actual value.
- `one()` and `write_one()` produce/write zero instead of one.
- `From<u8>`, `From<u16>`, `From<u32>`, `From<u64>`, `From<u128>` all call `Self::zero()` first; these work only because `ZERO` happens to be zero-initialized by BSS.

This produces a **forward/proving divergence**: the sequencer (x86, `naive::U256`) computes correct results; the prover (RISC-V, `DelegatedU256`) computes incorrect results for any EVM opcode that internally relies on `is_one()` or `one()`. The prover would either fail to generate a valid proof (liveness failure) or, in a worst case, prove an incorrect state transition.

---

### Likelihood Explanation

Any transaction that exercises EVM arithmetic paths using `is_one()` or `one()` in the proving mode triggers the bug. This includes standard EVM opcodes (e.g., `EXP` with exponent 1, division-by-one short-circuits, modular arithmetic). The bug is unconditional — it fires on every proving invocation because `delegated_u256::init()` is structurally absent from the proving entry point.

---

### Recommendation

Call `delegated_u256::init()` at the very start of `run_proving` in `proof_running_system/src/system/bootloader.rs`, before any arithmetic is performed:

```rust
pub fn run_proving<I: NonDeterminismCSRSourceImplementation, L: Logger + Default>(
    heap_start: *mut usize,
    heap_end: *mut usize,
) -> [u32; 8] {
    unsafe { init_allocator(heap_start, heap_end); }
+   delegated_u256::init();   // initialize ZERO and ONE before any U256 arithmetic
    let oracle = CsrBasedIOOracle::<I>::init();
    run_proving_inner::<_, I, L>(oracle)
}
```

Additionally, consider adding a debug-mode assertion (e.g., an `AtomicBool` guard) inside `DelegatedU256::zero()` and `DelegatedU256::one()` that panics if `init()` has not been called, to prevent silent regressions.

---

### Proof of Concept

1. Compile ZKsync OS for RISC-V with the `delegation` feature (proving mode).
2. Submit a transaction that executes `EXP(x, 1)` (i.e., `x^1 mod 2^256`).
3. In the proving path, `is_one()` is called on the exponent `1`. Because `ONE` is zero-initialized by BSS rather than properly written by `init()`, `is_one()` returns `false`.
4. The prover takes the slow exponentiation path instead of the identity short-circuit, computing a result that diverges from the sequencer's output.
5. The prover either fails to produce a valid proof or produces a proof for an incorrect state root.

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

**File:** supporting_crates/delegated_u256/src/arithmetic.rs (L102-122)
```rust
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

**File:** supporting_crates/u256/src/lib.rs (L14-15)
```rust
#[cfg(all(feature = "delegation", target_arch = "riscv32"))]
pub use self::risc_v::U256;
```

**File:** supporting_crates/u256/src/lib.rs (L70-72)
```rust
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

**File:** basic_system/src/system_functions/modexp/delegation/u256.rs (L4-13)
```rust
static mut ZERO: MaybeUninit<DelegatedU256> = MaybeUninit::uninit();
static mut ONE: MaybeUninit<DelegatedU256> = MaybeUninit::uninit();

pub(crate) fn init() {
    #[allow(static_mut_refs)]
    unsafe {
        ZERO.write(DelegatedU256::ZERO);
        ONE.write(DelegatedU256::ONE);
    }
}
```
