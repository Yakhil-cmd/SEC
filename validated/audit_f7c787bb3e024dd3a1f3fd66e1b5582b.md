### Title
Cross-Chain Signature Replay via Accepted Unprotected Legacy Transactions — (`File: basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/legacy_tx.rs`)

---

### Summary

ZKsync OS's bootloader explicitly accepts pre-EIP-155 ("unprotected") legacy transactions whose signing hash contains **no chain ID**. Any such transaction signed on Ethereum mainnet (or any other EVM chain) can be replayed verbatim on ZKsync OS, because the signature verification passes against a chain-agnostic hash. This is the direct analog of the Scroll `EnforcedTxGateway` bug: both share the root cause of a signed message that lacks a chain-binding field, enabling cross-chain replay.

---

### Finding Description

In `LegacyPayloadParser::try_parse_and_hash_for_signature_verification`, when the signature's `v` value equals `27` or `28` (i.e., `is_eip155()` returns `false`), the signing hash is computed over only the 6-field payload `[nonce, gasPrice, gasLimit, to, value, data]` — with no chain ID included:

```rust
// legacy_tx.rs lines 89-94
let sig_hash: Bytes32 = if legacy_signature.is_eip155() == false {
    // Unprotected legacy
    let mut hasher = crypto::sha3::Keccak256::new();
    apply_list_concatenation_encoding_to_hash(inner_slice.len() as u32, &mut hasher);
    hasher.update(inner_slice);
    hasher.finalize_reset().into()
``` [1](#0-0) 

The caller in `RlpEncodedTxInner::parse_and_compute_signed_hash` then stores this as the `Legacy` variant (not `LegacyWithEIP155`) and returns it without error:

```rust
// mod.rs lines 127-131
let tx = if sig_data.is_eip155() {
    Self::LegacyWithEIP155(tx, sig_data)
} else {
    Self::Legacy(tx, sig_data)
};
``` [2](#0-1) 

The `chain_id()` accessor explicitly returns `None` for the `Legacy` variant, confirming no chain binding is enforced:

```rust
// transaction.rs lines 82-87
pub fn chain_id(&self) -> Option<u64> {
    match &self.inner {
        RlpEncodedTxInner::Legacy(_, _) => None,
        _ => Some(self.chain_id),
    }
}
``` [3](#0-2) 

The signature verification in `ethereum/validation_impl.rs` then runs `ecrecover` against this chain-agnostic hash and accepts the transaction if the recovered address matches `from`: [4](#0-3) 

The nonce is part of the signed payload, so if the victim's nonce on ZKsync OS happens to match the nonce in the replayed transaction, the full validation passes and the transaction executes.

---

### Impact Explanation

An attacker who observes any unprotected legacy transaction (v=27/28) broadcast by a victim on Ethereum mainnet or any other EVM chain can submit the identical raw bytes to ZKsync OS. If the victim's account nonce on ZKsync OS equals the nonce in the replayed transaction, the bootloader will:

1. Parse the transaction successfully.
2. Compute the same chain-agnostic signing hash.
3. Recover the victim's address via `ecrecover`.
4. Accept the transaction as valid.
5. Execute the call (arbitrary `to`, `value`, `data`) on behalf of the victim.

This enables **theft of funds** (ETH/tokens transferred to attacker-controlled addresses) or **arbitrary contract calls** on behalf of the victim, without the victim's consent on ZKsync OS.

---

### Likelihood Explanation

- Many legacy wallets, hardware wallets in compatibility mode, and older dApps still produce unprotected legacy transactions (v=27/28).
- Ethereum mainnet mempool and block explorers expose these transactions publicly.
- The attacker needs only to: (a) find a victim who has used an unprotected legacy transaction on any EVM chain, and (b) wait until the victim's ZKsync OS nonce matches. Since nonces are sequential and start at 0, a fresh ZKsync OS account whose first-ever Ethereum transaction was unprotected is immediately vulnerable.
- No privileged access, leaked keys, or governance majority is required.

---

### Recommendation

Reject unprotected legacy transactions (v=27/28) at the parsing stage. The `Legacy` variant of `RlpEncodedTxInner` should be removed or treated as an error in `parse_and_compute_signed_hash`. Only `LegacyWithEIP155` (EIP-155 protected) transactions should be accepted. This is consistent with the approach already taken for all typed transactions (EIP-2930, EIP-1559, EIP-7702), which all enforce `chain_id != expected_chain_id` rejection. [5](#0-4) 

---

### Proof of Concept

1. On Ethereum mainnet, victim Alice sends an unprotected legacy transaction (v=27 or v=28) with nonce=0, to=attacker_contract, value=1 ETH, data=`transfer(attacker, all_tokens)`.
2. Alice's ZKsync OS account nonce is also 0 (fresh account).
3. Attacker copies the raw RLP bytes of Alice's Ethereum transaction.
4. Attacker submits those identical bytes as an L2 transaction to ZKsync OS.
5. The bootloader parses it as `RlpEncodedTxInner::Legacy`, computes `keccak256(rlp([nonce, gasPrice, gasLimit, to, value, data]))` — identical to the Ethereum signing hash.
6. `ecrecover` recovers Alice's address; `from` matches; nonce matches (both 0).
7. The transaction executes: Alice's ZKsync OS funds are drained. [6](#0-5) [7](#0-6)

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

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/mod.rs (L119-134)
```rust
        } else {
            // Legacy path
            let (tx, sig_data, sig_hash) =
                LegacyPayloadParser::try_parse_and_hash_for_signature_verification(
                    input,
                    expected_chain_id,
                )?;

            let tx = if sig_data.is_eip155() {
                Self::LegacyWithEIP155(tx, sig_data)
            } else {
                Self::Legacy(tx, sig_data)
            };

            Ok((tx, sig_hash))
        }
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
