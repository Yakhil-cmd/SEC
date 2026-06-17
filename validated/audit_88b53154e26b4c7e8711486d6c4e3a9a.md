### Title
Silently Discarded `Result` from EIP-7002/7251/6110 System Operations Corrupts `requests_hash` in Block Header - (File: `basic_bootloader/src/bootloader/block_flow/ethereum/post_tx_op_sequencing.rs`)

---

### Summary

In `post_tx_op_sequencing.rs`, the return values of `eip6110_events_parser`, `eip7002_system_part`, and `eip7251_system_part` are explicitly discarded with `let _ =`. Each of these functions returns a `Result` and mutates a shared `requests_hasher` before potentially returning an error. If any function fails after partially writing to the hasher, the `requests_hash` committed into the sealed block header is computed from corrupted/partial data — silently, with no error propagation.

---

### Finding Description

In `post_tx_op_sequencing.rs` lines 90–92:

```rust
// Environment may have no such contracts predeployed for tests or sequencing purposes
let _ = eip6110_events_parser(&system, &mut requests_hasher);
let _ = eip7002_system_part(&mut system, &mut requests_hasher);
let _ = eip7251_system_part(&mut system, &mut requests_hasher);

let requests_hash = Bytes32::from_array(requests_hasher.finalize().into());
``` [1](#0-0) 

The `eip7002_system_part` function signature is `-> Result<bool, SystemError>`: [2](#0-1) 

Inside `eip7002_system_part`, the hasher is updated **before** the loop of storage reads that can fail with `?`:

```rust
requests_hasher.update([WITHDRAWAL_REQUEST_EIP_7685_TYPE]);  // line 177 — hasher mutated

for i in 0..num_dequeued {
    ...
    let slot_0 = resources.with_infinite_ergs(|resources| {
        system.io.storage_read::<false>(...)   // can return Err, propagated via ?
    })?;
``` [3](#0-2) 

If a storage read inside the loop returns an error, the function exits via `?` — but the hasher has already been partially written (the EIP-7685 type byte, and potentially some address/pubkey bytes from earlier loop iterations). The `let _ =` in the caller silently discards this `Err`, and `requests_hasher.finalize()` is called on the partially-mutated state, producing an incorrect `requests_hash`.

The same structural issue applies to `eip7251_system_part` (consolidation contract), which follows the same pattern of updating the hasher before fallible storage reads.

---

### Impact Explanation

The `requests_hash` is recorded into the sealed block header via `result_keeper.record_sealed_block(metadata.block_level.header)` immediately after finalization: [4](#0-3) 

An incorrect `requests_hash` in the committed block header constitutes a **state-transition bug**: the block header attests to a hash that does not correspond to the actual set of EIP-7002/7251/6110 requests processed. This divergence between the committed header and the true execution state can cause:

- Block header invalidity under Ethereum consensus rules (EIP-7685 requests hash mismatch).
- Proof verification failure if the prover checks the `requests_hash` field against the actual requests processed.
- Silent acceptance of a corrupted block by the sequencer, with downstream state inconsistency.

---

### Likelihood Explanation

The comment `"Environment may have no such contracts predeployed"` reveals the intended use of `let _ =`: to tolerate the "contract not deployed" error path. However, in `eip7002_system_part`, the "not deployed" error is returned **before** any hasher mutation (lines 71–76), so that case is safe. The dangerous path is a failure **after** line 177 (the type-byte write), which requires:

1. The EIP-7002 withdrawal contract to be deployed (production environment).
2. At least one withdrawal request in the queue (`num_dequeued > 0`).
3. A `SystemError` from a storage read inside the loop.

Condition 3 is unlikely under normal operation but is not impossible — oracle-data-influencing callers or prover/forward execution inputs that produce unexpected storage states could trigger it. The `let _ =` pattern unconditionally suppresses all error classes, including ones that arise after partial hasher mutation.

---

### Recommendation

Replace the silent discard with proper error handling. The correct fix depends on the intended semantics:

**Option A** — Treat "contract not deployed" as a non-error, propagate all other errors:

```rust
match eip7002_system_part(&mut system, &mut requests_hasher) {
    Ok(_) => {}
    Err(SystemError::LeafDefect(_)) => {} // contract not deployed — expected
    Err(e) => return Err(e.into()),        // unexpected error — propagate
}
```

**Option B** — If partial hasher mutation on error is acceptable, reset the hasher to a known-good snapshot before each call and only commit the update on `Ok`.

At minimum, the three `let _ =` calls must not silently swallow errors that occur after the hasher has been mutated.

---

### Proof of Concept

1. Deploy the EIP-7002 withdrawal contract at `WITHDRAWAL_REQUEST_PREDEPLOY_ADDRESS`.
2. Submit one or more withdrawal requests so `num_dequeued > 0`.
3. Arrange (via oracle/prover input manipulation) for a storage read inside the `for i in 0..num_dequeued` loop to return a `SystemError`.
4. Observe that `eip7002_system_part` returns `Err`, but the `let _ =` in `post_tx_op_sequencing.rs` line 91 discards it.
5. The `requests_hasher` now contains the EIP-7685 type byte (and possibly partial address/pubkey bytes) but not the full request data.
6. `requests_hasher.finalize()` at line 94 produces a hash that does not match the actual requests, and this incorrect value is committed into the block header at line 105. [5](#0-4) [6](#0-5)

### Citations

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/post_tx_op_sequencing.rs (L86-105)
```rust
        use crypto::sha256::Digest;
        let mut requests_hasher = crypto::sha256::Sha256::new();

        // Environment may have no such contracts predeployed for tests or sequencing purposes
        let _ = eip6110_events_parser(&system, &mut requests_hasher);
        let _ = eip7002_system_part(&mut system, &mut requests_hasher);
        let _ = eip7251_system_part(&mut system, &mut requests_hasher);

        let requests_hash = Bytes32::from_array(requests_hasher.finalize().into());
        system_log!(system, "Requests hash = {:?}\n", &requests_hash);

        // Here we have to cascade everything

        let mut logger = system.get_logger();

        let System {
            mut io, metadata, ..
        } = system;

        result_keeper.record_sealed_block(metadata.block_level.header);
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/eip_7002_withdrawal_contract/mod.rs (L49-52)
```rust
pub fn eip7002_system_part<S: EthereumLikeTypes>(
    system: &mut System<S>,
    requests_hasher: &mut impl crypto::sha256::Digest,
) -> Result<bool, SystemError>
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/eip_7002_withdrawal_contract/mod.rs (L169-195)
```rust
    if num_dequeued == 0 {
        // we do not even need to reset the queue pointers as it's a hard invariant
        assert!(queue_head_index.is_zero());
        assert!(queue_tail_index.is_zero());
        update_excess_withdrawal_requests_and_reset_count(system)?;
        return Ok(false);
    }

    requests_hasher.update([WITHDRAWAL_REQUEST_EIP_7685_TYPE]);

    let mut logger = system.get_logger();

    for i in 0..num_dequeued {
        let queue_storage_slot = WITHDRAWAL_REQUEST_QUEUE_STORAGE_OFFSET
            + ((queue_head_index + U256::from(i as u64)) * SLOTS_PER_REQUEST);
        let slot_0 = Bytes32::from_array(queue_storage_slot.to_be_bytes::<32>());
        let slot_1 = Bytes32::from_array((queue_storage_slot + U256::from(1)).to_be_bytes::<32>());
        let slot_2 = Bytes32::from_array((queue_storage_slot + U256::from(2)).to_be_bytes::<32>());

        let slot_0 = resources.with_infinite_ergs(|resources| {
            system.io.storage_read::<false>(
                ExecutionEnvironmentType::NoEE,
                resources,
                &WITHDRAWAL_REQUEST_PREDEPLOY_ADDRESS,
                &slot_0,
            )
        })?;
```
