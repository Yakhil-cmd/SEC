Based on my thorough analysis of the code, this is a valid vulnerability. Here is the full assessment:

---

### Title
Unbounded `ingress_messages` in `TryFrom<pb::StrippedBlockProposal>` Allows Byzantine Peer to Spawn Unlimited Tokio Tasks and Exhaust Replica Memory — (`rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs`)

### Summary

A Byzantine peer below the fault threshold can craft a `StrippedBlockProposal` protobuf with an arbitrarily large `ingress_messages` repeated field. Because `TryFrom<pb::StrippedBlockProposal>` performs no upper-bound check on the count of ingress message IDs, `BlockProposalAssembler::new` allocates a `Vec` of size N and `assemble_message` subsequently spawns N concurrent Tokio tasks — one per missing message — with no cap. The code itself documents the intended invariant ("at most 1000 elements") but never enforces it at deserialization time.

### Finding Description

**Deserialization — no count guard:**

In `TryFrom<pb::StrippedBlockProposal>` the `ingress_messages` repeated field is converted unconditionally:

```rust
stripped_ingress_payload: StrippedIngressPayload {
    ingress_messages: value
        .ingress_messages
        .into_iter()
        .map(SignedIngressId::try_from)
        .collect::<Result<Vec<_>, _>>()?,
},
``` [1](#0-0) 

There is no check that `value.ingress_messages.len() <= MAX_INGRESS_MESSAGES_PER_BLOCK` (1000, defined in `rs/limits/src/lib.rs`). [2](#0-1) 

**Assembler allocation — Vec of size N:**

`BlockProposalAssembler::new` maps every deserialized ingress ID into a `Vec<(SignedIngressId, Option<SignedIngress>)>` entry:

```rust
ingress_messages: stripped_block_proposal
    .stripped_ingress_payload
    .ingress_messages
    .iter()
    .map(|signed_ingress_id| (signed_ingress_id.clone(), None))
    .collect(),
``` [3](#0-2) 

The comment in `try_insert` explicitly states the intended invariant that is never enforced:

```rust
// We can have at most 1000 elements in the vector, so it should be reasonably fast to do a
// linear scan here.
``` [4](#0-3) 

**Task spawning — one task per entry, no cap:**

`assemble_message` iterates over all N missing message IDs and spawns a `get_or_fetch` Tokio task for each:

```rust
for stripped_message_id in stripped_message_ids {
    join_set.spawn(get_or_fetch(
        stripped_message_id,
        self.ingress_pool.clone(),
        ...
    ));
}
``` [5](#0-4) 

**Bouncer check is too late:**

The bouncer fires only after all N tasks are already spawned. It checks whether the block ID is still wanted (based on height/finalization), not whether the ingress count is within bounds. The bouncer refresh period is 3 seconds, meaning all N tasks run for up to 3 seconds before being aborted. [6](#0-5) 

**Consensus-layer limit is bypassed:**

`MAX_INGRESS_MESSAGES_PER_BLOCK = 1000` is enforced only during consensus payload validation (after full block assembly), not during P2P deserialization of the stripped artifact. [2](#0-1) 

### Impact Explanation

Each spawned `get_or_fetch` task first checks the local ingress pool (a fast miss for fake IDs), then enters an exponential-backoff retry loop trying to download the fake ingress from peers. With N = 40,000 entries (achievable within a ~4 MB transport message, since each `StrippedIngressMessage` is ~100–150 bytes), the victim replica:

- Allocates a `Vec` of ~8 MB for the assembler state.
- Spawns ~40,000 Tokio tasks, each consuming ~8–16 KB of stack → **320–640 MB of memory** for task stacks alone.
- Saturates the Tokio task scheduler with retry loops.

The attack can be repeated every consensus round (every ~1–3 seconds), preventing memory from being reclaimed before the next wave arrives. This can cause a **single-replica OOM crash or task-scheduler stall**, breaking liveness for that replica.

### Likelihood Explanation

- The attacker needs only one Byzantine peer below the fault threshold — a realistic assumption in the threat model.
- The attacker must know a valid `ConsensusMessageId` for a block the victim is currently assembling. This is trivially observable from the public P2P gossip layer.
- No cryptographic material, admin keys, or majority corruption is required.
- The crafted message passes all existing checks in `TryFrom` (valid pruned block proto, empty ingress payload field, valid block proposal hash type).

### Recommendation

Add an upper-bound check in `TryFrom<pb::StrippedBlockProposal>` immediately after collecting `ingress_messages`:

```rust
if ingress_messages.len() > MAX_INGRESS_MESSAGES_PER_BLOCK as usize {
    return Err(ProxyDecodeError::Other(format!(
        "Too many ingress messages: {} > {}",
        ingress_messages.len(),
        MAX_INGRESS_MESSAGES_PER_BLOCK
    )));
}
```

Apply the same guard to `stripped_idkg_dealings`. This enforces the invariant already documented in the `try_insert` comment and aligns with the consensus-layer limit. [7](#0-6) 

### Proof of Concept

1. Construct a valid `pb::StrippedBlockProposal` with:
   - A real `unstripped_consensus_message_id` for a block the victim is currently downloading (observed from gossip).
   - A valid `pruned_block_proposal` with `ingress_payload = None`.
   - `ingress_messages` populated with N = 50,000 synthetic `StrippedIngressMessage` entries (each with a random 32-byte `ingress_bytes_hash` and a minimal `IngressMessageId`).
2. Serve this proto from a Byzantine peer when the victim requests the stripped artifact.
3. Observe on the victim: `BlockProposalAssembler::new` allocates a Vec of 50,000 entries; `assemble_message` spawns 50,000 `get_or_fetch` tasks; replica RSS spikes by hundreds of MB; Tokio task count saturates.
4. Repeat every round. Assert that the replica crashes or becomes unresponsive within minutes.

### Citations

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs (L100-130)
```rust
        Ok(Self {
            pruned_block_proposal_proto,
            stripped_ingress_payload: StrippedIngressPayload {
                ingress_messages: value
                    .ingress_messages
                    .into_iter()
                    .map(SignedIngressId::try_from)
                    .collect::<Result<Vec<_>, _>>()?,
            },
            unstripped_consensus_message_id,
            stripped_idkg_dealings: StrippedIDkgDealings {
                stripped_dealings: value
                    .stripped_idkg_dealings
                    .into_iter()
                    .map(|dealing| {
                        let idkg_artifact_id: IDkgArtifactId = try_from_option_field(
                            dealing.dealing_id,
                            "StrippedIDkgDealings::dealing_id",
                        )?;
                        if !matches!(idkg_artifact_id, IDkgArtifactId::Dealing(_, _)) {
                            return Err(ProxyDecodeError::Other(format!(
                                "The stripped IDKG artifact id {:?} is NOT for a dealing",
                                idkg_artifact_id,
                            )));
                        }
                        Ok((dealing.dealer_index, idkg_artifact_id))
                    })
                    .collect::<Result<Vec<_>, ProxyDecodeError>>()?,
            },
        })
    }
```

**File:** rs/limits/src/lib.rs (L78-78)
```rust
pub const MAX_INGRESS_MESSAGES_PER_BLOCK: u64 = 1000;
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L214-226)
```rust
        for stripped_message_id in stripped_message_ids {
            join_set.spawn(get_or_fetch(
                stripped_message_id,
                self.ingress_pool.clone(),
                self.idkg_pool.clone(),
                self.transport.clone(),
                id.as_ref().clone(),
                self.log.clone(),
                self.metrics.clone(),
                self.node_id,
                peer_rx.clone(),
            ));
        }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L233-242)
```rust
        let mut bouncer = self.fetch_stripped.bouncer_watcher();

        loop {
            let join_result = tokio::select! {
                _ = bouncer.wait_for(|bouncer| matches!(bouncer(&id), BouncerValue::Unwanted)) => {
                    self.metrics.report_aborted_block_assembly();
                    return AssembleResult::Unwanted;
                }
                join_result = join_set.join_next() => join_result,
            };
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L440-441)
```rust
        // We can have at most 1000 elements in the vector, so it should be reasonably fast to do a
        // linear scan here.
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L572-580)
```rust
    fn new(stripped_block_proposal: StrippedBlockProposal) -> Self {
        Self {
            ingress_messages: stripped_block_proposal
                .stripped_ingress_payload
                .ingress_messages
                .iter()
                .map(|signed_ingress_id| (signed_ingress_id.clone(), None))
                .collect(),
            signed_dealings: stripped_block_proposal
```
