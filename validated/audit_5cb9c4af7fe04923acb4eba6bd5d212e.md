All cited code references are confirmed accurate. The decode-before-verify ordering at lines 4135/4145 is real, `sub_tree_map_from` has no child-count guard, `FlatMap::from_key_values` sorts unconditionally, and the HTTP body cap is `5 * POOL_SLICE_BYTE_SIZE_MAX`. The SECURITY.md DoS exclusion is a generic Immunefi template scoped to volumetric DDoS under "Websites and Apps" — it does not exclude protocol-level memory exhaustion via crafted peer messages, which falls under the explicitly allowed High impact class.

---

Audit Report

## Title
Pre-authentication unbounded heap allocation and O(N log N) sort via crafted XNet `CertifiedStreamSlice` payload — (`rs/state_manager/src/lib.rs`, `rs/crypto/tree_hash/src/proto.rs`)

## Summary
In `decode_certified_stream_slice`, the XNet payload is fully decoded into a `LabeledTree` — including allocating and sorting all child nodes — before the certification signature is verified. Because `sub_tree_map_from` imposes no limit on the number of children, a single malicious XNet node can serve a crafted protobuf payload encoding millions of flat children, forcing the receiving replica to allocate ~1–2 GB of heap and perform an O(N log N) sort before the slice is rejected for invalid signature.

## Finding Description
In `rs/state_manager/src/lib.rs`, `decode_certified_stream_slice` calls `stream_encoding::decode_labeled_tree` at line 4135, which fully materializes the `LabeledTree` in memory, and only then calls `verify_recomputed_digest` at line 4145:

```
4135: let tree = stream_encoding::decode_labeled_tree(&certified_slice.payload)?;
...
4145: if !verify_recomputed_digest(...) { return Err(InvalidSignature) }
```

`decode_labeled_tree` → `v1::LabeledTree::proxy_decode` → `TryFrom<pb::LabeledTree>` → `sub_tree_map_from` in `rs/crypto/tree_hash/src/proto.rs` (lines 41–54) collects all children into a `Vec` with no count limit, then passes it to `FlatMap::from_key_values` in `rs/crypto/tree_hash/src/flat_map.rs` (lines 88–96), which sorts the entire Vec if unsorted. Prost's recursion limit (depth 100) does not apply to wide/flat trees. The HTTP client caps the body at `5 * POOL_SLICE_BYTE_SIZE_MAX` (`rs/xnet/payload_builder/src/lib.rs` line 1839), which at ~20 MB allows ~2.8M minimal children. Each child materializes as a heap-allocated `Label` (`Vec<u8>`) and `LabeledTree::Leaf(Vec<u8>)`; the sort buffer, the original Vec, and the final `FlatMap` (two separate `Vec`s) together consume ~1–2 GB of heap — all before any authentication check.

## Impact Explanation
A single malicious node on a remote subnet can repeatedly serve crafted slices to a receiving replica's XNet pool-refill task. Each crafted slice triggers ~1–2 GB of heap allocation and an O(N log N) sort before the slice is rejected. Under repeated fire, this causes sustained memory pressure or OOM on the receiving replica, crashing it or severely degrading its performance. This matches the allowed High impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

## Likelihood Explanation
Requires controlling one node on a remote subnet — below the consensus fault threshold, which is the explicitly allowed Byzantine peer attacker model. The XNet pool-refill task fetches from a single node per subnet per cycle, so one malicious node is sufficient. The crafted payload is trivially constructable as a protobuf `LabeledTree` with a `SubTree` containing millions of minimal `Child` entries.

## Recommendation
1. **Move signature verification before payload decoding**: verify `certification` against the raw `payload` bytes before calling `decode_labeled_tree`.
2. **Add a child-count limit** in `sub_tree_map_from`: reject if `subtree.children.len() > MAX_CHILDREN`.
3. **Add a payload byte-size limit** before calling `decode_labeled_tree`: reject if `payload.len()` exceeds a tight bound (e.g., `POOL_SLICE_BYTE_SIZE_MAX`).

## Proof of Concept
Construct a protobuf `CertifiedStreamSlice` whose `payload` field encodes a `LabeledTree::SubTree` with ~2.8M children (1-byte labels, empty leaf nodes), totaling ~20 MB. Serve this as an HTTP response from a node acting as a malicious XNet peer. The receiving replica's pool-refill task will fetch the slice, call `decode_labeled_tree`, allocate ~1–2 GB of heap, perform an O(N log N) sort, and only then reject the slice for invalid signature. A local integration test using `PocketIC` or a local replica fork can confirm OOM or significant latency by instrumentating heap allocation around the `decode_labeled_tree` call with the crafted payload bytes from the Python PoC provided in the submission.