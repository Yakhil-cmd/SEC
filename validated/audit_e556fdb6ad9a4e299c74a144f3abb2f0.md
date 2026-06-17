### Title
Inconsistent `MAX_BLOBS_PER_BLOCK` Constants Allow Blob Count Exceeding Metadata Capacity in Ethereum Execution Path - (File: `basic_bootloader/src/bootloader/block_flow/ethereum/block_header.rs`, `zk_ee/src/system/constants.rs`)

---

### Summary

The codebase defines `MAX_BLOBS_PER_BLOCK` in at least three separate locations with two different values (6 and 9). The Ethereum execution path uses the value 9 for both the block-level blob gas limit and per-transaction validation, while the transaction metadata storage structure is parameterized with the value 6 from `zk_ee`. This creates an inconsistency directly analogous to the external report: the check at one boundary (validation) uses a higher maximum than the capacity at another boundary (metadata storage), allowing a transaction with 7–9 blobs to pass validation while the metadata can only hold 6.

---

### Finding Description

Three separate definitions of `MAX_BLOBS_PER_BLOCK` exist in the codebase:

**Definition 1 — `zk_ee` (value = 6):** [1](#0-0) 

**Definition 2 — Ethereum block flow module (value = 9):** [2](#0-1) 

**Definition 3 — Ethereum block header (value = 9, local constant):** [3](#0-2) 

**Where each value is consumed:**

The `EthereumBlockMetadata` type in `metadata_op.rs` imports `MAX_BLOBS_PER_BLOCK` from `zk_ee` (= 6) and uses it to parameterize `EthereumTransactionMetadata`: [4](#0-3) 

This means `EthereumTransactionMetadata.blobs` is an `ArrayVec<Bytes32, 6>` — it can hold at most 6 blob hashes. [5](#0-4) 

However, `HeaderAndHistory::max_blobs()` returns the local constant 9, so `blobs_gas_limit()` = `9 * GAS_PER_BLOB`: [6](#0-5) 

The Ethereum transaction validation calls `parse_blobs_list::<MAX_BLOBS_PER_BLOCK>` via `use super::*;`, which resolves to the ethereum module's value of 9: [7](#0-6) 

The `parse_blobs_list` function enforces the count check against `MAX_BLOBS_IN_TX` (= 9 in this path): [8](#0-7) 

By contrast, the ZK validation path correctly imports `MAX_BLOBS_PER_BLOCK` from `zk_ee` (= 6) and uses it consistently: [9](#0-8) [10](#0-9) 

The ZK metadata also uses 6 consistently: [11](#0-10) 

---

### Impact Explanation

In the Ethereum execution path, a transaction carrying 7–9 blob hashes passes `parse_blobs_list::<9>` validation (count ≤ 9) and also passes the block-level blob gas check (`7 * GAS_PER_BLOB < 9 * GAS_PER_BLOB`). When the validated blob list is subsequently stored into `EthereumTransactionMetadata<6>.blobs` (capacity 6), the 7th push into the `ArrayVec` causes a panic (ArrayVec overflow on `push` when `len == capacity`). This is a reachable DoS path in the Ethereum block re-executor.

Additionally, the blob gas limit exposed by `blobs_gas_limit()` (= 9 × GAS_PER_BLOB) is inconsistent with the actual metadata capacity (6 blobs), meaning the block-level blob gas accounting ceiling is set 50% higher than the structure can actually accommodate. This is a resource accounting bug: the `check_for_block_limits` blob gas check will never fire for a block containing exactly 6 blobs, since `6 * GAS_PER_BLOB < 9 * GAS_PER_BLOB`. [12](#0-11) 

---

### Likelihood Explanation

Post-Pectra Ethereum allows up to 9 blobs per block. Any real Ethereum block with 7–9 blobs fed into the Ethereum block re-executor (via oracle input) triggers this path. An oracle-data-influencing caller or a prover supplying a crafted block header with `blob_gas_used` corresponding to 7+ blobs can reach this code path without any privileged access.

---

### Recommendation

Consolidate all `MAX_BLOBS_PER_BLOCK` definitions into a single authoritative constant. The Ethereum block header's local constant and the ethereum module's re-export should both reference `zk_ee::system::constants::MAX_BLOBS_PER_BLOCK`, or a single shared constant should be introduced and used everywhere. Specifically:

- Remove the local `const MAX_BLOBS_PER_BLOCK: usize = 9` in `block_header.rs` and `mod.rs`.
- Ensure `HeaderAndHistory::max_blobs()` returns the same value used to parameterize `EthereumTransactionMetadata`.
- Add an assertion or compile-time check that `EthereumTransactionMetadata::MAX_BLOBS == HeaderAndHistory::max_blobs()`.

---

### Proof of Concept

1. Construct an Ethereum block (oracle input) containing a single EIP-4844 transaction with 7 blob versioned hashes, all with valid `0x01` version bytes.
2. Feed this block into the Ethereum block re-executor via the oracle.
3. `parse_blobs_list::<9>` accepts the list (7 ≤ 9, all hashes valid).
4. The blob gas check passes: `7 * GAS_PER_BLOB < 9 * GAS_PER_BLOB`.
5. The 7 blob hashes are pushed into `EthereumTransactionMetadata<6>.blobs` (ArrayVec capacity 6).
6. The 7th `push` panics: `ArrayVec::push` panics when `len == capacity`.
7. The executor crashes — DoS achieved via oracle-supplied block data with no privileged access required.

### Citations

**File:** zk_ee/src/system/constants.rs (L31-31)
```rust
pub const MAX_BLOBS_PER_BLOCK: usize = 6;
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/mod.rs (L9-9)
```rust
pub const MAX_BLOBS_PER_BLOCK: usize = 9;
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/block_header.rs (L30-30)
```rust
const MAX_BLOBS_PER_BLOCK: usize = 9;
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/block_header.rs (L182-187)
```rust
    fn max_blobs(&self) -> usize {
        MAX_BLOBS_PER_BLOCK
    }
    fn blobs_gas_limit(&self) -> u64 {
        self.max_blobs() as u64 * GAS_PER_BLOB
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/metadata_op.rs (L7-22)
```rust
        MAX_BLOBS_PER_BLOCK, MAX_BLOCK_GAS_LIMIT,
    },
    types_config::EthereumIOTypesConfig,
};

use crate::bootloader::transaction_flow::ethereum::tx_level_metadata::EthereumTransactionMetadata;

use super::{
    block_header::HeaderAndHistory, BasicBootloaderExecutionConfig, EthereumMetadataOp,
    MetadataInitOp,
};

pub type EthereumBlockMetadata = SystemMetadata<
    EthereumIOTypesConfig,
    HeaderAndHistory,
    EthereumTransactionMetadata<{ MAX_BLOBS_PER_BLOCK }>,
```

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/tx_level_metadata.rs (L6-9)
```rust
pub struct EthereumTransactionMetadata<const MAX_BLOBS: usize> {
    pub tx_origin: B160,
    pub tx_gas_price: U256,
    pub blobs: arrayvec::ArrayVec<Bytes32, MAX_BLOBS>,
```

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/validation_impl.rs (L334-339)
```rust
        match parse_blobs_list::<MAX_BLOBS_PER_BLOCK>(blobs_list) {
            Ok(blobs) => blobs,
            Err(e) => {
                return Err(e);
            }
        }
```

**File:** basic_bootloader/src/bootloader/transaction/blobs.rs (L11-13)
```rust
    if blobs_list.count > MAX_BLOBS_IN_TX {
        return Err(TxError::Validation(InvalidTransaction::BlobListTooLong));
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L35-35)
```rust
use zk_ee::system::{GAS_PER_BLOB, MAX_BLOBS_PER_BLOCK};
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L411-411)
```rust
        match parse_blobs_list::<MAX_BLOBS_PER_BLOCK>(blobs_list) {
```

**File:** zk_ee/src/system/metadata/zk_metadata.rs (L28-28)
```rust
    pub blobs: arrayvec::ArrayVec<Bytes32, { MAX_BLOBS_PER_BLOCK }>,
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/mod.rs (L62-67)
```rust
    } else if blob_gas_used > system.get_blob_gas_limit() {
        system_log!(
            system,
            "Block blob gas limit reached, invalidating transaction\n"
        );
        Err(InvalidTransaction::BlockBlobGasLimitReached)
```
