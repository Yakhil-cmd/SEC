### Title
Insufficient Lower-Bound Validation on Oracle Body Iterator Causes Panic in `get_bytes_from_query` - (File: `zk_ee/src/oracle/mod.rs`)

### Summary
`get_bytes_from_query` validates that the oracle's body iterator is not *too long*, but never validates that it is *long enough* to cover the declared byte length. When the oracle returns fewer words than required to fill `num_bytes`, the subsequent `truncated_to_byte_length(num_bytes)` call panics unconditionally, crashing the proving or forward-execution system.

### Finding Description
`get_bytes_from_query` performs a two-step oracle query: first it fetches a declared byte length `num_bytes`, then it fetches the body as a word iterator of length `body_it_len`. [1](#0-0) 

The only guard present is:

```rust
if body_it_len > num_words {
    return Err(internal_error!("iterator len is inconsistent"));
}
```

where `num_words = num_bytes.div_ceil(USIZE_SIZE).next_multiple_of(2)`.

This check is **asymmetric**: it rejects iterators that are too long, but silently accepts iterators that are too short. After the check, `from_usize_iterator_in` constructs a `UsizeAlignedByteBox` whose `byte_capacity = body_it_len * USIZE_SIZE`. [2](#0-1) 

Immediately after, `truncated_to_byte_length(num_bytes)` asserts:

```rust
assert!(
    byte_len <= self.byte_capacity,
    "trying to truncate to {} bytes, but only {} bytes are initialized",
    ...
);
``` [3](#0-2) 

If `body_it_len * USIZE_SIZE < num_bytes`, this assertion fires and the process panics.

**Concrete example (64-bit host):** `num_bytes = 9`, `USIZE_SIZE = 8`, `num_words = ceil(9/8).next_multiple_of(2) = 2`. An oracle returning `body_it_len = 1` passes the guard (`1 ≤ 2`), but `1 × 8 = 8 < 9`, so `truncated_to_byte_length(9)` panics.

The function is called during block-header ingestion: [4](#0-3) 

### Impact Explanation
In the **proving system** (RISC-V target), a panic aborts the RISC-V program, making it impossible to generate a validity proof for the block. The block cannot be finalized on L1, freezing all pending L2→L1 withdrawals until the issue is resolved — a liveness failure with direct user impact.

In the **forward system** (sequencer), the same panic crashes the sequencer process mid-block, halting transaction processing.

### Likelihood Explanation
The entry path is oracle/prover-supplied data, which the prompt explicitly lists as in-scope (`prover/forward execution input`). Any oracle implementation that returns a body iterator shorter than the declared byte length — whether due to a bug, a protocol mismatch between 32-bit RISC-V and 64-bit host word sizes, or a malicious prover — triggers the panic. The `next_multiple_of(2)` slack added for arch-mismatch tolerance widens the gap between the accepted `body_it_len` range and the minimum required to satisfy `truncated_to_byte_length`.

### Recommendation
Add a lower-bound check immediately after the upper-bound check:

```rust
if body_it_len > num_words {
    return Err(internal_error!("iterator len is inconsistent"));
}
// NEW: ensure the iterator covers at least num_bytes
let min_words = num_bytes.div_ceil(USIZE_SIZE);
if body_it_len < min_words {
    return Err(internal_error!(
        "oracle body iterator too short: {} words < {} required",
        body_it_len, min_words
    ));
}
```

This mirrors the fix for the original report (`poolIds = new uint256[](length)`) — ensuring the container is properly sized before use.

### Proof of Concept
1. Construct an oracle that responds to `length_query_id` with `size = 9` (9 bytes).
2. Respond to `body_query_id` with an iterator of length `1` (one 8-byte word = 8 bytes).
3. Call any code path that invokes `get_bytes_from_query` (e.g., block-header ingestion via `HeaderAndHistory::new`).
4. `body_it_len = 1 ≤ num_words = 2` passes the guard.
5. `from_usize_iterator_in` sets `byte_capacity = 8`.
6. `truncated_to_byte_length(9)` asserts `9 ≤ 8` → **panic**. [5](#0-4)

### Citations

**File:** zk_ee/src/oracle/mod.rs (L151-179)
```rust
    fn get_bytes_from_query<A: Allocator, I: UsizeSerializable + UsizeDeserializable>(
        &mut self,
        length_query_id: u32, // must return number of bytes
        body_query_id: u32,   // must return
        input: &I,
        allocator: A,
    ) -> Result<Option<UsizeAlignedByteBox<A>>, InternalError> {
        use crate::internal_error;
        use crate::utils::USIZE_SIZE;

        let size = self.query_serializable::<I, u32>(length_query_id, input)?;
        if size == 0 {
            return Ok(None);
        }
        let num_bytes = size as usize;
        let num_words = num_bytes.div_ceil(USIZE_SIZE);
        // NOTE: we leave some slack for 64/32 bit arch mismatches
        let num_words = num_words.next_multiple_of(2);
        let body_query_it = self.raw_query(body_query_id, input)?;
        let body_it_len = body_query_it.len();
        if body_it_len > num_words {
            return Err(internal_error!("iterator len is inconsistent"));
        }
        // create buffer
        let mut buffer = UsizeAlignedByteBox::from_usize_iterator_in(body_query_it, allocator);
        buffer.truncated_to_byte_length(num_bytes);

        Ok(Some(buffer))
    }
```

**File:** zk_ee/src/utils/aligned_vector.rs (L117-134)
```rust
    pub fn from_usize_iterator_in(src: impl ExactSizeIterator<Item = usize>, allocator: A) -> Self {
        let word_capacity = src.len();
        let mut inner: alloc::boxed::Box<[MaybeUninit<usize>], A> =
            alloc::boxed::Box::new_uninit_slice_in(word_capacity, allocator);
        // iterators will have same length by the contract
        unsafe {
            core::hint::assert_unchecked(src.len() == inner.len());
        }
        for (src, dst) in src.zip(inner.iter_mut()) {
            dst.write(src);
        }
        let byte_capacity = word_capacity * USIZE_SIZE;

        Self {
            inner,
            byte_capacity,
            initialized_bytes: byte_capacity,
        }
```

**File:** zk_ee/src/utils/aligned_vector.rs (L160-169)
```rust
    #[track_caller]
    pub fn truncated_to_byte_length(&mut self, byte_len: usize) {
        assert!(
            byte_len <= self.byte_capacity,
            "trying to truncate to {} bytes, while capacity is just {} bytes",
            byte_len,
            self.byte_capacity
        );
        self.byte_capacity = byte_len;
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/block_header.rs (L200-209)
```rust
        let target_header_buffer = oracle.get_bytes_from_query(
            ETHEREUM_TARGET_HEADER_BUFFER_LEN_QUERY_ID,
            ETHEREUM_TARGET_HEADER_BUFFER_DATA_QUERY_ID,
            &(),
            allocator,
        )?;
        let target_header_buffer = target_header_buffer.expect("target header is not empty slice");
        let target_header =
            PectraForkHeaderReflection::decode_list_full(target_header_buffer.as_slice())
                .map_err(|_| internal_error!("must parse target header from bytes"))?;
```
