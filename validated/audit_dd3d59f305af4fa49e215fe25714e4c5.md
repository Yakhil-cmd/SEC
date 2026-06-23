Let me look at the ingress validation for duplicate detection and the P2P size limits to complete the analysis.

Based on my thorough analysis of the code, here is my assessment:

---

### Title
Duplicate `SignedIngressId` Entries in `StrippedBlockProposal` Cause K-Fold Concurrent RPC Amplification — (`rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs`)

### Summary

A Byzantine peer (including a Byzantine block maker) can send a crafted `StrippedBlockProposal` containing K duplicate `SignedIngressId` entries in `ingress_messages`. Because no deduplication occurs at deserialization, in `BlockProposalAssembler::new`, or in `missing_stripped_messages`, the `assemble_message` loop spawns K concurrent `get_or_fetch` tasks for the same ingress message ID, causing K simultaneous RPCs before the assembly is aborted.

### Finding Description

**Step 1 — No deduplication at deserialization.**

`TryFrom<pb::StrippedBlockProposal>` in `stripped.rs` converts the protobuf `repeated StrippedIngressMessage ingress_messages` field directly into a `Vec<SignedIngressId>` with no uniqueness check: [1](#0-0) 

**Step 2 — No deduplication in `BlockProposalAssembler::new`.**

The constructor maps every element of `stripped_ingress_payload.ingress_messages` into `self.ingress_messages: Vec<(SignedIngressId, Option<SignedIngress>)>` without deduplication: [2](#0-1) 

**Step 3 — `missing_stripped_messages` returns K duplicate IDs.**

`missing_artifacts()` iterates over `self.ingress_messages` and returns every entry whose `Option` is `None`. With K duplicate entries, all K are `None` initially, so K duplicate `StrippedMessageId::Ingress` values are returned: [3](#0-2) 

**Step 4 — K concurrent tasks spawned.**

`assemble_message` iterates over the returned IDs and calls `join_set.spawn(get_or_fetch(...))` for each one, spawning K concurrent async tasks: [4](#0-3) 

**Step 5 — K RPCs issued before abort.**

Each `get_or_fetch` task checks the local ingress pool first; if the message is absent, it issues an RPC to a peer. All K tasks are spawned concurrently, so K RPCs are in-flight simultaneously. Only after the *second* task completes does `try_insert` return `AlreadyInserted` (because `find` always returns the first matching entry, which is already `Some` after the first task fills it), causing `assemble_message` to return `Unwanted` and drop `join_set`, aborting the remaining K-2 tasks: [5](#0-4) [6](#0-5) 

**Step 6 — The `ingress_messages` list is unauthenticated.**

The `StrippedBlockProposal` wire format contains the signed `pruned_block_proposal_proto` (authenticated by the block maker's signature) but the `ingress_messages` list is unsigned metadata added by the sender. Any Byzantine peer can take a legitimate stripped block and replace the `ingress_messages` list with K duplicates before forwarding it. The `HASHES_IN_BLOCKS_ENABLED = true` flag means this code path is active on mainnet: [7](#0-6) [8](#0-7) 

### Impact Explanation

- **K-fold concurrent RPC amplification**: A single malicious `StrippedBlockProposal` with K duplicate entries causes K simultaneous outbound RPCs from the victim replica to its peers.
- **K-fold task spawning**: K tokio tasks are created in the `JoinSet` before any can be cancelled.
- **No consensus impact**: `try_assemble` would ultimately fail with `MismatchedConsensusMessageId` (or the `AlreadyInserted` path aborts first), so no invalid block is accepted. The damage is purely resource exhaustion.
- **Amplification bound**: Each `StrippedIngressMessage` proto entry is ~50 bytes (IngressMessageId + hash). With typical P2P message size limits in the MB range, K can reach tens of thousands per crafted message.

### Likelihood Explanation

- `HASHES_IN_BLOCKS_ENABLED` is `true`, so the code path is live.
- The attacker only needs to be a Byzantine subnet peer (not a majority). A single Byzantine node below the fault threshold can execute this.
- No cryptographic material needs to be forged; the attacker reuses a legitimately signed `pruned_block_proposal_proto` and only modifies the unsigned `ingress_messages` list.
- The attack is repeatable for every block proposal advertisement.

### Recommendation

Deduplicate `ingress_messages` at one of the following points (earliest is best):

1. **In `TryFrom<pb::StrippedBlockProposal>`**: reject or deduplicate if `ingress_messages` contains duplicate `SignedIngressId` values.
2. **In `BlockProposalAssembler::new`**: use a `BTreeSet` or `HashSet` instead of a `Vec` for `ingress_messages`, or deduplicate before constructing the `Vec`.
3. **In `missing_stripped_messages`**: deduplicate the returned iterator before it is consumed by `assemble_message`.

Option 1 is preferred as it rejects malformed input at the boundary.

### Proof of Concept

```rust
// Construct a StrippedBlockProposal with K=100 duplicate ingress entries
let ingress_id = fake_ingress_message("fake_1").id();
let duplicate_ids = vec![ingress_id.clone(); 100];
let stripped = fake_stripped_block_proposal_with_messages(duplicate_ids);
let assembler = BlockProposalAssembler::new(stripped);

// missing_stripped_messages returns 100 duplicate IDs — no deduplication
let missing = assembler.missing_stripped_messages();
assert_eq!(missing.len(), 100); // all 100 are returned

// assemble_message would spawn 100 concurrent get_or_fetch tasks,
// issuing up to 100 simultaneous RPCs for the same ingress message.
``` [9](#0-8)

### Citations

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs (L67-99)
```rust
impl TryFrom<pb::StrippedBlockProposal> for StrippedBlockProposal {
    type Error = ProxyDecodeError;

    fn try_from(value: pb::StrippedBlockProposal) -> Result<Self, Self::Error> {
        let pruned_block_proposal_proto = value
            .pruned_block_proposal
            .ok_or_else(|| ProxyDecodeError::MissingField("pruned_block_proposal"))?;

        if pruned_block_proposal_proto
            .value
            .as_ref()
            .is_some_and(|block| block.ingress_payload.is_some())
        {
            return Err(ProxyDecodeError::Other(String::from(
                "The ingress payload is NOT empty",
            )));
        }

        let unstripped_consensus_message_id: ConsensusMessageId = try_from_option_field(
            value.unstripped_consensus_message_id,
            "unstripped_consensus_message_id",
        )?;

        if !matches!(
            unstripped_consensus_message_id.hash,
            ConsensusMessageHash::BlockProposal(_)
        ) {
            return Err(ProxyDecodeError::Other(format!(
                "The unstripped consensus message id {:?} is NOT for a block proposal",
                unstripped_consensus_message_id,
            )));
        }

```

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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L435-453)
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
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L571-598)
```rust
impl BlockProposalAssembler {
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

    /// Returns the list of messages which have been stripped from the block.
    pub(crate) fn missing_stripped_messages(&self) -> Vec<StrippedMessageId> {
        let ingress_messages = PayloadAssembler::<SignedIngress>::missing_artifacts(self)
            .map(StrippedMessageId::Ingress);
        let idkg_dealings = PayloadAssembler::<SignedIDkgDealing>::missing_artifacts(self)
            .map(|(node_index, dealing_id)| StrippedMessageId::IDkgDealing(dealing_id, node_index));

        ingress_messages.chain(idkg_dealings).collect()
    }
```

**File:** rs/consensus/features/src/lib.rs (L1-6)
```rust
/// [IC-1718]: Whether the `hashes-in-blocks` feature is enabled. If the flag is set to `true`, we
/// will strip all ingress messages and IDKG dealings from blocks, before sending them to peers.
/// On a receiver side, we will reconstruct the blocks by looking up the referenced ingress messages
/// in the ingress pool and IDKG dealings in the IDKG pool, or, if they are not there, by fetching
/// missing artifacts from peers who are advertising the blocks.
pub const HASHES_IN_BLOCKS_ENABLED: bool = true;
```
