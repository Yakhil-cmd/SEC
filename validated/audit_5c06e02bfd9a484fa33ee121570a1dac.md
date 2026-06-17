### Title
Unprotected Legacy Transactions Accepted Without ChainId in Signing Hash, Enabling Cross-Chain Replay - (File: `basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/legacy_tx.rs`)

### Summary

ZKsync OS accepts pre-EIP-155 legacy transactions (where `v == 27` or `v == 28`) and computes their signing hash **without including the chain ID**. This is the direct protocol-layer analog of the reported "withdraw message replayable on a different chain" class: a signed message whose digest omits the chain ID is valid on every chain that accepts the same transaction format, enabling cross-chain replay of user-signed transactions and potential loss of funds.

### Finding Description

In `LegacyPayloadParser::try_parse_and_hash_for_signature_verification`, when the signature's `v` value equals `27` or `28` (i.e., `is_eip155()` returns `false`), the signing hash is computed over only the six-field RLP payload `[nonce, gasPrice, gasLimit, to, value, data]` — **no chain ID is mixed in**:

```rust
// legacy_tx.rs lines 89–94
let sig_hash: Bytes32 = if legacy_signature.is_eip155() == false {
    // Unprotected legacy
    let mut hasher = crypto::sha3::Keccak256::new();
    apply_list_concatenation_encoding_to_hash(inner_slice.len() as u32, &mut hasher);
    hasher.update(inner_slice);
    hasher.finalize_reset().into()
``` [1](#0-0) 

The `is_eip155()` predicate is simply:

```rust
pub fn is_eip155(&self) -> bool {
    self.v != 27 && self.v != 28
}
``` [2](#0-1) 

This unprotected variant is accepted and stored as `RlpEncodedTxInner::Legacy`:

```rust
let tx = if sig_data.is_eip155() {
    Self::LegacyWithEIP155(tx, sig_data)
} else {
    Self::Legacy(tx, sig_data)
};
Ok((tx, sig_hash))
``` [3](#0-2) 

`chain_id()` explicitly returns `None` for the `Legacy` variant, confirming no chain binding:

```rust
pub fn chain_id(&self) -> Option<u64> {
    match &self.inner {
        RlpEncodedTxInner::Legacy(_, _) => None,
        _ => Some(self.chain_id),
    }
}
``` [4](#0-3) 

The ZK validation path then uses this chain-ID-free hash directly for `ecrecover` without any additional rejection of the unprotected variant: [5](#0-4) 

### Impact Explanation

Because the signing hash for unprotected legacy transactions contains no chain ID, an identical `(nonce, gasPrice, gasLimit, to, value, data, v, r, s)` tuple is simultaneously valid on ZKsync OS and on every other EVM-compatible chain that accepts pre-EIP-155 transactions (including Ethereum mainnet). Concretely:

- **Replay from Ethereum → ZKsync OS**: A pre-EIP-155 Ethereum mainnet transaction (e.g., a token transfer or ETH send) whose sender holds funds on ZKsync OS with a matching nonce is accepted and executed on ZKsync OS without the sender's knowledge or consent.
- **Replay from ZKsync OS → Ethereum**: A user who signs a ZKsync OS transaction with `v=27/28` (e.g., via a wallet that defaults to unprotected legacy format) can have that transaction replayed on Ethereum mainnet, draining funds there.

Both directions result in **unintended fund loss** for the victim.

### Likelihood Explanation

- Any unprivileged attacker can submit a transaction to ZKsync OS; no special role is required.
- Pre-EIP-155 transactions are common in the wild (many hardware wallets and older tooling still produce them).
- The attacker only needs to observe a broadcast unprotected legacy transaction on one chain and submit it to the other; nonce alignment is the only practical constraint, and for new accounts or accounts with low nonce it is trivially satisfied.
- The code path is unconditionally reachable: there is no flag, feature gate, or operator permission guarding acceptance of `v=27/28` transactions.

### Recommendation

Reject unprotected legacy transactions (where `v == 27` or `v == 28`) at parse time in `LegacyPayloadParser::try_parse_and_hash_for_signature_verification`. Only EIP-155-protected legacy transactions (and typed EIP-2718 transactions, which already embed the chain ID) should be accepted. This mirrors the protection already applied to EIP-2930, EIP-1559, EIP-4844, and EIP-7702 transactions, all of which enforce `tx.chain_id == expected_chain_id`: [6](#0-5) 

### Proof of Concept

1. Alice signs a legacy ETH transfer on Ethereum mainnet with `v=27` (no EIP-155), nonce=0, sending 1 ETH to Bob. The signing hash is `keccak256(rlp([nonce, gasPrice, gasLimit, to, value, data]))` — no chain ID.
2. Alice also holds funds on ZKsync OS and her ZKsync OS account nonce is 0.
3. Attacker observes Alice's Ethereum transaction `(nonce=0, gasPrice, gasLimit, to, value, data, v=27, r, s)`.
4. Attacker submits the identical raw transaction bytes to ZKsync OS.
5. `LegacyPayloadParser::try_parse_and_hash_for_signature_verification` computes the same chain-ID-free hash, `ecrecover` recovers Alice's address, nonce check passes (both are 0), and the transaction executes — transferring Alice's ZKsync OS funds to Bob without Alice's consent.

The root cause is at: [1](#0-0)

### Citations

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/legacy_tx.rs (L89-94)
```rust
        let sig_hash: Bytes32 = if legacy_signature.is_eip155() == false {
            // Unprotected legacy
            let mut hasher = crypto::sha3::Keccak256::new();
            apply_list_concatenation_encoding_to_hash(inner_slice.len() as u32, &mut hasher);
            hasher.update(inner_slice);
            hasher.finalize_reset().into()
```

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/legacy_tx.rs (L129-131)
```rust
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

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/mod.rs (L127-134)
```rust
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L245-300)
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
            ecrecover_input[63] = (parity as u8) + 27;
            ecrecover_input[64..96][(32 - r.len())..].copy_from_slice(r);
            ecrecover_input[96..128][(32 - s.len())..].copy_from_slice(s);

            let mut ecrecover_output = ArrayBuilder::default();
            // We already charged gas for ecrecover in intrinsic cost, so we only need to charge native resources here.
            intrinsic_resources.with_infinite_ergs(|resources| {
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
        }
```
