Based on my thorough code analysis, here is the audit finding:

---

### Title
Unbounded JoinSet Task Spawning and Silent Block Discard via Crafted StrippedBlockProposal Duplicate Entries — (`rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs`)

### Summary
A Byzantine peer below the consensus fault threshold can send a `StrippedBlockProposal` containing an arbitrarily large number of duplicate `SignedIngressId` entries in `stripped_ingress_payload.ingress_messages`. Because `BlockProposalAssembler::new` copies the list verbatim into a `Vec` with no deduplication or size cap, and `assemble_message` spawns one unbounded `JoinSet` task per entry, this causes replica-local task/memory exhaustion. Additionally, when the first duplicate task fills a slot, every subsequent task for the same ID returns `InsertionError::AlreadyInserted`, which the caller treats as a fatal error and returns `AssembleResult::Unwanted`, silently aborting assembly of an otherwise valid block.

### Finding Description

**Entrypoint — deserialization with no size guard:**

`TryFrom<pb::StrippedBlockProposal> for StrippedBlockProposal` (stripped.rs lines 100–108) collects `ingress_messages` directly from the protobuf repeated field with no length check:

```rust
stripped_ingress_payload: StrippedIngressPayload {
    ingress_messages: value
        .ingress_messages
        .into_iter()
        .map(SignedIngressId::try_from)
        .collect::<Result<Vec<_>, _>>()?,
},
``` [1](#0-0) 

The protocol constant `MAX_INGRESS_MESSAGES_PER_BLOCK = 1000` is enforced only during ingress payload *validation* in `ingress_selector.rs`, not here. [2](#0-1) 

**Unbounded task spawning:**

`BlockProposalAssembler::new` maps the raw (potentially duplicate-filled) list into a `Vec<(SignedIngressId, Option<SignedIngress>)>` with no deduplication: [3](#0-2) 

`missing_stripped_messages()` then returns every entry whose slot is `None` — all of them on first call: [4](#0-3) 

`assemble_message` spawns one `get_or_fetch` task per returned ID with no cap: [5](#0-4) 

**Silent block discard via `AlreadyInserted`:**

`try_insert` uses `find` which always locates the *first* matching slot. After the first duplicate task fills it, every subsequent task for the same ID hits the `is_some()` branch and returns `InsertionError::AlreadyInserted`: [6](#0-5) 

The caller treats any `InsertionError` as fatal and returns `AssembleResult::Unwanted`, discarding the entire block assembly: [7](#0-6) 

The comment in `try_insert` acknowledges the assumed bound ("at most 1000 elements") but this is never enforced as an invariant at the deserialization boundary. [8](#0-7) 

### Impact Explanation

**Resource exhaustion:** A single crafted `StrippedBlockProposal` with N duplicate entries spawns N Tokio tasks. Each task that misses the local pool enters `download_stripped_message`, which loops indefinitely with exponential backoff until the bouncer fires. With N = 10,000 entries, this saturates the Tokio thread pool and heap for the duration of the bouncer window (up to ~120 s per task retry cycle), delaying or stalling block assembly at that height for the targeted replica.

**Silent valid-block discard:** If the Byzantine peer is the sole advertiser of a block proposal (e.e., it is the block proposer), the `AlreadyInserted` path causes the receiving replica to permanently discard the assembly attempt for that height, contributing to finalization delay.

### Likelihood Explanation

- Requires one Byzantine peer below the fault threshold — a realistic assumption in the threat model.
- The peer need not be the block proposer; it only needs to advertise a `ConsensusMessageId` it observed from an honest proposer and respond with a crafted stripped body.
- No cryptographic material needs to be forged; the `unstripped_consensus_message_id` hash is taken from the honest block and the duplicate list is purely in the stripped metadata.
- The attack is fully local to the targeted replica and leaves no on-chain trace.

### Recommendation

1. **Enforce a size cap at deserialization:** In `TryFrom<pb::StrippedBlockProposal>`, reject any `ingress_messages` list whose length exceeds `MAX_INGRESS_MESSAGES_PER_BLOCK` (and similarly for `stripped_idkg_dealings` vs. `dkg_dealings_per_block`).
2. **Deduplicate before spawning:** In `BlockProposalAssembler::new` or `missing_stripped_messages`, deduplicate the ID list before returning it to the task-spawning loop.
3. **Treat `AlreadyInserted` as non-fatal:** Rather than returning `AssembleResult::Unwanted` on `AlreadyInserted`, log a warning and continue — a duplicate fetch result should not abort a valid assembly.

### Proof of Concept

```rust
// Construct a StrippedBlockProposal proto with 10_000 copies of the same SignedIngressId.
let mut proto = pb::StrippedBlockProposal::default();
proto.pruned_block_proposal = Some(valid_pruned_block_proto());
proto.unstripped_consensus_message_id = Some(valid_consensus_message_id_proto());
let dup_id = fake_signed_ingress_id_proto();
proto.ingress_messages = vec![dup_id; 10_000];

// Deserialize — succeeds with no error today.
let stripped = StrippedBlockProposal::try_from(proto).unwrap();

// BlockProposalAssembler::new creates 10_000 slots.
let assembler = BlockProposalAssembler::new(stripped);
// missing_stripped_messages() returns 10_000 IDs.
assert_eq!(assembler.missing_stripped_messages().len(), 10_000);

// assemble_message would spawn 10_000 JoinSet tasks.
// The first task to complete fills slot 0; the second returns AlreadyInserted
// → AssembleResult::Unwanted, discarding the block.
```

### Citations

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs (L100-108)
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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L262-271)
```rust
            if let Err(err) = assembler.try_insert_stripped_message(message) {
                warn!(
                    self.log,
                    "Failed to insert stripped message of type {}: {}. This is a bug.",
                    message_type.as_str(),
                    err
                );

                return AssembleResult::Unwanted;
            }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L435-454)
```rust
    fn try_insert(
        &mut self,
        signed_ingress_id: SignedIngressId,
        ingress_message: SignedIngress,
    ) -> Result<(), InsertionError> {
        // We can have at most 1000 elements in the vector, so it should be reasonably fast to do a
        // linear scan here.
        let (_, ingress) = self
            .ingress_messages
            .iter_mut()
            .find(|(id, _maybe_ingress)| *id == signed_ingress_id)
            .ok_or(InsertionError::NotNeeded)?;

        if ingress.is_some() {
            Err(InsertionError::AlreadyInserted)
        } else {
            *ingress = Some(ingress_message);
            Ok(())
        }
    }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L572-588)
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
                .stripped_idkg_dealings
                .stripped_dealings
                .iter()
                .map(|(node_index, dealing_id)| ((*node_index, dealing_id.clone()), None))
                .collect(),
            stripped_block_proposal,
        }
    }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L591-598)
```rust
    pub(crate) fn missing_stripped_messages(&self) -> Vec<StrippedMessageId> {
        let ingress_messages = PayloadAssembler::<SignedIngress>::missing_artifacts(self)
            .map(StrippedMessageId::Ingress);
        let idkg_dealings = PayloadAssembler::<SignedIDkgDealing>::missing_artifacts(self)
            .map(|(node_index, dealing_id)| StrippedMessageId::IDkgDealing(dealing_id, node_index));

        ingress_messages.chain(idkg_dealings).collect()
    }
```
