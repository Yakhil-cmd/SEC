### Title
Forward/Proving Divergence in `requests_hash` Computation Causes Any Block With EIP Requests to Be Unprovable - (`basic_bootloader/src/bootloader/block_flow/ethereum/post_tx_op_sequencing.rs` / `post_tx_op_proving.rs`)

---

### Summary

The sequencer and prover compute the `requests_hash` field using structurally different hashing algorithms. When a block contains any EIP-6110 deposit events, EIP-7002 withdrawal requests, or EIP-7251 consolidation requests, the sequencer produces one hash value and the prover produces a different one. The prover then asserts equality against the sequencer-produced header value and panics with `"requests hash diverged"`. Any such block is permanently unprovable, halting the chain.

---

### Finding Description

**Sequencing mode** (`post_tx_op_sequencing.rs`, lines 87–94) feeds all three request-type functions directly into a single `requests_hasher`:

```rust
let mut requests_hasher = crypto::sha256::Sha256::new();
let _ = eip6110_events_parser(&system, &mut requests_hasher);
let _ = eip7002_system_part(&mut system, &mut requests_hasher);
let _ = eip7251_system_part(&mut system, &mut requests_hasher);
let requests_hash = Bytes32::from_array(requests_hasher.finalize().into());
```

This produces: `SHA256( type_byte ‖ raw_deposit_data ‖ type_byte ‖ raw_withdrawal_data ‖ type_byte ‖ raw_consolidation_data )`. [1](#0-0) 

**Proving mode** (`post_tx_op_proving.rs`, lines 204–240) uses a two-level scheme: each function writes into an `intermediate_hasher`, whose finalized digest is then fed into `requests_hasher`:

```rust
let mut requests_hasher = crypto::sha256::Sha256::new();
let mut intermediate_hasher = crypto::sha256::Sha256::new();
if eip6110_events_parser(&*system, &mut intermediate_hasher).expect(...) {
    let h = intermediate_hasher.finalize_reset();
    requests_hasher.update(h);          // hash-of-hash
}
// same pattern for eip7002 and eip7251
let requests_hash = Bytes32::from_array(requests_hasher.finalize().into());
```

This produces: `SHA256( SHA256(type_byte ‖ raw_deposit_data) ‖ SHA256(type_byte ‖ raw_withdrawal_data) ‖ SHA256(type_byte ‖ raw_consolidation_data) )`. [2](#0-1) 

The two expressions are cryptographically distinct for any non-empty request set. The prover then asserts:

```rust
assert_eq!(
    requests_hash, system.metadata.block_level.header.requests_hash,
    "requests hash diverged",
);
``` [3](#0-2) 

The block header's `requests_hash` was written by the sequencer using the flat algorithm. The prover's two-level result never matches it, so the assertion always fires for any block that contains at least one request.

---

### Impact Explanation

Any block that includes at least one EIP-6110 deposit event, EIP-7002 withdrawal request, or EIP-7251 consolidation request is permanently unprovable. The sequencer accepts and seals the block normally; the prover panics on the `assert_eq!` and cannot produce a valid proof. Because ZKsync OS is a ZK rollup, an unprovable block halts forward progress of the chain — no subsequent blocks can be finalized on L1.

---

### Likelihood Explanation

The EIP-7002 withdrawal contract is a Pectra-fork predeploy at `0x00000961Ef480Eb55e80D19ad83579A64c007002`. ZKsync OS explicitly implements the Pectra fork (the `PectraForkHeader` type is used throughout the block-flow code). Any user who calls the EIP-7002 contract to queue a validator withdrawal — a standard, permissionless Ethereum operation — will trigger the divergence. The same applies to EIP-7251 consolidation requests. The deposit contract (EIP-6110) is similarly a standard Pectra predeploy. No special privilege is required; a single ordinary transaction is sufficient. [4](#0-3) 

---

### Recommendation

Unify the `requests_hash` computation into a single shared function used by both sequencing and proving paths. The correct algorithm (matching the EIP-7685 specification) is the two-level scheme already used in the proving path: hash each request type independently, then hash the concatenation of those per-type hashes. The sequencing path must be updated to match:

```rust
// shared helper (both paths)
fn compute_requests_hash(deposit_data, withdrawal_data, consolidation_data) -> Bytes32 {
    let mut outer = Sha256::new();
    if !deposit_data.is_empty() {
        outer.update(Sha256::digest(deposit_data));
    }
    if !withdrawal_data.is_empty() {
        outer.update(Sha256::digest(withdrawal_data));
    }
    if !consolidation_data.is_empty() {
        outer.update(Sha256::digest(consolidation_data));
    }
    Bytes32::from_array(outer.finalize().into())
}
```

Additionally, the `let _ = ...` suppression in the sequencing path should be replaced with explicit error handling so that a contract-not-deployed condition is surfaced rather than silently producing a wrong hash.

---

### Proof of Concept

1. Deploy ZKsync OS in Ethereum (Pectra) mode with the EIP-7002 withdrawal contract predeploy active.
2. Submit a transaction that calls the EIP-7002 contract to queue one withdrawal request (standard Pectra user flow, no privilege required).
3. The sequencer seals the block. It computes `requests_hash = SHA256(0x01 ‖ address ‖ pubkey ‖ amount)` and writes it into the block header.
4. The prover attempts to prove the same block. It computes `requests_hash = SHA256(SHA256(0x01 ‖ address ‖ pubkey ‖ amount))` — a different value.
5. The assertion at `post_tx_op_proving.rs:268` fires: `"requests hash diverged"`. The prover panics; no proof is generated.
6. The chain cannot advance past this block.

The same outcome is triggered by any EIP-6110 deposit event or EIP-7251 consolidation request in the same block. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/post_tx_op_sequencing.rs (L86-94)
```rust
        use crypto::sha256::Digest;
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

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/post_tx_op_proving.rs (L267-271)
```rust
        // - requests
        assert_eq!(
            requests_hash, system.metadata.block_level.header.requests_hash,
            "requests hash diverged",
        );
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/eip_7002_withdrawal_contract/mod.rs (L26-27)
```rust
pub const WITHDRAWAL_REQUEST_PREDEPLOY_ADDRESS: B160 =
    B160::from_limbs([0xd83579a64c007002, 0xef480eb55e80d19a, 0x00000961]);
```
