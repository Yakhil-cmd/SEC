Looking at the code and the `simple_asn1` BitString semantics, I need to determine whether the `len` check at line 52 is sufficient to guarantee `key.as_slice().len() == PUBLIC_KEY_SIZE`.

**`simple_asn1` BitString parsing semantics:**

For `ASN1Block::BitString(offset, bit_count, data_vec)`, `simple_asn1` parses DER as:
- `bit_count = (content_bytes.len() - 1) * 8 - unused_bits`
- `data_vec = content_bytes[1..]` (unused-bits byte stripped)

Critically, `simple_asn1` does **not** validate that `unused_bits ∈ [0, 7]`. It accepts any byte value.

**The missed exploit path in the question's own analysis:**

The question analyzed two cases but missed a third:

| unused_bits | data bytes | bit_count | data_vec.len() | len check | outcome |
|---|---|---|---|---|---|
| 0x00 | 97 | 776 | 97 | 776≠768 → Err | safe |
| 0x08 | 96 | 760 | 96 | 760≠768 → Err | safe |
| **0x08** | **97** | **768** | **97** | **768==768 → pass** | **PANIC** |

For the third case: `97 * 8 - 8 = 768`, so `bit_count == PUBLIC_KEY_SIZE * 8` passes the guard at line 52, but `data_vec.len() == 97 ≠ 96 == PUBLIC_KEY_SIZE`, causing `copy_from_slice` to panic.

**The check at line 52 is not sufficient.** The invariant `bit_count == N*8` does NOT imply `data_vec.len() == N` when `unused_bits ≠ 0`. The `der_utils` library already recognized this and added an explicit cross-check: [1](#0-0) 

But `public_key_from_der` lacks this guard: [2](#0-1) 

---

### Title
Panic (DoS) via crafted BitString in `public_key_from_der` — (`rs/crypto/utils/threshold_sig_der/src/lib.rs`)

### Summary
An attacker can craft a DER blob containing a BitString with `unused_bits=8` and 97 data bytes. `simple_asn1` parses this as `BitString(_, 768, vec_of_97_bytes)`. The `bit_count` check at line 52 passes (768 == 768), but `copy_from_slice` at line 58 panics because the source slice is 97 bytes and the destination is 96 bytes.

### Finding Description
`public_key_from_der` checks `*len != PUBLIC_KEY_SIZE * 8` to validate the key length, but this check is not equivalent to `key.as_slice().len() == PUBLIC_KEY_SIZE`. In `simple_asn1`, `bit_count = (data_bytes) * 8 - unused_bits`. When `unused_bits = 8` and `data_bytes = 97`, `bit_count = 768` but `data_vec.len() = 97`. The guard passes, and `copy_from_slice` panics.

`simple_asn1` does not validate that `unused_bits ∈ [0, 7]`, making this input accepted by the parser. The `der_utils` sibling library already has the correct defense (`bits_count != key_bytes.len() * 8`), but `threshold_sig_der` does not.

### Impact Explanation
A panic in Rust causes thread unwinding or process abort. If `public_key_from_der` is called during consensus, certification verification, or state sync (outside of sandboxed canister execution), this causes a replica crash — a targeted DoS against individual nodes.

### Likelihood Explanation
The crafted DER is trivial to construct (one byte change to a valid BLS public key DER blob). Reachability depends on whether the caller passes attacker-controlled bytes; BLS public keys appear in IC certificates and subnet configurations that can be submitted or observed by unprivileged users.

### Recommendation
Add an explicit length check on `key` before `copy_from_slice`, mirroring the pattern already used in `der_utils`:

```rust
if key.len() != PUBLIC_KEY_SIZE {
    return Err(format!("unexpected key data length: {} bytes", key.len()));
}
key_bytes.copy_from_slice(key.as_slice());
```

### Proof of Concept
Craft DER bytes: `0x03 0x62 0x08 [97 bytes of 0x00]` as the BitString content inside the expected SEQUENCE structure. `simple_asn1::from_der` returns `BitString(_, 768, vec![0u8; 97])`. The `len` check passes. `copy_from_slice` panics.

### Citations

**File:** rs/crypto/internal/crypto_lib/basic_sig/der_utils/src/lib.rs (L176-179)
```rust
        if let ASN1Block::BitString(_offset, bits_count, key_bytes) = key_part {
            if *bits_count != key_bytes.len() * 8 {
                return Err(Self::parsing_error("Inconsistent key length"));
            }
```

**File:** rs/crypto/utils/threshold_sig_der/src/lib.rs (L52-58)
```rust
                if *len != PUBLIC_KEY_SIZE * 8 {
                    return Err(format!("unexpected key length: {len} bits"));
                }

                if ids[0] == bls_algorithm_id() && ids[1] == bls_curve_id() {
                    let mut key_bytes = [0_u8; PUBLIC_KEY_SIZE];
                    key_bytes.copy_from_slice(key.as_slice());
```
