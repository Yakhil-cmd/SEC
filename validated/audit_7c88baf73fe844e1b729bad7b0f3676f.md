Looking at the full call chain and all relevant guards, here is my analysis:

**Confirmed code facts:**

1. `TryFrom<pb::StrippedBlockProposal>` at lines 100-108 of `stripped.rs` iterates `value.ingress_messages` with no length cap and collects into a `Vec<SignedIngressId>`. [1](#0-0) 

2. `BlockProposalAssembler::new` maps those entries 1:1 into a `Vec<(SignedIngressId, Option<SignedIngress>)>`. [2](#0-1) 

3. `assemble_message` then spawns one `get_or_fetch` tokio task per entry in `missing_stripped_messages()`, with no cap. [3](#0-2) 

4. The developer's own comment in `try_insert` says "We can have at most 1000 elements in the vector" — but this assumption is never enforced in `TryFrom`. [4](#0-3) 

5. `MAX_INGRESS_MESSAGES_PER_BLOCK = 1000` exists in limits, but is a consensus-layer validation rule, not enforced during P2P deserialization. [5](#0-4) 

6. The QUIC transport cap is 128 MB — large enough to fit ~1.7 million `StrippedIngressMessage` entries (~72 bytes each in proto encoding). [6](#0-5) 

7. `HASHES_IN_BLOCKS_ENABLED = true`, so this code path is active in production. [7](#0-6) 

8. The bouncer check happens **after** deserialization — the `artifact` parameter to `assemble_message` is already a deserialized `MaybeStrippedConsensusMessage`. The task spawning loop happens only after the bouncer passes, but the Vec allocation happens unconditionally on receipt. [8](#0-7) 

---

### Title
Unbounded `ingress_messages` deserialization and task spawning in `TryFrom<pb::StrippedBlockProposal>` / `assemble_message` — (`rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs`)

### Summary
A Byzantine peer below the fault threshold can send a crafted `StrippedBlockProposal` protobuf with an arbitrarily large `ingress_messages` repeated field. The `TryFrom<pb::StrippedBlockProposal>` implementation has no length cap, causing unbounded `Vec<SignedIngressId>` allocation on deserialization. If the bouncer subsequently accepts the artifact (e.g., the Byzantine peer is a legitimate block proposer advertising a real block ID), `assemble_message` spawns one tokio task per entry with no upper bound, causing OOM or severe memory pressure on the receiving replica.

### Finding Description
`TryFrom<pb::StrippedBlockProposal>` at `stripped.rs:100-108` collects `value.ingress_messages` into a `Vec<SignedIngressId>` without checking its length. The consensus-layer limit `MAX_INGRESS_MESSAGES_PER_BLOCK = 1000` is enforced only during block validation, not during P2P artifact deserialization. The QUIC transport allows messages up to 128 MB, which accommodates approximately 1.7 million `StrippedIngressMessage` entries. After deserialization, `BlockProposalAssembler::new` maps these 1:1 into another Vec, and `assemble_message` spawns one `get_or_fetch` tokio task per entry. Each tokio task carries several KB of overhead; 10^5–10^6 tasks can exhaust available memory.

### Impact Explanation
- **Memory allocation**: Deserialization of N entries allocates O(N) memory unconditionally on receipt, before any bouncer check.
- **Task explosion**: If the bouncer accepts the artifact (valid block ID from a Byzantine-but-legitimate block proposer), N tokio tasks are spawned. At ~few KB per task, 10^6 tasks = several GB of memory pressure, potentially causing OOM on a single replica.
- **Scoped impact**: Single-replica crash or severe degradation; does not require subnet-majority corruption.

### Likelihood Explanation
`HASHES_IN_BLOCKS_ENABLED = true` in production. A single Byzantine node below the fault threshold that is a legitimate block proposer can craft and push a `StrippedBlockProposal` with a valid `unstripped_consensus_message_id` (for a real block they proposed) but with an inflated `ingress_messages` list. The transport limit (128 MB) is the only practical bound, and it is large enough to carry millions of entries.

### Recommendation
Add a length cap in `TryFrom<pb::StrippedBlockProposal>` before collecting `ingress_messages`:

```rust
const MAX_INGRESS_MESSAGES_PER_STRIPPED_BLOCK: usize = 1000; // matches MAX_INGRESS_MESSAGES_PER_BLOCK

if value.ingress_messages.len() > MAX_INGRESS_MESSAGES_PER_STRIPPED_BLOCK {
    return Err(ProxyDecodeError::Other(format!(
        "Too many ingress_messages: {} > {}",
        value.ingress_messages.len(),
        MAX_INGRESS_MESSAGES_PER_STRIPPED_BLOCK,
    )));
}
```

Apply the same cap to `stripped_idkg_dealings`. This enforces the invariant the developer already assumed ("at most 1000 elements") at the correct layer.

### Proof of Concept
Fuzz `TryFrom<pb::StrippedBlockProposal>` with a proto where `ingress_messages` has 10^6 entries (each ~72 bytes, total ~72 MB, within the 128 MB transport cap). Observe RSS growth proportional to N during deserialization. Then pass the result to `BlockProposalAssembler::new` and call `missing_stripped_messages()` — confirm it returns 10^6 entries. Pass to `assemble_message` with a bouncer that returns `Wants` — confirm 10^6 tokio tasks are spawned and RSS grows by several GB.

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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L183-209)
```rust
        let (stripped_artifact, peer) = match self
            .fetch_stripped
            .assemble_message(id.clone(), artifact, peer_rx.clone())
            .await
        {
            AssembleResult::Unwanted => return AssembleResult::Unwanted,
            AssembleResult::Done { message, peer_id } => (message, peer_id),
        };

        let stripped_block_proposal = match stripped_artifact {
            MaybeStrippedConsensusMessage::StrippedBlockProposal(stripped) => stripped,
            MaybeStrippedConsensusMessage::Unstripped(unstripped) => {
                total_timer.stop_and_discard();
                return AssembleResult::Done {
                    message: unstripped,
                    peer_id: peer,
                };
            }
        };

        let mut join_set = tokio::task::JoinSet::new();

        let timer = self
            .metrics
            .download_missing_stripped_messages_duration
            .start_timer();
        let mut assembler = BlockProposalAssembler::new(stripped_block_proposal);
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L211-226)
```rust
        let stripped_message_ids = assembler.missing_stripped_messages();
        // For each stripped object in the message, try to fetch it either from the local pools
        // or from a random peer who is advertising it.
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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L441-443)
```rust
        // linear scan here.
        let (_, ingress) = self
            .ingress_messages
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

**File:** rs/limits/src/lib.rs (L78-78)
```rust
pub const MAX_INGRESS_MESSAGES_PER_BLOCK: u64 = 1000;
```

**File:** rs/p2p/quic_transport/src/lib.rs (L74-74)
```rust
pub(crate) const MAX_MESSAGE_SIZE_BYTES: usize = 128 * 1024 * 1024;
```

**File:** rs/consensus/features/src/lib.rs (L6-6)
```rust
pub const HASHES_IN_BLOCKS_ENABLED: bool = true;
```
