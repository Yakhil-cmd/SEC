### Title
Unprotected Pre-EIP-155 Legacy Transactions Accepted Without Chain-ID Binding Enables Cross-Chain Signature Replay - (File: `basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/legacy_tx.rs`)

---

### Summary

ZKsync OS accepts pre-EIP-155 legacy transactions (v = 27 or v = 28) whose signing hash contains no chain-ID. A signature produced on any other EVM chain (e.g., Ethereum mainnet) for such a transaction is valid on ZKsync OS whenever the sender's nonce matches, enabling an attacker to replay the foreign transaction and drain the victim's ZKsync OS account.

---

### Finding Description

`LegacyPayloadParser::try_parse_and_hash_for_signature_verification` branches on `legacy_signature.is_eip155()`:

```rust
// is_eip155() returns false when v == 27 || v == 28
let sig_hash: Bytes32 = if legacy_signature.is_eip155() == false {
    // Unprotected legacy
    let mut hasher = crypto::sha3::Keccak256::new();
    apply_list_concatenation_encoding_to_hash(inner_slice.len() as u32, &mut hasher);
    hasher.update(inner_slice);
    hasher.finalize_reset().into()   // ← no chain_id in hash
} else {
    // EIP-155 protected: chain_id IS included
    ...
}
``` [1](#0-0) 

The unprotected branch hashes only `rlp([nonce, gasPrice, gasLimit, to, value, data])` — identical to the pre-EIP-155 Ethereum mainnet signing hash. The transaction is then returned as `Ok(Self::Legacy(...))` with no error:

```rust
let tx = if sig_data.is_eip155() {
    Self::LegacyWithEIP155(tx, sig_data)
} else {
    Self::Legacy(tx, sig_data)   // ← accepted unconditionally
};
Ok((tx, sig_hash))
``` [2](#0-1) 

`chain_id()` returns `None` for this variant, confirming no chain binding is enforced:

```rust
pub fn chain_id(&self) -> Option<u64> {
    match &self.inner {
        RlpEncodedTxInner::Legacy(_, _) => None,   // ← no chain_id
        _ => Some(self.chain_id),
    }
}
``` [3](#0-2) 

The validation flow then runs `ecrecover` against this chain-ID-free hash and accepts the transaction if the recovered address matches `from`: [4](#0-3) 

All typed transactions (EIP-2930, EIP-1559, EIP-4844, EIP-7702) correctly enforce `tx.chain_id != expected_chain_id` and reject mismatches. Only the unprotected legacy path is missing this guard. [5](#0-4) 

---

### Impact Explanation

An attacker who obtains a pre-EIP-155 transaction (v = 27 or v = 28) that Alice signed on Ethereum mainnet (or any other EVM chain) can submit it verbatim to ZKsync OS. If Alice's ZKsync OS account nonce equals the nonce in that transaction, ZKsync OS will:

1. Compute the same chain-ID-free signing hash.
2. Recover Alice's address via `ecrecover`.
3. Accept and execute the transaction — transferring Alice's ZKsync OS funds to the original `to` address.

This is a direct, unprivileged loss of user funds. The attacker needs no special role; they only need to submit a valid RLP-encoded transaction to the sequencer.

---

### Likelihood Explanation

Pre-EIP-155 transactions were the only format available before Spurious Dragon (late 2016) and remain valid on Ethereum mainnet today. Many early Ethereum users have such transactions in the public mempool or block history. Because Ethereum addresses are deterministic from private keys, every ZKsync OS user shares their address with every other EVM chain. The nonce constraint is the only practical barrier, but:

- A user whose ZKsync OS nonce is currently 0 is immediately vulnerable to any pre-EIP-155 nonce-0 transaction they ever signed elsewhere.
- An attacker can monitor both chains and wait for the nonce to align.
- The attack requires no privileged access, no oracle manipulation, and no governance majority.

Likelihood is **medium** — nonce alignment is required but is a realistic condition, especially for new ZKsync OS accounts.

---

### Recommendation

**Short term:** Reject unprotected legacy transactions outright. In `parse_and_compute_signed_hash`, after `LegacyPayloadParser::try_parse_and_hash_for_signature_verification` returns, check `sig_data.is_eip155()` and return `Err(InvalidTransaction::InvalidChainId)` if it is `false`. This mirrors the existing guard applied to all typed transaction formats.

**Long term:** Align with the broader Ethereum ecosystem direction of deprecating pre-EIP-155 transactions entirely. If backward compatibility with pre-EIP-155 is intentionally required, document it explicitly and add a warning that such transactions are inherently cross-chain replayable.

---

### Proof of Concept

1. Alice signs a pre-EIP-155 ETH transfer on Ethereum mainnet at nonce 0:
   - `v = 27`, `r`, `s` — no chain_id in the signed hash.
2. Alice deploys a fresh ZKsync OS account (nonce = 0) at the same address.
3. Attacker submits Alice's raw Ethereum mainnet transaction bytes to ZKsync OS.
4. `LegacyPayloadParser` parses it, detects `v == 27`, computes `keccak256(rlp([0, gasPrice, gasLimit, to, value, data]))` — identical to the Ethereum mainnet signing hash.
5. `ecrecover` recovers Alice's address; `recovered_from == from` passes.
6. ZKsync OS executes the transaction, transferring Alice's ZKsync OS ETH to the `to` address specified in the original Ethereum transaction.

The attacker spent nothing beyond a sequencer submission fee; Alice loses her ZKsync OS balance.

### Citations

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/legacy_tx.rs (L89-95)
```rust
        let sig_hash: Bytes32 = if legacy_signature.is_eip155() == false {
            // Unprotected legacy
            let mut hasher = crypto::sha3::Keccak256::new();
            apply_list_concatenation_encoding_to_hash(inner_slice.len() as u32, &mut hasher);
            hasher.update(inner_slice);
            hasher.finalize_reset().into()
        } else {
```

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/mod.rs (L59-101)
```rust
                EIP2930Tx::TX_TYPE => {
                    let (tx, sig_data, sig_hash) =
                        EIP2718PayloadParser::<EIP2930Tx<'a>>::try_parse_and_hash_for_signature_verification(
                            r.remaining()
                        )?;

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
                    Ok((Self::EIP7702(tx, sig_data), sig_hash))
```

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/mod.rs (L127-133)
```rust
            let tx = if sig_data.is_eip155() {
                Self::LegacyWithEIP155(tx, sig_data)
            } else {
                Self::Legacy(tx, sig_data)
            };

            Ok((tx, sig_hash))
```

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction.rs (L82-87)
```rust
    pub fn chain_id(&self) -> Option<u64> {
        match &self.inner {
            RlpEncodedTxInner::Legacy(_, _) => None,
            _ => Some(self.chain_id),
        }
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/validation_impl.rs (L208-245)
```rust
        let mut ecrecover_input = [0u8; 128];
        ecrecover_input[0..32].copy_from_slice(suggested_signed_hash.as_u8_array_ref());
        ecrecover_input[63] = (parity as u8) + 27;
        ecrecover_input[64..96][(32 - r.len())..].copy_from_slice(r);
        ecrecover_input[96..128][(32 - s.len())..].copy_from_slice(s);

        let mut ecrecover_output = ArrayBuilder::default();
        // We already charged gas for ecrecover in intrinsic cost, so we only need to charge native resources here.
        tx_resources
            .main_resources
            .with_infinite_ergs(|resources| {
                S::SystemFunctions::secp256k1_ec_recover(
                    ecrecover_input.as_slice(),
                    &mut ecrecover_output,
                    resources,
                    system.get_allocator(),
                )
                .map_err(SystemError::from)
            })?;

        if ecrecover_output.is_empty() {
            return Err(InvalidTransaction::IncorrectFrom {
                recovered: B160::ZERO,
                tx: from,
            }
            .into());
        }

        let recovered_from = B160::try_from_be_slice(&ecrecover_output.build()[12..])
            .ok_or(internal_error!("Invalid ecrecover return value"))?;

        if recovered_from != from {
            return Err(InvalidTransaction::IncorrectFrom {
                recovered: recovered_from,
                tx: from,
            }
            .into());
        }
```
