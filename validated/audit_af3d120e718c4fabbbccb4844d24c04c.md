Based on my thorough investigation of the codebase, here is my assessment:

---

### Title
Forward/Proving Divergence via Invalid Signature in Legacy Transactions — (`basic_bootloader/src/bootloader/config.rs`, `forward_system/src/run/mod.rs`)

### Summary

The production forward run uses `BasicBootloaderForwardSimulationConfig` (`VALIDATE_EOA_SIGNATURE=false`), while the proving run uses `BasicBootloaderProvingExecutionConfig` (`VALIDATE_EOA_SIGNATURE=true`). A legacy transaction with an invalid signature is accepted by the forward run (signature check skipped) but rejected by the proving run (ecrecover fails), producing divergent post-block states and making the block unprovable.

### Finding Description

**Config asymmetry** is hardcoded in `basic_bootloader/src/bootloader/config.rs`: [1](#0-0) 

`BasicBootloaderForwardSimulationConfig::VALIDATE_EOA_SIGNATURE = false`, while: [2](#0-1) 

`BasicBootloaderProvingExecutionConfig::VALIDATE_EOA_SIGNATURE = true`.

**The production forward run** (`run_block`) is hardwired to the simulation config: [3](#0-2) 

**The proving run** is hardwired to the proving config: [4](#0-3) 

**In the ZK validation path**, when `VALIDATE_EOA_SIGNATURE=false`, the ecrecover call is entirely skipped — only native resources are charged: [5](#0-4) 

When `VALIDATE_EOA_SIGNATURE=true`, the proving run performs the full ecrecover and returns `InvalidTransaction::IncorrectFrom` if the recovered address doesn't match `from`: [6](#0-5) 

**On a validation error**, the proving run's tx loop reverts the transaction's state changes and records it as failed — it does NOT panic: [7](#0-6) 

This means the proving run completes successfully but with a **different post-block state root** than the forward run (which applied the transaction's state changes). The proof is generated for a state that was never committed to.

**The test rig itself acknowledges this gap** with the comment: [8](#0-7) 

> "we use proving config here for benchmarking, although sequencer can have extra optimizations"

This confirms the sequencer's production forward run intentionally skips signature validation as an optimization, relying on an external mempool filter that is not enforced by the ZKsync OS codebase itself.

**The `from` address for RLP transactions is oracle-supplied** (provided by the sequencer separately from the tx bytes): [9](#0-8) 

An attacker who can influence the sequencer's mempool (or who exploits the absence of signature validation in the forward run) can submit a legacy tx with arbitrary `r`/`s` bytes and a `from` address that does not correspond to those bytes.

### Impact Explanation

The forward run accepts the transaction and applies its state changes (nonce increment, value transfer, contract execution). The proving run rejects the same transaction and reverts those changes. The resulting state roots diverge. The ZK proof, which commits to the proving run's state root, is invalid for the state the sequencer committed on-chain. The block becomes unprovable, requiring either a chain halt or a forced re-sequencing.

### Likelihood Explanation

The ZKsync OS codebase provides no in-bootloader signature validation during the forward run. The only defense is an external mempool filter. If the sequencer's mempool is absent, misconfigured, or bypassed (e.g., via direct RPC submission to a permissive endpoint), the attacker can trivially trigger the divergence with a single crafted transaction. The attack requires no privileged access.

### Recommendation

1. **Align configs**: Change `run_block` to use `BasicBootloaderForwardETHLikeConfig` (which has `VALIDATE_EOA_SIGNATURE=true`) or `BasicBootloaderProvingExecutionConfig`, so the forward and proving runs apply identical validation logic.
2. **Alternatively**, if the optimization is intentional, add a mandatory pre-filter in the `TxSource` or `TxDataResponder` layer that rejects transactions whose ecrecover output does not match the supplied `from` address before they reach the bootloader.

### Proof of Concept

```
1. Craft a legacy RLP transaction with:
   - from = 0xAAAA...AAAA (any funded address)
   - r = 0x1111...1111, s = 0x2222...2222 (arbitrary, not a valid sig for `from`)
   - valid nonce, gas_price, gas_limit, value, data

2. Submit to sequencer. Sequencer calls run_block (BasicBootloaderForwardSimulationConfig).
   → VALIDATE_EOA_SIGNATURE=false → ecrecover skipped → tx accepted, state changes applied.

3. Sequencer commits block with state root S_forward.

4. Prover calls run_proving_inner (BasicBootloaderProvingExecutionConfig).
   → VALIDATE_EOA_SIGNATURE=true → ecrecover runs → recovered ≠ from → IncorrectFrom error.
   → tx reverted → state root S_proving ≠ S_forward.

5. Proof is for S_proving but chain committed S_forward → proof invalid → block unprovable.
```

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L249-258)
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
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L259-300)
```rust
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
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs (L114-122)
```rust
                        Err(TxError::Validation(err)) => {
                            system_log!(
                                system,
                                "Tx execution result: Validation error = {err:?}\n",
                            );
                            // Revert to state before transaction
                            system.finish_global_frame(Some(&pre_tx_rollback_handle))?;
                            result_keeper.tx_processed(Err(err));
                        }
```

**File:** tests/rig/src/chain.rs (L674-676)
```rust
        // we use proving config here for benchmarking,
        // although sequencer can have extra optimizations
        run_forward_no_panic::<BasicBootloaderProvingExecutionConfig>(
```

**File:** forward_system/src/run/query_processors/tx_data.rs (L78-85)
```rust
                            NextTxResponse::Tx(EncodedTx::Rlp(next_tx, from)) => {
                                let next_tx_len = next_tx.len();
                                // `0` interpreted as seal batch
                                assert_ne!(next_tx_len, 0);
                                self.next_tx = Some(next_tx);
                                self.next_tx_format = Some(TxEncodingFormat::Rlp);
                                self.next_tx_from = Some(B160::from_alloy(from));
                                next_tx_len
```
