### Title
Unchecked Integer Underflow in `build_unsigned_transaction_from_inputs` Behind a Production-Disabled Guard - (File: `rs/bitcoin/ckbtc/minter/src/lib.rs`)

### Summary
In the ckBTC minter, `build_unsigned_transaction_from_inputs` performs an unchecked `u64` subtraction `inputs_value - amount` to compute the change output. The only guard against underflow is a `debug_assert!`, which is explicitly disabled in production release builds. If `inputs_value < amount`, the subtraction wraps around to a near-`u64::MAX` value, producing a corrupted `change_output` that is persisted in the minter's state.

### Finding Description
In `build_unsigned_transaction_from_inputs`, the invariant `inputs_value >= amount` is enforced only by a `debug_assert!`:

```rust
let inputs_value = input_utxos.iter().map(|u| u.value).sum::<u64>();

debug_assert!(inputs_value >= amount);   // disabled in production

let change = inputs_value - amount;      // unchecked u64 subtraction
let change_output = state::ChangeOutput {
    vout: outputs.len() as u32,
    value: change + minter_fee,          // unchecked u64 addition
};
```

The build system (`publish/defs.bzl`) explicitly compiles production canisters with `-Cdebug-assertions=off`, disabling all `debug_assert!` macros. In Rust release mode, unsigned integer underflow wraps silently (no panic). The two subsequent `debug_assert_eq!` consistency checks at lines 1286–1289 and 1331–1334 are also disabled in production.

The function is called from at least two paths:
1. **Normal withdrawal** (`build_unsigned_transaction`, line 1207): `utxos_selection` is called first and the `num_inputs == 0` guard (line 1248) provides indirect protection.
2. **Transaction resubmission** (`resubmit_transactions`, line 892): called directly with `input_utxos` from the stored submitted transaction and `outputs` reconstructed from stored requests. No prior check ensures `inputs_value >= amount` before the call.

### Impact Explanation
If `inputs_value < amount`, `change` wraps to approximately `u64::MAX - (amount - inputs_value)`. The resulting `change_output.value` (a wrapped huge number) is stored in the minter's `SubmittedBtcTransaction` state. When the minter later finalizes the transaction, it would attempt to credit this astronomically large UTXO value back to its available UTXO pool, corrupting the minter's internal BTC accounting. This could cause the minter to believe it holds far more BTC than it actually does, enabling subsequent over-withdrawal of ckBTC relative to the actual BTC reserve — a ledger conservation violation in the chain-fusion bridge.

### Likelihood Explanation
The normal withdrawal path is indirectly protected by `utxos_selection` and the `num_inputs == 0` check. However, the resubmission path (`resubmit_transactions`) calls `build_unsigned_transaction_from_inputs` directly with stored UTXOs and reconstructed outputs. Any state inconsistency between stored UTXO values and stored request amounts — which could arise from a separate bug, an upgrade migration error, or an edge case in the resubmission logic — would trigger the underflow with no production-time guard. The absence of a hard `assert!` means the invariant is entirely unverified at runtime in production.

### Recommendation
Replace the `debug_assert!` at line 1262 with a hard `assert!` or, preferably, use checked arithmetic:

```rust
let change = inputs_value.checked_sub(amount).ok_or(BuildTxError::NotEnoughFunds)?;
```

Similarly, replace the unchecked addition `change + minter_fee` at line 1270 with `change.checked_add(minter_fee).expect("change + minter_fee overflow")` or propagate an error. This matches the pattern already used correctly in the ledger core (`rs/ledger_suite/common/ledger_core/src/balances.rs`) and the ckETH minter (`rs/ethereum/cketh/minter/src/state.rs`).

### Proof of Concept
1. Arrange for `build_unsigned_transaction_from_inputs` to be called (via the resubmission timer path) with `input_utxos` whose total value is less than the sum of `outputs` amounts — e.g., through a state inconsistency introduced by a canister upgrade or a separate accounting bug.
2. In production (release mode, `-Cdebug-assertions=off`), the `debug_assert!(inputs_value >= amount)` at line 1262 is a no-op.
3. `let change = inputs_value - amount;` wraps: e.g., `inputs_value = 1_000`, `amount = 2_000` → `change = u64::MAX - 999 = 18446744073709550615`.
4. `change_output.value = 18446744073709550615 + minter_fee` wraps again to a large value.
5. This corrupted `change_output` is stored in `SubmittedBtcTransaction` state and later credited to the minter's UTXO pool, corrupting BTC accounting. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L888-899)
```rust
        let mut input_utxos = submitted_tx.used_utxos;
        let mut replaced_reason = state::eventlog::ReplacedReason::ToRetry;
        let mut new_tx_requests = submitted_tx.requests;
        let max_num_inputs_in_transaction = read_state(|s| s.max_num_inputs_in_transaction);
        let build_result = match build_unsigned_transaction_from_inputs(
            &input_utxos,
            outputs,
            &main_address,
            max_num_inputs_in_transaction,
            tx_fee_per_vbyte,
            fee_estimator,
        ) {
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1260-1271)
```rust
    let inputs_value = input_utxos.iter().map(|u| u.value).sum::<u64>();

    debug_assert!(inputs_value >= amount);

    let minter_fee =
        fee_estimator.evaluate_minter_fee(input_utxos.len() as u64, (outputs.len() + 1) as u64);

    let change = inputs_value - amount;
    let change_output = state::ChangeOutput {
        vout: outputs.len() as u32,
        value: change + minter_fee,
    };
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1286-1289)
```rust
    debug_assert_eq!(
        tx_outputs.iter().map(|out| out.value).sum::<u64>() - minter_fee,
        inputs_value
    );
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1331-1334)
```rust
    debug_assert_eq!(
        inputs_value,
        fee + unsigned_tx.outputs.iter().map(|u| u.value).sum::<u64>()
    );
```

**File:** publish/defs.bzl (L7-12)
```text
def _release_nostrip_transition_impl(_settings, _attr):
    return {
        "//command_line_option:compilation_mode": "opt",
        "//command_line_option:strip": "never",
        "@rules_rust//:extra_rustc_flags": ["-Cdebug-assertions=off"],
    }
```
