Audit Report

## Title
Cancellation-Unaware `wait_for` in `process_slot_update` Allows Byzantine Peer to Permanently Stall Assemble Tasks and Leak Unvalidated Pool Entries — (`rs/p2p/consensus_manager/src/receiver.rs`)

## Summary

After `assemble_message` returns `Done` or `Unwanted`, `process_slot_update` exits the outer `select!` and unconditionally awaits `peer_rx.wait_for(|p| p.is_empty())` with no cancellation guard. A Byzantine peer that advertises an artifact and never sends a slot-deletion keeps the `PeerCounter` non-empty indefinitely, permanently blocking the task. `UnvalidatedArtifactMutation::Remove` is never sent, the artifact leaks in the unvalidated pool, and the `active_assembles` entry is never reclaimed. The developers have already flagged this with `// TODO: NET-1774`.

## Finding Description

In `process_slot_update` (lines 480–536), the outer `select!` races three futures: `cancellation_token.cancelled()`, `assemble_artifact`, and `all_peers_deleted_artifact`. Once the `assemble_artifact` arm wins, execution leaves the `select!` entirely and the cancellation token is no longer polled.

The code then unconditionally awaits:

```rust
// TODO: NET-1774
let _ = peer_rx.wait_for(|p| p.is_empty()).await;   // line 500 (Done branch)
// …
let _ = peer_rx.wait_for(|p| p.is_empty()).await;   // line 521 (Unwanted branch)
```

`peer_rx` is a `watch::Receiver<PeerCounter>`. `wait_for` returns only when the predicate is true (counter empty) **or** when the `watch::Sender` is dropped. The sender lives in `active_assembles` and is only removed in `handle_artifact_processor_joined` (line 317–319) when the task finishes. This is a circular dependency: the task won't finish until `wait_for` returns; `wait_for` won't return until the sender is dropped; the sender won't be dropped until the task finishes.

A Byzantine peer that sends one slot-update for artifact X and then goes silent keeps the `PeerCounter` for X non-empty forever. The task is permanently stuck.

During shutdown, `start_event_loop` breaks on cancellation and drops `self`. Per the struct declaration order (lines 186–191), `active_assembles` (field 8) drops before `artifact_processor_tasks` (field 9, a `JoinSet`). Dropping `active_assembles` drops the sender, waking `wait_for` with `Err`. However, `artifact_processor_tasks` drops immediately after in the same synchronous sequence, aborting the task before it can execute the `Remove` send. `UnvalidatedArtifactMutation::Remove` is never sent even during graceful shutdown.

The only natural escape hatch is a topology update that removes the Byzantine peer from the subnet (lines 560–563). A Byzantine peer that remains a subnet member is never pruned this way.

## Impact Explanation

Per Byzantine peer, up to `slot_limit` assemble tasks can be permanently stuck. For each stuck task in the `Done` branch: `UnvalidatedArtifactMutation::Remove` is never sent, so the artifact leaks in the unvalidated pool for the lifetime of the node process. The `active_assembles` entry is permanently occupied, and Tokio task slots and watch-channel memory are consumed indefinitely. With `f` Byzantine peers each filling their slot table, the unvalidated pool accumulates up to `f × slot_limit` leaked artifacts. This is a persistent, bounded resource exhaustion that degrades replica performance over time without requiring any privileged access. This matches the **Medium** impact class: limited subnet availability impact with meaningful security impact, triggered by a below-threshold Byzantine peer with no special privileges.

## Likelihood Explanation

The attacker is a single Byzantine replica peer — an unprivileged protocol participant reachable via the standard P2P slot-update path (`update_handler` → `slot_updates_rx` → `handle_slot_update_receive`). No key material, governance majority, or external infrastructure is required. The peer simply sends one slot-update and then goes silent. The `// TODO: NET-1774` annotation at lines 499 and 520 confirms the DFINITY team has already identified this gap. The attack is trivially repeatable up to `slot_limit` times per Byzantine peer.

## Recommendation

Wrap both `wait_for` calls in a `select!` that also polls `cancellation_token.cancelled()`, treating cancellation as equivalent to "all peers deleted" (i.e., proceed to send `Remove` and exit):

```rust
select! {
    _ = cancellation_token.cancelled() => {}
    _ = peer_rx.wait_for(|p| p.is_empty()) => {
        let _ = sender.send(UnvalidatedArtifactMutation::Remove(id)).await;
    }
}
```

This is exactly what NET-1774 tracks and eliminates both the normal-operation stall and the shutdown leak.

## Proof of Concept

State-machine test (no network required):

1. Build a `ConsensusManagerReceiver` with a mock assembler that immediately returns `AssembleResult::Done`.
2. Call `handle_slot_update_receive` for peer A, artifact ID X — this spawns the assemble task.
3. Yield to the runtime so the task runs, sends `Insert`, and reaches `wait_for`.
4. Fire the cancellation token.
5. Assert (with a short timeout) that the task terminates **and** that `UnvalidatedArtifactMutation::Remove(X)` is received on the unvalidated-pool channel.

Under the current code, step 5 times out: the task is aborted by the `JoinSet` drop without ever sending `Remove`, confirming the leak.