Now I have a complete picture of the code. Let me analyze both claims precisely.

I now have a complete picture of the code. Let me trace both claims precisely.

**Claim 1: Unbounded JoinSet spawning**

`TryFrom<pb::StrippedBlockProposal> for StrippedBlockProposal` (lines 100–108 of `stripped.rs`) performs no deduplication on `ingress_messages`: [1](#0-0) 

A Byzantine peer can craft a `pb::StrippedBlockProposal` with N identical `StrippedIngressMessage` entries. All N deserialize successfully into `StrippedIngressPayload::ingress_messages`.

`BlockProposalAssembler::new` maps each entry to `(SignedIngressId, None)` with no deduplication: [2](#0-1) 

`missing_stripped_messages()` returns all N entries (all `None`): [3](#0-2) 

`assemble_message` spawns one `get_or_fetch` task per entry with **no cap**: [4](#0-3) 

**Claim 2: AlreadyInserted → Unwanted**

`try_insert` uses `find` (first-match linear scan). After the first task fills the first matching slot, the second task for the same ID finds that same slot (now `Some`) and returns `InsertionError::AlreadyInserted`: [5](#0-4) 

`assemble_message` treats any insertion error as fatal and returns `AssembleResult::Unwanted`, aborting the entire block assembly: [6](#0-5) 

The `try_assemble()` ID-mismatch check (which would catch a fabricated block) is **never reached** because `Unwanted` is returned first: [7](#0-6) 

**Attack vector feasibility**

The `StrippedBlockProposal` is received over the P2P transport layer. A Byzantine block maker for a given round knows the `unstripped_consensus_message_id` (the hash of their own block). They can advertise the legitimate `ConsensusMessageId` but return a crafted `StrippedBlockProposal` with N duplicate `ingress_messages` entries. The deserialization only validates that the `unstripped_consensus_message_id` is a `BlockProposal` hash — it does not cross-check the `ingress_messages` list against the pruned proto: [8](#0-7) 

The developer comment "We can have at most 1000 elements in the vector" confirms the expected bound, but it is never enforced: [9](#0-8) 

---

### Title
Unbounded JoinSet task spawning and forced `AssembleResult::Unwanted` via duplicate `StrippedIngressMessage` entries in crafted `StrippedBlockProposal` — (`rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs`)

### Summary
A Byzantine block maker below the fault threshold can craft a `StrippedBlockProposal` containing N duplicate `SignedIngressId` entries. The receiving replica spawns N unbounded concurrent `get_or_fetch` tasks (one per entry, no cap), and when the second task for a duplicate ID completes, `try_insert` returns `InsertionError::AlreadyInserted`, which causes `assemble_message` to immediately return `AssembleResult::Unwanted`, silently aborting block assembly for that height.

### Finding Description
`TryFrom<pb::StrippedBlockProposal> for StrippedBlockProposal` collects `ingress_messages` into a `Vec` with no uniqueness check. `BlockProposalAssembler::new` maps each entry to `(SignedIngressId, None)`, preserving all duplicates. `missing_stripped_messages()` returns all N entries (all `None`). `assemble_message` spawns one `get_or_fetch` Tokio task per entry into a `JoinSet` with no size cap. When the first task resolves and inserts the message, the second task for the same ID hits the `AlreadyInserted` branch in `try_insert` (which uses `find` and always returns the first slot, now `Some`). `assemble_message` treats any `InsertionError` as fatal and returns `AssembleResult::Unwanted`, bypassing the `try_assemble()` ID-mismatch check entirely.

### Impact Explanation
- **Resource exhaustion**: N tasks are spawned immediately. Each task that misses the local pool enters `download_stripped_message`'s infinite retry loop, consuming Tokio task slots, memory, and outbound transport connections until the bouncer fires `Unwanted` (up to the 3-second refresh period).
- **Block assembly failure**: The `AlreadyInserted` path causes the block to be discarded as `Unwanted` on the affected replica. If the Byzantine block maker sends the malicious stripped version to all peers, all replicas fail to assemble the block for that height, stalling consensus until the height advances via a different block maker.

### Likelihood Explanation
The block maker role rotates deterministically. A single Byzantine node below the fault threshold that holds the block maker role for any round can execute this attack. No threshold corruption, key compromise, or external infrastructure attack is required. The crafted `StrippedBlockProposal` passes all current deserialization checks.

### Recommendation
1. **Deduplicate on deserialization**: In `TryFrom<pb::StrippedBlockProposal>`, reject (or deduplicate) `ingress_messages` and `stripped_idkg_dealings` if any duplicate IDs are present.
2. **Cap JoinSet size**: Before spawning tasks in `assemble_message`, assert or enforce that `stripped_message_ids.len()` does not exceed the protocol-level ingress payload limit.
3. **Fix `AlreadyInserted` handling**: Instead of returning `Unwanted` on `AlreadyInserted`, treat it as a no-op (the slot is already filled) so that duplicate tasks do not abort a legitimately assembled block.

### Proof of Concept
```rust
// Craft a StrippedBlockProposal with 10_000 duplicate ingress IDs
let ingress_id = fake_ingress_message("dup").id(); // StrippedMessageId::Ingress
let ids = vec![ingress_id; 10_000];
let stripped = fake_stripped_block_proposal_with_messages(ids);

// Serialize and deserialize — passes all current checks
let proto = pb::StrippedBlockProposal::from(stripped.clone());
let deserialized = StrippedBlockProposal::try_from(proto).unwrap();

// BlockProposalAssembler::new creates 10_000 (id, None) entries
let assembler = BlockProposalAssembler::new(deserialized);
// missing_stripped_messages() returns 10_000 entries
assert_eq!(assembler.missing_stripped_messages().len(), 10_000);

// assemble_message spawns 10_000 tasks — assert JoinSet is bounded (currently fails)
// When any two tasks complete, AlreadyInserted fires → AssembleResult::Unwanted
```

### Citations

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs (L67-130)
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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L296-309)
```rust
        match assembler.try_assemble() {
            Ok(reconstructed_block_proposal) => AssembleResult::Done {
                message: ConsensusMessage::BlockProposal(reconstructed_block_proposal),
                peer_id: peer,
            },
            Err(err) => {
                warn!(
                    self.log,
                    "Failed to reassemble the block {}. This is a bug.", err
                );

                AssembleResult::Unwanted
            }
        }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L440-441)
```rust
        // We can have at most 1000 elements in the vector, so it should be reasonably fast to do a
        // linear scan here.
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L442-453)
```rust
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
