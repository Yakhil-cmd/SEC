### Title
Plaintext Secret Key Bytes Persist in Non-Zeroizing Prost-Generated Intermediate Buffers During Secret Key Store Serialization — (File: `rs/crypto/internal/crypto_service_provider/src/secret_key_store/proto_store.rs`)

---

### Summary

During every write to the `ProtoSecretKeyStore`, all node secret keys (Ed25519, MultiBLS12-381, ThresBLS12-381, TLS, NI-DKG forward-secure, MEGa iDKG, iDKG commitment openings) are serialized into plaintext CBOR bytes and placed into prost-generated `pb::SecretKeyV1` and `pb::SecretKeyStore` structs that do not implement `Zeroize` or `ZeroizeOnDrop`. A second plaintext copy is created by `encode_to_vec()` inside `write_protobuf_using_tmp_file`. Both copies are dropped without zeroing, leaving raw secret key bytes on the heap until the allocator reuses them.

---

### Finding Description

The `CspSecretKey` enum and all its inner types (`SecretArray`, `SecretVec`, `SecretBytes`, `TlsEd25519SecretKeyDerBytes`, `FsEncryptionSecretKey`, `MEGaPrivateKeyK256Bytes`, etc.) correctly derive `Zeroize` and `ZeroizeOnDrop`, ensuring in-memory key material is wiped on drop.

However, the serialization path in `secret_keys_to_sks_proto` breaks this guarantee:

```rust
// rs/crypto/internal/crypto_service_provider/src/secret_key_store/proto_store.rs:425-441
let key_as_cbor =
    serde_cbor::to_vec(&csp_key).map_err(|_ignored_so_that_no_data_is_leaked| { ... })?;
let sk_pb = match maybe_scope {
    Some(scope) => pb::SecretKeyV1 {
        csp_secret_key: key_as_cbor,   // ← plaintext CBOR bytes, no Zeroize
        ...
    },
    ...
};
sks_proto.key_id_to_secret_key_v1.insert(key_id_hex, sk_pb);
```

The prost-generated `pb::SecretKeyV1` and `pb::SecretKeyStore` types:

```rust
// rs/crypto/internal/crypto_service_provider/src/gen/ic.crypto.v1.rs:3-22
#[derive(Clone, PartialEq, ::prost::Message)]
pub struct SecretKeyV1 {
    #[prost(bytes = "vec", tag = "1")]
    pub csp_secret_key: ::prost::alloc::vec::Vec<u8>,  // ← no Zeroize
    ...
}
#[derive(Clone, PartialEq, ::prost::Message)]
pub struct SecretKeyStore {
    #[prost(map = "string, message", tag = "3")]
    pub key_id_to_secret_key_v1: ::std::collections::HashMap<...SecretKeyV1>,  // ← no Zeroize
}
```

A second plaintext copy is created inside `write_protobuf_using_tmp_file`:

```rust
// rs/sys/src/fs.rs:417-421
pub fn write_protobuf_using_tmp_file<P>(dest: P, message: &impl prost::Message) -> io::Result<()> {
    write_using_tmp_file(dest, |writer| {
        let encoded_message = message.encode_to_vec();  // ← second plaintext copy, no Zeroize
        writer.write_all(&encoded_message)?;
        Ok(())
    })
}
```

The same issue exists on the read path in `sks_data_from_disk_or_new`:

```rust
// rs/crypto/internal/crypto_service_provider/src/secret_key_store/proto_store.rs:336-337
let sks_pb = pb::SecretKeyStore::decode(&*data)
    .unwrap_or_else(|_ignored_so_that_no_data_is_leaked| panic!(...));
```

The decoded `pb::SecretKeyStore` holds all CBOR-encoded secret key bytes in plain `Vec<u8>` fields without zeroization.

This write path is triggered on every `insert`, `insert_or_replace`, `remove`, and `retain` call on the `ProtoSecretKeyStore`.

---

### Impact Explanation

The affected keys are all node-level cryptographic keys managed by the CSP:

- **Ed25519** node signing keys
- **MultiBLS12-381** committee signing keys
- **ThresBLS12-381** threshold signature keys
- **TLS Ed25519** keys for inter-node TLS
- **NI-DKG forward-secure encryption keys** (BTE tree nodes for each epoch)
- **MEGa K256 iDKG encryption keys** (used for threshold ECDSA/Schnorr key shares)
- **iDKG commitment openings** (Simple and Pedersen)

Recovery of any of these keys by an attacker would allow: impersonating the node in TLS connections, decrypting iDKG dealings (breaking threshold ECDSA/Schnorr key shares), or forging threshold BLS signatures. The MEGa and NI-DKG keys are particularly sensitive because they underpin chain-key cryptography.

---

### Likelihood Explanation

The attack requires reading the replica process heap memory. The realistic path is a malicious canister exploiting a memory-safety bug in the Wasmtime execution engine or the canister sandbox to read the replica process address space. Canisters are explicitly in scope as an attacker-controlled entry point. The window of exposure is every key store write operation (key generation, key rotation, iDKG resharing), during which two plaintext copies of all node secret keys exist on the heap without any zeroization guarantee. The heap allocator may not reuse these pages for an extended period, widening the window.

---

### Recommendation

1. **Zeroize the CBOR intermediate buffer**: After `serde_cbor::to_vec`, explicitly zeroize the resulting `Vec<u8>` before it goes out of scope, or use a custom serializer that writes directly to a zeroizing buffer.
2. **Zeroize the protobuf encode buffer**: In `write_protobuf_using_tmp_file`, zeroize `encoded_message` after `write_all` completes.
3. **Add `Zeroize`/`ZeroizeOnDrop` to the generated protobuf types**: Either regenerate with a custom template that adds these derives, or wrap `pb::SecretKeyV1` and `pb::SecretKeyStore` in hand-written newtype wrappers that implement `Drop` with explicit zeroing of the `csp_secret_key` field.
4. **Zeroize the decoded protobuf on the read path**: After `pb::SecretKeyStore::decode` and extraction of keys into `SecretKeys`, explicitly zero the `csp_secret_key` bytes in each `SecretKeyV1` before dropping the `pb::SecretKeyStore`.

---

### Proof of Concept

**Write path** — two non-zeroizing plaintext copies created on every keystore mutation:

**Copy 1** — CBOR bytes in prost struct (no `Zeroize`): [1](#0-0) 

**Copy 2** — protobuf-encoded bytes in `write_protobuf_using_tmp_file` (no `Zeroize`): [2](#0-1) 

**Prost-generated types lack `Zeroize`**: [3](#0-2) 

**Read path** — decoded `pb::SecretKeyStore` also not zeroized**: [4](#0-3) 

**Contrast with the protected in-memory representation** (correctly zeroized): [5](#0-4) [6](#0-5)

### Citations

**File:** rs/crypto/internal/crypto_service_provider/src/secret_key_store/proto_store.rs (L333-351)
```rust
    fn sks_data_from_disk_or_new(sks_data_file: &Path) -> SecretKeys {
        let proto_file = match fs::read(sks_data_file) {
            Ok(data) => {
                let sks_pb = pb::SecretKeyStore::decode(&*data).unwrap_or_else(
                    |_ignored_so_that_no_data_is_leaked| panic!("error parsing SKS protobuf data"),
                );
                let keys = ProtoSecretKeyStore::migrate_to_current_version(sks_pb);
                Some(keys)
            }
            Err(err) => {
                if err.kind() == ErrorKind::NotFound {
                    None
                } else {
                    panic!("Error reading SKS data: {err}")
                }
            }
        };
        proto_file.unwrap_or_default()
    }
```

**File:** rs/crypto/internal/crypto_service_provider/src/secret_key_store/proto_store.rs (L425-441)
```rust
            let key_as_cbor =
                serde_cbor::to_vec(&csp_key).map_err(|_ignored_so_that_no_data_is_leaked| {
                    SecretKeyStoreWriteError::SerializationError(format!(
                        "Error serializing key with ID {key_id}"
                    ))
                })?;
            let sk_pb = match maybe_scope {
                Some(scope) => pb::SecretKeyV1 {
                    csp_secret_key: key_as_cbor,
                    scope: String::from(scope),
                },
                None => pb::SecretKeyV1 {
                    csp_secret_key: key_as_cbor,
                    scope: String::from(""),
                },
            };
            sks_proto.key_id_to_secret_key_v1.insert(key_id_hex, sk_pb);
```

**File:** rs/sys/src/fs.rs (L413-422)
```rust
pub fn write_protobuf_using_tmp_file<P>(dest: P, message: &impl prost::Message) -> io::Result<()>
where
    P: AsRef<Path>,
{
    write_using_tmp_file(dest, |writer| {
        let encoded_message = message.encode_to_vec();
        writer.write_all(&encoded_message)?;
        Ok(())
    })
}
```

**File:** rs/crypto/internal/crypto_service_provider/src/gen/ic.crypto.v1.rs (L1-22)
```rust
// This file is @generated by prost-build.
#[derive(Clone, PartialEq, ::prost::Message)]
pub struct SecretKeyV1 {
    /// CBOR serialization of `CspSecretKey`
    #[prost(bytes = "vec", tag = "1")]
    pub csp_secret_key: ::prost::alloc::vec::Vec<u8>,
    /// Rust's `to_string()` of `Scope`
    #[prost(string, tag = "2")]
    pub scope: ::prost::alloc::string::String,
}
/// SecretKeyStore stores secret keys.
#[derive(Clone, PartialEq, ::prost::Message)]
pub struct SecretKeyStore {
    /// Version of SecretKeyStore
    #[prost(uint32, tag = "2")]
    pub version: u32,
    /// Mapping from KeyId to SecretKeyV1.
    /// `KeyId` is represented as a hex-string (32 bytes).
    #[prost(map = "string, message", tag = "3")]
    pub key_id_to_secret_key_v1:
        ::std::collections::HashMap<::prost::alloc::string::String, SecretKeyV1>,
}
```

**File:** rs/crypto/internal/crypto_service_provider/src/types.rs (L49-68)
```rust
#[derive(
    Clone, Eq, PartialEq, Deserialize, EnumCount, IntoStaticStr, Serialize, Zeroize, ZeroizeOnDrop,
)]
#[cfg_attr(test, derive(Arbitrary))]
pub enum CspSecretKey {
    #[cfg_attr(test, proptest(strategy(arbitrary_ed25519_secret_key)))]
    Ed25519(ed25519_types::SecretKeyBytes),
    #[cfg_attr(test, proptest(strategy(arbitrary_multi_bls12381_secret_key)))]
    MultiBls12_381(multi_types::SecretKeyBytes),
    #[cfg_attr(test, proptest(strategy(arbitrary_threshold_bls12381_secret_key)))]
    ThresBls12_381(threshold_types::SecretKeyBytes),
    #[cfg_attr(test, proptest(strategy(arbitrary_tls_ed25519_secret_key)))]
    TlsEd25519(TlsEd25519SecretKeyDerBytes),
    #[cfg_attr(test, proptest(value(default_fs_encryption_key_set)))]
    FsEncryption(CspFsEncryptionKeySet),
    #[cfg_attr(test, proptest(strategy(arbitrary_mega_k256_encryption_key_set)))]
    MEGaEncryptionK256(MEGaKeySetK256Bytes),
    #[cfg_attr(test, proptest(strategy(arbitrary_threshold_ecdsa_opening)))]
    IDkgCommitmentOpening(CommitmentOpeningBytes),
}
```

**File:** rs/crypto/secrets_containers/src/secret_array.rs (L10-25)
```rust
#[derive(Clone, Eq, PartialEq, Zeroize, ZeroizeOnDrop)]
pub struct SecretArray<const N: usize> {
    inner_secret: Box<[u8; N]>,
}

impl<const N: usize> SecretArray<N> {
    /// Constructs a `SecretArray` from the provided `secret`, and clears the
    /// memory used by `secret`.
    pub fn new_and_zeroize_argument(secret: &mut [u8; N]) -> Self {
        let mut ret = Self {
            inner_secret: Box::new([0_u8; N]),
        };
        ret.inner_secret.copy_from_slice(&secret[..]);
        secret.zeroize();
        ret
    }
```
