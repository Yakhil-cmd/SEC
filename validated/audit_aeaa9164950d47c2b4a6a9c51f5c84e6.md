### Title
Error Condition Detected but Execution Continues After Invalid Reject Signal for Response/Refund - (File: `rs/messaging/src/routing/stream_handler.rs`)

### Summary

In `StreamHandlerImpl::handle_rejected_messages`, when a `Response` or `Refund` message receives a reject signal with a reason other than `CanisterMigrating`, the code logs a critical error and increments a counter, but then unconditionally proceeds to reroute the message. A malicious remote subnet (xnet origin) can craft certified stream slices containing reject signals with arbitrary `RejectReason` values for responses/refunds, bypassing the intended signal-type validation and forcing rerouting of messages that should not be rerouted.

### Finding Description

`handle_rejected_messages` in `rs/messaging/src/routing/stream_handler.rs` processes messages for which a reject signal was received from a remote subnet. For `StreamMessage::Response` and `StreamMessage::Refund` variants, the only semantically valid reject reason is `RejectReason::CanisterMigrating`. The code explicitly checks for this:

```rust
StreamMessage::Response(_) | StreamMessage::Refund(_) => {
    if reason != RejectReason::CanisterMigrating {
        error!(
            self.log,
            "{}: Received unsupported reject reason {:?} from {} for {:?}",
            CRITICAL_ERROR_BAD_REJECT_SIGNAL_FOR_RESPONSE,
            ...
        );
        self.metrics.critical_error_bad_reject_signal_for_response.inc();
    }
    // The policy for guaranteed responses enforces rerouting all responses
    // regardless of signal/response pairing.
    reroute_message(msg, state, streams, &self.log);
}
``` [1](#0-0) 

The error branch logs the anomaly and increments a metric, but **does not return or abort**. Execution falls through unconditionally to `reroute_message`, which pushes the message into an outgoing stream based on the routing table. The XNet payload builder's `validate_signals` function, which gates inclusion of stream slices into blocks, validates monotonicity and range of signals but **does not validate `RejectReason` field values**: [2](#0-1) 

This means a certified stream slice from a remote subnet carrying `RejectReason::CanisterNotFound`, `RejectReason::QueueFull`, or any other non-`CanisterMigrating` reason for a response/refund will pass all validation checks and reach `handle_rejected_messages`, where the error is noted but the message is rerouted anyway.

### Impact Explanation

**Vulnerability class**: message-routing/xnet ordering bug; cycles/resource accounting bug.

When a response or refund is rerouted due to an invalid reject signal:

1. `reroute_message` calls `state.metadata.network_topology.route(response.receiver().get()).expect(...)`. If the receiver canister is absent from the routing table (e.g., deleted canister), this panics, causing a deterministic execution divergence / subnet halt for all nodes processing that batch. [3](#0-2) 

2. If the canister is present, the response is pushed into an outgoing stream to whatever subnet the routing table currently maps the receiver to. This can cause the response to be delivered to a subnet that did not originate the request, breaking the guaranteed-response delivery invariant and potentially causing cycles attached to the response to be credited to the wrong canister or lost.

3. The `CRITICAL_ERROR_BAD_REJECT_SIGNAL_FOR_RESPONSE` counter is incremented but the subnet continues processing, masking the attack as a metric anomaly rather than halting or rejecting the offending slice.

### Likelihood Explanation

The attacker is a **remote subnet acting as an xnet origin**. To produce a certified stream slice with an arbitrary `RejectReason`, a majority of the remote subnet's nodes must collude to sign the stream header. This is a subnet-level Byzantine fault. While this raises the bar, the IC explicitly lists "xnet origin" as a valid attacker class in its threat model, and the receiving subnet's validation pipeline has no defense against it: `validate_signals` checks only index ordering and range, not the semantic validity of `RejectReason` values. Once such a slice is included in a block (which passes all existing validation), every honest node on the receiving subnet will execute the same erroneous rerouting deterministically.

### Recommendation

1. **Add a `return` after the error branch**: When `reason != RejectReason::CanisterMigrating` for a response or refund, do not call `reroute_message`. Instead, push an accept signal to the reverse stream (to prevent the remote subnet from retrying) and drop the message, logging the cycles lost.

2. **Validate `RejectReason` in `validate_signals`**: Extend the XNet payload builder's signal validation to reject stream slices that carry non-`CanisterMigrating` reject signals for message indices that correspond to responses/refunds in the outgoing stream. This would block the malformed slice before it reaches `handle_rejected_messages`. [4](#0-3) 

### Proof of Concept

1. A malicious remote subnet `R` has a response `resp` in its incoming stream from the local subnet `L` (i.e., `L` sent a request to `R`, `R` responded, and `L` is waiting for the response to be GC'd via a signal).
2. `R`'s nodes collude to produce a certified stream header with a reject signal `RejectSignal { reason: RejectReason::CanisterNotFound, index: <resp_index> }` for `resp`.
3. `L`'s XNet payload builder calls `validate_signals`, which checks only that `signals_end` is monotonically increasing and within bounds — the `CanisterNotFound` reason passes unchecked.
4. The slice is included in a block on `L`. During `process_batch`, `garbage_collect_local_state` → `garbage_collect_messages` extracts `(CanisterNotFound, resp)` as a rejected message.
5. `handle_rejected_messages` matches `StreamMessage::Response(_)`, logs `CRITICAL_ERROR_BAD_REJECT_SIGNAL_FOR_RESPONSE`, increments the counter, and then calls `reroute_message(resp, ...)`.
6. If `resp.receiver()` (the originator canister on `L`) has since been deleted, `route(...).expect(...)` panics — every node on `L` halts at the same height, causing a subnet-wide stall.
7. If the canister still exists, `resp` is pushed into an outgoing stream, potentially delivering it to the wrong subnet or causing a duplicate delivery. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/messaging/src/routing/stream_handler.rs (L555-573)
```rust
        fn reroute_message(
            response: StreamMessage,
            state: &ReplicatedState,
            streams: &mut StreamMap,
            log: &ReplicaLogger,
        ) {
            let new_destination = state
                .metadata
                .network_topology
                .route(response.receiver().get())
                .expect("Canister disappeared from registry. Registry in an inconsistent state.");
            info!(
                log,
                "Canister {} is being migrated, rerouting to subnet {} for {:?}",
                response.receiver(),
                new_destination,
                response,
            );
            streams.entry(new_destination).or_default().push(response);
```

**File:** rs/messaging/src/routing/stream_handler.rs (L622-642)
```rust
                // Refunds are treated the same as responses for rerouting purposes.
                StreamMessage::Response(_) | StreamMessage::Refund(_) => {
                    if reason != RejectReason::CanisterMigrating {
                        // Signals other than `CanisterMigrating` shouldn't be possible for
                        // responses or refunds.
                        error!(
                            self.log,
                            "{}: Received unsupported reject reason {:?} from {} for {:?}",
                            CRITICAL_ERROR_BAD_REJECT_SIGNAL_FOR_RESPONSE,
                            reason,
                            remote_subnet_id,
                            msg,
                        );
                        self.metrics
                            .critical_error_bad_reject_signal_for_response
                            .inc();
                    }
                    // The policy for guaranteed responses enforces rerouting all responses
                    // regardless of signal/response pairing.
                    reroute_message(msg, state, streams, &self.log);
                }
```

**File:** rs/xnet/payload_builder/src/lib.rs (L515-534)
```rust
    /// Validates the signals of the incoming `StreamSlice` from
    /// `subnet_id` with respect to `expected` (the expected signal index);
    /// and to `messages_end()` of the outgoing `Stream` to `subnet_id`.
    ///
    /// In particular:
    ///
    ///  1. `signals_end` must be monotonically increasing, i.e. `expected <=
    ///     signals_end`;
    ///
    ///  2. signals must only refer to past and current messages, i.e.
    ///     `signals_end <= stream.messages_end()`;
    ///
    ///  3. `signals_end - reject_signals[0] <= MAX_STREAM_MESSAGES`; and
    ///
    ///  4. `concat(reject_signals, [signals_end])` must be strictly increasing.
    ///     and
    ///
    /// Because this code is used both for validating slices before inclusion into a
    /// payload; and for validating proposed payloads; validation errors are logged
    /// at configurable levels (e.g. `info` at selection, `warn` at validation).
```

**File:** rs/xnet/payload_builder/src/lib.rs (L535-609)
```rust
    fn validate_signals(
        &self,
        subnet_id: SubnetId,
        signals_end: StreamIndex,
        reject_signals: &VecDeque<RejectSignal>,
        expected: StreamIndex,
        state: &ReplicatedState,
        log_level: slog::Level,
    ) -> SignalsValidationResult {
        // `messages_end()` of the outgoing stream.
        let (self_messages_begin, self_messages_end) = state
            .streams()
            .get(&subnet_id)
            .map(|s| (s.messages_begin(), s.messages_end()))
            .unwrap_or_default();

        // Must expect signal for existing message (or just beyond last message).
        assert!(
            self_messages_begin <= expected && expected <= self_messages_end,
            "Subnet {subnet_id}: invalid expected signal; messages_begin() ({self_messages_begin}) <= expected ({expected}) <= messages_end() ({self_messages_end})"
        );

        if expected > signals_end || signals_end > self_messages_end {
            log!(
                self.log,
                log_level,
                "Invalid stream from {}: expected ({}) <= signals_end ({}) <= self.messages_end() ({})",
                subnet_id,
                expected,
                signals_end,
                self_messages_end
            );
            return SignalsValidationResult::Invalid;
        }

        if !reject_signals.is_empty() {
            // A stream can never have more than `MAX_SIGNALS` signals by design,
            // since we stop pulling messages after reaching this limit. Any subnet with
            // more than this number of signals can therefore be classified as dishonest.
            // The factor of 2 allows for wiggle room in increasing this constant without
            // touching this, but is still good enough as a guard against dishonest subnets.
            // Furthermore, an honest subnet will only produce signals for the messages in
            // the incoming stream (i.e. no signals for future messages; and all signals for
            // past messages have been GC-ed).
            let signals_begin = reject_signals.front().unwrap();
            if signals_end.get() - signals_begin.index.get() > 2 * MAX_SIGNALS as u64 {
                log!(
                    self.log,
                    log_level,
                    "Too old reject signal in stream from {}: signals_begin {}, signals_end {}",
                    subnet_id,
                    signals_begin.index,
                    signals_end
                );
                return SignalsValidationResult::Invalid;
            }

            let mut next = signals_end;
            for signal in reject_signals.iter().rev() {
                if signal.index >= next {
                    log!(
                        self.log,
                        log_level,
                        "Invalid signals in stream from {}: reject_signals {:?}, signals_end {}",
                        subnet_id,
                        reject_signals,
                        signals_end
                    );
                    return SignalsValidationResult::Invalid;
                }
                next = signal.index;
            }
        }

        SignalsValidationResult::Valid
```
