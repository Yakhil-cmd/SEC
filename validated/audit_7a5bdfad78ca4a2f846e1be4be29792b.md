The code confirms all the described behaviors. Let me verify the key lines precisely before rendering the final output.

Audit Report

## Title
Canister Upgrade During `sweep_icp` Permanently Locks Buyer ICP With No Recovery Path - (File: rs/sns/swap/src/types.rs)

## Summary
`TransferableAmount::transfer_helper` sets `transfer_start_timestamp_seconds` to a non-zero value at line 621 **before** the async ledger `await` at line 633. If the SNS swap canister is upgraded between those two points, the async callback is dropped, leaving `transfer_start_timestamp_seconds > 0` and `transfer_success_timestamp_seconds == 0` persisted in stable state. Subsequent `sweep_icp` calls skip the buyer (`AlreadyStarted`), and `error_refund_icp` blocks the buyer with "ICP in escrow" because it gates solely on `transfer_success_timestamp_seconds == 0`. The buyer's ICP is permanently unrecoverable without a privileged manual upgrade.

## Finding Description
**Root cause — `rs/sns/swap/src/types.rs`, `transfer_helper`, lines 617–633:**

```
617: if self.transfer_start_timestamp_seconds > 0 {
618:     return TransferResult::AlreadyStarted;
619: }
621: self.transfer_start_timestamp_seconds = now_fn(false);   // ← written before await
625: let result = ledger.transfer_funds(...).await;            // ← upgrade can occur here
642:     self.transfer_success_timestamp_seconds = now_fn(true); // ← never reached after upgrade
655:     self.transfer_start_timestamp_seconds = 0;              // ← never reached after upgrade
```

On the IC, when a canister is upgraded, in-flight async continuations (the Rust future state machines stored on the heap) are discarded. The canister state serialized to stable memory via `pre_upgrade` captures `transfer_start_timestamp_seconds > 0` but `transfer_success_timestamp_seconds == 0`. After the upgrade, the ledger response callback is never delivered, so neither the success branch (line 642) nor the failure branch (line 655) executes.

**Resulting stuck state — two independent locks:**

1. `sweep_icp` (`rs/sns/swap/src/swap.rs`, lines 2113–2131): calls `transfer_helper`, which returns `AlreadyStarted` at line 617–619 because `transfer_start_timestamp_seconds > 0`. The buyer is counted as `skipped` and never retried.

2. `error_refund_icp` (`rs/sns/swap/src/swap.rs`, lines 1950–1959): checks `transfer_success_timestamp_seconds == 0` as the sole gate. Because this field is still 0, the function returns the precondition error "ICP in escrow" and aborts before querying the subaccount balance or attempting any transfer.

Both recovery paths are simultaneously blocked. The check at line 1952 does not distinguish between "transfer confirmed" (`transfer_success_timestamp_seconds > 0`) and "transfer in-flight but unconfirmed" (`transfer_start_timestamp_seconds > 0`, `transfer_success_timestamp_seconds == 0`). The correct guard for "truly in escrow and not yet attempted" would require both timestamps to be zero.

## Impact Explanation
Any SNS swap participant whose `sweep_icp` transfer was in-flight at the moment of a canister upgrade permanently loses access to their ICP. The funds remain in the buyer's subaccount on the ICP ledger but are unreachable: `sweep_icp` will never re-attempt the transfer, and `error_refund_icp` will always return a precondition error. The only remediation is a subsequent privileged canister upgrade that manually zeroes `transfer_start_timestamp_seconds` for affected buyers. This constitutes a concrete, permanent loss of user ICP funds with no unprivileged recovery path, matching the High impact class: **Significant SNS security impact with concrete user or protocol harm**.

## Likelihood Explanation
SNS swap canister upgrades are routine NNS governance operations that occur independently of swap finalization timing. `sweep_icp` iterates over all buyers and issues one async ledger call per buyer; for swaps with many participants, the execution window spans many rounds. Any NNS upgrade proposal that executes during this window — without any malicious intent — triggers the stuck state for all buyers whose transfers were in-flight. This is a non-adversarial, realistic scenario requiring no special privileges from the triggering party.

## Recommendation
In `transfer_helper` (`rs/sns/swap/src/types.rs`), do not write `transfer_start_timestamp_seconds` before the `await`. Use a transient in-memory flag (not persisted to stable state) to prevent concurrent re-entry, and only write `transfer_start_timestamp_seconds` after the ledger confirms success. Alternatively, change the `error_refund_icp` gate in `rs/sns/swap/src/swap.rs` at line 1952 from:

```rust
transfer.transfer_success_timestamp_seconds == 0
```

to:

```rust
transfer.transfer_start_timestamp_seconds == 0 && transfer.transfer_success_timestamp_seconds == 0
```

so that a buyer whose transfer was started but not confirmed (the upgrade-interrupted state) is not permanently blocked from the error-refund path.

## Proof of Concept
1. Deploy SNS swap canister with `max_participant_icp_e8s = X`.
2. Buyer deposits `X` ICP into their swap subaccount and calls `refresh_buyer_tokens`; `BuyerState.icp.amount_e8s = X`.
3. Swap reaches ABORTED. `sweep_icp` is called. For this buyer, `transfer_helper` executes line 621 (`transfer_start_timestamp_seconds = now_fn(false)`) and issues `transfer_funds` to the ICP ledger.
4. Before the ledger response arrives, upgrade the SNS swap canister (simulate via PocketIC by installing a new Wasm mid-execution, or by using `ic-cdk`'s upgrade hooks in an integration test that drops the pending response).
5. After upgrade, inspect state: `transfer_start_timestamp_seconds > 0`, `transfer_success_timestamp_seconds == 0`.
6. Call `sweep_icp` again. Observe buyer counted as `skipped` (not `success` or `failure`).
7. Call `error_refund_icp` for the buyer. Observe response: precondition error "ICP in escrow."
8. Confirm buyer's ICP subaccount still holds `X` on the ledger but is permanently inaccessible through any public canister interface.