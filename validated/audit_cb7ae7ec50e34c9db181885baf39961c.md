### Title
Unprotected Legacy Transactions Accepted Without Chain-ID Binding Enables Cross-Chain Replay — (`basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/legacy_tx.rs`)

---

### Summary

ZKsync OS accepts pre-EIP-155 legacy transactions (v=27 or v=28) whose signing hash is computed without any chain-ID component. Because the hash commits only to `[nonce, gasPrice, gasLimit, to, value, data]`, the same signature is cryptographically valid on every EVM-compatible chain. An attacker who observes such a transaction on Ethereum mainnet (or any other chain) can replay it verbatim on ZKsync OS and drain the sender's funds there, provided the sender's ZKsync nonce matches the replayed transaction's nonce.

---

### Finding Description

In `LegacyPayloadParser::try_parse_and_hash_for_signature_verification`, the code branches on whether the signature is EIP-155-protected:

```rust
// legacy_tx.rs lines 89-94
let sig_hash: Bytes32 = if legacy_signature.is_eip155() == false {
    // Unprotected legacy
    let mut hasher = crypto::sha3::Keccak256::new();
    apply_list_concatenation_encoding_to_hash(inner_slice.len() as u32, &mut hasher);
    hasher.update(inner_slice);
    hasher.finalize_reset().into()
``` [1](#0-0) 

`is_eip155()` returns `false` whenever `v == 27 || v == 28`:

```rust
// lines 129-131
pub fn is_eip155(&self) -> bool {
    self.v != 27 && self.v != 28
}
``` [2](#0-1) 

When the branch is taken, the chain ID is **never hashed into the signing digest**. The resulting `sig_hash` is then passed directly to `ecrecover` in both the ZK and Ethereum validation paths: [3](#0-2) 

The parsed transaction is stored as `Self::Legacy` (no EIP-155 variant) and proceeds through the full validation pipeline without any chain-ID assertion:

```rust
// mod.rs lines 127-131
let tx = if sig_data.is_eip155() {
    Self::LegacyWithEIP155(tx, sig_data)
} else {
    Self::Legacy(tx, sig_data)   // ← no chain-ID check ever applied
};
``` [4](#0-3) 

By contrast, every typed transaction (EIP-2930, EIP-1559, EIP-4844, EIP-7702) and every EIP-155 legacy transaction explicitly asserts `tx.chain_id == expected_chain_id` before proceeding: [5](#0-4) 

The `expected_chain_id` itself is read from the oracle-supplied block metadata (`system.get_chain_id()`), so it correctly reflects the ZKsync OS chain: [6](#0-5) 

The unprotected legacy path is the sole gap: it skips the domain check entirely.

---

### Impact Explanation

An attacker who observes a pre-EIP-155 legacy transaction broadcast on Ethereum mainnet (or any other EVM chain) can submit the identical raw bytes to ZKsync OS. Because:

1. The signing hash contains no chain ID, the `ecrecover` output is identical on both chains.
2. ZKsync OS uses the same 20-byte address space as Ethereum.
3. ZKsync OS uses the same u64 nonce space.

If the victim's ZKsync nonce equals the replayed transaction's nonce, the transaction passes all validation checks and executes — transferring value or invoking arbitrary calldata — without the victim's knowledge or consent. This is a direct, unprivileged, externally-reachable path to loss of user funds.

---

### Likelihood Explanation

Pre-EIP-155 transactions (v=27/28) are still produced by some hardware wallets, older libraries, and certain DeFi protocols that sign typed-data hashes with raw `ecrecover`. A realistic scenario: a user's first ZKsync transaction (nonce=0) coincides with a pre-EIP-155 Ethereum transaction they signed at nonce=0. The attacker needs only to monitor the Ethereum mempool and submit the same bytes to ZKsync OS. No privileged access, no oracle manipulation, and no brute force is required.

---

### Recommendation

Reject unprotected legacy transactions at the parsing layer. The simplest fix is to return `InvalidTransaction::InvalidChainId` (or a new `MissingChainId` variant) when `is_eip155()` is false, mirroring the enforcement already applied to all typed transactions. If backward compatibility with pre-EIP-155 wallets is required, at minimum the chain ID should be asserted to equal the current chain's ID before the transaction is accepted.

---

### Proof of Concept

1. On Ethereum mainnet, broadcast a legacy transfer with `v=27` (no EIP-155), `nonce=0`, `to=attacker`, `value=X`.
2. The victim's ZKsync OS account has nonce=0 and balance ≥ X.
3. Submit the identical RLP-encoded bytes to ZKsync OS as a type-`Rlp` transaction.
4. `LegacyPayloadParser` computes the same 6-field hash (no chain ID), `ecrecover` recovers the victim's address, nonce check passes, and the transfer executes — draining X from the victim's ZKsync OS balance.

The root cause is confirmed at: [7](#0-6)

### Citations

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/legacy_tx.rs (L89-115)
```rust
        let sig_hash: Bytes32 = if legacy_signature.is_eip155() == false {
            // Unprotected legacy
            let mut hasher = crypto::sha3::Keccak256::new();
            apply_list_concatenation_encoding_to_hash(inner_slice.len() as u32, &mut hasher);
            hasher.update(inner_slice);
            hasher.finalize_reset().into()
        } else {
            // EIP-155 protected legacy: v must match 35 + 2*chainId (+ {0,1})
            let min_v = U256::from(35) + U256::from(expected_chain_id) * U256::from(2);
            if !(legacy_signature.v == min_v || legacy_signature.v == min_v + U256::ONE) {
                return Err(InvalidTransaction::InvalidEncoding.into());
            }

            // Compute signing hash over the 6-field payload plus chainId and two empty strings.
            let chain_id = expected_chain_id;
            let chain_id_encoding_len = u64_encoding_len(chain_id);

            let mut hasher = crypto::sha3::Keccak256::new();
            apply_list_concatenation_encoding_to_hash(
                (inner_slice.len() + chain_id_encoding_len + 2) as u32, // 0x80, 0x80 for r/s
                &mut hasher,
            );
            hasher.update(inner_slice);
            apply_u64_encoding_to_hash(chain_id, &mut hasher);
            hasher.update(&[0x80, 0x80]);
            hasher.finalize_reset().into()
        };
```

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/legacy_tx.rs (L128-131)
```rust
impl<'a> LegacySignatureData<'a> {
    pub fn is_eip155(&self) -> bool {
        self.v != 27 && self.v != 28
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L245-265)
```rust
    let suggested_signed_hash: Bytes32 = transaction.signed_hash()?;

    // Only service transactions have no signature,
    // we don't even charge gas/native related to ecrecover for them.
    if let Some((parity, r, s)) = transaction.sig_parity_r_s() {
        // Even if we don't validate a signature, we still need to charge for ecrecover for equivalent behavior
        // Note that gas is charged already in intrinsic cost, so now
        // we only need to charge native resources.
        if !Config::VALIDATE_EOA_SIGNATURE | Config::SIMULATION {
            intrinsic_resources.charge(&Resources::from_native(
                <<S as SystemTypes>::Resources as Resources>::Native::from_computational(
                    ECRECOVER_NATIVE_COST,
                ),
            ))?;
        } else {
            if U256::from_be_slice(s) > U256::from_be_bytes(SECP256K1N_HALF) {
                return Err(InvalidTransaction::MalleableSignature.into());
            }

            let mut ecrecover_input = [0u8; 128];
            ecrecover_input[0..32].copy_from_slice(suggested_signed_hash.as_u8_array_ref());
```

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/mod.rs (L65-100)
```rust
                    if tx.chain_id != expected_chain_id {
                        return Err(InvalidTransaction::InvalidChainId.into());
                    }
                    Ok((Self::EIP2930(tx, sig_data), sig_hash))
                }
                EIP1559Tx::TX_TYPE => {
                    let (tx, sig_data, sig_hash) =
                        EIP2718PayloadParser::<EIP1559Tx<'a>>::try_parse_and_hash_for_signature_verification(
                            r.remaining()
                        )?;
                    if tx.chain_id != expected_chain_id {
                        return Err(InvalidTransaction::InvalidChainId.into());
                    }
                    Ok((Self::EIP1559(tx, sig_data), sig_hash))
                }
                #[cfg(feature = "eip-4844")]
                EIP4844Tx::TX_TYPE => {
                    let (tx, sig_data, sig_hash) =
                        EIP2718PayloadParser::<EIP4844Tx<'a>>::try_parse_and_hash_for_signature_verification(
                            r.remaining()
                        )?;
                    if tx.chain_id != expected_chain_id {
                        return Err(InvalidTransaction::InvalidChainId.into());
                    }
                    Ok((Self::EIP4844(tx, sig_data), sig_hash))
                }
                #[cfg(feature = "eip-7702")]
                EIP7702Tx::TX_TYPE => {
                    let (tx, sig_data, sig_hash) =
                        EIP2718PayloadParser::<EIP7702Tx<'a>>::try_parse_and_hash_for_signature_verification(
                            r.remaining()
                        )?;

                    if tx.chain_id != expected_chain_id {
                        return Err(InvalidTransaction::InvalidChainId.into());
                    }
```

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/mod.rs (L127-131)
```rust
            let tx = if sig_data.is_eip155() {
                Self::LegacyWithEIP155(tx, sig_data)
            } else {
                Self::Legacy(tx, sig_data)
            };
```

**File:** basic_bootloader/src/bootloader/transaction/mod.rs (L74-84)
```rust
        let expected_chain_id = system.get_chain_id();

        // query the transaction encoding format from the oracle
        let format: TxEncodingFormat = TxEncodingFormatQuery::get(system.io.oracle(), &())?;

        match format {
            TxEncodingFormat::Rlp => {
                // RLP-encoded transactions don't include the `from` field, so we need to query it from the oracle.
                // This is so that sequencer can skip ecrecover (for simulation, for example).
                let from = TxFromQuery::get(system.io.oracle(), &())?;
                let tx = RlpEncodedTransaction::parse_from_buffer(buffer, expected_chain_id, from)?;
```
