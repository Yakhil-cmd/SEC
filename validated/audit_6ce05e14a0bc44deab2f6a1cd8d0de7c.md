### Title
Unprotected Legacy Transaction Signature Replay Across Chains — (`File: basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/legacy_tx.rs`)

---

### Summary

ZKsync OS's bootloader explicitly accepts pre-EIP-155 legacy transactions (those with `v = 27` or `v = 28`) whose signing hash is computed **without** the chain ID. A transaction signed on Ethereum mainnet (or any other EVM chain) without EIP-155 protection can be replayed verbatim on ZKsync OS, because the signature covers only `keccak256(rlp([nonce, gasPrice, gasLimit, to, value, data]))` — a chain-agnostic hash. Any attacker who observes such a transaction on-chain can submit it to ZKsync OS and have it executed against the victim's account there.

---

### Finding Description

In `LegacyPayloadParser::try_parse_and_hash_for_signature_verification`, the code branches on whether the signature is EIP-155-protected:

```rust
// legacy_tx.rs lines 89–94
let sig_hash: Bytes32 = if legacy_signature.is_eip155() == false {
    // Unprotected legacy
    let mut hasher = crypto::sha3::Keccak256::new();
    apply_list_concatenation_encoding_to_hash(inner_slice.len() as u32, &mut hasher);
    hasher.update(inner_slice);
    hasher.finalize_reset().into()
``` [1](#0-0) 

The `is_eip155()` predicate returns `false` exactly when `v == 27 || v == 28`:

```rust
// legacy_tx.rs lines 129–131
pub fn is_eip155(&self) -> bool {
    self.v != 27 && self.v != 28
}
``` [2](#0-1) 

When `is_eip155()` is `false`, the signing hash is computed over only the 6-field payload (`nonce, gasPrice, gasLimit, to, value, data`) with **no chain ID**. The transaction is then stored as `RlpEncodedTxInner::Legacy` and proceeds through the full validation pipeline:

```rust
// mod.rs lines 127–131
let tx = if sig_data.is_eip155() {
    Self::LegacyWithEIP155(tx, sig_data)
} else {
    Self::Legacy(tx, sig_data)
};
Ok((tx, sig_hash))
``` [3](#0-2) 

The signature is then verified in `validate_and_compute_fee_for_transaction` using `suggested_signed_hash`, which for the `Legacy` variant is the chain-ID-free hash computed above: [4](#0-3) 

There is **no rejection** of unprotected legacy transactions anywhere in the bootloader. The typed transaction types (EIP-2930, EIP-1559, EIP-7702) all enforce `tx.chain_id == expected_chain_id`: [5](#0-4) 

But the unprotected legacy path has no equivalent guard.

---

### Impact Explanation

An attacker who observes any pre-EIP-155 legacy transaction (v=27 or v=28) broadcast on Ethereum mainnet or any other EVM chain can replay it on ZKsync OS if:

1. The victim's ZKsync OS account nonce matches the nonce in the replayed transaction.
2. The victim holds funds on ZKsync OS.

The most realistic scenario is a victim with a fresh ZKsync OS account (nonce = 0) who previously signed a pre-EIP-155 transaction with nonce = 0 on Ethereum mainnet. The attacker replays that transaction on ZKsync OS, causing an arbitrary call or ETH transfer to execute from the victim's account — draining funds or triggering unintended contract interactions.

**Vulnerability class**: EVM semantic mismatch / cross-chain signature replay.  
**Impact**: Direct loss of user funds on ZKsync OS.

---

### Likelihood Explanation

Pre-EIP-155 transactions (before the 2016 Spurious Dragon hard fork) are publicly visible on Ethereum mainnet. Millions of such transactions exist. Any ZKsync OS user whose address was active on Ethereum before EIP-155 and who has not yet incremented their ZKsync OS nonce past the nonce of any of those old transactions is vulnerable. The attack requires no privileged access — only the ability to submit a transaction to the ZKsync OS sequencer, which is open to any unprivileged sender.

---

### Recommendation

Reject unprotected legacy transactions (v = 27 or v = 28) at the bootloader level. ZKsync OS is a new chain and has no backward-compatibility obligation to accept pre-EIP-155 signatures. The fix is to return `Err(InvalidTransaction::InvalidChainId)` (or a new `MissingChainId` variant) when `legacy_signature.is_eip155() == false` inside `LegacyPayloadParser::try_parse_and_hash_for_signature_verification`.

Alternatively, if unprotected legacy transactions must be supported for tooling compatibility, the bootloader should at minimum document this as a known replay risk and advise users never to reuse addresses that have pre-EIP-155 transaction history.

---

### Proof of Concept

1. On Ethereum mainnet, locate any pre-EIP-155 transaction from address `A` with nonce `N`, `to = B`, `value = V`, `gasPrice = P`, `gasLimit = G`, `data = D`, and signature `(v=27, r, s)`. Such transactions are freely available on Etherscan.

2. Ensure address `A` on ZKsync OS has nonce `N` and balance ≥ `V + P*G`.

3. Submit the **identical RLP-encoded transaction bytes** to the ZKsync OS sequencer.

4. The bootloader parses it as `RlpEncodedTxInner::Legacy`, computes `sig_hash = keccak256(rlp([N, P, G, B, V, D]))` — the same hash that was signed on Ethereum mainnet — and `ecrecover` returns `A`.

5. The transaction passes signature verification and executes: `V` ETH is transferred from `A` to `B` on ZKsync OS, and `A`'s nonce is incremented.

The root cause is confirmed at: [6](#0-5) [2](#0-1) [7](#0-6)

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

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/legacy_tx.rs (L128-131)
```rust
impl<'a> LegacySignatureData<'a> {
    pub fn is_eip155(&self) -> bool {
        self.v != 27 && self.v != 28
    }
```

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/mod.rs (L65-67)
```rust
                    if tx.chain_id != expected_chain_id {
                        return Err(InvalidTransaction::InvalidChainId.into());
                    }
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

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/validation_impl.rs (L194-212)
```rust
    let suggested_signed_hash: Bytes32 = transaction.signed_hash()?;
    let from = *transaction.from();
    let Some((parity, r, s)) = transaction.sig_parity_r_s() else {
        // Ethereum txs should have signature
        return Err(InvalidTransaction::InvalidStructure.into());
    };

    if !Config::VALIDATE_EOA_SIGNATURE | Config::SIMULATION {
        // No native for Eth STF
    } else {
        if U256::from_be_slice(s) > U256::from_be_bytes(SECP256K1N_HALF) {
            return Err(InvalidTransaction::MalleableSignature.into());
        }

        let mut ecrecover_input = [0u8; 128];
        ecrecover_input[0..32].copy_from_slice(suggested_signed_hash.as_u8_array_ref());
        ecrecover_input[63] = (parity as u8) + 27;
        ecrecover_input[64..96][(32 - r.len())..].copy_from_slice(r);
        ecrecover_input[96..128][(32 - s.len())..].copy_from_slice(s);
```
