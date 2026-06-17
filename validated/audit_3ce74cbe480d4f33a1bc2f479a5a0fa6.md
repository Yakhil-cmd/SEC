### Title
Forward/Proving Divergence in `requests_hash` Computation Across EIP-6110/7002/7251 Request Types - (`basic_bootloader/src/bootloader/block_flow/ethereum/post_tx_op_sequencing.rs` vs `post_tx_op_proving.rs`)

---

### Summary

The `requests_hash` field (EIP-7685) is computed using structurally different hashing schemes in sequencing (forward) mode versus proving mode. In sequencing mode, all EIP request bytes are streamed directly into a single flat SHA-256 hasher. In proving mode, each EIP type's bytes are first finalized into an intermediate SHA-256 digest, and only that digest is fed into the outer hasher. When any of EIP-6110, EIP-7002, or EIP-7251 contracts are active and produce requests, the two modes produce different `requests_hash` values — a forward/proving divergence that causes unprovable state transitions.

---

### Finding Description

**Sequencing mode** (`post_tx_op_sequencing.rs`, lines 87–94):

```rust
let mut requests_hasher = crypto::sha256::Sha256::new();
// Environment may have no such contracts predeployed for tests or sequencing purposes
let _ = eip6110_events_parser(&system, &mut requests_hasher);
let _ = eip7002_system_part(&mut system, &mut requests_hasher);
let _ = eip7251_system_part(&mut system, &mut requests_hasher);
let requests_hash = Bytes32::from_array(requests_hasher.finalize().into());
```

Each EIP parser writes its raw bytes (type byte, addresses, pubkeys, amounts) **directly** into `requests_hasher`. The final hash is:

```
SHA256( eip6110_raw_bytes || eip7002_raw_bytes || eip7251_raw_bytes )
```

**Proving mode** (`post_tx_op_proving.rs`, lines 205–240):

```rust
let mut requests_hasher = crypto::sha256::Sha256::new();
let mut intermediate_hasher = crypto::sha256::Sha256::new();
if eip6110_events_parser(&*system, &mut intermediate_hasher).expect(...) {
    let h = intermediate_hasher.finalize_reset();
    requests_hasher.update(h);
}
if eip7002_system_part(system, &mut intermediate_hasher).expect(...) {
    let h = intermediate_hasher.finalize_reset();
    requests_hasher.update(h);
}
if eip7251_system_part(system, &mut intermediate_hasher).expect(...) {
    let h = intermediate_hasher.finalize_reset();
    requests_hasher.update(h);
}
let requests_hash = Bytes32::from_array(requests_hasher.finalize().into());
```

Each EIP parser writes its raw bytes into `intermediate_hasher`, which is then finalized and its **digest** (not raw bytes) is fed into `requests_hasher`. The final hash is:

```
SHA256( SHA256(eip6110_raw_bytes) || SHA256(eip7002_raw_bytes) || SHA256(eip7251_raw_bytes) )
```

These two expressions are cryptographically distinct for any non-empty request set. The EIP-7685 specification itself mandates the two-level structure (hash-of-hashes), so the proving mode is correct and the sequencing mode is wrong.

A secondary divergence exists in error handling: sequencing mode silently discards all errors from the three parsers (`let _ = ...`), while proving mode panics on any error (`expect(...)`). If a contract is not deployed, sequencing mode silently omits its contribution to `requests_hash`, while proving mode aborts.

---

### Impact Explanation

`requests_hash` is sealed into the block header via `record_sealed_block` and becomes part of the committed state transition output. When EIP-7002 withdrawal requests or EIP-6110 deposit events are present in a block:

1. The sequencer commits a `requests_hash` computed with the flat scheme.
2. The prover recomputes `requests_hash` with the hash-of-hashes scheme.
3. The two values differ → the proof cannot verify the sequencer's committed header → **the block is unprovable**.

An unprovable block halts the chain's ability to finalize state on L1. Any user who submitted a transaction in that block cannot have their state transition proven or settled. This is a valid-execution unprovability / state-transition bug with direct impact on fund finality.

---

### Likelihood Explanation

The divergence is latent until EIP-6110, EIP-7002, or EIP-7251 system contracts are deployed and produce at least one request in a block. The EIP-7002 withdrawal contract and EIP-6110 deposit log parser are already wired into the post-block processing path (both sequencing and proving). Once any validator submits a withdrawal request or a deposit event is emitted, the divergence activates. No privileged access is required; any user triggering the relevant system contract (e.g., calling the EIP-7002 withdrawal predeploy) is sufficient.

---

### Recommendation

Align the sequencing-mode `requests_hash` computation with the proving-mode two-level scheme:

```rust
let mut requests_hasher = crypto::sha256::Sha256::new();
let mut intermediate_hasher = crypto::sha256::Sha256::new();
if let Ok(true) = eip6110_events_parser(&system, &mut intermediate_hasher) {
    requests_hasher.update(intermediate_hasher.finalize_reset());
}
if let Ok(true) = eip7002_system_part(&mut system, &mut intermediate_hasher) {
    requests_hasher.update(intermediate_hasher.finalize_reset());
}
if let Ok(true) = eip7251_system_part(&mut system, &mut intermediate_hasher) {
    requests_hasher.update(intermediate_hasher.finalize_reset());
}
let requests_hash = Bytes32::from_array(requests_hasher.finalize().into());
```

Error handling should also be reconciled: either both modes should tolerate missing contracts gracefully, or both should treat it as a fatal error.

---

### Proof of Concept

Suppose EIP-7002 produces one withdrawal request with raw bytes `B` (type byte + address + pubkey + amount).

- **Sequencing mode** feeds `B` directly into `requests_hasher` → `requests_hash = SHA256(B)`.
- **Proving mode** feeds `B` into `intermediate_hasher`, finalizes to `H = SHA256(B)`, then feeds `H` into `requests_hasher` → `requests_hash = SHA256(H) = SHA256(SHA256(B))`.

`SHA256(B) ≠ SHA256(SHA256(B))` for any non-trivial `B`. The sequencer seals a block with `requests_hash = SHA256(B)`; the prover recomputes `SHA256(SHA256(B))` and cannot produce a valid proof for the sequencer's header. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/post_tx_op_sequencing.rs (L87-94)
```rust
        let mut requests_hasher = crypto::sha256::Sha256::new();

        // Environment may have no such contracts predeployed for tests or sequencing purposes
        let _ = eip6110_events_parser(&system, &mut requests_hasher);
        let _ = eip7002_system_part(&mut system, &mut requests_hasher);
        let _ = eip7251_system_part(&mut system, &mut requests_hasher);

        let requests_hash = Bytes32::from_array(requests_hasher.finalize().into());
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/post_tx_op_proving.rs (L204-240)
```rust
        use crypto::sha256::Digest;
        let mut requests_hasher = crypto::sha256::Sha256::new();
        let mut intermediate_hasher = crypto::sha256::Sha256::new();
        if eip6110_events_parser(&*system, &mut intermediate_hasher)
            .expect("must filter EIP-6110 deposit requests")
        {
            let requests_hash = intermediate_hasher.finalize_reset();
            system_log!(
                system,
                "EIP-6110 ops hash = {:?}\n",
                Bytes32::from_array(requests_hash.into())
            );
            requests_hasher.update(requests_hash);
        }
        if eip7002_system_part(system, &mut intermediate_hasher)
            .expect("withdrawal requests must be processed")
        {
            let requests_hash = intermediate_hasher.finalize_reset();
            system_log!(
                system,
                "EIP-7002 ops hash = {:?}\n",
                Bytes32::from_array(requests_hash.into())
            );
            requests_hasher.update(requests_hash);
        }
        if eip7251_system_part(system, &mut intermediate_hasher)
            .expect("consolidation requests must be processed")
        {
            let requests_hash = intermediate_hasher.finalize_reset();
            system_log!(
                system,
                "EIP-7251 ops hash = {:?}\n",
                Bytes32::from_array(requests_hash.into())
            );
            requests_hasher.update(requests_hash);
        }
        let requests_hash = Bytes32::from_array(requests_hasher.finalize().into());
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/eip_7002_withdrawal_contract/mod.rs (L177-235)
```rust
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
        let slot_1 = resources.with_infinite_ergs(|resources| {
            system.io.storage_read::<false>(
                ExecutionEnvironmentType::NoEE,
                resources,
                &WITHDRAWAL_REQUEST_PREDEPLOY_ADDRESS,
                &slot_1,
            )
        })?;
        let slot_2 = resources.with_infinite_ergs(|resources| {
            system.io.storage_read::<false>(
                ExecutionEnvironmentType::NoEE,
                resources,
                &WITHDRAWAL_REQUEST_PREDEPLOY_ADDRESS,
                &slot_2,
            )
        })?;

        logger_log!(logger, "Processing EIP-7002 withdrawal queue element with:");

        logger_log!(logger, "\nAddress = ");
        let address = &slot_0.as_u8_array_ref()[12..];
        let _ = logger.log_data(address.iter().copied());
        requests_hasher.update(address);

        let pubkey_part_0 = slot_1.as_u8_array_ref();
        let pubkey_part_1 = &slot_2.as_u8_array_ref()[..16];

        requests_hasher.update(pubkey_part_0);
        requests_hasher.update(pubkey_part_1);
        logger_log!(logger, "\nPubkey = ");
        let _ = logger.log_data(ExactSizeChain::new(
            pubkey_part_0.iter().copied(),
            pubkey_part_1.iter().copied(),
        ));

        // NOTE: we need to bytereverse it
        let amount = &slot_2.as_u8_array_ref()[16..][..8];
        let amount = u64::from_be_bytes(amount.try_into().unwrap());
        logger_log!(logger, "\nAmount = {amount}\n");
        requests_hasher.update(amount.to_le_bytes());
```
