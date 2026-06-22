I have all the code needed to make a definitive determination. Let me trace the exact path.

---

### Title
Duplicate `SignedIngressId` in `StrippedBlockProposal` causes `AlreadyInserted` → `AssembleResult::Unwanted`, suppressing valid block proposals — (`rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs`)

### Summary

A Byzantine peer can craft a `StrippedBlockProposal` protobuf with duplicate entries in `ingress_messages`. Neither `StrippedBlockProposal::try_from` nor `BlockProposalAssembler::new` deduplicate these entries. This causes two concurrent `get_or_fetch` tasks to be spawned for the same `SignedIngressId`. When both complete and the second insertion is attempted, `try_insert` returns `InsertionError::AlreadyInserted`, which causes `assemble_message` to return `AssembleResult::Unwanted` — permanently suppressing a valid block proposal from an honest block maker.

### Finding Description

**No deduplication in `StrippedBlockProposal::try_from`:**

The deserialization of a peer-supplied `pb::StrippedBlockProposal` collects `ingress_messages` directly into a `Vec` with no uniqueness check: [1](#0-0) 

**No deduplication in `BlockProposalAssembler::new`:**

The assembler maps the `ingress_messages` vec 1:1 into `(SignedIngressId, Option<SignedIngress>)` pairs, preserving any duplicates: [2](#0-1) 

**`missing_stripped_messages` returns both duplicate entries:**

`missing_artifacts` iterates the full vec and returns all `None` slots — both duplicates are `None` initially, so both are returned: [3](#0-2) 

**Two `get_or_fetch` tasks are spawned for the same ID:** [4](#0-3) 

**`try_insert` uses `find` (returns first match) and returns `AlreadyInserted` on the second call:**

The first call finds the first duplicate slot (`None`), fills it. The second call finds the same first slot (now `Some`), returns `AlreadyInserted`. The second duplicate slot remains `None` forever: [5](#0-4) 

**`AlreadyInserted` causes `assemble_message` to return `Unwanted`:** [6](#0-5) 

The final ID-integrity check in `try_assemble` is never reached because `Unwanted` is returned before it: [7](#0-6) 

### Impact Explanation

`AssembleResult::Unwanted` signals to the P2P layer that the artifact should not be processed. A Byzantine peer who is first to push a `StrippedBlockProposal` for a given `unstripped_consensus_message_id` (which is observable on the wire) can inject a duplicate-ID version, causing every honest node that processes it to permanently discard that block proposal ID. This is a **consensus liveness attack**: the honest block maker's proposal is suppressed, stalling the round.

### Likelihood Explanation

- The attacker only needs to be a single Byzantine subnet peer (well below the fault threshold).
- The `unstripped_consensus_message_id` is broadcast in the clear and can be observed before the full stripped proposal is processed.
- The attack requires crafting a valid protobuf with a repeated field — trivial with any protobuf library.
- No cryptographic material needs to be forged; the malicious stripped proposal carries the correct claimed ID.
- The existing test `stripped_message_insertion_existing_fails_test` (assembler.rs:961–979) already confirms `AlreadyInserted` is returned on a second insertion, but no test covers the duplicate-ID-in-stripped-proposal path. [8](#0-7) 

### Recommendation

Add a deduplication check in `StrippedBlockProposal::try_from` immediately after collecting `ingress_messages`:

```rust
// After collecting ingress_messages:
let mut seen = std::collections::HashSet::new();
for id in &ingress_messages {
    if !seen.insert(id) {
        return Err(ProxyDecodeError::Other(
            "Duplicate SignedIngressId in stripped_ingress_payload".into()
        ));
    }
}
```

Apply the same check to `stripped_idkg_dealings`. This rejects the malicious artifact at deserialization time, before any assembler state is created. [1](#0-0) 

### Proof of Concept

```rust
// Construct a stripped block proposal with two identical SignedIngressId entries
let (ingress, ingress_id) = fake_ingress_message_with_arg_size("dup", 64);
let mut stripped = fake_stripped_block_proposal_with_messages(vec![
    StrippedMessageId::Ingress(ingress_id.clone()),
    StrippedMessageId::Ingress(ingress_id.clone()), // duplicate
]);

let mut assembler = BlockProposalAssembler::new(stripped);
// Both slots are missing → two tasks spawned
assert_eq!(assembler.missing_stripped_messages().len(), 2);

// First insertion succeeds
assembler.try_insert_stripped_message(
    StrippedMessage::Ingress(ingress_id.clone(), ingress.clone())
).unwrap();

// Second insertion returns AlreadyInserted → assemble_message returns Unwanted
assert_eq!(
    assembler.try_insert_stripped_message(
        StrippedMessage::Ingress(ingress_id.clone(), ingress.clone())
    ),
    Err(InsertionError::AlreadyInserted)
);
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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L428-433)
```rust
    fn missing_artifacts(&self) -> impl Iterator<Item = SignedIngressId> {
        self.ingress_messages
            .iter()
            .filter(|(_, maybe_ingress)| maybe_ingress.is_none())
            .map(|(id, _)| id.clone())
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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L571-588)
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
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L640-646)
```rust
        let assembled_id = reconstructed_block_proposal.get_id();
        if assembled_id != claimed_id {
            return Err(AssemblyError::MismatchedConsensusMessageId {
                claimed: claimed_id,
                assembled: assembled_id,
            });
        }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L961-979)
```rust
    #[test]
    fn stripped_message_insertion_existing_fails_test() {
        let ingress_2 = fake_ingress_message("fake_2");
        let idkg_dealing_2 = fake_idkg_dealing(NODE_2, 2);
        let stripped_block_proposal =
            fake_stripped_block_proposal_with_messages(vec![ingress_2.id(), idkg_dealing_2.id()]);

        let mut assembler = BlockProposalAssembler::new(stripped_block_proposal);

        for message in [ingress_2, idkg_dealing_2] {
            assembler
                .try_insert_stripped_message(message.clone())
                .expect("Should successfully insert the missing message");

            assert_eq!(
                assembler.try_insert_stripped_message(message),
                Err(InsertionError::AlreadyInserted)
            );
        }
```
