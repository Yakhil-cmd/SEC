### Title
Forward/Proving Divergence: EOA Signature Validation Skipped in Sequencer Execution Path but Enforced in Proving Path — (`forward_system/src/run/mod.rs`, `basic_bootloader/src/bootloader/config.rs`)

---

### Summary

The primary sequencer execution entry point `run_block` uses `BasicBootloaderForwardSimulationConfig`, which sets `VALIDATE_EOA_SIGNATURE: false`, completely skipping EOA signature verification during forward execution. The proving path uses `BasicBootloaderProvingExecutionConfig`, which sets `VALIDATE_EOA_SIGNATURE: true` and enforces signature verification. This structural divergence means a block accepted by the sequencer's forward run can be rejected by the prover, producing an unprovable block and a liveness failure. The analog to the external report is exact: the forward run performs an "event-based" authentication check (a compile-time boolean flag) rather than a "result-based" one (always-enforced cryptographic verification), so the check can be absent without any cryptographic binding to the protected state transition.

---

### Finding Description

**Root cause — config divergence:**

`BasicBootloaderForwardSimulationConfig` (used by the sequencer) sets `VALIDATE_EOA_SIGNATURE: false`:

```rust
// basic_bootloader/src/bootloader/config.rs
impl BasicBootloaderExecutionConfig for BasicBootloaderForwardSimulationConfig {
    const VALIDATE_EOA_SIGNATURE: bool = false;   // ← no sig check
    const SIMULATION: bool = false;
}
``` [1](#0-0) 

`BasicBootloaderProvingExecutionConfig` (used by the prover) sets `VALIDATE_EOA_SIGNATURE: true`:

```rust
impl BasicBootloaderExecutionConfig for BasicBootloaderProvingExecutionConfig {
    const SIMULATION: bool = false;
    const VALIDATE_EOA_SIGNATURE: bool = true;    // ← sig check enforced
}
``` [2](#0-1) 

**The public sequencer entry point hard-codes the no-validation config:**

```rust
// forward_system/src/run/mod.rs:96
run_forward::<BasicBootloaderForwardSimulationConfig>(oracle, &mut result_keeper, tracer, validator);
``` [3](#0-2) 

`run_block_with_oracle_dump` (the testing/dump variant) also hard-codes the same config: [4](#0-3) 

**The conditional guard in the ZK validation path:**

```rust
// basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs:253
if !Config::VALIDATE_EOA_SIGNATURE | Config::SIMULATION {
    // charge native cost only — no ecrecover, no from-address check
    intrinsic_resources.charge(...)?;
} else {
    // actual ecrecover + recovered_from != from check
}
``` [5](#0-4) 

The same pattern exists in the Ethereum flow: [6](#0-5) 

**The proving path always uses the enforcing config:**

```rust
// proof_running_system/src/system/bootloader.rs:185
ProvingBootloader::<O, L>::run_prepared::<BasicBootloaderProvingExecutionConfig>(...)
``` [7](#0-6) 

**Result:** the sequencer's STF and the prover's STF evaluate the same transaction under different invariants. A transaction with an invalid or forged signature is accepted by `run_block` (forward) and rejected by `run_proving_inner` (proving).

---

### Impact Explanation

**Classification:** Forward/proving divergence → valid-execution unprovability → chain liveness failure.

If a transaction with an invalid EOA signature is included in a sequenced block:

1. The forward run (`run_block` / `BasicBootloaderForwardSimulationConfig`) accepts it — no `ecrecover`, no `recovered_from != from` check.
2. The proving run (`run_proving_inner` / `BasicBootloaderProvingExecutionConfig`) rejects it — `ecrecover` is executed and the `recovered_from != from` guard fires, returning `Err(InvalidTransaction::IncorrectFrom)`.
3. The prover panics: `expect("Tried to prove a failing batch")`.
4. The block is unprovable. The sequencer cannot advance the chain past this block.

This is a **liveness-breaking** impact: the rollup halts until the invalid block is discarded or the sequencer is patched. Depending on the operator's recovery procedure, funds committed to in-flight transactions may be temporarily inaccessible.

---

### Likelihood Explanation

The sequencer is a trusted entity that controls mempool admission. However:

- The forward execution path (`run_block`) provides **zero cryptographic enforcement** of signature validity. There is no in-STF fallback that would catch an invalid signature before the block is committed to the proving pipeline.
- The `TxValidator` trait passed to `run_forward` is `NopTxValidator` in all visible call sites (tests, oracle-dump replay, simulation). No production validator implementation that checks signatures is visible in the codebase.
- An attacker who can influence the sequencer's mempool (e.g., via a crafted RPC submission that bypasses mempool-level signature checks, or via a sequencer bug) can inject a transaction with a forged `from` address and an invalid signature. The sequencer will execute it successfully (forward run), commit the block, and then fail to prove it.
- The `BasicBootloaderForwardSimulationConfig` comment itself acknowledges the risk: *"It can be used to optimize forward run"* — the optimization is only safe if an external layer enforces signatures, but that layer is not part of the STF and is not visible in this codebase.

Likelihood: **Medium** — requires either a mempool-level bypass or a sequencer implementation that relies solely on `run_block` for validation.

---

### Recommendation

1. **Use `BasicBootloaderForwardETHLikeConfig`** (which has `VALIDATE_EOA_SIGNATURE: true`) in the `run_block` entry point, or introduce a new config that enforces signatures in the sequencer path. The proving config already does this correctly.
2. **Eliminate the divergence** by ensuring the forward STF and the proving STF enforce identical invariants. The `VALIDATE_EOA_SIGNATURE` flag should not exist as a runtime-selectable bypass in the sequencer path.
3. If the optimization is required, move signature validation to a mandatory pre-execution step that is enforced before any transaction is admitted to a block, and document this as a hard invariant of the sequencer.
4. Audit all `run_block` / `run_forward` call sites to confirm that a signature-validating `TxValidator` is always provided in production.

---

### Proof of Concept

**Deterministic reasoning (no-privilege attacker):**

```
1. Attacker constructs a transaction:
   - from: victim_address (any funded EOA)
   - signature: (parity=0, r=[0u8;32], s=[1u8;32])  ← invalid, ecrecover returns ≠ victim_address

2. Attacker submits to sequencer RPC.

3. Sequencer calls run_block(...) → run_forward::<BasicBootloaderForwardSimulationConfig>
   - validate_and_compute_fee_for_transaction is called
   - Config::VALIDATE_EOA_SIGNATURE = false → ecrecover branch is skipped entirely
   - Transaction passes validation, nonce is incremented, fee is charged from victim_address
   - Block is finalized and committed

4. Prover calls run_proving_inner::<BasicBootloaderProvingExecutionConfig>
   - validate_and_compute_fee_for_transaction is called
   - Config::VALIDATE_EOA_SIGNATURE = true → ecrecover is executed
   - recovered_from ≠ victim_address → Err(InvalidTransaction::IncorrectFrom)
   - run_prepared returns Err → expect("Tried to prove a failing batch") → panic

5. Block is unprovable. Chain liveness halted.
```

The divergence is structurally guaranteed by the config constants at compile time. No fuzzing or probabilistic reasoning is required.

### Citations

**File:** basic_bootloader/src/bootloader/config.rs (L10-15)
```rust
pub struct BasicBootloaderProvingExecutionConfig;

impl BasicBootloaderExecutionConfig for BasicBootloaderProvingExecutionConfig {
    const SIMULATION: bool = false;
    const VALIDATE_EOA_SIGNATURE: bool = true;
}
```

**File:** basic_bootloader/src/bootloader/config.rs (L18-23)
```rust
pub struct BasicBootloaderForwardSimulationConfig;

impl BasicBootloaderExecutionConfig for BasicBootloaderForwardSimulationConfig {
    const VALIDATE_EOA_SIGNATURE: bool = false;
    const SIMULATION: bool = false;
}
```

**File:** forward_system/src/run/mod.rs (L96-101)
```rust
    run_forward::<BasicBootloaderForwardSimulationConfig>(
        oracle,
        &mut result_keeper,
        tracer,
        validator,
    );
```

**File:** forward_system/src/run/mod.rs (L337-348)
```rust
    run_block_with_oracle_dump_ext::<T, PS, TS, TR, BasicBootloaderForwardSimulationConfig>(
        block_context,
        tree,
        preimage_source,
        tx_source,
        tx_result_callback,
        proof_data,
        da_commitment_scheme,
        tracer,
        validator,
    )
}
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L249-301)
```rust
    if let Some((parity, r, s)) = transaction.sig_parity_r_s() {
        // Even if we don't validate a signature, we still need to charge for ecrecover for equivalent behavior
        // Note that gas is charged already in intrinsic cost, so now
        // we only need to charge native resources.
        if !Config::VALIDATE_EOA_SIGNATURE | Config::SIMULATION {
            intrinsic_resources.charge(&Resources::from_native(
                <<S as SystemTypes>::Resources as Resources>::Native::from_computational(
                    ECRECOVER_NATIVE_COST,
                ),
            ))?;
        } else {
            if U256::from_be_slice(s) > U256::from_be_bytes(SECP256K1N_HALF) {
                return Err(InvalidTransaction::MalleableSignature.into());
            }

            let mut ecrecover_input = [0u8; 128];
            ecrecover_input[0..32].copy_from_slice(suggested_signed_hash.as_u8_array_ref());
            ecrecover_input[63] = (parity as u8) + 27;
            ecrecover_input[64..96][(32 - r.len())..].copy_from_slice(r);
            ecrecover_input[96..128][(32 - s.len())..].copy_from_slice(s);

            let mut ecrecover_output = ArrayBuilder::default();
            // We already charged gas for ecrecover in intrinsic cost, so we only need to charge native resources here.
            intrinsic_resources.with_infinite_ergs(|resources| {
                S::SystemFunctions::secp256k1_ec_recover(
                    ecrecover_input.as_slice(),
                    &mut ecrecover_output,
                    resources,
                    system.get_allocator(),
                )
                .map_err(SystemError::from)
            })?;

            if ecrecover_output.is_empty() {
                return Err(InvalidTransaction::IncorrectFrom {
                    recovered: B160::ZERO,
                    tx: from,
                }
                .into());
            }

            let recovered_from = B160::try_from_be_slice(&ecrecover_output.build()[12..])
                .ok_or(internal_error!("Invalid ecrecover return value"))?;

            if recovered_from != from {
                return Err(InvalidTransaction::IncorrectFrom {
                    recovered: recovered_from,
                    tx: from,
                }
                .into());
            }
        }
    };
```

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/validation_impl.rs (L201-246)
```rust
    if !Config::VALIDATE_EOA_SIGNATURE | Config::SIMULATION {
        // No native for Eth STF
    } else {
        if U256::from_be_slice(s) > U256::from_be_bytes(SECP256K1N_HALF) {
            return Err(InvalidTransaction::MalleableSignature.into());
        }

        let mut ecrecover_input = [0u8; 128];
        ecrecover_input[0..32].copy_from_slice(suggested_signed_hash.as_u8_array_ref());
        ecrecover_input[63] = (parity as u8) + 27;
        ecrecover_input[64..96][(32 - r.len())..].copy_from_slice(r);
        ecrecover_input[96..128][(32 - s.len())..].copy_from_slice(s);

        let mut ecrecover_output = ArrayBuilder::default();
        // We already charged gas for ecrecover in intrinsic cost, so we only need to charge native resources here.
        tx_resources
            .main_resources
            .with_infinite_ergs(|resources| {
                S::SystemFunctions::secp256k1_ec_recover(
                    ecrecover_input.as_slice(),
                    &mut ecrecover_output,
                    resources,
                    system.get_allocator(),
                )
                .map_err(SystemError::from)
            })?;

        if ecrecover_output.is_empty() {
            return Err(InvalidTransaction::IncorrectFrom {
                recovered: B160::ZERO,
                tx: from,
            }
            .into());
        }

        let recovered_from = B160::try_from_be_slice(&ecrecover_output.build()[12..])
            .ok_or(internal_error!("Invalid ecrecover return value"))?;

        if recovered_from != from {
            return Err(InvalidTransaction::IncorrectFrom {
                recovered: recovered_from,
                tx: from,
            }
            .into());
        }
    }
```

**File:** proof_running_system/src/system/bootloader.rs (L184-192)
```rust
    let (mut oracle, public_input) =
        ProvingBootloader::<O, L>::run_prepared::<BasicBootloaderProvingExecutionConfig>(
            oracle,
            &mut (),
            &mut NopResultKeeper::default(),
            &mut NopTracer::default(),
            &mut NopTxValidator,
        )
        .expect("Tried to prove a failing batch");
```
