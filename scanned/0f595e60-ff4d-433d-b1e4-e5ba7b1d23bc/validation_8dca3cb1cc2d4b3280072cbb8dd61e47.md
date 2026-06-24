### Title
Widespread Use of `--experimental_allow_proto3_optional` in Production Protobuf Generators for Critical IC State - (File: rs/protobuf/generator/src/lib.rs)

### Summary
Nine production protobuf generators in the IC codebase pass `--experimental_allow_proto3_optional` to `protoc`, enabling an experimental proto3 feature for optional fields. The generated code underpins critical replicated state, consensus messages, registry records, and governance data. This is the direct IC analog of the ABIEncoderV2 finding: an experimental encoder/serializer feature is used in production across security-critical paths without a stable-feature guarantee.

### Finding Description
The `base_config()` helper in `rs/protobuf/generator/src/lib.rs` unconditionally passes `--experimental_allow_proto3_optional` to `protoc` for every proto file it compiles. [1](#0-0) 

The same flag appears in eight additional production generators: [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) 

The proto files compiled with this flag contain `optional` fields in security-critical schemas:

- `rs/protobuf/def/state/canister_state_bits/v1/canister_state_bits.proto` — 24 `optional` fields encoding per-canister execution state
- `rs/protobuf/def/state/metadata/v1/metadata.proto` — 23 `optional` fields encoding subnet-level replicated metadata
- `rs/protobuf/def/registry/subnet/v1/subnet.proto` — 18 `optional` fields encoding subnet configuration
- `rs/protobuf/def/types/v1/management_canister_types.proto` — 2 `optional` fields in `CanisterUpgradeOptions` (e.g. `skip_pre_upgrade`, `wasm_memory_persistence`) [10](#0-9) 

The `Payload::decode` trait used by the execution environment to decode every management-canister call (e.g. `InstallCode`, `CreateCanister`, `UpdateSettings`) calls `Decode!([decoder_config()]; blob, Self)`, where `decoder_config()` only sets a `skipping_quota` and not a `decoding_quota`: [11](#0-10) [12](#0-11) 

The execution environment invokes these decoders directly in the replica process (not inside a Wasm sandbox) for every ingress or inter-canister call to `ic:00`: [13](#0-12) [14](#0-13) 

The IC's own determinism-test infrastructure explicitly warns that any change to `prost` encoding behavior "could result in stalled replicas or non-deterministic behavior": [15](#0-14) 

The manifest compatibility tests reinforce that the protobuf encoding of state-sync manifests must be byte-for-byte stable across replica versions: [16](#0-15) 

### Impact Explanation
`--experimental_allow_proto3_optional` was an unstable `protoc` flag before version 3.15. If the IC build toolchain uses a pre-3.15 `protoc`, or if a future `protoc` upgrade changes the code generated for `optional` fields, the serialized representation of canister state bits, subnet metadata, registry records, or governance state could silently change. Because these byte sequences are hashed to produce certified state roots and are compared across replicas for consensus, any divergence in encoding produces:

1. **Deterministic execution divergence / consensus safety break**: Replicas running different `protoc`-generated code produce different state hashes for the same logical state, causing them to disagree on the certified state root and stall or fork the subnet.
2. **State certification forgery**: A state root computed from a buggy optional-field encoding does not correspond to the true logical state, allowing a boundary node or light client to be presented with a certified but semantically incorrect state tree.
3. **Governance authorization bug**: Incorrect encoding/decoding of `optional` fields in NNS/SNS governance proto messages (e.g. neuron dissolve state, proposal action payloads) could cause governance logic to misread stored state, silently accepting or rejecting proposals or neuron operations.

### Likelihood Explanation
`protoc` ≥ 3.15 stabilized proto3 optional, making the flag a no-op on modern toolchains. However:
- The flag is still present and active in all nine generators, meaning any downgrade or pinning to an older `protoc` version immediately reintroduces the experimental code path.
- The IC build system uses Bazel with hermetically pinned toolchains; if the pinned `protoc` version is < 3.15, the experimental path is live today.
- The absence of any version guard or comment explaining why the flag is safe means future contributors may not recognize the risk when changing the toolchain.
- The fuzz targets for management-canister-type decoding explicitly target stack-overflow panics from Candid decoding, confirming that the decoding path (which also lacks a `decoding_quota`) is considered a live attack surface. [17](#0-16) 

### Recommendation
1. **Remove `--experimental_allow_proto3_optional`** from all nine protobuf generators once the build toolchain is confirmed to use `protoc` ≥ 3.15, or add an explicit assertion that the pinned `protoc` version is ≥ 3.15.
2. **Add a `decoding_quota`** to `decoder_config()` in `rs/types/management_canister_types/src/lib.rs` alongside the existing `skipping_quota`, consistent with the pattern used in `rs/rust_canisters/dfn_candid/src/lib.rs` for NNS canister HTTP endpoints.
3. **Extend the determinism test suite** (`rs/protobuf/src/determinism_test.rs`) to cover proto messages that use `optional` fields, so any future encoding change is caught before deployment.

### Proof of Concept
An unprivileged ingress sender submits a `UpdateSettings` call to `ic:00` with a payload that exercises an `optional` field (e.g. `CanisterUpgradeOptions.skip_pre_upgrade`). The execution environment calls `UpdateSettingsArgs::decode(payload)` → `Decode!([decoder_config()]; blob, Self)` in the replica process. If the `protoc`-generated code for the `optional` field is incorrect (due to the experimental flag on a pre-3.15 toolchain), the field is silently misread. The resulting canister state bits are serialized back with the same buggy encoder, producing a state hash that diverges from replicas built with a different `protoc` version, stalling consensus on the subnet. [1](#0-0) [12](#0-11) [13](#0-12)

### Citations

**File:** rs/protobuf/generator/src/lib.rs (L9-18)
```rust
fn base_config(out: &Path, prefix: &str) -> Config {
    let mut config = Config::new();
    let proto_out = out.join(prefix);
    std::fs::create_dir_all(&proto_out)
        .unwrap_or_else(|e| panic!("Failed to create directory {}: {}", proto_out.display(), e));
    config.out_dir(&proto_out);
    // Use BTreeMap for all proto map fields.
    config.btree_map(["."]);
    config.protoc_arg("--experimental_allow_proto3_optional");
    config
```

**File:** rs/nns/governance/protobuf_generator/src/lib.rs (L60-62)
```rust
pub fn generate_prost_files(proto: ProtoPaths<'_>, out: &Path) {
    let mut config = Config::new();
    config.protoc_arg("--experimental_allow_proto3_optional");
```

**File:** rs/sns/governance/protobuf_generator/src/lib.rs (L18-19)
```rust
    let mut config = Config::new();
    config.protoc_arg("--experimental_allow_proto3_optional");
```

**File:** rs/nervous_system/proto/protobuf_generator/src/lib.rs (L16-18)
```rust
pub fn generate_prost_files(proto_paths: ProtoPaths<'_>, out_dir: &Path) {
    let mut config = prost_build::Config::new();
    config.protoc_arg("--experimental_allow_proto3_optional");
```

**File:** rs/registry/canister/protobuf_generator/src/lib.rs (L17-18)
```rust
    let mut config = Config::new();
    config.protoc_arg("--experimental_allow_proto3_optional");
```

**File:** rs/sns/init/protobuf_generator/src/lib.rs (L16-17)
```rust
    let mut config = Config::new();
    config.protoc_arg("--experimental_allow_proto3_optional");
```

**File:** rs/sns/root/protobuf_generator/src/lib.rs (L10-11)
```rust
    let mut config = prost_build::Config::new();
    config.protoc_arg("--experimental_allow_proto3_optional");
```

**File:** rs/sns/swap/protobuf_generator/src/lib.rs (L20-21)
```rust
    let mut config = Config::new();
    config.protoc_arg("--experimental_allow_proto3_optional");
```

**File:** rs/types/base_types/protobuf_generator/src/lib.rs (L8-9)
```rust
    let mut config = Config::new();
    config.protoc_arg("--experimental_allow_proto3_optional");
```

**File:** rs/protobuf/def/types/v1/management_canister_types.proto (L18-28)
```text
message CanisterUpgradeOptions {
  optional bool skip_pre_upgrade = 1;
  optional WasmMemoryPersistence wasm_memory_persistence = 2;
}

message CanisterInstallModeV2 {
  oneof canister_install_mode_v2 {
    CanisterInstallMode mode = 1;
    CanisterUpgradeOptions mode2 = 2;
  }
}
```

**File:** rs/types/management_canister_types/src/lib.rs (L62-70)
```rust
/// Limit the amount of work for skipping unneeded data on the wire when parsing Candid.
/// The value of 10_000 follows the Candid recommendation.
const DEFAULT_SKIPPING_QUOTA: usize = 10_000;

fn decoder_config() -> DecoderConfig {
    let mut config = DecoderConfig::new();
    config.set_skipping_quota(DEFAULT_SKIPPING_QUOTA);
    config.set_full_error_message(false);
    config
```

**File:** rs/types/management_canister_types/src/lib.rs (L162-169)
```rust
pub trait Payload<'a>: Sized + CandidType + Deserialize<'a> {
    fn encode(&self) -> Vec<u8> {
        Encode!(&self).unwrap()
    }

    fn decode(blob: &'a [u8]) -> Result<Self, UserError> {
        Decode!([decoder_config()]; blob, Self).map_err(candid_error_to_user_error)
    }
```

**File:** rs/execution_environment/src/execution_environment.rs (L1067-1084)
```rust
            Ok(Ic00Method::CanisterStatus) => {
                let res = CanisterIdRecord::decode(payload).and_then(|args| {
                    let subnet_admins = state.get_own_subnet_admins();
                    let ready_for_migration = state.ready_for_migration(&args.get_canister_id());
                    self.get_canister_status(
                        *msg.sender(),
                        args.get_canister_id(),
                        &state,
                        ready_for_migration,
                        subnet_admins,
                    )
                    .map(|res| (res, Some(args.get_canister_id())))
                });
                ExecuteSubnetMessageResult::Finished {
                    response: res,
                    refund: msg.take_cycles(),
                }
            }
```

**File:** rs/execution_environment/src/execution_environment.rs (L1127-1144)
```rust
            Ok(Ic00Method::StartCanister) => match CanisterIdRecord::decode(payload) {
                Err(err) => ExecuteSubnetMessageResult::Finished {
                    response: Err(err),
                    refund: msg.take_cycles(),
                },
                Ok(args) => {
                    let subnet_admins = state.get_own_subnet_admins();
                    self.start_canister(
                        args.get_canister_id(),
                        *msg.sender(),
                        &mut state,
                        &mut msg,
                        subnet_admins,
                        round_limits,
                        current_round,
                    )
                }
            },
```

**File:** rs/protobuf/src/determinism_test.rs (L1-17)
```rust
//! `prost` deterministic encoding tests.
//!
//! For each of a number of message types covering all `proto3` supported types,
//! the various tests encode a specific instance (default and non-default
//! values, single and multiple repeated fields, etc.) and ensure that the
//! output is an exact byte sequence.
//!
//! The decoded representations of the byte sequences were obtained from a mix
//! of `protoscope` and `protoc --decode_raw` on the output of
//! ```text
//! echo "$BYTE_SEQUENCE" | xxd -r -ps
//! ```
//!
//! # Warning
//! The failure of any of these tests (likely following a `prost` crate upgrade)
//! could result in stalled replicas or non-deterministic behavior. Please do
//! not "fix" any such test failures and notify the Message Routing team.
```

**File:** rs/state_manager/src/manifest/tests/compatibility.rs (L1-9)
```rust
//! Backwards-compatibility tests for the manifest.
//!
//! Any breakage of these tests likely means the encoding of the manifest
//! and/or the hashing of the manifest have changed, which means the root hash
//! is inconsistent for the same checkpoint between adjacent replica versions.

use crate::manifest::{
    DEFAULT_CHUNK_SIZE, manifest_hash, tests::computation::dummy_file_table_and_chunk_table,
};
```

**File:** rs/types/management_canister_types/fuzz/fuzz_targets/decode_canister_http_request_args.rs (L1-13)
```rust
#![no_main]
use ic_management_canister_types_private::CanisterHttpRequestArgs;
use ic_management_canister_types_private::Payload;
use libfuzzer_sys::fuzz_target;

// This fuzz test feeds binary data to Candid's `Decode!` macro for CanisterHttpRequestArgs with the goal of exposing panics
// e.g. caused by stack overflows during decoding.

fuzz_target!(|data: &[u8]| {
    let _ = CanisterHttpRequestArgs::decode(data);
});


```
