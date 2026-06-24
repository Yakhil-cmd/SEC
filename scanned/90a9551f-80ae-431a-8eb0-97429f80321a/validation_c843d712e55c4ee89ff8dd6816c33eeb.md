### Title
Byzantine Peer Can Suppress Valid Block Proposal Assembly via Duplicate Ingress IDs in StrippedBlockProposal — (`rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs`)

---

### Summary

A Byzantine peer can craft a `StrippedBlockProposal` protobuf with duplicate `SignedIngressId` entries in `ingress_messages`. The deserialization path has no deduplication guard. This causes `BlockProposalAssembler` to spawn two concurrent `get_or_fetch` tasks for the same ingress ID; when the second result is inserted, `try_insert` finds the already-filled first slot and returns `InsertionError::AlreadyInserted`, which causes `assemble_message` to return `AssembleResult::Unwanted` — permanently aborting assembly of an otherwise valid block proposal for that invocation.

---

### Finding Description

**Step 1 — No deduplication in `StrippedBlockProposal::try_from`** [1](#0-0) 

`value.ingress_messages` is mapped directly into a `Vec<SignedIngressId>` with no uniqueness check. A Byzantine peer can include the same `pb::StrippedIngressMessage` twice in the protobuf and deserialization succeeds.

**Step 2 — Duplicates propagate into `BlockProposalAssembler::ingress_messages`** [2](#0-1) 

Each entry is mapped to `(signed_ingress_id.clone(), None)`, so the Vec contains two identical `SignedIngressId` keys, both with `None` values.

**Step 3 — `missing_stripped_messages` returns both duplicate entries** [3](#0-2) 

`missing_artifacts` filters for `None` slots; both duplicate slots are `None`, so both IDs are returned.

**Step 4 — Two concurrent `get_or_fetch` tasks are spawned** [4](#0-3) 

One task per entry in `stripped_message_ids`, so two tasks race to fetch the same ingress message.

**Step 5 — `try_insert` uses `find` (first-match), not find-first-None** [5](#0-4) 

`iter_mut().find(|(id, _)| *id == signed_ingress_id)` always returns the **first** matching entry regardless of whether it is `None` or `Some`. After the first task fills slot 0 with `Some(ingress)`, the second task's `find` again lands on slot 0 (now `Some`) and returns `Err(InsertionError::AlreadyInserted)`. Slot 1 (the duplicate, still `None`) is never considered.

**Step 6 — `AlreadyInserted` causes `AssembleResult::Unwanted`** [6](#0-5) 

Any `Err` from `try_insert_stripped_message` immediately returns `AssembleResult::Unwanted`, aborting assembly of the block proposal for this invocation.

---

### Impact Explanation

A single Byzantine peer (no threshold corruption required) can prevent any replica from assembling a valid block proposal by pushing a malformed `StrippedBlockProposal` with duplicate ingress IDs. The `unstripped_consensus_message_id` in the crafted message can be set to the real block's ID (learned from the public consensus advertisement). The `try_assemble` hash-mismatch check is never reached because `Unwanted` is returned earlier. If the P2P layer does not immediately retry with a fresh stripped artifact from an honest peer, the block proposal is suppressed for that assembly cycle, stalling consensus progress.

---

### Likelihood Explanation

The attack requires only a single Byzantine peer participating in the P2P gossip layer — well below any consensus fault threshold. The crafted protobuf is trivially constructable (repeat one `StrippedIngressMessage` entry). No cryptographic material, admin keys, or privileged access is needed. The attacker only needs to know the `ConsensusMessageId` of the target block, which is publicly advertised.

---

### Recommendation

Add a duplicate-ID check in `StrippedBlockProposal::try_from` (or in `BlockProposalAssembler::new`) before building the `ingress_messages` Vec. For example, after collecting the `Vec<SignedIngressId>`, verify that all entries are distinct:

```rust
// In TryFrom<pb::StrippedBlockProposal> for StrippedBlockProposal
let ingress_messages: Vec<SignedIngressId> = value
    .ingress_messages
    .into_iter()
    .map(SignedIngressId::try_from)
    .collect::<Result<Vec<_>, _>>()?;

let unique_count = ingress_messages.iter().collect::<std::collections::HashSet<_>>().len();
if unique_count != ingress_messages.len() {
    return Err(ProxyDecodeError::Other(
        "stripped_ingress_payload contains duplicate ingress IDs".into()
    ));
}
```

Alternatively, fix `try_insert` to search for the first `None` slot matching the ID rather than the first slot matching the ID regardless of fill state — but the deserialization guard is the correct defense-in-depth layer.

---

### Proof of Concept

```rust
#[test]
fn duplicate_ingress_id_causes_unwanted() {
    let (ingress, ingress_id) = fake_ingress_message_with_arg_size("fake_1", 1024);
    // Build a stripped block proposal with the same ingress ID listed twice
    let stripped = fake_stripped_block_proposal_with_messages(vec![
        StrippedMessageId::Ingress(ingress_id.clone()),
        StrippedMessageId::Ingress(ingress_id.clone()), // duplicate
    ]);
    let mut assembler = BlockProposalAssembler::new(stripped);

    // First insertion succeeds
    assembler
        .try_insert_stripped_message(StrippedMessage::Ingress(ingress_id.clone(), ingress.clone()))
        .expect("first insert should succeed");

    // Second insertion (same ID, same message — simulating the second get_or_fetch result)
    // returns AlreadyInserted, which assemble_message maps to AssembleResult::Unwanted
    assert_eq!(
        assembler.try_insert_stripped_message(StrippedMessage::Ingress(ingress_id, ingress)),
        Err(InsertionError::AlreadyInserted)
    );
}
```

### Citations

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs (L100-107)
```rust
        Ok(Self {
            pruned_block_proposal_proto,
            stripped_ingress_payload: StrippedIngressPayload {
                ingress_messages: value
                    .ingress_messages
                    .into_iter()
                    .map(SignedIngressId::try_from)
                    .collect::<Result<Vec<_>, _>>()?,
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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L574-579)
```rust
            ingress_messages: stripped_block_proposal
                .stripped_ingress_payload
                .ingress_messages
                .iter()
                .map(|signed_ingress_id| (signed_ingress_id.clone(), None))
                .collect(),
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
