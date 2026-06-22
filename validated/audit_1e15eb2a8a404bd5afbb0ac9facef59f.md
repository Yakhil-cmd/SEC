### Title
Unbounded `candid::Int` in ICRC-1 Block Causes Panic in `Value::hash()` - (File: `packages/icrc-ledger-types/src/icrc/generic_value.rs`)

---

### Summary

`Value::hash()` unconditionally panics via `.expect()` when called on a `Value::Int` whose inner `candid::Int` (arbitrary-precision `BigInt`) does not fit in 128 bits. A malicious ICRC-1 ledger canister can return blocks containing oversized `Int` values in extra/unknown fields, causing any index-ng canister pointed at it to trap when it calls `original_block.hash()` inside `append_block`.

---

### Finding Description

`Value::hash()` in `packages/icrc-ledger-types/src/icrc/generic_value.rs` handles the `Value::Int` arm by calling `.to_i128().expect(...)`: [1](#0-0) 

`candid::Int` is backed by an arbitrary-precision `BigInt`. If the integer exceeds 128 bits, `to_i128()` returns `None` and the `.expect()` call panics unconditionally. The comment at line 133 even acknowledges this is a known limitation.

The `icrc1_block_from_value` function in `rs/ledger_suite/icrc1/src/blocks.rs` can produce such a `GenericValue::Int` from a CBOR `NEG_BIGNUM` tag (tag 3) whose byte payload exceeds 16 bytes — it does so without any size guard: [2](#0-1) 

In the index-ng canister's `append_block`, the block received from the ledger via `icrc3_get_blocks` is stored as `original_block`. After `generic_block_to_encoded_block` and `Block::<Tokens>::decode` succeed (which they do when the oversized integer is placed in an unknown/extra field that the typed decoder ignores), `original_block.hash()` is called: [3](#0-2) 

The `original_block` still carries the attacker-supplied `Value::Int` with the oversized integer, so `Value::hash()` panics, trapping the canister.

The `encoded_block_to_generic_block` function also uses unconditional `.expect()` wrappers, meaning any caller that processes attacker-supplied CBOR containing a large `NEG_BIGNUM` tag will also panic: [4](#0-3) 

---

### Impact Explanation

The index-ng canister traps (Wasm trap = canister crash) every time it attempts to index a block from the malicious ledger. Because `append_block` is called from the periodic indexing timer, the canister enters a permanent crash loop and becomes permanently unavailable. All balance queries, transaction history, and ICRC-3 block serving from the index-ng canister are denied. Any Rosetta API instance (`RosettaBlock::from_encoded_block`) pointed at the same ledger is equally affected.

---

### Likelihood Explanation

Any IC principal can deploy a canister that implements the `icrc3_get_blocks` interface and returns blocks with oversized `Int` values in extra map fields. No privileged access, governance vote, or threshold key is required. The attacker only needs cycles to deploy the malicious ledger canister. The index-ng canister is designed to work with arbitrary ICRC-1 ledgers, so the attack surface is inherent to the architecture.

---

### Recommendation

- **Short term:** Guard `Value::hash()` against integers that do not fit in 128 bits by returning an error or saturating instead of panicking. Replace the `.expect()` with a checked conversion that propagates an error up the call stack.
- **Long term:** Change `Value::hash()` to return `Result<Hash, HashError>` so callers can handle oversized integers gracefully. Add a validation step in `append_block` that rejects blocks containing `Value::Int` values before calling `hash()`, analogous to the existing unknown-field detection logic.

---

### Proof of Concept

A malicious canister implementing `icrc3_get_blocks` returns the following Candid-encoded block (pseudocode):

```
ICRC3Value::Map({
  "ts":  ICRC3Value::Nat(1_000_000_000),
  "tx":  ICRC3Value::Map({
           "op":  ICRC3Value::Text("mint"),
           "to":  ...,
           "amt": ICRC3Value::Nat(1_000),
         }),
  // Extra field with a 17-byte (136-bit) negative integer — exceeds i128
  "x":   ICRC3Value::Int(candid::Int::from(-1_000_000_000_000_000_000_000_000_000_000_000_000_000u128 - 1)),
})
```

1. Index-ng canister fetches this block; `original_block` holds the map above.
2. `generic_block_to_encoded_block(block)` succeeds — the `Int` is serialized as a CBOR `NEG_BIGNUM` tag with 17 bytes.
3. `Block::<Tokens>::decode(block)` succeeds — the unknown key `"x"` is ignored by the typed decoder.
4. `original_block.hash()` is called; `Value::Int` arm reaches `.to_i128().expect(...)` → `None` → **panic / canister trap**.
5. The index-ng canister is permanently unavailable. [1](#0-0) [2](#0-1) [5](#0-4)

### Citations

**File:** packages/icrc-ledger-types/src/icrc/generic_value.rs (L127-131)
```rust
            Value::Int(int) => {
                let v = int
                    .0
                    .to_i128()
                    .expect("BUG: blocks cannot contain integers that do not fit into the 128-bit representation");
```

**File:** rs/ledger_suite/icrc1/src/blocks.rs (L75-79)
```rust
pub fn encoded_block_to_generic_block(encoded_block: &EncodedBlock) -> GenericBlock {
    let value: CiboriumValue =
        ciborium::de::from_reader(encoded_block.as_slice()).expect("failed to decode block");
    icrc1_block_from_value(value, 0).expect("failed to decode encoded block")
}
```

**File:** rs/ledger_suite/icrc1/src/blocks.rs (L150-158)
```rust
        CiboriumValue::Tag(NEG_BIGNUM, value) => {
            use num_bigint::{BigInt, BigUint, Sign};
            let value_bytes = value.into_bytes().map_err(|_| {
                ValueDecodingError::UnsupportedValueType("non-bytes negative bignums")
            })?;
            Ok(GenericValue::Int(Int(BigInt::from_biguint(
                Sign::Minus,
                BigUint::from_bytes_be(&value_bytes),
            ) - 1)))
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L875-916)
```rust
fn append_block(block_index: BlockIndex64, block: GenericBlock) -> Result<(), SyncError> {
    measure_span(&PROFILING_DATA, "append_blocks", move || {
        let original_block = block.clone();

        let block = match generic_block_to_encoded_block(block) {
            Ok(block) => block,
            Err(e) => {
                let message = format!(
                    "Unable to decode generic block at index {block_index}: {}. Error: {e}",
                    original_block
                );
                return Err(SyncError {
                    message,
                    retriable: false,
                });
            }
        };

        let decoded_block = match Block::<Tokens>::decode(block.clone()) {
            Ok(block) => block,
            Err(e) => {
                let message = format!(
                    "Unable to decode encoded block at index {block_index}: {}. Error: {e}",
                    original_block
                );
                return Err(SyncError {
                    message,
                    retriable: false,
                });
            }
        };
        let decoded_value = encoded_block_to_generic_block(&decoded_block.clone().encode());
        if original_block.hash() != decoded_value.hash() {
            let message = format!(
                "Block at index {block_index} has unknown fields. Original block: {}, decoded block: {}.",
                original_block, decoded_value
            );
            return Err(SyncError {
                message,
                retriable: false,
            });
        }
```
